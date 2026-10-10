#!/usr/bin/env node
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { resolvePaths, resolveTools } from "./paths.mjs";

const SAMPLE_SCHEMA_VERSION = "voice-journey.filtering-sample.v1";
const RESULTS_SCHEMA_VERSION = "voice-journey.filter-results.v1";
const VJ = resolvePaths();
const TOOLS = resolveTools(process.env, VJ);
const DEFAULT_INDEX = VJ.manifest("recording-index.json");
const DEFAULT_SAMPLE = VJ.manifest("filtering-sample.json");
const DEFAULT_RESULTS = VJ.manifest("filter-results.json");
const DEFAULT_FULL_CORPUS_RESULTS = VJ.manifest("full-corpus-filter-results.json");
const DEFAULT_FFMPEG = TOOLS.ffmpegBin;
const SAMPLE_RATE = 16000;
const FULL_CORPUS_RESULTS_SCHEMA_VERSION = "voice-journey.full-corpus-filter-results.v1";

class FilterProofError extends Error {}

function parseArgs(argv) {
  const [command, ...rest] = argv;
  const options = {
    approval: null,
    command,
    corpusRoot: null,
    dryRun: false,
    ffmpegBin: DEFAULT_FFMPEG,
    generatedAt: null,
    index: DEFAULT_INDEX,
    max: 18,
    out: command === "analyze-index" ? DEFAULT_FULL_CORPUS_RESULTS : command === "analyze" ? DEFAULT_RESULTS : DEFAULT_SAMPLE,
    perYear: 2,
    sample: DEFAULT_SAMPLE,
  };

  const args = [...rest];
  while (args.length > 0) {
    const next = args.shift();
    if (next === "--approval") {
      options.approval = requireValue(args, next);
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
    } else if (next === "--max") {
      options.max = parseNonNegativeInteger(requireValue(args, next), next);
    } else if (next === "--out") {
      options.out = requireValue(args, next);
    } else if (next === "--per-year") {
      options.perYear = parseNonNegativeInteger(requireValue(args, next), next);
    } else if (next === "--sample") {
      options.sample = requireValue(args, next);
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new FilterProofError(`unsupported argument: ${next}`);
    }
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new FilterProofError(`${flag} requires a value`);
  return value;
}

function parseNonNegativeInteger(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) throw new FilterProofError(`${flag} must be a non-negative integer`);
  return parsed;
}

function printHelp() {
  process.stdout.write(`Voice Journey filtering proof tooling.

Usage:
  voice-journey-filter select-sample [--index PATH] [--out PATH] [--per-year N] [--max N]
  voice-journey-filter analyze --dry-run [--sample PATH] [--corpus-root PATH]
  voice-journey-filter analyze --sample PATH --corpus-root PATH --approval TEXT [--out PATH] [--ffmpeg-bin PATH]
  voice-journey-filter analyze-index --dry-run [--index PATH] [--corpus-root PATH]
  voice-journey-filter analyze-index --index PATH --corpus-root PATH --approval TEXT [--out PATH] [--ffmpeg-bin PATH]

select-sample and analyze-index --dry-run read only committed manifests. analyze
and analyze-index non-dry-run read audio bytes through the approved host
execution path and therefore require release-gate approval before use.
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

function selectSample(indexManifest, { generatedAt = new Date().toISOString(), max = 18, perYear = 2 } = {}) {
  generatedAt ??= new Date().toISOString();
  const byYear = new Map();
  for (const recording of indexManifest.recordings ?? []) {
    if (recording.source?.type === "measurement") continue;
    const year = yearFor(recording);
    if (!byYear.has(year)) byYear.set(year, []);
    byYear.get(year).push(recording);
  }

  const selected = [];
  const seen = new Set();
  for (const year of [...byYear.keys()].sort()) {
    const entries = byYear.get(year).slice().sort((left, right) => {
      return (left.file?.sizeBytes ?? 0) - (right.file?.sizeBytes ?? 0) || left.filename.localeCompare(right.filename);
    });
    for (const index of candidateIndexesFor(entries.length, perYear)) {
      const entry = entries[index];
      if (!entry || seen.has(entry.recordingId)) continue;
      seen.add(entry.recordingId);
      selected.push({ entry, reason: sampleReason(index, entries.length) });
    }
  }

  return {
    schemaVersion: SAMPLE_SCHEMA_VERSION,
    generatedAt,
    sourceIndex: {
      schemaVersion: indexManifest.schemaVersion,
      recordingCount: indexManifest.totals?.recordings ?? indexManifest.recordings?.length ?? null,
    },
    selectionStrategy: {
      kind: "metadata_only_year_size_stratified",
      perYear,
      max,
      inputs: ["capturedAt year", "file.sizeBytes", "filename"],
      audioBytesRead: false,
      notes: "For each year, select small/large size representatives from the committed index only.",
    },
    releaseGateRequiredForAnalysis: true,
    sample: selected.slice(0, max).map(({ entry, reason }, index) => ({
      sampleOrder: index + 1,
      recordingId: entry.recordingId,
      filename: entry.filename,
      capturedAt: entry.capturedAt,
      year: yearFor(entry),
      sizeBytes: entry.file?.sizeBytes ?? null,
      selectionReason: reason,
      sourceRef: entry.sourceRef,
      indexClassification: entry.contentClassification,
    })),
  };
}

function candidateIndexesFor(length, perYear) {
  if (length <= 0 || perYear <= 0) return [];
  if (perYear === 1 || length === 1) return [0];
  const last = length - 1;
  const indexes = new Set();
  for (let position = 0; position < perYear; position += 1) {
    indexes.add(Math.round((position * last) / (perYear - 1)));
  }
  return [...indexes].sort((left, right) => left - right);
}

function sampleReason(index, length) {
  if (length <= 1) return "only_recording_for_year";
  if (index === 0) return "small_size_representative_for_year";
  if (index === length - 1) return "large_size_representative_for_year";
  return "mid_size_representative_for_year";
}

function analyzeDryRun(sampleManifest, options) {
  return {
    dryRun: true,
    operation: "filtering-proof-sample-analysis",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["recording-index-manifest", "index-feeds-filtering", "host-access-seam", "seam-provides-filter-audio"],
      writes: ["filter-results-manifest", "filtering-writes-results"],
    },
    corpusRoot: options.corpusRoot,
    sampleCount: sampleManifest.sample?.length ?? 0,
    ffmpegBin: options.ffmpegBin,
    outputPath: options.out,
    readScope: {
      sampleRecordingsOnly: true,
      audioBytes: true,
      decodeToMonoPcm: true,
      retainedAudioBytes: false,
      waveformImages: false,
      spectrogramImages: false,
      transcription: false,
      upload: false,
      corpusMutation: false,
    },
    extractedFeatures: [
      "durationSeconds",
      "activeRatio",
      "voicedRatio",
      "meanRmsDb",
      "meanZeroCrossingRate",
      "meanPitchHz",
      "meanPitchConfidence",
      "pitchStability",
      "clippingRatio",
    ],
    sample: sampleManifest.sample?.map((entry) => ({
      recordingId: entry.recordingId,
      filename: entry.filename,
      year: entry.year,
      sizeBytes: entry.sizeBytes,
    })) ?? [],
  };
}

function analyzeIndexDryRun(indexManifest, options) {
  return {
    dryRun: true,
    operation: "full-corpus-classification",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["recording-index-manifest", "index-feeds-filtering", "host-access-seam", "seam-provides-filter-audio"],
      writes: ["filter-results-manifest", "filtering-writes-results"],
    },
    corpusRoot: options.corpusRoot,
    recordingCount: indexManifest.recordings?.length ?? 0,
    sourceIndex: {
      schemaVersion: indexManifest.schemaVersion,
      generatedAt: indexManifest.generatedAt,
      recordingCount: indexManifest.totals?.recordings ?? indexManifest.recordings?.length ?? null,
    },
    ffmpegBin: options.ffmpegBin,
    outputPath: options.out,
    expectedRuntime: "approximately one hour for 2,433 recordings on the current host inventory",
    resumeBehavior: {
      enabled: true,
      key: "recordingId",
      outputWrittenAfterEachRecording: true,
      existingResultsSkipped: true,
      rerunCommandIsSameAsInitialCommand: true,
    },
    readScope: {
      allIndexedRecordings: true,
      audioBytes: true,
      decodeToMonoPcm: true,
      retainedAudioBytes: false,
      waveformImages: false,
      spectrogramImages: false,
      transcription: false,
      upload: false,
      corpusMutation: false,
    },
    extractedFeatures: extractedFeatureNames(),
  };
}

async function analyzeSample(sampleManifest, options) {
  if (!options.approval) throw new FilterProofError("analyze requires --approval for non-dry-run audio reads");
  if (!options.corpusRoot) throw new FilterProofError("analyze requires --corpus-root");
  const toolVersions = { ffmpeg: await readToolVersion(options.ffmpegBin, ["-version"]) };
  const results = [];
  for (const entry of sampleManifest.sample ?? []) {
    const audioPath = path.join(options.corpusRoot, entry.filename);
    const pcm = await decodePcmWithFfmpeg(audioPath, options.ffmpegBin);
    const features = extractFeaturesFromPcm(pcm, SAMPLE_RATE);
    const classification = classifyFeatures(features);
    results.push({
      recordingId: entry.recordingId,
      filename: entry.filename,
      year: entry.year,
      sourceRef: entry.sourceRef,
      features,
      classification,
      humanSpotCheck: {
        recommended: classification.finalBucket === "uncertain_manual_review" || classification.confidence < 0.7,
        status: "not_reviewed",
        notes: null,
      },
    });
  }
  return buildResultsManifest(sampleManifest, results, { ...options, toolVersions });
}

async function analyzeIndexManifest(indexManifest, options, hooks = {}) {
  if (!options.approval) throw new FilterProofError("analyze-index requires --approval for non-dry-run audio reads");
  if (!options.corpusRoot) throw new FilterProofError("analyze-index requires --corpus-root");
  if (options.out === "-") throw new FilterProofError("analyze-index requires a file output for incremental resume support");

  const existing = await readExistingFullCorpusManifest(options.out);
  const existingById = new Map((existing?.results ?? []).map((row) => [row.recordingId, row]));
  const toolVersions = hooks.toolVersions ?? { ffmpeg: await readToolVersion(options.ffmpegBin, ["-version"]) };
  const manifest = buildFullCorpusResultsManifest(indexManifest, [...existingById.values()], {
    ...options,
    toolVersions,
    skippedCompleted: existingById.size,
  });
  await writeJson(options.out, manifest);

  const analyzeRecording = hooks.analyzeRecording ?? analyzeRecordingEntry;
  for (const entry of indexManifest.recordings ?? []) {
    if (existingById.has(entry.recordingId)) continue;
    // Measurement-chain takes are not Voice Memos: the singing filter does not apply (Phase B).
    if (entry.source?.type === "measurement") continue;
    const row = await analyzeRecording(entry, options);
    manifest.results.push(row);
    manifest.resume.completedCount = manifest.results.length;
    manifest.resume.remainingCount = Math.max((indexManifest.recordings?.length ?? 0) - manifest.results.length, 0);
    manifest.resume.lastCompletedRecordingId = row.recordingId;
    manifest.updatedAt = new Date().toISOString();
    await writeJson(options.out, manifest);
  }
  return manifest;
}

async function analyzeRecordingEntry(entry, options) {
  const audioPath = path.join(options.corpusRoot, entry.filename);
  const pcm = await decodePcmWithFfmpeg(audioPath, options.ffmpegBin);
  const features = extractFeaturesFromPcm(pcm, SAMPLE_RATE);
  return buildResultRow(entry, features);
}

function buildResultRow(entry, features) {
  const classification = classifyFeatures(features);
  return {
    recordingId: entry.recordingId,
    filename: entry.filename,
    capturedAt: entry.capturedAt ?? null,
    year: yearFor(entry),
    sourceRef: entry.sourceRef,
    features,
    classification,
    humanSpotCheck: {
      recommended: classification.finalBucket === "uncertain_manual_review" || classification.confidence < 0.7,
      status: "not_reviewed",
      reviewedBy: null,
      notes: null,
    },
  };
}

function buildResultsManifest(sampleManifest, results, options = {}) {
  return {
    schemaVersion: RESULTS_SCHEMA_VERSION,
    generatedAt: options.generatedAt ?? new Date().toISOString(),
    sourceSample: {
      schemaVersion: sampleManifest.schemaVersion,
      generatedAt: sampleManifest.generatedAt,
      sampleCount: sampleManifest.sample?.length ?? 0,
    },
    releaseGateApproval: options.approval ?? null,
    readScope: {
      sampleRecordingsOnly: true,
      audioBytes: true,
      retainedAudioBytes: false,
      audibleDerivedArtifacts: false,
      upload: false,
      corpusMutation: false,
    },
    toolChoices: [
      {
        name: "ffmpeg",
        version: options.toolVersions?.ffmpeg ?? null,
        stage: "decode-to-mono-pcm-feature-input",
        openness: "open-source",
        openSource: true,
        openSwapCandidate: null,
        notes: "Host-provided ffmpeg decodes sample audio to transient PCM for aggregate feature extraction only.",
      },
      {
        name: "Node.js standard library DSP heuristics",
        version: process.version,
        stage: "feature-extraction-and-classification-proof",
        openness: "open-source-runtime-project-code",
        openSource: true,
        openSwapCandidate: "Essentia, librosa, or pyAudioAnalysis local classifier if the proof needs stronger accuracy.",
        notes: "No Python stack or closed-source analyzer is required for this sample proof implementation.",
      },
      {
        name: "afinfo/afconvert",
        version: null,
        stage: "not-used-fallback",
        openness: "closed-source-macos-coreaudio",
        openSource: false,
        openSwapCandidate: "ffmpeg/ffprobe",
        notes: "Available on host but intentionally not used in the preferred open-source path.",
      },
    ],
    buckets: {
      content: ["singing", "non_singing", "uncertain"],
      contamination: ["clean", "noise_contaminated", "music_contaminated", "not_applicable", "uncertain"],
      final: ["non_singing", "clean_singing", "noise_contaminated_singing", "music_contaminated_singing", "uncertain_manual_review"],
    },
    results,
  };
}

function buildFullCorpusResultsManifest(indexManifest, results, options = {}) {
  const recordingCount = indexManifest.recordings?.length ?? 0;
  return {
    schemaVersion: FULL_CORPUS_RESULTS_SCHEMA_VERSION,
    generatedAt: options.generatedAt ?? new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    sourceIndex: {
      schemaVersion: indexManifest.schemaVersion,
      generatedAt: indexManifest.generatedAt,
      recordingCount: indexManifest.totals?.recordings ?? recordingCount,
    },
    releaseGateApproval: options.approval ?? null,
    readScope: {
      allIndexedRecordings: true,
      audioBytes: true,
      retainedAudioBytes: false,
      audibleDerivedArtifacts: false,
      upload: false,
      corpusMutation: false,
    },
    resume: {
      enabled: true,
      key: "recordingId",
      outputWrittenAfterEachRecording: true,
      existingResultsSkipped: true,
      skippedCompletedCount: options.skippedCompleted ?? 0,
      completedCount: results.length,
      remainingCount: Math.max(recordingCount - results.length, 0),
      lastCompletedRecordingId: results.at(-1)?.recordingId ?? null,
    },
    toolChoices: toolChoicesFor(options.toolVersions),
    buckets: classificationBuckets(),
    results,
  };
}

async function readExistingFullCorpusManifest(filePath) {
  try {
    return JSON.parse(await readFile(filePath, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

function toolChoicesFor(toolVersions = {}) {
  return [
    {
      name: "ffmpeg",
      version: toolVersions.ffmpeg ?? null,
      stage: "decode-to-mono-pcm-feature-input",
      openness: "open-source",
      openSource: true,
      openSwapCandidate: null,
      notes: "Host-provided ffmpeg decodes approved local audio to transient PCM for aggregate feature extraction only.",
    },
    {
      name: "Node.js standard library DSP heuristics",
      version: process.version,
      stage: "feature-extraction-and-classification-proof",
      openness: "open-source-runtime-project-code",
      openSource: true,
      openSwapCandidate: "Essentia, librosa, or pyAudioAnalysis local classifier if the proof needs stronger accuracy.",
      notes: "No Python stack or closed-source analyzer is required for this classifier implementation.",
    },
    {
      name: "afinfo/afconvert",
      version: null,
      stage: "not-used-fallback",
      openness: "closed-source-macos-coreaudio",
      openSource: false,
      openSwapCandidate: "ffmpeg/ffprobe",
      notes: "Available on host but intentionally not used in the preferred open-source path.",
    },
  ];
}

function classificationBuckets() {
  return {
    content: ["singing", "non_singing", "uncertain"],
    contamination: ["clean", "noise_contaminated", "music_contaminated", "not_applicable", "uncertain"],
    final: ["non_singing", "clean_singing", "noise_contaminated_singing", "music_contaminated_singing", "uncertain_manual_review"],
  };
}

function extractedFeatureNames() {
  return [
    "durationSeconds",
    "activeRatio",
    "voicedRatio",
    "meanRmsDb",
    "meanZeroCrossingRate",
    "meanPitchHz",
    "meanPitchConfidence",
    "pitchStability",
    "clippingRatio",
    "frameCount",
  ];
}

async function readToolVersion(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new FilterProofError(`${path.basename(command)} version probe failed: ${Buffer.concat(stderr).toString("utf8").trim()}`));
        return;
      }
      resolve(Buffer.concat(stdout).toString("utf8").split("\n")[0] ?? null);
    });
  });
}

async function decodePcmWithFfmpeg(audioPath, ffmpegBin) {
  return new Promise((resolve, reject) => {
    const child = spawn(ffmpegBin, [
      "-hide_banner",
      "-nostdin",
      "-v",
      "error",
      "-i",
      audioPath,
      "-ac",
      "1",
      "-ar",
      String(SAMPLE_RATE),
      "-f",
      "s16le",
      "pipe:1",
    ]);
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new FilterProofError(`ffmpeg failed for ${path.basename(audioPath)}: ${Buffer.concat(stderr).toString("utf8").trim()}`));
        return;
      }
      resolve(Buffer.concat(stdout));
    });
  });
}

function extractFeaturesFromPcm(buffer, sampleRate = SAMPLE_RATE) {
  const sampleCount = Math.floor(buffer.byteLength / 2);
  const frameSize = 1024;
  const hopSize = 512;
  const samples = new Int16Array(sampleCount);
  let clipped = 0;
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = buffer.readInt16LE(index * 2);
    samples[index] = sample;
    if (Math.abs(sample) >= 32760) clipped += 1;
  }

  const frames = [];
  for (let offset = 0; offset + frameSize <= samples.length; offset += hopSize) {
    frames.push(analyzeFrame(samples.subarray(offset, offset + frameSize), sampleRate));
  }

  const activeFrames = frames.filter((frame) => frame.rms > 0.01);
  const voicedFrames = activeFrames.filter((frame) => frame.pitchConfidence >= 0.45);
  const pitchValues = voicedFrames.map((frame) => frame.pitchHz).filter(Boolean);
  return {
    durationSeconds: round(sampleCount / sampleRate, 3),
    activeRatio: ratio(activeFrames.length, frames.length),
    voicedRatio: ratio(voicedFrames.length, frames.length),
    meanRmsDb: round(mean(activeFrames.map((frame) => 20 * Math.log10(frame.rms || 1e-9))), 2),
    meanZeroCrossingRate: round(mean(activeFrames.map((frame) => frame.zeroCrossingRate)), 4),
    meanPitchHz: pitchValues.length ? round(mean(pitchValues), 1) : null,
    meanPitchConfidence: round(mean(voicedFrames.map((frame) => frame.pitchConfidence)), 3),
    pitchStability: pitchValues.length > 1 ? round(stddev(pitchValues) / Math.max(mean(pitchValues), 1), 3) : null,
    clippingRatio: round(ratio(clipped, sampleCount), 6),
    frameCount: frames.length,
  };
}

function analyzeFrame(frame, sampleRate) {
  let sumSquares = 0;
  let zeroCrossings = 0;
  for (let index = 0; index < frame.length; index += 1) {
    const value = frame[index] / 32768;
    sumSquares += value * value;
    if (index > 0 && Math.sign(frame[index - 1]) !== Math.sign(frame[index])) zeroCrossings += 1;
  }
  const pitch = estimatePitch(frame, sampleRate);
  return {
    rms: Math.sqrt(sumSquares / frame.length),
    zeroCrossingRate: zeroCrossings / frame.length,
    pitchHz: pitch.pitchHz,
    pitchConfidence: pitch.confidence,
  };
}

function estimatePitch(frame, sampleRate) {
  const minLag = Math.floor(sampleRate / 400);
  const maxLag = Math.floor(sampleRate / 80);
  let bestLag = 0;
  let bestCorrelation = 0;
  let energy = 0;
  for (const sample of frame) energy += sample * sample;
  if (energy === 0) return { pitchHz: null, confidence: 0 };
  for (let lag = minLag; lag <= maxLag; lag += 1) {
    let correlation = 0;
    for (let index = 0; index < frame.length - lag; index += 1) {
      correlation += frame[index] * frame[index + lag];
    }
    const normalized = correlation / energy;
    if (normalized > bestCorrelation) {
      bestCorrelation = normalized;
      bestLag = lag;
    }
  }
  return {
    pitchHz: bestLag ? sampleRate / bestLag : null,
    confidence: Math.max(0, Math.min(1, bestCorrelation)),
  };
}

function classifyFeatures(features) {
  let contentLabel = "uncertain";
  let contentConfidence = 0.45;
  const pitchStability = features.pitchStability ?? 1;
  if (features.activeRatio < 0.04) {
    contentLabel = "non_singing";
    contentConfidence = 0.7;
  } else if (features.voicedRatio >= 0.42 && features.meanPitchConfidence >= 0.45 && pitchStability <= 0.38) {
    contentLabel = "singing";
    contentConfidence = clamp(0.55 + features.voicedRatio * 0.25 + features.meanPitchConfidence * 0.2 - pitchStability * 0.15);
  } else if (features.voicedRatio < 0.22 || features.meanPitchConfidence < 0.25) {
    contentLabel = "non_singing";
    contentConfidence = clamp(0.5 + (0.25 - features.meanPitchConfidence) + (0.25 - features.voicedRatio));
  }

  let contaminationLabel = "not_applicable";
  let contaminationConfidence = contentLabel === "non_singing" ? contentConfidence : 0.4;
  if (contentLabel === "singing") {
    const noiseScore = clamp((features.meanZeroCrossingRate - 0.12) * 3 + (1 - features.meanPitchConfidence) * 0.4 + features.clippingRatio * 10);
    const musicScore = clamp((features.activeRatio - 0.78) * 1.1 + (features.voicedRatio - 0.65) * 0.9 + (pitchStability < 0.08 ? 0.25 : 0));
    if (musicScore >= 0.62) {
      contaminationLabel = "music_contaminated";
      contaminationConfidence = musicScore;
    } else if (noiseScore >= 0.55) {
      contaminationLabel = "noise_contaminated";
      contaminationConfidence = noiseScore;
    } else if (features.meanPitchConfidence >= 0.55 && features.meanZeroCrossingRate <= 0.14) {
      contaminationLabel = "clean";
      contaminationConfidence = clamp(0.55 + features.meanPitchConfidence * 0.3 - features.meanZeroCrossingRate);
    } else {
      contaminationLabel = "uncertain";
      contaminationConfidence = 0.45;
    }
  } else if (contentLabel === "uncertain") {
    contaminationLabel = "uncertain";
  }

  return {
    method: "voice-journey.local-heuristic-proof.v1",
    contentLabel,
    contaminationLabel,
    finalBucket: finalBucketFor(contentLabel, contaminationLabel),
    confidence: round(Math.min(contentConfidence, contaminationConfidence === 0 ? contentConfidence : contaminationConfidence), 3),
    rationale: rationaleFor(contentLabel, contaminationLabel, features),
  };
}

function finalBucketFor(contentLabel, contaminationLabel) {
  if (contentLabel === "non_singing") return "non_singing";
  if (contentLabel !== "singing") return "uncertain_manual_review";
  if (contaminationLabel === "clean") return "clean_singing";
  if (contaminationLabel === "noise_contaminated") return "noise_contaminated_singing";
  if (contaminationLabel === "music_contaminated") return "music_contaminated_singing";
  return "uncertain_manual_review";
}

function rationaleFor(contentLabel, contaminationLabel, features) {
  return `content=${contentLabel}; contamination=${contaminationLabel}; voicedRatio=${features.voicedRatio}; pitchConfidence=${features.meanPitchConfidence}; zcr=${features.meanZeroCrossingRate}; activeRatio=${features.activeRatio}`;
}

function mean(values) {
  if (!values.length) return 0;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function stddev(values) {
  if (values.length < 2) return 0;
  const average = mean(values);
  return Math.sqrt(mean(values.map((value) => (value - average) ** 2)));
}

function ratio(numerator, denominator) {
  return denominator ? numerator / denominator : 0;
}

function round(value, places = 3) {
  return Number.isFinite(value) ? Number(value.toFixed(places)) : null;
}

function clamp(value) {
  return Math.max(0, Math.min(1, value));
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (!options.command || options.help) {
    printHelp();
    return 0;
  }
  if (options.command === "select-sample") {
    const index = await readJson(options.index);
    const sample = selectSample(index, options);
    await writeJson(options.out, sample);
    return 0;
  }
  if (options.command === "analyze") {
    const sample = await readJson(options.sample);
    if (options.dryRun) {
      await writeJson("-", analyzeDryRun(sample, options));
      return 0;
    }
    const results = await analyzeSample(sample, options);
    await writeJson(options.out, results);
    return 0;
  }
  if (options.command === "analyze-index") {
    const index = await readJson(options.index);
    if (options.dryRun) {
      await writeJson("-", analyzeIndexDryRun(index, options));
      return 0;
    }
    await analyzeIndexManifest(index, options);
    return 0;
  }
  throw new FilterProofError(`unsupported command: ${options.command}`);
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
  analyzeDryRun,
  analyzeIndexDryRun,
  analyzeIndexManifest,
  buildFullCorpusResultsManifest,
  buildResultsManifest,
  classifyFeatures,
  extractFeaturesFromPcm,
  selectSample,
  run,
};
