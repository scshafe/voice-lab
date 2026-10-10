#!/usr/bin/env node
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { resolvePaths } from "./paths.mjs";

const DEFAULT_CORPUS_ROOT = resolvePaths().phoneCorpusRoot;
const ALLOWED_EXTENSIONS = new Set([".m4a"]);
// The Voice Memos metadata database (and its SQLite sidecars) sits beside the
// recordings. The mirror is the only caller that reads it, and only as a copy.
const METADATA_DB_NAMES = ["CloudRecordings.db", "CloudRecordings.db-wal", "CloudRecordings.db-shm"];
// The mirror's pass-2 manifest (written by src/mirror.mjs, pushed last): size
// and sha256 per file. The mirrored drop also holds rsync's hidden partial dir
// and `.name.XXXXXX` temp files; none of those are recordings.
const MIRROR_MANIFEST_PATH = path.join("_mirror", "latest.json");
const FORBIDDEN_COMMANDS = new Set(["write", "delete", "remove", "rm", "upload", "copy-in", "mutate"]);
const FILENAME_STEM_RE = /^(?<date>\d{8}) (?<time>\d{6})-(?<suffix>.+)$/u;

class HostAccessError extends Error {}

class RefusedOperationError extends HostAccessError {}

function recordingId(filename) {
  return createHash("sha256").update(filename, "utf8").digest("hex").slice(0, 16);
}

function capturedAtFromFilename(filename) {
  const parsed = path.parse(filename);
  const match = FILENAME_STEM_RE.exec(parsed.name);
  if (!match?.groups) return null;
  const { date, time } = match.groups;
  const year = Number(date.slice(0, 4));
  const monthIndex = Number(date.slice(4, 6)) - 1;
  const day = Number(date.slice(6, 8));
  const hour = Number(time.slice(0, 2));
  const minute = Number(time.slice(2, 4));
  const second = Number(time.slice(4, 6));
  const timestamp = Date.UTC(year, monthIndex, day, hour, minute, second);
  if (Number.isNaN(timestamp)) return null;
  const dateObject = new Date(timestamp);
  if (
    dateObject.getUTCFullYear() !== year ||
    dateObject.getUTCMonth() !== monthIndex ||
    dateObject.getUTCDate() !== day
  ) {
    return null;
  }
  return dateObject.toISOString().replace(".000Z", "Z");
}

function corpusRootFromOptions(options) {
  return path.resolve(
    options.corpusRoot ?? DEFAULT_CORPUS_ROOT,
  );
}

function printJson(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}

async function assertCorpusRoot(root) {
  let stats;
  try {
    stats = await stat(root);
  } catch {
    throw new HostAccessError(`corpus root does not exist: ${root}`);
  }
  if (!stats.isDirectory()) {
    throw new HostAccessError(`corpus root is not a directory: ${root}`);
  }
}

async function listEntries(root) {
  await assertCorpusRoot(root);
  const dirents = await readdir(root, { withFileTypes: true });
  const entries = [];
  for (const dirent of dirents.sort((left, right) => left.name.localeCompare(right.name))) {
    if (!dirent.isFile() || dirent.name.startsWith(".") || !ALLOWED_EXTENSIONS.has(path.extname(dirent.name).toLowerCase())) {
      continue;
    }
    const hostPath = path.join(root, dirent.name);
    const stats = await stat(hostPath);
    entries.push({
      recordingId: recordingId(dirent.name),
      filename: dirent.name,
      hostPath,
      capturedAt: capturedAtFromFilename(dirent.name),
      sizeBytes: stats.size,
      modifiedAt: stats.mtime.toISOString(),
    });
  }
  return entries;
}

// The mirror manifest of a server-side drop, or null when there is none (the
// Mac's own corpus, or a drop the mirror has not completed yet). Read-only; a
// torn or foreign file counts as absent.
async function readMirrorManifest(root) {
  try {
    const manifest = JSON.parse(await readFile(path.join(root, MIRROR_MANIFEST_PATH), "utf8"));
    if (manifest?.schema !== "voice-journey-mirror-manifest/1" || !Array.isArray(manifest.files)) return null;
    return {
      runId: manifest.runId ?? null,
      files: new Map(manifest.files.filter((file) => typeof file?.path === "string").map((file) => [file.path, file])),
    };
  } catch {
    return null;
  }
}

// mirrorManifest: true applies the drop's manifest. A file the manifest lists
// with a different size is held back (a newer copy landed and its manifest has
// not, or the copy is not the one that was hashed), so only verified bytes are
// ingested. A file the manifest does not list is kept: rsync renames files in
// whole, and the manifest follows the recordings by design. Each kept entry
// says whether the latest manifest lists it (`inManifest`); `null` means no
// manifest exists.
async function listCorpusEntries(root, { limit = null, mirrorManifest = false } = {}) {
  let entries = await listEntries(root);
  if (mirrorManifest) {
    const manifest = await readMirrorManifest(root);
    entries = entries.flatMap((entry) => {
      const listed = manifest?.files.get(entry.filename);
      if (listed && listed.sizeBytes !== entry.sizeBytes) return [];
      return [{ ...entry, inManifest: manifest ? Boolean(listed) : null }];
    });
  }
  const limitedEntries = limit === null ? entries : entries.slice(0, limit);
  return limitedEntries.map((entry) => publicEntry(entry));
}

// Metadata database files present beside the recordings: [{ name, sizeBytes }].
async function listMetadataDatabaseFiles(root) {
  await assertCorpusRoot(root);
  const files = [];
  for (const name of METADATA_DB_NAMES) {
    try {
      const stats = await stat(path.join(root, name));
      if (stats.isFile()) files.push({ name, sizeBytes: stats.size });
    } catch {
      // absent sidecars are normal
    }
  }
  return files;
}

// Read-only byte stream for one allowlisted corpus file: a bare .m4a name or a
// metadata database name. Anything else (paths, other extensions) is refused.
function openCorpusFileForRead(root, name) {
  const allowed =
    name === path.basename(name) &&
    (ALLOWED_EXTENSIONS.has(path.extname(name).toLowerCase()) || METADATA_DB_NAMES.includes(name));
  if (!allowed) throw new RefusedOperationError("refusing to read a file outside the corpus allowlist");
  return createReadStream(path.join(root, name), { flags: "r" });
}

const SESSION_DIR_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$/u;

// Accepted measurement sessions (corpus/measurement/<sessionId>/session.json
// plus the segment WAVs). The intake publishes a session directory with one
// atomic rename, so a directory that has a session.json is whole. Each
// segment file is one entry, tagged with its source so the pipeline can tell
// the chains apart. A missing directory is an empty list, not an error.
async function listMeasurementEntries(root) {
  let dirents;
  try {
    dirents = await readdir(root, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
  const entries = [];
  for (const dirent of dirents.sort((left, right) => left.name.localeCompare(right.name))) {
    if (!dirent.isDirectory() || !SESSION_DIR_RE.test(dirent.name)) continue;
    let session;
    try {
      session = JSON.parse(await readFile(path.join(root, dirent.name, "session.json"), "utf8"));
    } catch {
      continue;
    }
    const segmentByFile = new Map((session.segments ?? []).map((segment) => [segment.file, segment]));
    for (const file of session.files ?? []) {
      const hostPath = path.join(root, dirent.name, file.name);
      let stats;
      try {
        stats = await stat(hostPath);
      } catch {
        continue;
      }
      const filename = `${dirent.name}/${file.name}`;
      entries.push({
        recordingId: recordingId(`measurement/${filename}`),
        filename,
        hostPath,
        capturedAt: session.startedAt ?? null,
        sizeBytes: stats.size,
        modifiedAt: stats.mtime.toISOString(),
        source: {
          type: "measurement",
          chainId: session.chainId,
          sessionId: dirent.name,
          segmentKey: segmentByFile.get(file.name)?.key ?? null,
        },
      });
    }
  }
  return entries;
}

function publicEntry(entry, { includePath = false } = {}) {
  const payload = {
    recordingId: entry.recordingId,
    filename: entry.filename,
    capturedAt: entry.capturedAt,
    sizeBytes: entry.sizeBytes,
    modifiedAt: entry.modifiedAt,
    ...(entry.source ? { source: entry.source } : {}),
    ...(entry.inManifest !== undefined ? { inManifest: entry.inManifest } : {}),
  };
  if (includePath) payload.hostPath = entry.hostPath;
  return payload;
}

async function findRecording(root, selector) {
  const entries = await listEntries(root);
  const entry = entries.find((candidate) => candidate.recordingId === selector || candidate.filename === selector);
  if (!entry) {
    throw new HostAccessError(`recording not found by id or filename: ${selector}`);
  }
  return entry;
}

function dryRunPayload(operation, root, extra = {}) {
  return {
    dryRun: true,
    operation,
    corpusRoot: root,
    allowedExtensions: [...ALLOWED_EXTENSIONS].sort(),
    willReadAudioBytes: false,
    ...extra,
  };
}

function parseArgs(argv) {
  const args = [...argv];
  const options = { corpusRoot: null, dryRun: false, limit: null, approval: null, positional: [] };
  while (args.length > 0) {
    const next = args.shift();
    if (next === "--corpus-root") {
      options.corpusRoot = requireValue(args, next);
    } else if (next === "--dry-run") {
      options.dryRun = true;
    } else if (next === "--limit") {
      const value = Number(requireValue(args, next));
      if (!Number.isInteger(value) || value < 0) throw new HostAccessError("--limit must be a non-negative integer");
      options.limit = value;
    } else if (next === "--approval") {
      options.approval = requireValue(args, next);
    } else {
      options.positional.push(next);
    }
  }
  const [command = null, ...rest] = options.positional;
  return { command, rest, options };
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) {
    throw new HostAccessError(`${flag} requires a value`);
  }
  return value;
}

function printHelp() {
  process.stdout.write(`Read-only host-access seam for the local Voice Journey corpus.

Usage:
  voice-journey-corpus [--corpus-root PATH] describe
  voice-journey-corpus [--corpus-root PATH] list [--dry-run] [--limit N]
  voice-journey-corpus [--corpus-root PATH] metadata RECORDING [--dry-run]
  voice-journey-corpus [--corpus-root PATH] read-handle RECORDING --approval TEXT [--dry-run]

Allowed operations: describe, list, metadata, read-handle.
Refused operations: ${[...FORBIDDEN_COMMANDS].sort().join(", ")}.
The seam never writes, deletes, uploads, or prints audio bytes.
`);
}

async function run(argv = process.argv.slice(2)) {
  if (argv.length > 0 && FORBIDDEN_COMMANDS.has(argv[0])) {
    throw new RefusedOperationError(`refusing unsupported corpus mutation/upload operation: ${argv[0]}`);
  }
  const { command, rest, options } = parseArgs(argv);
  if (FORBIDDEN_COMMANDS.has(command)) {
    throw new RefusedOperationError(`refusing unsupported corpus mutation/upload operation: ${command}`);
  }
  const root = corpusRootFromOptions(options);
  if (!command || command === "--help" || command === "help") {
    printHelp();
    return 0;
  }
  if (command === "describe") {
    printJson({
      command: "voice-journey-corpus",
      corpusRoot: root,
      operations: ["describe", "list", "metadata", "read-handle"],
      refusedOperations: [...FORBIDDEN_COMMANDS].sort(),
      privacy: {
        localOnly: true,
        uploadsAudio: false,
        mutatesCorpus: false,
        readsAudioBytes: false,
        audioCommittedToRepo: false,
      },
    });
    return 0;
  }
  if (command === "list") {
    if (options.dryRun) {
      printJson(dryRunPayload("list", root, { wouldReadDirectory: true, limit: options.limit }));
      return 0;
    }
    const recordings = await listCorpusEntries(root, { limit: options.limit });
    printJson({ corpusRoot: root, count: recordings.length, recordings });
    return 0;
  }
  if (command === "metadata") {
    const [recording] = rest;
    if (!recording) throw new HostAccessError("metadata requires a recording id or filename");
    if (options.dryRun) {
      printJson(dryRunPayload("metadata", root, { wouldReadDirectory: true, wouldStatMatchedFile: true, recording }));
      return 0;
    }
    const entry = await findRecording(root, recording);
    printJson({ corpusRoot: root, recording: publicEntry(entry) });
    return 0;
  }
  if (command === "read-handle") {
    const [recording] = rest;
    if (!recording) throw new HostAccessError("read-handle requires a recording id or filename");
    if (options.dryRun) {
      printJson(dryRunPayload("read-handle", root, {
        wouldReadDirectory: true,
        wouldStatMatchedFile: true,
        recording,
        approval: options.approval,
      }));
      return 0;
    }
    if (!options.approval) throw new HostAccessError("read-handle requires --approval describing operator approval");
    const entry = await findRecording(root, recording);
    printJson({
      corpusRoot: root,
      handle: {
        ...publicEntry(entry, { includePath: true }),
        readOnly: true,
        approval: options.approval,
        audioBytesIncluded: false,
      },
    });
    return 0;
  }
  throw new HostAccessError(`unsupported operation: ${command}`);
}

async function main() {
  try {
    process.exitCode = await run();
  } catch (error) {
    if (error instanceof HostAccessError) {
      process.stderr.write(`error: ${error.message}\n`);
      process.exitCode = error instanceof RefusedOperationError ? 2 : 1;
      return;
    }
    throw error;
  }
}

const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  await main();
}

export {
  HostAccessError,
  METADATA_DB_NAMES,
  RefusedOperationError,
  capturedAtFromFilename,
  listCorpusEntries,
  listMeasurementEntries,
  listMetadataDatabaseFiles,
  openCorpusFileForRead,
  readMirrorManifest,
  recordingId,
  run,
};
