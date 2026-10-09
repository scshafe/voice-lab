#!/usr/bin/env node
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { listCorpusEntries, listMeasurementEntries } from "./host-access.mjs";
import { resolvePaths } from "./paths.mjs";

const VJ = resolvePaths();
const DEFAULT_CORPUS_ROOT = VJ.phoneCorpusRoot;
const DEFAULT_OUTPUT = VJ.manifest("recording-index.json");
const SCHEMA_VERSION = "voice-journey.recording-index.v1";

class IndexError extends Error {}

function parseArgs(argv) {
  const args = [...argv];
  const options = {
    corpusRoot: DEFAULT_CORPUS_ROOT,
    dryRun: false,
    generatedAt: null,
    limit: null,
    measurementRoot: VJ.measurementDir,
    out: DEFAULT_OUTPUT,
    pretty: true,
  };
  while (args.length > 0) {
    const next = args.shift();
    if (next === "--corpus-root") {
      options.corpusRoot = requireValue(args, next);
    } else if (next === "--dry-run") {
      options.dryRun = true;
    } else if (next === "--generated-at") {
      options.generatedAt = requireValue(args, next);
    } else if (next === "--limit") {
      const value = Number(requireValue(args, next));
      if (!Number.isInteger(value) || value < 0) throw new IndexError("--limit must be a non-negative integer");
      options.limit = value;
    } else if (next === "--measurement-root") {
      options.measurementRoot = requireValue(args, next);
    } else if (next === "--out") {
      options.out = requireValue(args, next);
    } else if (next === "--compact") {
      options.pretty = false;
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new IndexError(`unsupported argument: ${next}`);
    }
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new IndexError(`${flag} requires a value`);
  return value;
}

function printHelp() {
  process.stdout.write(`Build the repo-safe Voice Journey recording index.

Usage:
  voice-journey-index [--corpus-root PATH] [--measurement-root PATH] [--out PATH] [--generated-at ISO] [--limit N]
  voice-journey-index --dry-run [--corpus-root PATH] [--out PATH]

The indexer reads recording metadata through the host-access seam only. It never
reads audio bytes, writes to the corpus, uploads data, or scans arbitrary paths.
`);
}

function buildDryRun(options) {
  return {
    dryRun: true,
    operation: "rebuild-recording-index",
    architecture: {
      slug: "kickoff-corpus-flow",
      readsVia: ["host-access-seam", "seam-feeds-indexer"],
      writes: ["recording-index-manifest", "indexer-writes-manifest"],
    },
    corpusRoot: path.resolve(options.corpusRoot),
    measurementRoot: path.resolve(options.measurementRoot),
    outputPath: options.out,
    readScope: {
      throughSeamOnly: true,
      directoryEntries: true,
      fileStatMetadata: true,
      audioBytes: false,
      durationExtraction: false,
      mutation: false,
      upload: false,
    },
    manifestShape: {
      schemaVersion: SCHEMA_VERSION,
      recordingFields: [
        "recordingId",
        "sourceRef",
        "filename",
        "capturedAt",
        "durationSeconds",
        "file",
        "indexingStatus",
        "contentClassification",
        "contaminationClassification",
      ],
      classificationFields: [
        "contentClassification.singingStatus",
        "contentClassification.confidence",
        "contentClassification.classifier",
        "contaminationClassification.noiseMusicStatus",
      ],
      toolChoiceFields: ["name", "stage", "openness", "openSource", "openSwapCandidate"],
    },
  };
}

async function readPreviousIndex(out) {
  if (out === "-") return null;
  try {
    return JSON.parse(await readFile(out, "utf8"));
  } catch {
    return null;
  }
}

// A recording deleted on the phone stays on the server and is marked, never
// removed. The mirror never deletes, and its manifest lists what the Mini saw
// at the last run, so a file the manifest no longer lists is `deleted` only if
// the previous index had seen it listed (`present`) or already marked it
// (sticky). A file the manifest has not caught up with yet is `unverified`.
// Without a manifest (no drop run yet, local corpus) no state is recorded.
function markPhoneState(entries, previous) {
  const before = new Map((previous?.recordings ?? []).map((row) => [row.recordingId, row.phoneState]));
  return entries.map(({ inManifest, ...entry }) => {
    if (inManifest === null || inManifest === undefined) return entry;
    if (inManifest) return { ...entry, phoneState: "present" };
    const seen = before.get(entry.recordingId);
    return { ...entry, phoneState: seen === "present" || seen === "deleted" ? "deleted" : "unverified" };
  });
}

function buildManifest(recordings, options) {
  const generatedAt = options.generatedAt ?? new Date().toISOString();
  const sortedRecordings = [...recordings].sort((left, right) => {
    const leftCaptured = left.capturedAt ?? "";
    const rightCaptured = right.capturedAt ?? "";
    return leftCaptured.localeCompare(rightCaptured) || left.filename.localeCompare(right.filename);
  });
  return {
    schemaVersion: SCHEMA_VERSION,
    generatedAt,
    source: {
      kind: "voice-memos-host-access-seam",
      architecture: "kickoff-corpus-flow",
      seamNodeKey: "host-access-seam",
      indexerNodeKey: "recording-indexer",
      manifestNodeKey: "recording-index-manifest",
      readScope: {
        directoryEntries: true,
        fileStatMetadata: true,
        audioBytes: false,
        durationExtraction: false,
        mutation: false,
        upload: false,
      },
    },
    toolChoices: [
      {
        name: "Node.js standard library fs/stat",
        stage: "metadata-index",
        openness: "open-source-runtime",
        openSource: true,
        openSwapCandidate: null,
        notes: "Used only for directory enumeration and file stat metadata through the approved seam.",
      },
      {
        name: "Singing/non-singing classifier not selected in this phase",
        stage: "content-classification-placeholder",
        openness: "not-selected",
        openSource: null,
        openSwapCandidate: "Essentia, librosa, or pyAudioAnalysis-based local classifier in the filtering phase",
        notes: "The index carries classification fields now; later phases populate them from approved local analysis.",
      },
    ],
    classificationSchema: {
      singingStatusValues: ["unknown", "singing", "non_singing", "uncertain"],
      noiseMusicStatusValues: ["not_evaluated", "clean", "noise_contaminated", "music_contaminated", "uncertain"],
    },
    totals: {
      recordings: sortedRecordings.length,
      audioBytesIndexed: 0,
    },
    recordings: sortedRecordings.map((entry) => ({
      recordingId: entry.recordingId,
      sourceRef: {
        seam: "voice-journey-corpus",
        selector: entry.recordingId,
        filename: entry.filename,
      },
      filename: entry.filename,
      capturedAt: entry.capturedAt,
      durationSeconds: null,
      durationSource: "not_read_metadata_only_index",
      file: {
        extension: path.extname(entry.filename).toLowerCase(),
        sizeBytes: entry.sizeBytes,
        modifiedAt: entry.modifiedAt,
      },
      ...(entry.source ? { source: entry.source } : {}),
      ...(entry.phoneState ? { phoneState: entry.phoneState } : {}),
      indexingStatus: entry.capturedAt ? "indexed_metadata" : "indexed_metadata_filename_unparsed",
      contentClassification: {
        singingStatus: "unknown",
        confidence: null,
        classifier: null,
        reviewedBy: null,
        notes: "Pending later singing/non-singing classification pass.",
      },
      contaminationClassification: {
        noiseMusicStatus: "not_evaluated",
        confidence: null,
        classifier: null,
        reviewedBy: null,
        notes: "Pending later noise/music filtering pass for recordings classified as singing.",
      },
    })),
  };
}

async function writeManifest(manifest, outPath, { pretty }) {
  const body = `${JSON.stringify(manifest, null, pretty ? 2 : 0)}\n`;
  if (outPath === "-") {
    process.stdout.write(body);
    return;
  }
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, body, "utf8");
  process.stdout.write(`${outPath}\n`);
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (options.help) {
    printHelp();
    return 0;
  }
  if (options.dryRun) {
    process.stdout.write(`${JSON.stringify(buildDryRun(options), null, 2)}\n`);
    return 0;
  }
  const phone = await listCorpusEntries(path.resolve(options.corpusRoot), { limit: options.limit, mirrorManifest: true });
  const recordings = [
    ...markPhoneState(phone, await readPreviousIndex(options.out)),
    ...(await listMeasurementEntries(path.resolve(options.measurementRoot))),
  ];
  const manifest = buildManifest(recordings, options);
  await writeManifest(manifest, options.out, { pretty: options.pretty });
  return 0;
}

async function main() {
  try {
    process.exitCode = await run();
  } catch (error) {
    if (error instanceof Error) {
      process.stderr.write(`error: ${error.message}\n`);
      process.exitCode = 1;
      return;
    }
    throw error;
  }
}

const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  await main();
}

export { buildDryRun, buildManifest, markPhoneState, run };
