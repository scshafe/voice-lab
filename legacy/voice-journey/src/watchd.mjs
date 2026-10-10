#!/usr/bin/env node
// watchd — the breathing loop (docs/breathing-studio-plan.md, phase D).
//
// On the server the corpus is the Mini's mirror drop (corpus/phone/, read-only
// to this process): the scan ignores rsync temp files, .rsync-partial/ and
// _mirror/, and holds back files whose size differs from the mirror manifest.
//
// Lives INSIDE the corpus browser process on purpose: macOS TCC attributes
// corpus reads to the service's responsible process (voice-journey-launcher,
// already granted Full Disk Access), so the watch loop and every pipeline
// child it spawns inherit the grant. A separate daemon would need its own
// compiled launcher and a second Settings trip.
//
// Scanning is polling + a two-scan stability gate rather than fs events:
// iCloud delivers files in bursts and partial states, so a change only counts
// once two consecutive scans agree AND the agreed state differs from the
// recording index. Every downstream stage is already incremental/resumable
// (the STT pattern), so the pipeline is safe to re-run whole.
//
// Privacy: scans go through the host-access seam's listing (metadata only).
// ntfy notifications carry stage keys, counts, and durations — never
// transcript text, lyric text, or stage output. Full stage stderr goes to the
// local service log only.

import { execFile } from "node:child_process";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { listCorpusEntries, listMeasurementEntries } from "./host-access.mjs";
import { resolvePaths } from "./paths.mjs";

const DAY_INDEX = { sunday: 0, monday: 1, tuesday: 2, wednesday: 3, thursday: 4, friday: 5, saturday: 6 };
const STATE_VERSION = "voice-journey.watchd-state.v1";
const MAX_RECENT_RUNS = 10;
const FAILURE_BACKOFF_MS = 30 * 60 * 1000;

export function scanSignature(entries) {
  return (entries ?? [])
    .map((entry) => `${entry.recordingId}:${entry.sizeBytes}`)
    .sort()
    .join("|");
}

export function countNewSinceIndex(entries, indexManifest) {
  const indexed = new Map((indexManifest?.recordings ?? []).map((row) => [row.recordingId, row.file?.sizeBytes ?? row.sizeBytes]));
  let count = 0;
  for (const entry of entries ?? []) {
    if (!indexed.has(entry.recordingId) || indexed.get(entry.recordingId) !== entry.sizeBytes) count += 1;
  }
  return count;
}

// The pipeline, in dependency order. Approval flags only on the stages that
// read audio bytes; everything else works from committed manifests.
export function buildStages({ corpusRoot, approval, measurementRoot = null }) {
  return [
    { key: "index", script: path.join("src", "index-recordings.mjs"), args: ["--corpus-root", corpusRoot, ...(measurementRoot ? ["--measurement-root", measurementRoot] : [])], timeoutMs: 10 * 60_000 },
    { key: "filter", script: path.join("src", "filter-proof.mjs"), args: ["analyze-index", "--corpus-root", corpusRoot, "--approval", approval], timeoutMs: 4 * 60 * 60_000 },
    { key: "stt", script: path.join("src", "local-stt.mjs"), args: ["transcribe-index", "--corpus-root", corpusRoot, "--approval", approval], timeoutMs: 12 * 60 * 60_000 },
    { key: "lyric", script: path.join("src", "lyric-indicators.mjs"), args: ["analyze"], timeoutMs: 30 * 60_000 },
    { key: "reclassify", script: path.join("src", "reclassify.mjs"), args: ["apply"], timeoutMs: 30 * 60_000 },
    { key: "features", script: path.join("src", "features.mjs"), args: ["extract-index", "--corpus-root", corpusRoot, "--approval", approval], timeoutMs: 12 * 60 * 60_000 },
    { key: "journeys", script: path.join("src", "journeys.mjs"), args: ["analyze"], timeoutMs: 60 * 60_000 },
  ];
}

export function nextDigestAt(fromMs, { day = "sunday", hour = 18 } = {}) {
  const dayIndex = DAY_INDEX[String(day).toLowerCase()];
  if (dayIndex === undefined) throw new Error(`unknown digest day: ${day}`);
  const candidate = new Date(fromMs);
  candidate.setHours(hour, 0, 0, 0);
  for (let i = 0; i < 8; i += 1) {
    if (candidate.getDay() === dayIndex && candidate.getTime() > fromMs) return candidate.getTime();
    candidate.setDate(candidate.getDate() + 1);
  }
  throw new Error("nextDigestAt failed to converge");
}

function countBy(rows, pick) {
  const counts = {};
  for (const row of rows) {
    const key = pick(row) ?? "unknown";
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

// Counts only — the digest is deliberately incapable of carrying content.
export function digestSnapshotFromManifests({ results, transcripts, lyrics, journeys } = {}) {
  return {
    rows: results?.results?.length ?? 0,
    buckets: countBy(results?.results ?? [], (row) => row.classification?.finalBucket ?? row.bucket),
    transcribed: transcripts?.transcripts?.length ?? transcripts?.results?.length ?? transcripts?.rows?.length ?? 0,
    clusters: lyrics?.clusters?.length ?? 0,
    songJourneys: journeys?.journeys?.length ?? 0,
    verdicts: countBy(journeys?.improvementIndex ?? [], (dim) => dim.verdict),
  };
}

export function digestDiffLines(previous, next) {
  const delta = (before, after) => {
    const diff = after - (before ?? 0);
    return previous && diff !== 0 ? ` (${diff > 0 ? "+" : ""}${diff})` : "";
  };
  const lines = [
    `takes classified: ${next.rows}${delta(previous?.rows, next.rows)}`,
    `transcribed: ${next.transcribed}${delta(previous?.transcribed, next.transcribed)}`,
    `lyric clusters: ${next.clusters}${delta(previous?.clusters, next.clusters)}`,
    `song journeys: ${next.songJourneys}${delta(previous?.songJourneys, next.songJourneys)}`,
  ];
  for (const [bucket, count] of Object.entries(next.buckets).sort()) {
    const before = previous?.buckets?.[bucket];
    if (previous && before === count) continue;
    lines.push(`  ${bucket}: ${count}${delta(before, count)}`);
  }
  return lines;
}

function defaultRunStage(stage, { repoRoot, registerChild }) {
  return new Promise((resolve, reject) => {
    const child = execFile(
      process.execPath,
      [stage.script, ...stage.args],
      { cwd: repoRoot, timeout: stage.timeoutMs, maxBuffer: 64 * 1024 * 1024, killSignal: "SIGTERM" },
      (error, stdout, stderr) => {
        registerChild?.(null);
        if (error) {
          const failure = new Error(`${stage.key}: exit ${error.code ?? `signal ${error.signal ?? "?"}`}`);
          failure.stderrTail = String(stderr ?? "").slice(-2000);
          reject(failure);
          return;
        }
        resolve({ stdoutTail: String(stdout ?? "").slice(-400) });
      },
    );
    registerChild?.(child);
  });
}

async function defaultNotify({ url, title, body, priority = "default", tags = "" }, log) {
  if (!url) return;
  try {
    await fetch(url, {
      method: "POST",
      body,
      headers: { Title: title, Priority: priority, ...(tags ? { Tags: tags } : {}) },
      signal: AbortSignal.timeout(10_000),
    });
  } catch (error) {
    log(`ntfy delivery failed: ${error.message}`);
  }
}

async function readOptionalJsonFile(filePath) {
  try {
    return JSON.parse(await readFile(filePath, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

export function createWatchd(options = {}, deps = {}) {
  const {
    corpusRoot,
    measurementRoot = resolvePaths().measurementDir,
    approval,
    intervalMs = 60_000,
    statePath = resolvePaths().artifact("watchd-state.json"),
    ntfyUrl = null,
    digestDay = "sunday",
    digestHour = 18,
    repoRoot = process.cwd(),
    manifests = {},
  } = options;
  if (!corpusRoot) throw new Error("watchd requires corpusRoot");
  if (!approval) throw new Error("watchd requires an operator approval string for audio-reading stages");

  const log = options.log ?? ((line) => process.stderr.write(`[watchd] ${line}\n`));
  // Phone recordings and accepted measurement sessions are scanned together; both
  // are stable once the debounce sees the same set twice.
  const listEntries = deps.listEntries ?? (async () => [
    ...(await listCorpusEntries(corpusRoot, { mirrorManifest: true })),
    ...(await listMeasurementEntries(measurementRoot)),
  ]);
  const runStage = deps.runStage ?? defaultRunStage;
  const notify = deps.notify ?? ((message) => defaultNotify({ url: ntfyUrl, ...message }, log));
  const now = deps.now ?? (() => Date.now());
  const reloadServeState = deps.reloadServeState ?? null;

  const manifestPaths = {
    index: path.resolve(repoRoot, manifests.index ?? resolvePaths().manifest("recording-index.json")),
    results: path.resolve(repoRoot, manifests.results ?? resolvePaths().manifest("full-corpus-filter-results.json")),
    transcripts: path.resolve(repoRoot, manifests.transcripts ?? resolvePaths().manifest("local-stt-transcripts.json")),
    lyrics: path.resolve(repoRoot, manifests.lyrics ?? resolvePaths().manifest("local-lyric-indicators.json")),
    journeys: path.resolve(repoRoot, manifests.journeys ?? resolvePaths().manifest("song-journeys.json")),
  };
  const stages = buildStages({ corpusRoot, approval, measurementRoot });
  const lockPath = `${statePath}.lock`;

  const watchd = {
    started: false,
    busy: false,
    timer: null,
    currentChild: null,
    pendingSignature: null,
    scanErrorStreak: 0,
    lastTick: null,
    state: null, // persisted portion, loaded lazily
  };

  async function loadState() {
    if (watchd.state) return watchd.state;
    const persisted = await readOptionalJsonFile(statePath);
    watchd.state = persisted?.version === STATE_VERSION ? persisted : {
      version: STATE_VERSION,
      lastRun: null,
      recentRuns: [],
      lastDigest: null,
      nextDigestAt: null,
      failure: null,
    };
    if (!watchd.state.nextDigestAt) watchd.state.nextDigestAt = nextDigestAt(now(), { day: digestDay, hour: digestHour });
    return watchd.state;
  }

  async function persistState() {
    await mkdir(path.dirname(statePath), { recursive: true });
    await writeFile(statePath, `${JSON.stringify(watchd.state, null, 2)}\n`);
  }

  async function acquireLock() {
    const payload = `${JSON.stringify({ pid: process.pid, startedAt: new Date(now()).toISOString() })}\n`;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await mkdir(path.dirname(lockPath), { recursive: true });
        await writeFile(lockPath, payload, { flag: "wx" });
        return true;
      } catch (error) {
        if (error?.code !== "EEXIST") throw error;
        const holder = await readOptionalJsonFile(lockPath);
        const holderPid = holder?.pid;
        if (holderPid && holderPid !== process.pid && pidAlive(holderPid)) return false;
        await rm(lockPath, { force: true });
      }
    }
    return false;
  }

  function pidAlive(pid) {
    try {
      process.kill(pid, 0);
      return true;
    } catch (error) {
      // EPERM = the process exists but isn't ours to signal — still alive.
      return error?.code === "EPERM";
    }
  }

  async function releaseLock() {
    await rm(lockPath, { force: true });
  }

  async function runPipeline(newCount, signature) {
    if (!(await acquireLock())) {
      log("pipeline skipped: another run holds the lock");
      return { ok: false, skipped: "locked" };
    }
    const run = { startedAt: new Date(now()).toISOString(), signature, newCount, ok: false, stages: [] };
    try {
      for (const stage of stages) {
        const stageStart = now();
        try {
          const result = await runStage(stage, {
            repoRoot,
            registerChild: (child) => { watchd.currentChild = child; },
          });
          run.stages.push({ key: stage.key, ok: true, ms: now() - stageStart });
          if (result?.stdoutTail) log(`stage ${stage.key} ok (${Math.round((now() - stageStart) / 1000)}s)`);
        } catch (error) {
          run.stages.push({ key: stage.key, ok: false, ms: now() - stageStart, error: error.message });
          if (error.stderrTail) log(`stage ${stage.key} stderr tail: ${error.stderrTail}`);
          await notify({
            title: "Voice Journey watchd: stage failed",
            body: `${error.message} — pipeline aborted after ${run.stages.length}/${stages.length} stages; will retry when the corpus changes or after backoff.`,
            priority: "high",
            tags: "warning",
          });
          watchd.state.failure = { signature, until: now() + FAILURE_BACKOFF_MS };
          return run;
        }
      }
      run.ok = true;
      watchd.state.failure = null;
      const totalSeconds = Math.round(run.stages.reduce((sum, stage) => sum + stage.ms, 0) / 1000);
      await notify({
        title: "Voice Journey watchd: ingested",
        body: `${newCount} new take(s) processed through ${stages.length} stages in ${totalSeconds}s.`,
        tags: "white_check_mark",
      });
      if (reloadServeState) {
        try {
          await reloadServeState();
          log("serve state reloaded");
        } catch (error) {
          log(`serve-state reload failed: ${error.message}`);
        }
      }
      return run;
    } finally {
      run.endedAt = new Date(now()).toISOString();
      watchd.state.lastRun = run;
      watchd.state.recentRuns = [...(watchd.state.recentRuns ?? []), run].slice(-MAX_RECENT_RUNS);
      watchd.currentChild = null;
      await releaseLock();
      await persistState();
    }
  }

  async function maybeDigest() {
    if (!ntfyUrl && !deps.notify) return;
    const state = await loadState();
    if (now() < state.nextDigestAt) return;
    const [results, transcripts, lyrics, journeys] = await Promise.all([
      readOptionalJsonFile(manifestPaths.results),
      readOptionalJsonFile(manifestPaths.transcripts),
      readOptionalJsonFile(manifestPaths.lyrics),
      readOptionalJsonFile(manifestPaths.journeys),
    ]);
    const snapshot = digestSnapshotFromManifests({ results, transcripts, lyrics, journeys });
    const lines = digestDiffLines(state.lastDigest?.snapshot ?? null, snapshot);
    await notify({ title: "Voice Journey weekly digest", body: lines.join("\n"), tags: "musical_note" });
    state.lastDigest = { snapshot, sentAt: new Date(now()).toISOString() };
    state.nextDigestAt = nextDigestAt(now(), { day: digestDay, hour: digestHour });
    await persistState();
  }

  async function tick() {
    if (watchd.busy) return { status: "busy" };
    watchd.busy = true;
    try {
      const state = await loadState();
      let entries;
      try {
        entries = await listEntries();
        watchd.scanErrorStreak = 0;
      } catch (error) {
        watchd.scanErrorStreak += 1;
        if (watchd.scanErrorStreak === 3) {
          await notify({
            title: "Voice Journey watchd: corpus scan failing",
            body: `Three consecutive scan failures (${error.message}). Check corpus access (TCC/Full Disk Access on the launcher).`,
            priority: "high",
            tags: "rotating_light",
          });
        }
        watchd.lastTick = { at: new Date(now()).toISOString(), status: "scan-error" };
        return watchd.lastTick;
      }

      const signature = scanSignature(entries);
      if (signature !== watchd.pendingSignature) {
        // First sight of this corpus state — wait for a second agreeing scan
        // so mid-sync partial files never trigger a run.
        watchd.pendingSignature = signature;
        watchd.lastTick = { at: new Date(now()).toISOString(), status: "unstable" };
        return watchd.lastTick;
      }

      const indexManifest = await readOptionalJsonFile(manifestPaths.index);
      const newCount = countNewSinceIndex(entries, indexManifest);
      let status = "idle";
      if (newCount > 0) {
        const failedRecently = state.failure?.signature === signature && now() < state.failure.until;
        const alreadyProcessed = state.lastRun?.ok && state.lastRun.signature === signature;
        if (!failedRecently && !alreadyProcessed) {
          const run = await runPipeline(newCount, signature);
          status = run.skipped ?? (run.ok ? "ran" : "failed");
        } else {
          status = failedRecently ? "backoff" : "idle";
        }
      }
      await maybeDigest();
      watchd.lastTick = { at: new Date(now()).toISOString(), status, newCount };
      return watchd.lastTick;
    } finally {
      watchd.busy = false;
    }
  }

  function handleShutdownSignal(signal) {
    if (watchd.currentChild) {
      log("shutdown: terminating active pipeline stage");
      watchd.currentChild.kill("SIGTERM");
    }
    if (watchd.timer) clearInterval(watchd.timer);
    // Registering any signal handler cancels node's default terminate-on-signal
    // behavior, so the watcher must finish the job itself — otherwise the
    // server survives launchd's SIGTERM as an orphan squatting on the port
    // while replacement launches crash-loop on EADDRINUSE.
    process.exit(signal === "SIGINT" ? 130 : 143);
  }

  return {
    tick,
    start() {
      if (watchd.started) return;
      watchd.started = true;
      tick().catch((error) => log(`tick failed: ${error.message}`));
      watchd.timer = setInterval(() => {
        tick().catch((error) => log(`tick failed: ${error.message}`));
      }, intervalMs);
      process.on("SIGTERM", handleShutdownSignal);
      process.on("SIGINT", handleShutdownSignal);
      log(`watching ${corpusRoot} every ${Math.round(intervalMs / 1000)}s`);
    },
    stop() {
      if (watchd.timer) clearInterval(watchd.timer);
      watchd.timer = null;
      watchd.started = false;
      process.off("SIGTERM", handleShutdownSignal);
      process.off("SIGINT", handleShutdownSignal);
    },
    statusSnapshot() {
      return {
        enabled: true,
        intervalSeconds: Math.round(intervalMs / 1000),
        corpusRoot,
        approvalRecorded: Boolean(approval),
        ntfy: Boolean(ntfyUrl),
        lastTick: watchd.lastTick,
        lastRun: watchd.state?.lastRun ?? null,
        recentRuns: (watchd.state?.recentRuns ?? []).slice(-5),
        nextDigestAt: watchd.state?.nextDigestAt ? new Date(watchd.state.nextDigestAt).toISOString() : null,
        stages: stages.map((stage) => stage.key),
      };
    },
  };
}
