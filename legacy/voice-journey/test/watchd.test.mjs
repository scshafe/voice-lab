import assert from "node:assert/strict";
import { access, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  buildStages,
  countNewSinceIndex,
  createWatchd,
  digestDiffLines,
  digestSnapshotFromManifests,
  nextDigestAt,
  scanSignature,
} from "../src/watchd.mjs";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));

async function withTempDir(callback) {
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-watchd-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function entry(recordingId, sizeBytes) {
  return { recordingId, filename: `${recordingId}.m4a`, sizeBytes };
}

const PIPELINE_ORDER = ["index", "filter", "stt", "lyric", "reclassify", "features", "journeys"];

function makeWatchd(root, overrides = {}, depOverrides = {}) {
  const calls = [];
  const notices = [];
  const context = { calls, notices, reloads: 0, clock: 1_000_000_000_000, entries: [entry("a", 10)] };
  const watchd = createWatchd({
    corpusRoot: "/fake-corpus",
    approval: "test approval",
    statePath: path.join(root, "watchd-state.json"),
    repoRoot: root,
    ntfyUrl: "http://ntfy.test/topic",
    log: () => {},
    ...overrides,
  }, {
    listEntries: async () => context.entries,
    runStage: async (stage) => {
      calls.push(stage.key);
      return {};
    },
    notify: async (message) => {
      notices.push(message);
    },
    now: () => context.clock,
    reloadServeState: async () => {
      context.reloads += 1;
    },
    ...depOverrides,
  });
  return { watchd, context };
}

test("scan signature is order-insensitive and size-sensitive", () => {
  const entries = [entry("a", 1), entry("b", 2)];
  assert.equal(scanSignature(entries), scanSignature([...entries].reverse()));
  assert.notEqual(scanSignature(entries), scanSignature([entry("a", 1), entry("b", 3)]));
  assert.notEqual(scanSignature(entries), scanSignature([entry("a", 1)]));
});

test("countNewSinceIndex counts unindexed and resized recordings", () => {
  const index = { recordings: [{ recordingId: "a", file: { sizeBytes: 10 } }] };
  assert.equal(countNewSinceIndex([entry("a", 10)], index), 0);
  assert.equal(countNewSinceIndex([entry("a", 10), entry("b", 5)], index), 1);
  assert.equal(countNewSinceIndex([entry("a", 99)], index), 1);
  assert.equal(countNewSinceIndex([entry("a", 10), entry("b", 5)], null), 2);
});

test("buildStages keeps pipeline order, wires approval only into audio-reading stages, and points at real scripts", async () => {
  const stages = buildStages({ corpusRoot: "/corpus", approval: "approved" });
  assert.deepEqual(stages.map((stage) => stage.key), PIPELINE_ORDER);
  const withApproval = stages.filter((stage) => stage.args.includes("--approval")).map((stage) => stage.key);
  assert.deepEqual(withApproval, ["filter", "stt", "features"]);
  const withCorpusRoot = stages.filter((stage) => stage.args.includes("--corpus-root")).map((stage) => stage.key);
  assert.deepEqual(withCorpusRoot, ["index", "filter", "stt", "features"]);
  for (const stage of stages) {
    await access(path.join(repoRoot, stage.script));
  }
});

test("watchd waits for two agreeing scans, runs the pipeline in order, reloads serve state, and does not rerun", async () => {
  await withTempDir(async (root) => {
    const { watchd, context } = makeWatchd(root);
    const first = await watchd.tick();
    assert.equal(first.status, "unstable");
    assert.equal(context.calls.length, 0);

    const second = await watchd.tick();
    assert.equal(second.status, "ran");
    assert.deepEqual(context.calls, PIPELINE_ORDER);
    assert.equal(context.reloads, 1);
    assert.ok(context.notices.some((notice) => notice.title.includes("ingested")));
    await access(path.join(root, "watchd-state.json"));

    const third = await watchd.tick();
    assert.equal(third.status, "idle");
    assert.deepEqual(context.calls, PIPELINE_ORDER, "no rerun for an already-processed corpus state");
    assert.equal(context.reloads, 1);
  });
});

test("a failing stage aborts the run, notifies, backs off, and retries when the corpus changes", async () => {
  await withTempDir(async (root) => {
    let failStt = true;
    const { watchd, context } = makeWatchd(root, {}, {
      runStage: async (stage) => {
        context.calls.push(stage.key);
        if (stage.key === "stt" && failStt) {
          const error = new Error("stt: exit 1");
          error.stderrTail = "boom";
          throw error;
        }
        return {};
      },
    });

    await watchd.tick();
    const failed = await watchd.tick();
    assert.equal(failed.status, "failed");
    assert.deepEqual(context.calls, ["index", "filter", "stt"], "stages after the failure never run");
    assert.ok(context.notices.some((notice) => notice.title.includes("stage failed")));
    assert.equal(context.reloads, 0);

    const backoff = await watchd.tick();
    assert.equal(backoff.status, "backoff");
    assert.deepEqual(context.calls, ["index", "filter", "stt"]);

    failStt = false;
    context.calls.length = 0;
    context.entries = [...context.entries, entry("b", 20)];
    const unstable = await watchd.tick();
    assert.equal(unstable.status, "unstable");
    const recovered = await watchd.tick();
    assert.equal(recovered.status, "ran");
    assert.deepEqual(context.calls, PIPELINE_ORDER);
  });
});

test("a lock held by another live process excludes the pipeline; stale locks are reclaimed", async () => {
  await withTempDir(async (root) => {
    const statePath = path.join(root, "watchd-state.json");
    // pid 1 (launchd) is always alive and never ours — kill(1, 0) yields EPERM,
    // which must read as "alive".
    await writeFile(`${statePath}.lock`, `${JSON.stringify({ pid: 1, startedAt: "now" })}\n`);
    const { watchd, context } = makeWatchd(root, { statePath });
    await watchd.tick();
    const locked = await watchd.tick();
    assert.equal(locked.status, "locked");
    assert.equal(context.calls.length, 0);
  });
  await withTempDir(async (root) => {
    const statePath = path.join(root, "watchd-state.json");
    // A dead pid is a stale lock from a crashed run — reclaim and proceed.
    await writeFile(`${statePath}.lock`, `${JSON.stringify({ pid: 999999, startedAt: "now" })}\n`);
    const { watchd, context } = makeWatchd(root, { statePath });
    await watchd.tick();
    const ran = await watchd.tick();
    assert.equal(ran.status, "ran");
    assert.deepEqual(context.calls, PIPELINE_ORDER);
  });
});

test("nextDigestAt lands on the configured local weekday and hour", () => {
  const wednesday = new Date(2026, 7, 12, 12, 0, 0);
  const next = new Date(nextDigestAt(wednesday.getTime(), { day: "sunday", hour: 18 }));
  assert.equal(next.getDay(), 0);
  assert.equal(next.getHours(), 18);
  assert.ok(next.getTime() > wednesday.getTime());
  assert.ok(next.getTime() - wednesday.getTime() < 8 * 86_400_000);

  const sundayBefore = new Date(2026, 7, 16, 17, 0, 0);
  assert.equal(nextDigestAt(sundayBefore.getTime(), { day: "sunday", hour: 18 }) - sundayBefore.getTime(), 3_600_000);

  const sundayAfter = new Date(2026, 7, 16, 19, 0, 0);
  const following = new Date(nextDigestAt(sundayAfter.getTime(), { day: "sunday", hour: 18 }));
  assert.equal(following.getDay(), 0);
  assert.ok(following.getTime() - sundayAfter.getTime() > 6 * 86_400_000);
});

test("digest snapshot and diff carry counts only", () => {
  const snapshot = digestSnapshotFromManifests({
    results: {
      results: [
        { classification: { finalBucket: "clean_singing" } },
        { classification: { finalBucket: "noise" } },
        { classification: { finalBucket: "clean_singing" } },
      ],
    },
    transcripts: { transcripts: [{}, {}] },
    lyrics: { clusters: [{}] },
    journeys: { journeys: [{}, {}, {}], improvementIndex: [{ verdict: "flat" }, { verdict: "flat" }] },
  });
  assert.deepEqual(snapshot, {
    rows: 3,
    buckets: { clean_singing: 2, noise: 1 },
    transcribed: 2,
    clusters: 1,
    songJourneys: 3,
    verdicts: { flat: 2 },
  });
  assert.deepEqual(Object.keys(snapshot).sort(), ["buckets", "clusters", "rows", "songJourneys", "transcribed", "verdicts"]);

  const fresh = digestDiffLines(null, snapshot);
  assert.ok(fresh.some((line) => line.includes("takes classified: 3")));
  const delta = digestDiffLines({ ...snapshot, rows: 1 }, snapshot);
  assert.ok(delta.some((line) => line.includes("takes classified: 3 (+2)")));
});

test("the weekly digest fires once the scheduled time passes and reschedules", async () => {
  await withTempDir(async (root) => {
    await mkdir(path.join(root, "manifests"), { recursive: true });
    await writeFile(
      path.join(root, "manifests", "recording-index.json"),
      JSON.stringify({ recordings: [{ recordingId: "a", file: { sizeBytes: 10 } }] }),
    );
    const { watchd, context } = makeWatchd(root);

    await watchd.tick();
    const idle = await watchd.tick();
    assert.equal(idle.status, "idle");
    assert.equal(context.notices.length, 0);

    context.clock += 9 * 86_400_000;
    await watchd.tick();
    const digests = context.notices.filter((notice) => notice.title.includes("weekly digest"));
    assert.equal(digests.length, 1);
    assert.ok(digests[0].body.includes("takes classified: 0"));

    await watchd.tick();
    assert.equal(context.notices.filter((notice) => notice.title.includes("weekly digest")).length, 1, "digest does not refire until the next scheduled time");
  });
});
