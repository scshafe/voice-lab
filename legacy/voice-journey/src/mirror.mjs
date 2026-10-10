#!/usr/bin/env node
// Corpus mirror: Mac mini -> Lubuntu. Design: docs/move-to-lubuntu.md, phase 3.
//
// Reads the corpus ONLY through the host-access seam (directory listing, stat,
// and read-only streams of allowlisted files), and pushes new/changed
// recordings plus the Voice Memos metadata database with rsync. The server end
// is an `rrsync -wo` restricted key: write-only, rooted at corpus/phone/, so
// every remote path here is relative and nothing is ever read back.
//
// Never deletes remotely (no --delete*). Never prints recording names: logs and
// rsync's own output carry counts only.
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { createWriteStream } from "node:fs";
import { mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import { pathToFileURL } from "node:url";
import {
  HostAccessError,
  RefusedOperationError,
  listCorpusEntries,
  listMetadataDatabaseFiles,
  openCorpusFileForRead,
} from "./host-access.mjs";

const DEFAULT_CORPUS_ROOT = path.join(
  homedir(),
  "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings",
);
// Relative to the rrsync root (corpus/phone/ on the server): "./" is the root.
// Absolute remote paths are deliberately not the default; rrsync resolves
// every path under its own root.
const DEFAULT_DEST = "vj-mirror@cole-lubuntu-laptop:";
// Homebrew rsync 3.x by full path: macOS 26's /usr/bin/rsync is openrsync
// (protocol 29), untested against rrsync.
const DEFAULT_RSYNC = "/opt/homebrew/bin/rsync";
const DEFAULT_STATE_DIR = path.join(homedir(), ".mission-control/state/voice-journey-mirror");
const PARTIAL_DIR = ".rsync-partial";
const MANIFEST_DIR = "_mirror";
const STALE_LOCK_MS = 6 * 60 * 60 * 1000;

export const EXIT = { ok: 0, failure: 1, usage: 2, locked: 3, transfer: 4 };

class UsageError extends Error {}

export function parseArgs(argv) {
  const options = {
    corpusRoot: process.env.VOICE_JOURNEY_CORPUS_ROOT ?? DEFAULT_CORPUS_ROOT,
    dest: process.env.VOICE_JOURNEY_MIRROR_DEST ?? DEFAULT_DEST,
    stateDir: process.env.VOICE_JOURNEY_MIRROR_STATE ?? DEFAULT_STATE_DIR,
    sshKey: process.env.VOICE_JOURNEY_MIRROR_SSH_KEY ?? null,
    rsyncBin: process.env.VOICE_JOURNEY_RSYNC ?? DEFAULT_RSYNC,
    approval: null,
    dryRun: false,
    help: false,
  };
  const args = [...argv];
  const value = (flag) => {
    const next = args.shift();
    if (!next || next.startsWith("--")) throw new UsageError(`${flag} requires a value`);
    return next;
  };
  while (args.length > 0) {
    const flag = args.shift();
    if (flag === "--corpus-root") options.corpusRoot = value(flag);
    else if (flag === "--dest") options.dest = value(flag);
    else if (flag === "--state-dir") options.stateDir = value(flag);
    else if (flag === "--ssh-key") options.sshKey = value(flag);
    else if (flag === "--rsync-bin") options.rsyncBin = value(flag);
    else if (flag === "--approval") options.approval = value(flag);
    else if (flag === "--dry-run") options.dryRun = true;
    else if (flag === "--help" || flag === "help") options.help = true;
    else throw new UsageError(`unknown argument: ${flag}`);
  }
  options.corpusRoot = path.resolve(options.corpusRoot);
  options.stateDir = path.resolve(options.stateDir);
  return options;
}

function isInside(parent, child) {
  const rel = path.relative(parent, child);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

// The destination must be remote ([user@]host:path or rsync://), so rsync can
// never be pointed at a local directory, least of all the corpus. State lives
// outside the corpus too.
export function validateOptions(options) {
  const remote = /^(rsync:\/\/|[^/:\s]+:)/u.test(options.dest);
  if (!remote) throw new RefusedOperationError("refusing a local destination: the mirror only pushes to a remote rsync target");
  if (isInside(options.corpusRoot, options.stateDir)) {
    throw new RefusedOperationError("refusing to keep mirror state inside the corpus");
  }
}

function destWithPrefix(dest, prefix) {
  const base = dest.endsWith("/") || dest.endsWith(":") ? dest : `${dest}/`;
  return `${base}${prefix}`;
}

function sshCommand(options) {
  const parts = [
    "ssh", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
  ];
  if (options.sshKey) parts.push("-i", options.sshKey);
  return parts.join(" ");
}

// Shared rsync flags. Why these work under `rrsync -wo`: this host is always
// the sender, the server only receives. There is no --delete*, no pull, and the
// receiver's quick check (size+mtime) is answered by the receiver itself, which
// -wo allows. rsync writes each file to a temp name (inside --partial-dir, a
// hidden directory) and renames it into place only when it is complete.
// --delay-updates is NOT used: rrsync rewrites --partial-dir to an absolute
// path, which rsync cannot combine with --delay-updates (it silently discards
// the file, still exit 0).
export function rsyncBaseArgs(options) {
  return [
    "-rt", "--no-perms", "--no-owner", "--no-group", "--omit-dir-times",
    // The Mini's files may be 0600; the server reads through the vj-mirror group.
    "--chmod=D2750,F0640",
    `--partial-dir=${PARTIAL_DIR}`,
    "--timeout=300", "--no-motd",
    "-e", sshCommand(options),
  ];
}

// Pass 1: recordings, from the corpus root, driven by a NUL-separated list.
export function recordingsRsyncArgs(options, listPath) {
  return [...rsyncBaseArgs(options), "--from0", `--files-from=${listPath}`, `${options.corpusRoot}/`, options.dest];
}

// Pass 2: database snapshot + manifest from the staging dir. Sent only after
// pass 1 succeeded, so the manifest's arrival means its files are complete.
export function stagingRsyncArgs(options, stagingDir, listPath) {
  return [...rsyncBaseArgs(options), "--from0", `--files-from=${listPath}`, `${stagingDir}/`, options.dest];
}

async function sha256Stream(readable, writable = null) {
  const hash = createHash("sha256");
  readable.on("data", (chunk) => hash.update(chunk));
  if (writable) await pipeline(readable, writable);
  else await new Promise((resolve, reject) => readable.on("end", resolve).on("error", reject));
  return hash.digest("hex");
}

async function readJson(file, fallback) {
  try {
    return JSON.parse(await readFile(file, "utf8"));
  } catch {
    return fallback;
  }
}

async function acquireLock(lockPath) {
  const payload = JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await writeFile(lockPath, payload, { flag: "wx" });
      return true;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      const holder = await readJson(lockPath, null);
      const age = Date.now() - (holder?.startedAt ? Date.parse(holder.startedAt) : 0);
      let alive = false;
      if (holder?.pid) {
        try {
          process.kill(holder.pid, 0);
          alive = true;
        } catch (killError) {
          alive = killError.code === "EPERM";
        }
      }
      if (alive && age < STALE_LOCK_MS) return false;
      await rm(lockPath, { force: true });
    }
  }
  return false;
}

function runRsync(bin, args) {
  return new Promise((resolve) => {
    const child = spawn(bin, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => resolve({ code: 127, stderr: String(error.code ?? "spawn error") }));
    child.on("close", (code, signal) => resolve({ code: code ?? 128, signal, stderr }));
  });
}

// rrsync serializes the drop with a lock: a concurrent run is told so. The
// text is matched here and never logged.
export function isDropLocked(result) {
  return /Another instance of rrsync is already accessing this directory/u.test(result.stderr);
}

// rsync error lines can quote file names: keep only a count.
function summarizeFailure(result) {
  const lines = result.stderr.split("\n").filter((line) => line.trim() !== "").length;
  return `rsync exited ${result.code}${result.signal ? ` (${result.signal})` : ""}; ${lines} diagnostic line(s) suppressed`;
}

export async function planMirror(options) {
  const recordings = await listCorpusEntries(options.corpusRoot);
  const dbFiles = await listMetadataDatabaseFiles(options.corpusRoot);
  return { recordings, dbFiles };
}

export async function runMirror(argv, { log = (line) => process.stderr.write(`[mirror] ${line}\n`) } = {}) {
  let options;
  try {
    options = parseArgs(argv);
    if (options.help) {
      process.stdout.write(HELP);
      return EXIT.ok;
    }
    validateOptions(options);
  } catch (error) {
    if (error instanceof UsageError || error instanceof RefusedOperationError) {
      log(`error: ${error.message}`);
      return EXIT.usage;
    }
    throw error;
  }

  if (options.dryRun) {
    // Reads nothing: no corpus, no network. Shows what a run would do.
    process.stdout.write(`${JSON.stringify({
      dryRun: true,
      operation: "mirror",
      corpusRoot: options.corpusRoot,
      dest: options.dest,
      stateDir: options.stateDir,
      willReadAudioBytes: false,
      wouldHashAndPushAudio: true,
      neverDeletesRemotely: true,
      recordingsRsync: [options.rsyncBin, ...recordingsRsyncArgs(options, "<recordings-list>")],
      stagingRsync: [options.rsyncBin, ...stagingRsyncArgs(options, "<staging-dir>", "<staging-list>")],
    }, null, 2)}\n`);
    return EXIT.ok;
  }
  if (!options.approval) {
    log("error: mirroring reads audio bytes; pass --approval TEXT recording the release-gate decision");
    return EXIT.usage;
  }

  await mkdir(options.stateDir, { recursive: true });
  const lockPath = path.join(options.stateDir, "mirror.lock");
  if (!(await acquireLock(lockPath))) {
    log("skipped: another mirror run holds the lock");
    return EXIT.locked;
  }
  try {
    return await mirrorLocked(options, log);
  } finally {
    await rm(lockPath, { force: true });
  }
}

async function mirrorLocked(options, log) {
  const startedAt = new Date();
  const runId = startedAt.toISOString().replace(/[-:]/gu, "").replace(/\.\d+Z$/u, "Z");
  let plan;
  try {
    plan = await planMirror(options);
  } catch (error) {
    if (error instanceof HostAccessError) {
      log(`error: ${error.message.replace(options.corpusRoot, "<corpus>")}`);
      return EXIT.failure;
    }
    throw error;
  }

  // Hash cache: only files whose size+mtime moved since the last run are re-read.
  const statePath = path.join(options.stateDir, "hashes.json");
  const cache = await readJson(statePath, { files: {} });
  const nextCache = { files: {} };
  const files = [];
  let rehashed = 0;
  for (const entry of plan.recordings) {
    const cached = cache.files?.[entry.filename];
    let sha256 = cached && cached.sizeBytes === entry.sizeBytes && cached.modifiedAt === entry.modifiedAt ? cached.sha256 : null;
    if (!sha256) {
      sha256 = await sha256Stream(openCorpusFileForRead(options.corpusRoot, entry.filename));
      rehashed += 1;
    }
    nextCache.files[entry.filename] = { sizeBytes: entry.sizeBytes, modifiedAt: entry.modifiedAt, sha256 };
    files.push({ path: entry.filename, sizeBytes: entry.sizeBytes, modifiedAt: entry.modifiedAt, sha256 });
  }

  // Database: snapshot into staging so what is hashed is what is sent.
  const stagingDir = path.join(options.stateDir, "staging");
  await rm(stagingDir, { recursive: true, force: true });
  await mkdir(path.join(stagingDir, MANIFEST_DIR), { recursive: true });
  const dbPaths = [];
  for (const db of plan.dbFiles) {
    const target = path.join(stagingDir, db.name);
    const sha256 = await sha256Stream(openCorpusFileForRead(options.corpusRoot, db.name), createWriteStream(target));
    const info = await stat(target);
    files.push({ path: db.name, sizeBytes: info.size, modifiedAt: info.mtime.toISOString(), sha256, snapshot: true });
    dbPaths.push(db.name);
  }

  const manifest = {
    schema: "voice-journey-mirror-manifest/1",
    runId,
    startedAt: startedAt.toISOString(),
    recordingCount: plan.recordings.length,
    databaseFileCount: plan.dbFiles.length,
    note: "Deletions are never propagated; files absent here may still exist on the server.",
    files,
  };
  const manifestName = `manifest-${runId}.json`;
  const manifestBody = `${JSON.stringify(manifest, null, 2)}\n`;
  await writeFile(path.join(stagingDir, MANIFEST_DIR, manifestName), manifestBody);
  await writeFile(path.join(stagingDir, MANIFEST_DIR, "latest.json"), manifestBody);
  const manifestsDir = path.join(options.stateDir, "manifests");
  await mkdir(manifestsDir, { recursive: true });
  await writeFile(path.join(manifestsDir, manifestName), manifestBody);

  const recordingsList = path.join(options.stateDir, "recordings.list");
  await writeFile(recordingsList, plan.recordings.map((entry) => `${entry.filename}\0`).join(""));
  const stagingList = path.join(options.stateDir, "staging.list");
  await writeFile(
    stagingList,
    [...dbPaths, `${MANIFEST_DIR}/${manifestName}`, `${MANIFEST_DIR}/latest.json`].map((name) => `${name}\0`).join(""),
  );

  log(`run ${runId}: ${plan.recordings.length} recording(s), ${plan.dbFiles.length} database file(s), ${rehashed} hashed`);

  const first = plan.recordings.length > 0
    ? await runRsync(options.rsyncBin, recordingsRsyncArgs(options, recordingsList))
    : { code: 0, stderr: "" };
  if (first.code !== 0) {
    if (isDropLocked(first)) {
      log("skipped: the server drop is locked by another rrsync run; the next period retries");
      return EXIT.locked;
    }
    log(`transfer failed: ${summarizeFailure(first)}; manifest withheld, the next run retries`);
    return EXIT.transfer;
  }
  const second = await runRsync(options.rsyncBin, stagingRsyncArgs(options, stagingDir, stagingList));
  if (second.code !== 0) {
    if (isDropLocked(second)) {
      log("skipped: the server drop is locked by another rrsync run; the next period retries");
      return EXIT.locked;
    }
    log(`manifest transfer failed: ${summarizeFailure(second)}`);
    return EXIT.transfer;
  }
  await writeFile(`${statePath}.tmp`, JSON.stringify(nextCache));
  await rename(`${statePath}.tmp`, statePath);
  log(`run ${runId}: done`);
  return EXIT.ok;
}

const HELP = `Mirror the Voice Memos corpus to the Lubuntu server (write-only rsync).

Usage:
  voice-journey-mirror --approval TEXT [--dest TARGET] [--corpus-root PATH]
                       [--state-dir PATH] [--ssh-key PATH] [--dry-run]

  --dest      rsync target, relative to the server's rrsync root
              (default ${DEFAULT_DEST})
  --rsync-bin rsync binary (default ${DEFAULT_RSYNC}; env VOICE_JOURNEY_RSYNC)
  --approval  release-gate approval; required, the mirror reads audio bytes
  --dry-run   print the plan and rsync command lines; reads nothing

Exit codes: 0 ok, 1 corpus/unexpected failure, 2 usage or refused, 3 locked
(another local run, or rrsync's drop lock; try the next period), 4 rsync failed
(the next run retries).
Never deletes on the server. Logs carry counts, never recording names.
`;

const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  process.exitCode = await runMirror(process.argv.slice(2));
}
