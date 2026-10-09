#!/usr/bin/env node
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { isInsideArtifacts, resolvePaths, resolveTools } from "./paths.mjs";

const MANIFEST_SCHEMA_VERSION = "voice-journey.local-voice-features.v1";
const METHOD_ID = "voice-journey.feature-extract.v1";
const VJ = resolvePaths();
const TOOLS = resolveTools(process.env, VJ);
const DEFAULT_INDEX = VJ.manifest("recording-index.json");
const DEFAULT_RESULTS = VJ.manifest("full-corpus-filter-results.json");
const DEFAULT_OUT = VJ.manifest("local-voice-features.json");
const DEFAULT_FEATURE_DIR = VJ.artifact("voice-features");
const DEFAULT_VENV_PYTHON = TOOLS.venvPython;
const DEFAULT_WORKER = path.join("analysis", "extract_features.py");
const DEFAULT_FFMPEG = TOOLS.ffmpegBin;
const DEFAULT_SAMPLE_RATE = 22050;
const DEFAULT_BUCKETS = "clean_singing";

class FeaturesError extends Error {}

function parseArgs(argv) {
  const [maybeCommand, ...rest] = argv;
  const hasCommand = maybeCommand && !maybeCommand.startsWith("--") && maybeCommand !== "help";
  const command = hasCommand ? maybeCommand : "extract-index";
  const args = hasCommand ? rest : argv;
  const options = {
    approval: null,
    buckets: DEFAULT_BUCKETS,
    command,
    corpusRoot: null,
    dryRun: false,
    featureDir: DEFAULT_FEATURE_DIR,
    ffmpegBin: DEFAULT_FFMPEG,
    generatedAt: null,
    index: DEFAULT_INDEX,
    limit: null,
    out: DEFAULT_OUT,
    results: DEFAULT_RESULTS,
    sampleRate: DEFAULT_SAMPLE_RATE,
    venvPython: DEFAULT_VENV_PYTHON,
    worker: DEFAULT_WORKER,
  };

  while (args.length > 0) {
    const next = args.shift();
    if (next === "--approval") {
      options.approval = requireValue(args, next);
    } else if (next === "--buckets") {
      options.buckets = requireValue(args, next);
    } else if (next === "--corpus-root") {
      options.corpusRoot = requireValue(args, next);
    } else if (next === "--dry-run") {
      options.dryRun = true;
    } else if (next === "--feature-dir") {
      options.featureDir = requireValue(args, next);
    } else if (next === "--ffmpeg-bin") {
      options.ffmpegBin = requireValue(args, next);
    } else if (next === "--generated-at") {
      options.generatedAt = requireValue(args, next);
    } else if (next === "--index") {
      options.index = requireValue(args, next);
    } else if (next === "--limit") {
      options.limit = parsePositiveInteger(requireValue(args, next), next);
    } else if (next === "--out") {
      options.out = requireValue(args, next);
    } else if (next === "--results") {
      options.results = requireValue(args, next);
    } else if (next === "--sample-rate") {
      options.sampleRate = parsePositiveInteger(requireValue(args, next), next);
    } else if (next === "--venv-python") {
      options.venvPython = requireValue(args, next);
    } else if (next === "--worker") {
      options.worker = requireValue(args, next);
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new FeaturesError(`unsupported argument: ${next}`);
    }
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new FeaturesError(`${flag} requires a value`);
  return value;
}

function parsePositiveInteger(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) throw new FeaturesError(`${flag} must be a positive integer`);
  return parsed;
}

function printHelp() {
  process.stdout.write(`Voice Journey per-recording feature extraction (Arc 2, feature contract v1).

Usage:
  voice-journey-features extract-index --dry-run [--index PATH] [--results PATH] [--buckets CSV]
  voice-journey-features extract-index --index PATH --results PATH --corpus-root PATH --approval TEXT [--buckets CSV] [--out PATH] [--feature-dir PATH] [--venv-python PATH] [--worker PATH] [--ffmpeg-bin PATH] [--sample-rate HZ] [--limit N]

Dry-run reads only committed manifests and reports the selection, tools, and
resume plan. Non-dry-run decodes each selected recording to a temporary mono
WAV (deleted afterwards), runs the local Python analysis worker, writes the
detailed per-recording feature JSON to the gitignored feature store, and
appends only summary aggregates plus a content digest to the repo-safe
manifest. Resumable by recordingId; failed recordings record a failed row and
the run continues.
`);
}

async function readJson(filePath) {
  return JSON.parse(await readFile(filePath, "utf8"));
}

async function writeJson(filePath, payload) {
  const body = `${JSON.stringify(payload, null, 2)}\n`;
  if (filePath === "-") {
    process.stdout.write(body);
    return;
  }
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(filePath, body, "utf8");
  process.stdout.write(`${filePath}\n`);
}

function yearFor(entry) {
  return entry.capturedAt ? entry.capturedAt.slice(0, 4) : "unknown";
}

function round(value, places = 3) {
  return Number.isFinite(value) ? Number(value.toFixed(places)) : null;
}

function sha256Hex(text) {
  return createHash("sha256").update(text).digest("hex");
}

function bucketSetFor(options) {
  return new Set(options.buckets.split(",").map((bucket) => bucket.trim()).filter(Boolean));
}

function selectRecordings(indexManifest, resultsManifest, options) {
  const buckets = bucketSetFor(options);
  const bucketById = new Map(
    (resultsManifest.results ?? []).map((row) => [row.recordingId, row.classification?.finalBucket ?? "unclassified"]),
  );
  const selected = [];
  for (const entry of indexManifest.recordings ?? []) {
    const bucket = bucketById.get(entry.recordingId) ?? "unclassified";
    if (!buckets.has(bucket)) continue;
    selected.push({ entry, bucket });
    if (options.limit && selected.length >= options.limit) break;
  }
  return selected;
}

function featuresReadScope() {
  return {
    selectedBucketsOnly: true,
    audioBytes: true,
    decodeToTemporaryWav: true,
    retainedAudioBytes: false,
    featureArraysRetainedLocally: true,
    featureArraysCommittedToRepo: false,
    transcriptText: false,
    upload: false,
    corpusMutation: false,
  };
}

function analysisConfigFor(options, versions = null, workerConfig = null) {
  return {
    methodId: METHOD_ID,
    sampleRateHz: options.sampleRate,
    decode: "ffmpeg to temporary mono WAV at sampleRateHz, deleted after each recording",
    workerConfigAuthority: "analysis/extract_features.py CONFIG governs detailed parameters; the worker reports it in its hello line and it is embedded here",
    workerConfig,
    versions,
  };
}

function configFingerprintFor(options, versions, workerConfig = null) {
  return sha256Hex(JSON.stringify(analysisConfigFor(options, versions, workerConfig)));
}

function ffmpegToolChoice(options, toolVersions = {}) {
  return {
    name: "ffmpeg",
    version: toolVersions?.ffmpeg ?? null,
    stage: "decode-to-temporary-mono-wav",
    openness: "open-source",
    openSource: true,
    path: options.ffmpegBin,
    notes: `Decodes approved local audio to ${options.sampleRate} Hz mono WAV in a temporary directory, deleted after each recording.`,
  };
}

function analysisToolChoice(options, versions = null) {
  return {
    name: "praat-parselmouth + librosa (analysis/.venv)",
    version: versions ? JSON.stringify(versions) : null,
    stage: "per-recording-voice-feature-extraction",
    openness: "open-source",
    openSource: true,
    path: options.venvPython,
    workerScript: options.worker,
    methodId: METHOD_ID,
    openSwapCandidate: "Essentia; torchcrepe planned as optional --f0-engine upgrade",
    notes: "Praat autocorrelation f0 + Praat voice-quality metrics + librosa spectral features. Detailed arrays stay in the gitignored local feature store.",
  };
}

function buildFeaturesDryRun(indexManifest, resultsManifest, options) {
  const selected = selectRecordings(indexManifest, resultsManifest, options);
  const byBucket = {};
  for (const { bucket } of selected) byBucket[bucket] = (byBucket[bucket] ?? 0) + 1;
  return {
    dryRun: true,
    operation: "voice-feature-extraction",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["recording-index-manifest", "filter-results-manifest", "host-access-seam"],
      writes: ["local-voice-feature-store", "voice-feature-manifest"],
    },
    selection: {
      buckets: [...bucketSetFor(options)],
      selectedCount: selected.length,
      byBucket,
      limit: options.limit,
    },
    sourceIndex: sourceProvenance(indexManifest),
    sourceResults: sourceProvenance(resultsManifest),
    outputPath: options.out,
    localFeatureDir: options.featureDir,
    analysisConfig: analysisConfigFor(options),
    tools: [ffmpegToolChoice(options), analysisToolChoice(options)],
    provisioning: [
      "python3.12 -m venv analysis/.venv",
      "analysis/.venv/bin/pip install -r analysis/requirements.txt",
      "analysis/.venv/bin/python analysis/selftest.py",
    ],
    resumeBehavior: {
      enabled: true,
      key: "recordingId",
      outputWrittenAfterEachRecording: true,
      existingCompletedResultsSkipped: true,
      failedRowsRetriedOnRerun: true,
    },
    readScope: featuresReadScope(),
  };
}

function sourceProvenance(manifest) {
  return {
    schemaVersion: manifest.schemaVersion ?? null,
    generatedAt: manifest.generatedAt ?? null,
    updatedAt: manifest.updatedAt ?? null,
    recordingCount: manifest.totals?.recordings ?? manifest.results?.length ?? manifest.recordings?.length ?? null,
  };
}

function buildFeaturesManifest(indexManifest, resultsManifest, results, options, extras = {}) {
  const timestamp = options.generatedAt ?? new Date().toISOString();
  const completedCount = results.filter((row) => row.status === "completed").length;
  return {
    schemaVersion: MANIFEST_SCHEMA_VERSION,
    generatedAt: timestamp,
    updatedAt: timestamp,
    sourceIndex: sourceProvenance(indexManifest),
    sourceResults: { ...sourceProvenance(resultsManifest), selectedBuckets: [...bucketSetFor(options)] },
    releaseGateApproval: options.approval ?? null,
    readScope: featuresReadScope(),
    localFeatureStore: {
      root: options.featureDir,
      gitignoredExpected: isGitignoredLocalPath(options.featureDir),
      containsFeatureArrays: true,
      committedToRepo: false,
    },
    repoSafeManifest: {
      containsAudioBytes: false,
      containsTranscriptText: false,
      containsFeatureArrays: false,
    },
    analysisConfig: { ...analysisConfigFor(options, extras.versions ?? null, extras.workerConfig ?? null), configFingerprint: extras.configFingerprint ?? null },
    resume: {
      enabled: true,
      key: "recordingId",
      outputWrittenAfterEachRecording: true,
      existingCompletedResultsSkipped: true,
      failedRowsRetriedOnRerun: true,
      skippedCompletedCount: extras.skippedCompleted ?? 0,
      completedCount,
      failedCount: results.filter((row) => row.status === "failed").length,
      remainingCount: Math.max((extras.selectedCount ?? results.length) - completedCount, 0),
      lastProcessedRecordingId: results.at(-1)?.recordingId ?? null,
    },
    toolChoices: [ffmpegToolChoice(options, extras.toolVersions), analysisToolChoice(options, extras.versions ?? null)],
    results,
  };
}

function buildFeatureRow(entry, bucket, workerResult, options, extras = {}) {
  const base = {
    recordingId: entry.recordingId,
    filename: entry.filename,
    capturedAt: entry.capturedAt ?? null,
    year: yearFor(entry),
    sourceRef: entry.sourceRef,
    bucket,
    status: workerResult.status,
    configFingerprint: extras.configFingerprint ?? null,
    toolRun: {
      extractor: options.worker,
      python: options.venvPython,
      methodId: METHOD_ID,
      audioPrep: `ffmpeg decode to ${options.sampleRate} Hz mono wav in a temporary directory`,
    },
  };
  if (workerResult.status !== "completed") {
    return { ...base, error: workerResult.error ?? "unknown extraction failure", features: null, featuresDigest: null, localArtifacts: null };
  }
  return {
    ...base,
    features: workerResult.summary,
    featuresDigest: extras.featuresDigest ?? null,
    localArtifacts: {
      featureJsonPath: localPathRef(extras.featureJsonPath),
      localOnly: true,
      gitignoredExpected: isGitignoredLocalPath(extras.featureJsonPath),
      arraysIncludedInManifest: false,
    },
  };
}

async function extractIndexManifest(indexManifest, resultsManifest, options, hooks = {}) {
  if (!options.approval) throw new FeaturesError("extract-index requires --approval for non-dry-run audio reads");
  if (!options.corpusRoot) throw new FeaturesError("extract-index requires --corpus-root");
  if (options.out === "-") throw new FeaturesError("extract-index requires a file output for incremental resume support");

  await mkdir(options.featureDir, { recursive: true });
  const selected = selectRecordings(indexManifest, resultsManifest, options);
  const existing = await readExistingManifest(options.out);
  const completedById = new Map(
    (existing?.results ?? []).filter((row) => row.status === "completed").map((row) => [row.recordingId, row]),
  );

  const createWorker = hooks.createWorker ?? createFeatureWorker;
  const worker = await createWorker(options);
  const versions = worker.versions ?? null;
  const workerConfig = worker.config ?? null;
  const configFingerprint = configFingerprintFor(options, versions, workerConfig);
  const toolVersions = hooks.toolVersions ?? { ffmpeg: await readToolVersion(options.ffmpegBin, ["-version"]) };

  const manifest = buildFeaturesManifest(indexManifest, resultsManifest, [...completedById.values()], options, {
    versions,
    workerConfig,
    configFingerprint,
    toolVersions,
    skippedCompleted: completedById.size,
    selectedCount: selected.length,
  });
  await writeJson(options.out, manifest);

  try {
    const decodeRecording = hooks.decodeRecording ?? decodeRecordingToWav;
    for (const { entry, bucket } of selected) {
      if (completedById.has(entry.recordingId)) continue;
      const decoded = await decodeRecording(entry, options);
      let workerResult;
      try {
        workerResult = await worker.extract({ recordingId: entry.recordingId, wavPath: decoded.wavPath });
      } finally {
        await decoded.cleanup();
      }
      const extras = { configFingerprint };
      if (workerResult.status === "completed") {
        const featureJsonPath = path.join(options.featureDir, `${entry.recordingId}.json`);
        const detailBody = `${JSON.stringify({ recordingId: entry.recordingId, methodId: METHOD_ID, summary: workerResult.summary, detail: workerResult.detail }, null, 2)}\n`;
        await writeFile(featureJsonPath, detailBody, "utf8");
        extras.featureJsonPath = featureJsonPath;
        extras.featuresDigest = sha256Hex(detailBody);
      }
      const row = buildFeatureRow(entry, bucket, workerResult, options, extras);
      manifest.results.push(row);
      manifest.resume.completedCount = manifest.results.filter((candidate) => candidate.status === "completed").length;
      manifest.resume.failedCount = manifest.results.filter((candidate) => candidate.status === "failed").length;
      manifest.resume.remainingCount = Math.max(selected.length - manifest.resume.completedCount, 0);
      manifest.resume.lastProcessedRecordingId = row.recordingId;
      manifest.updatedAt = options.generatedAt ?? new Date().toISOString();
      await writeJson(options.out, manifest);
    }
  } finally {
    await worker.close();
  }
  return manifest;
}

async function decodeRecordingToWav(entry, options) {
  const tempRoot = await mkdtemp(path.join(tmpdir(), "voice-journey-features-"));
  const wavPath = path.join(tempRoot, `${entry.recordingId}.wav`);
  await runCommand(options.ffmpegBin, [
    "-hide_banner",
    "-nostdin",
    "-v",
    "error",
    "-y",
    "-i",
    path.join(options.corpusRoot, entry.filename),
    "-ac",
    "1",
    "-ar",
    String(options.sampleRate),
    wavPath,
  ], `ffmpeg failed for ${entry.filename}`);
  return { wavPath, cleanup: () => rm(tempRoot, { recursive: true, force: true }) };
}

async function createFeatureWorker(options) {
  const child = spawn(options.venvPython, [options.worker], { stdio: ["pipe", "pipe", "pipe"] });
  const stderrChunks = [];
  child.stderr.on("data", (chunk) => stderrChunks.push(chunk));
  const lines = createInterface({ input: child.stdout });
  const pending = [];
  let helloPayload = null;
  let helloResolve = null;
  let helloReject = null;
  const hello = new Promise((resolve, reject) => {
    helloResolve = resolve;
    helloReject = reject;
  });
  const fail = (error) => {
    if (helloReject) {
      helloReject(error);
      helloReject = null;
      helloResolve = null;
    }
    while (pending.length > 0) pending.shift().reject(error);
  };
  lines.on("line", (line) => {
    if (!line.trim()) return;
    let payload;
    try {
      payload = JSON.parse(line);
    } catch {
      fail(new FeaturesError(`feature worker emitted unparseable output: ${line.slice(0, 200)}`));
      return;
    }
    if (payload.hello) {
      helloPayload = payload.hello;
      if (helloResolve) {
        helloResolve(payload.hello);
        helloResolve = null;
        helloReject = null;
      }
      return;
    }
    const next = pending.shift();
    if (next) next.resolve(payload);
  });
  child.on("error", fail);
  child.on("close", (code) => {
    const stderrTail = Buffer.concat(stderrChunks).toString("utf8").trim().split("\n").slice(-5).join("\n");
    fail(new FeaturesError(`feature worker exited (code ${code})${stderrTail ? `: ${stderrTail}` : ""}`));
  });
  await hello;
  return {
    versions: helloPayload?.versions ?? null,
    config: helloPayload?.config ?? null,
    extract(task) {
      return new Promise((resolve, reject) => {
        pending.push({ resolve, reject });
        child.stdin.write(`${JSON.stringify(task)}\n`);
      });
    },
    close() {
      return new Promise((resolve) => {
        child.once("close", resolve);
        child.stdin.end();
      });
    },
  };
}

async function readExistingManifest(filePath) {
  try {
    return JSON.parse(await readFile(filePath, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function readToolVersion(command, args) {
  try {
    const result = await captureCommand(command, args);
    return result.split("\n").find((line) => line.trim())?.trim() ?? null;
  } catch {
    return null;
  }
}

async function runCommand(command, args, message) {
  await captureCommand(command, args, message);
}

async function captureCommand(command, args, message = `${path.basename(command)} failed`) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("error", reject);
    child.on("close", (code) => {
      const out = Buffer.concat(stdout).toString("utf8");
      const err = Buffer.concat(stderr).toString("utf8").trim();
      if (code !== 0) {
        reject(new FeaturesError(`${message}: ${err || `exit code ${code}`}`));
        return;
      }
      resolve(out || err);
    });
  });
}

function localPathRef(filePath) {
  const relative = path.relative(process.cwd(), filePath);
  return relative.startsWith("..") || path.isAbsolute(relative) ? filePath : relative;
}

function isGitignoredLocalPath(filePath) {
  return isInsideArtifacts(filePath);
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (!options.command || options.help) {
    printHelp();
    return 0;
  }
  if (options.command === "extract-index") {
    const indexManifest = await readJson(options.index);
    const resultsManifest = await readJson(options.results);
    if (options.dryRun) {
      await writeJson("-", buildFeaturesDryRun(indexManifest, resultsManifest, options));
      return 0;
    }
    await extractIndexManifest(indexManifest, resultsManifest, options);
    return 0;
  }
  throw new FeaturesError(`unsupported command: ${options.command}`);
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

export {
  buildFeatureRow,
  buildFeaturesDryRun,
  buildFeaturesManifest,
  configFingerprintFor,
  extractIndexManifest,
  run,
  selectRecordings,
};
