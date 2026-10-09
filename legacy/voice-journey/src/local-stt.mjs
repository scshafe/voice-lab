#!/usr/bin/env node
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { isInsideArtifacts, resolvePaths, resolveTools } from "./paths.mjs";

const MANIFEST_SCHEMA_VERSION = "voice-journey.local-stt-transcripts.v1";
const VJ = resolvePaths();
const TOOLS = resolveTools(process.env, VJ);
const DEFAULT_INDEX = VJ.manifest("recording-index.json");
const DEFAULT_OUT = VJ.manifest("local-stt-transcripts.json");
const DEFAULT_TRANSCRIPT_DIR = VJ.artifact("stt-transcripts");
const DEFAULT_FFMPEG = TOOLS.ffmpegBin;
const DEFAULT_WHISPER_BIN = TOOLS.whisperBin;
const DEFAULT_WHISPER_MODEL = TOOLS.whisperModel;
const DEFAULT_WHISPER_THREADS = TOOLS.whisperThreads;
const SAMPLE_RATE = 16000;

class LocalSttError extends Error {}

function parseArgs(argv) {
  const [maybeCommand, ...rest] = argv;
  const hasCommand = maybeCommand && !maybeCommand.startsWith("--") && maybeCommand !== "help";
  const command = hasCommand ? maybeCommand : "transcribe-index";
  const args = hasCommand ? rest : argv;
  const options = {
    approval: null,
    backfilledAt: null,
    command,
    corpusRoot: null,
    dryRun: false,
    ffmpegBin: DEFAULT_FFMPEG,
    generatedAt: null,
    index: DEFAULT_INDEX,
    language: "auto",
    manifest: DEFAULT_OUT,
    model: DEFAULT_WHISPER_MODEL,
    out: DEFAULT_OUT,
    outSet: false,
    threads: DEFAULT_WHISPER_THREADS,
    transcriptDir: DEFAULT_TRANSCRIPT_DIR,
    whisperBin: DEFAULT_WHISPER_BIN,
  };

  while (args.length > 0) {
    const next = args.shift();
    if (next === "--approval") {
      options.approval = requireValue(args, next);
    } else if (next === "--backfilled-at") {
      options.backfilledAt = requireValue(args, next);
    } else if (next === "--corpus-root") {
      options.corpusRoot = requireValue(args, next);
    } else if (next === "--dry-run") {
      options.dryRun = true;
    } else if (next === "--ffmpeg-bin") {
      options.ffmpegBin = requireValue(args, next);
    } else if (next === "--generated-at") {
      options.generatedAt = requireValue(args, next);
    } else if (next === "--index") {
      options.index = requireValue(args, next);
    } else if (next === "--language") {
      options.language = requireValue(args, next);
    } else if (next === "--manifest") {
      options.manifest = requireValue(args, next);
    } else if (next === "--model") {
      options.model = requireValue(args, next);
    } else if (next === "--out") {
      options.out = requireValue(args, next);
      options.outSet = true;
    } else if (next === "--threads") {
      options.threads = parsePositiveInteger(requireValue(args, next), next);
    } else if (next === "--transcript-dir") {
      options.transcriptDir = requireValue(args, next);
    } else if (next === "--whisper-bin") {
      options.whisperBin = requireValue(args, next);
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new LocalSttError(`unsupported argument: ${next}`);
    }
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new LocalSttError(`${flag} requires a value`);
  return value;
}

function parsePositiveInteger(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) throw new LocalSttError(`${flag} must be a positive integer`);
  return parsed;
}

function printHelp() {
  process.stdout.write(`Voice Journey local STT tooling.

Usage:
  voice-journey-stt transcribe-index --dry-run [--index PATH] [--out PATH]
  voice-journey-stt transcribe-index --index PATH --corpus-root PATH --approval TEXT [--out PATH] [--transcript-dir PATH] [--whisper-bin PATH] [--model PATH] [--ffmpeg-bin PATH] [--language LANG] [--threads N (default $WHISPER_CPP_THREADS)]
  voice-journey-stt backfill-manifest --dry-run [--manifest PATH]
  voice-journey-stt backfill-manifest [--manifest PATH] [--transcript-dir PATH] [--out PATH] [--backfilled-at ISO]

Dry-run reads only committed manifests. Non-dry-run reads approved local audio,
writes transcript text under the local transcript directory, and writes only
repo-safe aggregate metadata plus local path references to the manifest.

backfill-manifest re-derives durationProcessedSeconds (and the duration-based
derived rates) from the local transcript JSON segment offsets. It reads no
audio, re-runs no STT, and writes only numeric metadata back to the repo-safe
manifest. Its dry-run reads only the manifest.
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

function buildSttDryRun(indexManifest, options) {
  return {
    dryRun: true,
    operation: "local-stt-transcription",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["recording-index-manifest", "index-feeds-stt", "host-access-seam", "seam-provides-stt-audio"],
      writes: ["local-transcript-store", "transcript-manifest", "stt-writes-local-transcripts", "stt-writes-transcript-manifest"],
    },
    recordingCount: indexManifest.recordings?.length ?? 0,
    sourceIndex: sourceIndexFor(indexManifest),
    corpusRoot: options.corpusRoot,
    outputPath: options.out,
    localTranscriptDir: options.transcriptDir,
    sttTool: sttToolChoice(options, {}),
    audioPrepTool: ffmpegToolChoice(options, {}),
    model: {
      path: options.model,
      family: "whisper.cpp ggml",
      defaultSelection: path.basename(options.model),
      openSource: true,
    },
    language: options.language,
    expectedRuntime: "overnight-scale for about 66 hours of indexed audio on the current host inventory",
    resumeBehavior: {
      enabled: true,
      key: "recordingId",
      outputWrittenAfterEachRecording: true,
      existingCompletedResultsSkipped: true,
      rerunCommandIsSameAsInitialCommand: true,
    },
    readScope: readScope(),
    repoSafeManifestFields: [
      "status",
      "language",
      "durationProcessedSeconds",
      "confidenceStats",
      "wordCount",
      "segmentCount",
      "derivedFeatures",
      "local transcript path references",
      "tool/model provenance",
    ],
  };
}

async function transcribeIndexManifest(indexManifest, options, hooks = {}) {
  if (!options.approval) throw new LocalSttError("transcribe-index requires --approval for non-dry-run audio reads");
  if (!options.corpusRoot) throw new LocalSttError("transcribe-index requires --corpus-root");
  if (options.out === "-") throw new LocalSttError("transcribe-index requires a file output for incremental resume support");

  await mkdir(options.transcriptDir, { recursive: true });
  const existing = await readExistingManifest(options.out);
  const completedById = new Map((existing?.results ?? []).filter((row) => row.status === "completed").map((row) => [row.recordingId, row]));
  const toolVersions = hooks.toolVersions ?? {
    ffmpeg: await readToolVersion(options.ffmpegBin, ["-version"]),
    whisperCpp: await readToolVersion(options.whisperBin, ["--version"]),
  };
  const manifest = buildSttManifest(indexManifest, [...completedById.values()], {
    ...options,
    toolVersions,
    skippedCompleted: completedById.size,
  });
  await writeJson(options.out, manifest);

  const transcribeRecording = hooks.transcribeRecording ?? transcribeRecordingEntry;
  for (const entry of indexManifest.recordings ?? []) {
    if (completedById.has(entry.recordingId)) continue;
    // Measurement-chain takes are indexed but not transcribed yet (Phase B).
    if (entry.source?.type === "measurement") continue;
    const result = await transcribeRecording(entry, options);
    const materialized = await materializeTranscriptResult(entry, result, options);
    const row = buildTranscriptRow(entry, materialized, options);
    manifest.results.push(row);
    manifest.resume.completedCount = manifest.results.filter((candidate) => candidate.status === "completed").length;
    manifest.resume.remainingCount = Math.max((indexManifest.recordings?.length ?? 0) - manifest.resume.completedCount, 0);
    manifest.resume.lastCompletedRecordingId = row.recordingId;
    manifest.updatedAt = new Date().toISOString();
    await writeJson(options.out, manifest);
  }
  return manifest;
}

async function transcribeRecordingEntry(entry, options) {
  const audioPath = path.join(options.corpusRoot, entry.filename);
  const transcriptBase = path.join(options.transcriptDir, entry.recordingId);
  const tempRoot = await mkdtemp(path.join(tmpdir(), "voice-journey-stt-"));
  const wavPath = path.join(tempRoot, `${entry.recordingId}.wav`);
  try {
    await runCommand(options.ffmpegBin, [
      "-hide_banner",
      "-nostdin",
      "-v",
      "error",
      "-y",
      "-i",
      audioPath,
      "-ac",
      "1",
      "-ar",
      String(SAMPLE_RATE),
      wavPath,
    ], `ffmpeg failed for ${entry.filename}`);
    const args = ["-m", options.model, "-f", wavPath, "-l", options.language, "-otxt", "-oj", "-of", transcriptBase];
    if (options.threads) args.push("-t", String(options.threads));
    await runCommand(options.whisperBin, args, `whisper.cpp failed for ${entry.filename}`);
    return {
      textPath: `${transcriptBase}.txt`,
      jsonPath: `${transcriptBase}.json`,
      rawTranscript: await readJson(`${transcriptBase}.json`),
    };
  } finally {
    await rm(tempRoot, { recursive: true, force: true });
  }
}

async function materializeTranscriptResult(entry, result, options) {
  const transcriptBase = path.join(options.transcriptDir, entry.recordingId);
  const rawTranscript = result.rawTranscript ?? {
    text: result.transcriptText ?? "",
    language: result.language ?? options.language,
    segments: result.segments ?? [],
    durationSeconds: result.durationSeconds ?? null,
  };
  const textPath = result.textPath ?? `${transcriptBase}.txt`;
  const jsonPath = result.jsonPath ?? `${transcriptBase}.json`;
  if (!result.textPath) await writeFile(textPath, `${result.transcriptText ?? textFromTranscript(rawTranscript)}\n`, "utf8");
  if (!result.jsonPath) await writeFile(jsonPath, `${JSON.stringify(rawTranscript, null, 2)}\n`, "utf8");
  return { textPath, jsonPath, rawTranscript };
}

function buildBackfillDryRun(manifest, options) {
  const rows = manifest.results ?? [];
  return {
    dryRun: true,
    operation: "local-stt-duration-backfill",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["transcript-manifest", "local-transcript-store"],
      writes: ["transcript-manifest"],
    },
    manifestPath: options.manifest,
    outputPath: backfillTargetFor(options),
    localTranscriptDir: options.transcriptDir,
    rows: {
      total: rows.length,
      completed: rows.filter((row) => row.status === "completed").length,
      durationProcessedSecondsNull: rows.filter((row) => (row.durationProcessedSeconds ?? null) === null).length,
      wordsPerMinuteNull: rows.filter((row) => (row.derivedFeatures?.wordsPerMinute ?? null) === null).length,
      segmentDensityPerMinuteNull: rows.filter((row) => (row.derivedFeatures?.segmentDensityPerMinute ?? null) === null).length,
      confidenceStatsAllNull: rows.filter((row) => confidenceStatsAllNull(row.confidenceStats)).length,
    },
    method: backfillMethod(),
    readScope: backfillReadScope(),
  };
}

function confidenceStatsAllNull(stats) {
  return [stats?.meanLogProbability, stats?.meanNoSpeechProbability, stats?.meanCompressionRatio]
    .every((value) => (value ?? null) === null);
}

function backfillMethod() {
  return {
    id: "whisper_cpp_offsets_ms.v1",
    prefersSegmentOffsetsMilliseconds: true,
    timestampFallbackNormalizesCommaDecimals: true,
    rereadsAudio: false,
    rerunsStt: false,
    confidenceStatsNote: "extracted only when transcript JSON segments carry avg_logprob/no_speech_prob/compression_ratio; whisper-cli -oj output typically does not",
  };
}

function backfillReadScope() {
  return {
    audioBytes: false,
    transcriptJsonParsedForTimingMetadataOnly: true,
    transcriptTextCommittedToRepo: false,
    upload: false,
    corpusMutation: false,
  };
}

function backfillTargetFor(options) {
  return options.outSet ? options.out : options.manifest;
}

async function backfillManifest(manifest, options) {
  const rows = manifest.results ?? [];
  const counters = {
    rowsExamined: 0,
    rowsSkippedNotCompleted: 0,
    rowsUpdatedDuration: 0,
    rowsRefreshedDuration: 0,
    rowsNoDurationAvailable: 0,
    rowsMissingTranscriptJson: 0,
    rowsUnparseableTranscriptJson: 0,
    rowsConfidenceStatsUpdated: 0,
  };
  const missingRecordingIds = [];
  for (const row of rows) {
    counters.rowsExamined += 1;
    if (row.status !== "completed") {
      counters.rowsSkippedNotCompleted += 1;
      continue;
    }
    const jsonPath = path.join(options.transcriptDir, `${row.recordingId}.json`);
    let rawTranscript;
    try {
      rawTranscript = await readJson(jsonPath);
    } catch (error) {
      if (error?.code === "ENOENT") {
        counters.rowsMissingTranscriptJson += 1;
        missingRecordingIds.push(row.recordingId);
        continue;
      }
      if (error instanceof SyntaxError) {
        counters.rowsUnparseableTranscriptJson += 1;
        continue;
      }
      throw error;
    }
    const segments = segmentsFromTranscript(rawTranscript);
    const duration = durationFromTranscript(rawTranscript, segments);
    if (duration === null) {
      counters.rowsNoDurationAvailable += 1;
    } else {
      if ((row.durationProcessedSeconds ?? null) === null) {
        counters.rowsUpdatedDuration += 1;
      } else if (row.durationProcessedSeconds !== duration) {
        counters.rowsRefreshedDuration += 1;
      }
      row.durationProcessedSeconds = duration;
      const words = row.counts?.words ?? 0;
      const segmentCount = row.counts?.segments ?? segments.length;
      row.derivedFeatures = {
        ...row.derivedFeatures,
        wordsPerMinute: duration > 0 ? round(words / (duration / 60), 2) : null,
        segmentDensityPerMinute: duration > 0 ? round(segmentCount / (duration / 60), 2) : null,
      };
    }
    const confidenceStats = confidenceStatsFor(segments);
    if (!confidenceStatsAllNull(confidenceStats)) {
      row.confidenceStats = confidenceStats;
      counters.rowsConfidenceStatsUpdated += 1;
    }
  }
  const backfilledAt = options.backfilledAt ?? new Date().toISOString();
  manifest.durationBackfill = {
    method: backfillMethod(),
    backfilledAt,
    localTranscriptDir: options.transcriptDir,
    ...counters,
    missingRecordingIds,
    fieldsUpdated: [
      "durationProcessedSeconds",
      "derivedFeatures.wordsPerMinute",
      "derivedFeatures.segmentDensityPerMinute",
      "confidenceStats (only when present in transcript JSON)",
    ],
    transcriptTextWrittenToManifest: false,
  };
  manifest.updatedAt = backfilledAt;
  await writeJson(backfillTargetFor(options), manifest);
  return manifest;
}

function buildSttManifest(indexManifest, results, options = {}) {
  const recordingCount = indexManifest.recordings?.length ?? 0;
  return {
    schemaVersion: MANIFEST_SCHEMA_VERSION,
    generatedAt: options.generatedAt ?? new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    sourceIndex: sourceIndexFor(indexManifest),
    releaseGateApproval: options.approval ?? null,
    readScope: readScope(),
    localTranscriptStore: {
      root: options.transcriptDir,
      gitignoredExpected: isGitignoredLocalPath(options.transcriptDir),
      containsTranscriptText: true,
      committedToRepo: false,
    },
    repoSafeManifest: {
      containsTranscriptText: false,
      containsAudioBytes: false,
      containsAudibleDerivedArtifacts: false,
    },
    resume: {
      enabled: true,
      key: "recordingId",
      outputWrittenAfterEachRecording: true,
      existingCompletedResultsSkipped: true,
      skippedCompletedCount: options.skippedCompleted ?? 0,
      completedCount: results.filter((row) => row.status === "completed").length,
      remainingCount: Math.max(recordingCount - results.filter((row) => row.status === "completed").length, 0),
      lastCompletedRecordingId: results.at(-1)?.recordingId ?? null,
    },
    toolChoices: [ffmpegToolChoice(options, options.toolVersions), sttToolChoice(options, options.toolVersions)],
    results,
  };
}

function buildTranscriptRow(entry, materialized, options = {}) {
  const normalized = normalizeTranscript(materialized.rawTranscript, options);
  return {
    recordingId: entry.recordingId,
    filename: entry.filename,
    capturedAt: entry.capturedAt ?? null,
    year: yearFor(entry),
    sourceRef: entry.sourceRef,
    status: "completed",
    language: normalized.language,
    durationProcessedSeconds: normalized.durationProcessedSeconds,
    confidenceStats: normalized.confidenceStats,
    counts: normalized.counts,
    derivedFeatures: normalized.derivedFeatures,
    localArtifacts: {
      transcriptTextPath: localPathRef(materialized.textPath),
      transcriptJsonPath: localPathRef(materialized.jsonPath),
      localOnly: true,
      gitignoredExpected: isGitignoredLocalPath(materialized.textPath) && isGitignoredLocalPath(materialized.jsonPath),
      textIncludedInManifest: false,
    },
    toolRun: {
      sttTool: "whisper.cpp",
      whisperBin: options.whisperBin,
      modelPath: options.model,
      audioPrep: "ffmpeg decode to 16 kHz mono wav in a temporary directory",
    },
  };
}

function normalizeTranscript(rawTranscript, options = {}) {
  const segments = segmentsFromTranscript(rawTranscript);
  const text = textFromTranscript(rawTranscript);
  const wordCount = wordCountFor(text);
  const durationProcessedSeconds = durationFromTranscript(rawTranscript, segments);
  return {
    language: {
      requested: options.language ?? "auto",
      detected: rawTranscript?.language ?? rawTranscript?.result?.language ?? null,
      probability: numberOrNull(rawTranscript?.languageProbability ?? rawTranscript?.result?.language_probability),
    },
    durationProcessedSeconds,
    confidenceStats: confidenceStatsFor(segments),
    counts: {
      words: wordCount,
      segments: segments.length,
      characters: text.length,
    },
    derivedFeatures: {
      wordsPerMinute: durationProcessedSeconds ? round(wordCount / (durationProcessedSeconds / 60), 2) : null,
      averageWordsPerSegment: segments.length ? round(wordCount / segments.length, 2) : null,
      segmentDensityPerMinute: durationProcessedSeconds ? round(segments.length / (durationProcessedSeconds / 60), 2) : null,
      hasTranscribedWords: wordCount > 0,
    },
  };
}

function sourceIndexFor(indexManifest) {
  return {
    schemaVersion: indexManifest.schemaVersion,
    generatedAt: indexManifest.generatedAt,
    recordingCount: indexManifest.totals?.recordings ?? indexManifest.recordings?.length ?? null,
  };
}

function readScope() {
  return {
    allIndexedRecordings: true,
    audioBytes: true,
    decodeToTemporaryWav: true,
    retainedAudioBytes: false,
    transcriptTextRetainedLocally: true,
    transcriptTextCommittedToRepo: false,
    upload: false,
    corpusMutation: false,
  };
}

function ffmpegToolChoice(options, toolVersions = {}) {
  return {
    name: "ffmpeg",
    version: toolVersions?.ffmpeg ?? null,
    stage: "decode-to-temporary-16khz-mono-wav",
    openness: "open-source",
    openSource: true,
    path: options.ffmpegBin,
    notes: "Host ffmpeg decodes approved local audio into a temporary wav that is deleted after each transcription.",
  };
}

function sttToolChoice(options, toolVersions = {}) {
  return {
    name: "whisper.cpp",
    version: toolVersions?.whisperCpp ?? null,
    stage: "local-speech-to-text",
    openness: "open-source",
    openSource: true,
    path: options.whisperBin,
    modelPath: options.model,
    modelFamily: "whisper.cpp ggml",
    language: options.language,
    metalAcceleration: "host Homebrew whisper.cpp uses Metal on supported macOS hardware",
    notes: "Transcript text is written only to the local transcript store; the repo manifest stores aggregate metadata and local path references only.",
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
        reject(new LocalSttError(`${message}: ${err || `exit code ${code}`}`));
        return;
      }
      resolve(out || err);
    });
  });
}

function segmentsFromTranscript(rawTranscript) {
  if (Array.isArray(rawTranscript?.segments)) return rawTranscript.segments;
  if (Array.isArray(rawTranscript?.transcription)) return rawTranscript.transcription;
  return [];
}

function textFromTranscript(rawTranscript) {
  if (typeof rawTranscript?.text === "string") return rawTranscript.text;
  return segmentsFromTranscript(rawTranscript).map((segment) => segment.text ?? "").join(" ").trim();
}

function durationFromTranscript(rawTranscript, segments) {
  const explicit = numberOrNull(rawTranscript?.durationSeconds ?? rawTranscript?.duration_seconds ?? rawTranscript?.duration);
  if (explicit !== null) return round(explicit, 3);
  const ends = segments.map((segment) => segmentEndSeconds(segment)).filter((value) => value !== null);
  return ends.length ? round(Math.max(...ends), 3) : null;
}

function segmentEndSeconds(segment) {
  const offsetMs = numberOrNull(segment.offsets?.to);
  if (offsetMs !== null) return offsetMs / 1000;
  return timestampSeconds(segment.end ?? segment.t1 ?? segment.timestamps?.to);
}

function confidenceStatsFor(segments) {
  return {
    meanLogProbability: meanOrNull(segments.map((segment) => numberOrNull(segment.avg_logprob ?? segment.meanLogProbability))),
    meanNoSpeechProbability: meanOrNull(segments.map((segment) => numberOrNull(segment.no_speech_prob ?? segment.noSpeechProbability))),
    meanCompressionRatio: meanOrNull(segments.map((segment) => numberOrNull(segment.compression_ratio ?? segment.compressionRatio))),
  };
}

function wordCountFor(text) {
  const trimmed = text.trim();
  return trimmed ? trimmed.split(/\s+/u).length : 0;
}

function timestampSeconds(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return null;
  const parts = value.split(":").map((part) => Number(part.replace(",", ".")));
  if (parts.some((part) => Number.isNaN(part))) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] ?? null;
}

function meanOrNull(values) {
  const numbers = values.filter((value) => value !== null);
  if (!numbers.length) return null;
  return round(numbers.reduce((sum, value) => sum + value, 0) / numbers.length, 6);
}

function numberOrNull(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function round(value, places = 3) {
  return Number.isFinite(value) ? Number(value.toFixed(places)) : null;
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
  if (options.command === "transcribe-index") {
    const index = await readJson(options.index);
    if (options.dryRun) {
      await writeJson("-", buildSttDryRun(index, options));
      return 0;
    }
    await transcribeIndexManifest(index, options);
    return 0;
  }
  if (options.command === "backfill-manifest") {
    const manifest = await readJson(options.manifest);
    if (options.dryRun) {
      await writeJson("-", buildBackfillDryRun(manifest, options));
      return 0;
    }
    await backfillManifest(manifest, options);
    return 0;
  }
  throw new LocalSttError(`unsupported command: ${options.command}`);
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
  backfillManifest,
  buildBackfillDryRun,
  buildSttDryRun,
  buildSttManifest,
  buildTranscriptRow,
  normalizeTranscript,
  run,
  transcribeIndexManifest,
};
