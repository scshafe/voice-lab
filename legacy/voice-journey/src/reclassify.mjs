#!/usr/bin/env node
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { resolvePaths } from "./paths.mjs";

const RECLASSIFY_METHOD = "voice-journey.evidence-join-reclassify.v2";
const VJ = resolvePaths();
const DEFAULT_RESULTS = VJ.manifest("full-corpus-filter-results.json");
const DEFAULT_LYRICS = VJ.manifest("local-lyric-indicators.json");

// Contamination score constants mirror classifyFeatures in filter-proof.mjs
// (voice-journey.local-heuristic-proof.v1). A test cross-checks agreement.
const NOISE_SCORE_THRESHOLD = 0.55;
const MUSIC_SCORE_THRESHOLD = 0.62;

const RULES = [
  {
    id: "recurring_cluster",
    description: "Recording sits in a cross-recording lyric cluster (recurring repertoire across the archive) — strong singing evidence.",
  },
  {
    id: "wordless_vocalise",
    description: "Wordless/vocalise STT signal (zero words or non-English detection) with voicedRatio >= 0.3 — sung vocalise evidence.",
  },
  {
    id: "repetition_structure",
    description: "Repeated-phrase transcript structure (>= 3 repeated 4-grams) with voicedRatio >= 0.3 — chorus-like singing evidence.",
  },
];

class ReclassifyError extends Error {}

function parseArgs(argv) {
  const [maybeCommand, ...rest] = argv;
  const hasCommand = maybeCommand && !maybeCommand.startsWith("--") && maybeCommand !== "help";
  const command = hasCommand ? maybeCommand : "apply";
  const args = hasCommand ? rest : argv;
  const options = {
    command,
    dryRun: false,
    lyrics: DEFAULT_LYRICS,
    out: DEFAULT_RESULTS,
    outSet: false,
    reclassifiedAt: null,
    results: DEFAULT_RESULTS,
  };

  while (args.length > 0) {
    const next = args.shift();
    if (next === "--dry-run") {
      options.dryRun = true;
    } else if (next === "--lyrics") {
      options.lyrics = requireValue(args, next);
    } else if (next === "--out") {
      options.out = requireValue(args, next);
      options.outSet = true;
    } else if (next === "--reclassified-at") {
      options.reclassifiedAt = requireValue(args, next);
    } else if (next === "--results") {
      options.results = requireValue(args, next);
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new ReclassifyError(`unsupported argument: ${next}`);
    }
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new ReclassifyError(`${flag} requires a value`);
  return value;
}

function printHelp() {
  process.stdout.write(`Voice Journey evidence-join reclassifier.

Usage:
  voice-journey-reclassify apply --dry-run [--results PATH] [--lyrics PATH]
  voice-journey-reclassify apply [--results PATH] [--lyrics PATH] [--out PATH] [--reclassified-at ISO]

Joins the committed full-corpus filter results with the committed lyric
indicator manifest and resolves uncertain_manual_review rows that carry
singing evidence (recurring clusters, wordless vocalise, repetition
structure). Contamination for resolved rows uses the same noise/music score
formulas as the v1 heuristic; rows without evidence stay in the review
bucket. Decided rows are never changed, but non_singing rows that sit in a
recurring cluster get a repo-safe evidenceConflict flag for review.

Reads committed repo-safe manifests only: no audio, no transcript text, no
host-only paths. Dry-run computes the same result and prints counters
without writing.
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

function clamp(value, min = 0, max = 1) {
  return Math.min(Math.max(value, min), max);
}

function round(value, places = 3) {
  return Number.isFinite(value) ? Number(value.toFixed(places)) : null;
}

function noiseScoreFor(features) {
  return clamp(
    (features.meanZeroCrossingRate - 0.12) * 3
    + (1 - features.meanPitchConfidence) * 0.4
    + features.clippingRatio * 10,
  );
}

function musicScoreFor(features) {
  const pitchStability = features.pitchStability ?? 1;
  return clamp(
    (features.activeRatio - 0.78) * 1.1
    + (features.voicedRatio - 0.65) * 0.9
    + (pitchStability < 0.08 ? 0.25 : 0),
  );
}

function finalBucketFor(contentLabel, contaminationLabel) {
  if (contentLabel === "non_singing") return "non_singing";
  if (contentLabel !== "singing") return "uncertain_manual_review";
  if (contaminationLabel === "clean") return "clean_singing";
  if (contaminationLabel === "noise_contaminated") return "noise_contaminated_singing";
  if (contaminationLabel === "music_contaminated") return "music_contaminated_singing";
  return "uncertain_manual_review";
}

function lyricSignalsByRecordingId(lyricsManifest) {
  const map = new Map();
  for (const row of lyricsManifest.results ?? []) {
    if (row?.recordingId) map.set(row.recordingId, row.lyricMatch ?? null);
  }
  return map;
}

function matchRule(lyricMatch, features) {
  if (!lyricMatch) return null;
  const cluster = lyricMatch.signals?.crossRecordingCluster;
  if (cluster?.marked) {
    return {
      rule: "recurring_cluster",
      contentConfidence: clamp(
        0.6 + Math.min(cluster.clusterSize ?? 0, 10) * 0.02 + Math.max((features.voicedRatio ?? 0) - 0.22, 0) * 0.25,
        0,
        0.9,
      ),
      signals: {
        clusterId: cluster.clusterId ?? null,
        clusterLabel: cluster.clusterLabel ?? null,
        clusterSize: cluster.clusterSize ?? null,
        clusterConfidence: cluster.confidence ?? null,
      },
    };
  }
  const wordless = lyricMatch.signals?.wordlessVocalise;
  if (wordless?.marked && (features.voicedRatio ?? 0) >= 0.3) {
    return {
      rule: "wordless_vocalise",
      contentConfidence: wordless.wordCount === 0 ? 0.7 : 0.62,
      signals: {
        wordCount: wordless.wordCount ?? null,
        detectedLanguage: wordless.detectedLanguage ?? null,
      },
    };
  }
  const repetition = lyricMatch.signals?.repetitionStructure;
  if (repetition?.marked && (repetition.repeatedNgramCount ?? 0) >= 3 && (features.voicedRatio ?? 0) >= 0.3) {
    return {
      rule: "repetition_structure",
      contentConfidence: clamp(0.55 + Math.min(repetition.repeatedNgramCount ?? 0, 10) * 0.015, 0, 0.7),
      signals: {
        repeatedNgramCount: repetition.repeatedNgramCount ?? null,
      },
    };
  }
  return null;
}

function reclassifyRow(row, lyricMatch) {
  const features = row.features ?? {};
  const matched = matchRule(lyricMatch, features);
  if (!matched) return null;

  const priorContentLabel = row.classification?.contentLabel ?? null;
  const contentConfidence = clamp(
    matched.contentConfidence + (priorContentLabel === "singing" ? 0.05 : 0),
    0,
    0.9,
  );
  const noiseScore = round(noiseScoreFor(features), 3);
  const musicScore = round(musicScoreFor(features), 3);
  let contaminationLabel = "clean";
  let contaminationConfidence = clamp(0.55 + (NOISE_SCORE_THRESHOLD - Math.max(noiseScore, musicScore)) * 0.3, 0, 0.75);
  if (musicScore >= MUSIC_SCORE_THRESHOLD) {
    contaminationLabel = "music_contaminated";
    contaminationConfidence = musicScore;
  } else if (noiseScore >= NOISE_SCORE_THRESHOLD) {
    contaminationLabel = "noise_contaminated";
    contaminationConfidence = noiseScore;
  }
  const finalBucket = finalBucketFor("singing", contaminationLabel);
  const confidence = round(Math.min(contentConfidence, contaminationConfidence), 3);
  return {
    classification: {
      method: RECLASSIFY_METHOD,
      contentLabel: "singing",
      contaminationLabel,
      finalBucket,
      confidence,
      rationale: `content=singing(evidence:${matched.rule}); contamination=${contaminationLabel}(noiseScore=${noiseScore}; musicScore=${musicScore}); priorContent=${priorContentLabel}; voicedRatio=${features.voicedRatio}; pitchConfidence=${features.meanPitchConfidence}; zcr=${features.meanZeroCrossingRate}`,
    },
    evidence: {
      rule: matched.rule,
      signals: matched.signals,
      priorContentLabel,
      contaminationScores: { noiseScore, musicScore },
      sources: {
        lyricIndicators: true,
        v1Features: true,
        audioBytes: false,
        transcriptText: false,
      },
    },
  };
}

function conflictFor(row, lyricMatch, reclassifiedAt) {
  if (row.classification?.finalBucket !== "non_singing") return null;
  const cluster = lyricMatch?.signals?.crossRecordingCluster;
  if (!cluster?.marked) return null;
  return {
    type: "non_singing_in_recurring_cluster",
    clusterId: cluster.clusterId ?? null,
    clusterSize: cluster.clusterSize ?? null,
    flaggedAt: reclassifiedAt,
    note: "Classified non_singing but its transcript recurs across recordings; verify by listening.",
  };
}

function spotCheckRecommended(classification) {
  return classification.finalBucket === "uncertain_manual_review" || classification.confidence < 0.7;
}

function applyReclassification(resultsManifest, lyricsManifest, options) {
  const reclassifiedAt = options.reclassifiedAt ?? new Date().toISOString();
  const lyricById = lyricSignalsByRecordingId(lyricsManifest);
  const counters = {
    rowsExamined: 0,
    uncertainBefore: 0,
    uncertainAfter: 0,
    resolvedByRule: { recurring_cluster: 0, wordless_vocalise: 0, repetition_structure: 0 },
    resolvedToBucket: { clean_singing: 0, noise_contaminated_singing: 0, music_contaminated_singing: 0 },
    decidedRowsUntouched: 0,
    conflictsFlagged: 0,
  };

  for (const row of resultsManifest.results ?? []) {
    counters.rowsExamined += 1;
    const lyricMatch = lyricById.get(row.recordingId) ?? null;
    if (row.classification?.finalBucket !== "uncertain_manual_review") {
      counters.decidedRowsUntouched += 1;
      const conflict = conflictFor(row, lyricMatch, reclassifiedAt);
      if (conflict) {
        row.evidenceConflict = conflict;
        row.humanSpotCheck = { ...row.humanSpotCheck, recommended: true };
        counters.conflictsFlagged += 1;
      }
      continue;
    }
    counters.uncertainBefore += 1;
    const resolved = reclassifyRow(row, lyricMatch);
    if (!resolved) {
      counters.uncertainAfter += 1;
      continue;
    }
    row.previousClassification = row.classification;
    row.classification = resolved.classification;
    row.evidence = resolved.evidence;
    row.humanSpotCheck = { ...row.humanSpotCheck, recommended: spotCheckRecommended(resolved.classification) };
    counters.resolvedByRule[resolved.evidence.rule] += 1;
    counters.resolvedToBucket[resolved.classification.finalBucket] += 1;
  }

  resultsManifest.reclassification = {
    method: RECLASSIFY_METHOD,
    reclassifiedAt,
    rules: RULES,
    scope: "uncertain_manual_review rows only; decided rows are never relabelled (non_singing rows in recurring clusters get evidenceConflict flags)",
    contamination: {
      formulas: "noiseScore/musicScore mirror voice-journey.local-heuristic-proof.v1",
      noiseScoreThreshold: NOISE_SCORE_THRESHOLD,
      musicScoreThreshold: MUSIC_SCORE_THRESHOLD,
      cleanRule: "evidence-backed singing with no contamination flag resolves to clean",
    },
    sources: {
      results: sourceProvenance(resultsManifest, options.results),
      lyrics: sourceProvenance(lyricsManifest, options.lyrics),
    },
    counters,
    readScope: reclassifyReadScope(),
    repoSafe: {
      containsTranscriptText: false,
      containsAudioBytes: false,
      containsLyricText: false,
    },
  };
  resultsManifest.updatedAt = reclassifiedAt;
  return { manifest: resultsManifest, counters };
}

function sourceProvenance(manifest, manifestPath) {
  return {
    path: manifestPath,
    schemaVersion: manifest.schemaVersion ?? null,
    generatedAt: manifest.generatedAt ?? null,
    updatedAt: manifest.updatedAt ?? null,
  };
}

function reclassifyReadScope() {
  return {
    committedManifestsOnly: true,
    audioBytes: false,
    transcriptText: false,
    hostOnlyPaths: false,
    upload: false,
    corpusMutation: false,
  };
}

function buildReclassifyDryRun(resultsManifest, lyricsManifest, options) {
  const cloned = JSON.parse(JSON.stringify(resultsManifest));
  const { counters } = applyReclassification(cloned, lyricsManifest, {
    ...options,
    reclassifiedAt: options.reclassifiedAt ?? "dry-run",
  });
  return {
    dryRun: true,
    operation: "evidence-join-reclassification",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["filter-results-manifest", "lyric-indicator-manifest"],
      writes: ["filter-results-manifest"],
    },
    resultsPath: options.results,
    lyricsPath: options.lyrics,
    outputPath: reclassifyTargetFor(options),
    method: RECLASSIFY_METHOD,
    rules: RULES,
    counters,
    readScope: reclassifyReadScope(),
  };
}

function reclassifyTargetFor(options) {
  return options.outSet ? options.out : options.results;
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (!options.command || options.help) {
    printHelp();
    return 0;
  }
  if (options.command === "apply") {
    const resultsManifest = await readJson(options.results);
    const lyricsManifest = await readJson(options.lyrics);
    if (options.dryRun) {
      await writeJson("-", buildReclassifyDryRun(resultsManifest, lyricsManifest, options));
      return 0;
    }
    const { manifest } = applyReclassification(resultsManifest, lyricsManifest, options);
    await writeJson(reclassifyTargetFor(options), manifest);
    return 0;
  }
  throw new ReclassifyError(`unsupported command: ${options.command}`);
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
  applyReclassification,
  buildReclassifyDryRun,
  matchRule,
  musicScoreFor,
  noiseScoreFor,
  reclassifyRow,
  run,
};
