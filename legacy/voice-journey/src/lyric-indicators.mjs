#!/usr/bin/env node
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { resolvePaths } from "./paths.mjs";

const SCHEMA_VERSION = "voice-journey.local-lyric-indicators.v1";
const VJ = resolvePaths();
const DEFAULT_TRANSCRIPTS = VJ.manifest("local-stt-transcripts.json");
const DEFAULT_OUT = VJ.manifest("local-lyric-indicators.json");
const DEFAULT_TRANSCRIPT_DIR = VJ.artifact("stt-transcripts");
const MIN_SHINGLE_WORDS = 5;
const DEFAULT_CLUSTER_THRESHOLD = 0.22;
const DEFAULT_MIN_CLUSTER_SIZE = 2;
const WORDLESS_LANGUAGE_EXCLUSIONS = new Set(["en", "unknown", "und", null]);

class LyricIndicatorError extends Error {}

function parseArgs(argv) {
  const [maybeCommand, ...rest] = argv;
  const hasCommand = maybeCommand && !maybeCommand.startsWith("--") && maybeCommand !== "help";
  const command = hasCommand ? maybeCommand : "analyze";
  const args = hasCommand ? rest : argv;
  const options = {
    command,
    clusterThreshold: DEFAULT_CLUSTER_THRESHOLD,
    dryRun: false,
    generatedAt: null,
    minClusterSize: DEFAULT_MIN_CLUSTER_SIZE,
    out: DEFAULT_OUT,
    transcripts: DEFAULT_TRANSCRIPTS,
    transcriptDir: DEFAULT_TRANSCRIPT_DIR,
  };

  while (args.length > 0) {
    const next = args.shift();
    if (next === "--cluster-threshold") {
      options.clusterThreshold = parseRatio(requireValue(args, next), next);
    } else if (next === "--dry-run") {
      options.dryRun = true;
    } else if (next === "--generated-at") {
      options.generatedAt = requireValue(args, next);
    } else if (next === "--min-cluster-size") {
      options.minClusterSize = parsePositiveInteger(requireValue(args, next), next);
    } else if (next === "--out") {
      options.out = requireValue(args, next);
    } else if (next === "--transcripts") {
      options.transcripts = requireValue(args, next);
    } else if (next === "--transcript-dir") {
      options.transcriptDir = requireValue(args, next);
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new LyricIndicatorError(`unsupported argument: ${next}`);
    }
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new LyricIndicatorError(`${flag} requires a value`);
  return value;
}

function parsePositiveInteger(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) throw new LyricIndicatorError(`${flag} must be a positive integer`);
  return parsed;
}

function parseRatio(value, flag) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 1) throw new LyricIndicatorError(`${flag} must be a ratio in (0, 1]`);
  return parsed;
}

function printHelp() {
  process.stdout.write(`Voice Journey local lyric indicators.

Usage:
  voice-journey-lyric analyze --dry-run [--transcripts PATH] [--out PATH]
  voice-journey-lyric analyze --transcripts PATH --transcript-dir PATH [--out PATH] [--cluster-threshold RATIO] [--min-cluster-size N]

analyze reads local gitignored transcript text, computes local-first lyric
indicator marks, and writes a repo-safe manifest with no raw lyric text.
No external lyric databases or network services are used.
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

function buildDryRun(transcriptManifest, options) {
  return {
    dryRun: true,
    operation: "local-lyric-indicators",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["transcript-manifest", "local-transcript-store"],
      writes: ["transcript-manifest"],
    },
    sourceTranscripts: sourceTranscriptsFor(transcriptManifest),
    recordingCount: transcriptManifest.results?.length ?? 0,
    transcriptDir: options.transcriptDir,
    outputPath: options.out,
    strategy: strategy(options),
    readScope: {
      committedTranscriptManifest: true,
      localTranscriptText: true,
      localTranscriptTextReadInDryRun: false,
      transcriptTextCommittedToRepo: false,
      audioBytes: false,
      upload: false,
      externalLyricDatabases: false,
      corpusMutation: false,
    },
  };
}

async function analyzeLyricIndicators(transcriptManifest, options, hooks = {}) {
  const rows = transcriptManifest.results ?? [];
  const readTranscript = hooks.readTranscript ?? readTranscriptForRow;
  const analyzed = [];
  for (const row of rows) {
    const text = row.status === "completed" ? await readTranscript(row, options) : "";
    analyzed.push(analyzeTranscriptRow(row, text));
  }
  return buildLyricManifest(transcriptManifest, analyzed, options);
}

async function readTranscriptForRow(row, options) {
  const filePath = resolveTranscriptPath(row, options);
  try {
    return await readFile(filePath, "utf8");
  } catch (error) {
    throw new LyricIndicatorError(`failed to read local transcript for ${row.recordingId}: ${error.message}`);
  }
}

function resolveTranscriptPath(row, options) {
  if (row.localArtifacts?.transcriptTextPath) {
    const basename = path.basename(row.localArtifacts.transcriptTextPath);
    return path.join(options.transcriptDir, basename);
  }
  return path.join(options.transcriptDir, `${row.recordingId}.txt`);
}

function analyzeTranscriptRow(row, text) {
  const tokens = tokenize(text);
  const shingles = shingleHashes(tokens, MIN_SHINGLE_WORDS);
  const repeated = repetitionSignal(tokens);
  const wordless = wordlessSignal(row);
  return {
    row,
    tokens,
    shingles,
    repeated,
    wordless,
    charCount: text.length,
  };
}

function buildLyricManifest(transcriptManifest, analyzed, options = {}) {
  const clusters = clusterTranscripts(analyzed, options);
  const clusterById = new Map();
  for (const cluster of clusters) {
    for (const recordingId of cluster.recordingIds) clusterById.set(recordingId, cluster);
  }
  const results = analyzed.map((entry) => resultFor(entry, clusterById.get(entry.row.recordingId) ?? null));
  return {
    schemaVersion: SCHEMA_VERSION,
    generatedAt: options.generatedAt ?? new Date().toISOString(),
    sourceTranscripts: sourceTranscriptsFor(transcriptManifest),
    readScope: {
      transcriptManifest: true,
      localTranscriptText: true,
      transcriptTextCommittedToRepo: false,
      rawLyricTextInManifest: false,
      audioBytes: false,
      upload: false,
      externalLyricDatabases: false,
      corpusMutation: false,
    },
    strategy: strategy(options),
    privacy: {
      containsRawLyricText: false,
      containsTranscriptText: false,
      clusterLabelsAreNonIdentifying: true,
    },
    totals: summarize(results, clusters),
    clusters,
    results,
  };
}

function resultFor(entry, cluster) {
  const signals = {
    crossRecordingCluster: cluster ? {
      marked: true,
      clusterId: cluster.clusterId,
      clusterLabel: cluster.label,
      clusterSize: cluster.recordingIds.length,
      confidence: cluster.confidence,
    } : { marked: false, clusterId: null, clusterLabel: null, clusterSize: 0, confidence: 0 },
    repetitionStructure: entry.repeated,
    wordlessVocalise: entry.wordless,
  };
  const methods = [];
  if (signals.crossRecordingCluster.marked) methods.push("cross_recording_similarity");
  if (signals.repetitionStructure.marked) methods.push("repetition_structure");
  if (signals.wordlessVocalise.marked) methods.push("wordless_vocalise_signal");
  const confidence = Math.max(signals.crossRecordingCluster.confidence, signals.repetitionStructure.confidence, signals.wordlessVocalise.confidence);
  return {
    recordingId: entry.row.recordingId,
    filename: entry.row.filename,
    capturedAt: entry.row.capturedAt ?? null,
    year: entry.row.year ?? yearFor(entry.row.capturedAt),
    lyricMatch: {
      marked: methods.length > 0,
      status: methods.length > 0 ? "marked_indicator" : "not_marked",
      method: methods.join("+") || "none",
      confidence: round(confidence),
      matchedCluster: cluster?.clusterId ?? null,
      matchedClusterLabel: cluster?.label ?? null,
      rationaleCodes: rationaleCodesFor(signals),
      signals,
    },
  };
}

function clusterTranscripts(analyzed, options = {}) {
  const candidates = analyzed.filter((entry) => entry.shingles.size > 0);
  const union = new UnionFind(candidates.map((entry) => entry.row.recordingId));
  const edges = [];
  for (let leftIndex = 0; leftIndex < candidates.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < candidates.length; rightIndex += 1) {
      const left = candidates[leftIndex];
      const right = candidates[rightIndex];
      const similarity = jaccard(left.shingles, right.shingles);
      if (similarity >= (options.clusterThreshold ?? DEFAULT_CLUSTER_THRESHOLD)) {
        union.union(left.row.recordingId, right.row.recordingId);
        edges.push([left.row.recordingId, right.row.recordingId, similarity]);
      }
    }
  }

  const groups = new Map();
  for (const entry of candidates) {
    const root = union.find(entry.row.recordingId);
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root).push(entry);
  }

  const clusters = [];
  for (const entries of groups.values()) {
    if (entries.length < (options.minClusterSize ?? DEFAULT_MIN_CLUSTER_SIZE)) continue;
    const ids = entries.map((entry) => entry.row.recordingId).sort();
    const relatedEdges = edges.filter(([left, right]) => ids.includes(left) && ids.includes(right));
    const confidence = relatedEdges.length ? Math.max(...relatedEdges.map((edge) => edge[2])) : DEFAULT_CLUSTER_THRESHOLD;
    clusters.push({
      seed: ids[0],
      ids,
      years: uniqueSorted(entries.map((entry) => entry.row.year ?? yearFor(entry.row.capturedAt))),
      confidence: round(Math.min(0.99, 0.45 + confidence * 0.7 + Math.min(entries.length, 8) * 0.025)),
    });
  }

  clusters.sort((left, right) => right.ids.length - left.ids.length || left.seed.localeCompare(right.seed));
  return clusters.map((cluster, index) => ({
    clusterId: `lyric-cluster-${String(index + 1).padStart(3, "0")}`,
    label: `recurring_lyric_cluster_${String(index + 1).padStart(3, "0")}`,
    method: "cross_recording_shingle_similarity.v1",
    recordingIds: cluster.ids,
    years: cluster.years,
    size: cluster.ids.length,
    confidence: cluster.confidence,
    containsRawLyricText: false,
  }));
}

function repetitionSignal(tokens) {
  if (tokens.length < 12) return { marked: false, method: "repeated_ngram_density.v1", confidence: 0, repeatedNgramCount: 0 };
  const counts = new Map();
  for (const gram of ngrams(tokens, 4)) counts.set(gram, (counts.get(gram) ?? 0) + 1);
  const repeated = [...counts.values()].filter((count) => count >= 2).length;
  const density = repeated / Math.max(counts.size, 1);
  return {
    marked: repeated >= 2 || density >= 0.12,
    method: "repeated_ngram_density.v1",
    confidence: round(Math.min(0.9, 0.35 + repeated * 0.08 + density)),
    repeatedNgramCount: repeated,
  };
}

function wordlessSignal(row) {
  const words = row.counts?.words ?? 0;
  const language = row.language?.detected ?? "unknown";
  const nonEnglish = !WORDLESS_LANGUAGE_EXCLUSIONS.has(language);
  const marked = words === 0 || nonEnglish;
  return {
    marked,
    method: "zero_word_or_non_en_stt_language.v1",
    confidence: marked ? (words === 0 ? 0.8 : 0.62) : 0,
    wordCount: words,
    detectedLanguage: language,
  };
}

function rationaleCodesFor(signals) {
  const codes = [];
  if (signals.crossRecordingCluster.marked) codes.push("recurs_across_recordings");
  if (signals.repetitionStructure.marked) codes.push("repetition_structure_detected");
  if (signals.wordlessVocalise.wordCount === 0) codes.push("zero_word_transcript");
  if (signals.wordlessVocalise.marked && signals.wordlessVocalise.wordCount > 0) codes.push("non_en_language_candidate_vocalise");
  return codes;
}

function summarize(results, clusters) {
  const methodCounts = {};
  let marked = 0;
  let wordlessVocalise = 0;
  let repetition = 0;
  let crossRecording = 0;
  for (const result of results) {
    if (result.lyricMatch.marked) marked += 1;
    for (const method of result.lyricMatch.method.split("+").filter((method) => method && method !== "none")) {
      methodCounts[method] = (methodCounts[method] ?? 0) + 1;
    }
    if (result.lyricMatch.signals.wordlessVocalise.marked) wordlessVocalise += 1;
    if (result.lyricMatch.signals.repetitionStructure.marked) repetition += 1;
    if (result.lyricMatch.signals.crossRecordingCluster.marked) crossRecording += 1;
  }
  return {
    recordings: results.length,
    marked,
    notMarked: results.length - marked,
    clusters: clusters.length,
    crossRecording,
    repetition,
    wordlessVocalise,
    methodCounts,
  };
}

function sourceTranscriptsFor(manifest) {
  return {
    schemaVersion: manifest.schemaVersion,
    generatedAt: manifest.generatedAt,
    updatedAt: manifest.updatedAt,
    recordingCount: manifest.results?.length ?? manifest.sourceIndex?.recordingCount ?? null,
    localTranscriptStore: manifest.localTranscriptStore?.root ?? null,
  };
}

function strategy(options = {}) {
  return {
    methods: [
      "cross_recording_shingle_similarity.v1",
      "repeated_ngram_density.v1",
      "zero_word_or_non_en_stt_language.v1",
    ],
    clusterThreshold: options.clusterThreshold ?? DEFAULT_CLUSTER_THRESHOLD,
    minClusterSize: options.minClusterSize ?? DEFAULT_MIN_CLUSTER_SIZE,
    externalLyricDatabases: false,
    rawLyricTextInOutput: false,
  };
}

function tokenize(text) {
  return text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/gu, "")
    .replace(/[^a-z0-9'\s]/gu, " ")
    .split(/\s+/u)
    .map((token) => token.replace(/^'+|'+$/gu, ""))
    .filter((token) => token.length > 1);
}

function shingleHashes(tokens, size) {
  return new Set(ngrams(tokens, size).map((gram) => createHash("sha256").update(gram).digest("hex").slice(0, 16)));
}

function ngrams(tokens, size) {
  const grams = [];
  for (let index = 0; index + size <= tokens.length; index += 1) grams.push(tokens.slice(index, index + size).join(" "));
  return grams;
}

function jaccard(left, right) {
  if (!left.size || !right.size) return 0;
  let intersection = 0;
  for (const value of left) if (right.has(value)) intersection += 1;
  return intersection / (left.size + right.size - intersection);
}

function yearFor(capturedAt) {
  return capturedAt ? capturedAt.slice(0, 4) : "unknown";
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort();
}

function round(value, places = 3) {
  return Number.isFinite(value) ? Number(value.toFixed(places)) : null;
}

class UnionFind {
  constructor(values) {
    this.parents = new Map(values.map((value) => [value, value]));
  }

  find(value) {
    const parent = this.parents.get(value) ?? value;
    if (parent === value) return value;
    const root = this.find(parent);
    this.parents.set(value, root);
    return root;
  }

  union(left, right) {
    const leftRoot = this.find(left);
    const rightRoot = this.find(right);
    if (leftRoot !== rightRoot) this.parents.set(rightRoot, leftRoot);
  }
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (!options.command || options.help) {
    printHelp();
    return 0;
  }
  if (options.command === "analyze") {
    const transcriptManifest = await readJson(options.transcripts);
    if (options.dryRun) {
      await writeJson("-", buildDryRun(transcriptManifest, options));
      return 0;
    }
    const manifest = await analyzeLyricIndicators(transcriptManifest, options);
    await writeJson(options.out, manifest);
    return 0;
  }
  throw new LyricIndicatorError(`unsupported command: ${options.command}`);
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
  analyzeLyricIndicators,
  buildDryRun,
  buildLyricManifest,
  repetitionSignal,
  run,
  tokenize,
  wordlessSignal,
};
