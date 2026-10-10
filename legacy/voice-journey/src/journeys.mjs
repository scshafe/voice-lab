#!/usr/bin/env node
import { readFile, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { resolvePaths } from "./paths.mjs";

const MANIFEST_SCHEMA_VERSION = "voice-journey.song-journeys.v1";
const VJ = resolvePaths();
const DEFAULT_FEATURES = VJ.manifest("local-voice-features.json");
const DEFAULT_LYRICS = VJ.manifest("local-lyric-indicators.json");
const DEFAULT_FEATURE_DIR = VJ.artifact("voice-features");
const DEFAULT_OUT = VJ.manifest("song-journeys.json");
const RELIABLE_REJECTION_MAX = 0.10;

// Note segmentation over the stored 50 ms voiced-only f0 contour: points are
// grouped into one note while consecutive samples stay within the cents jump
// threshold and the time gap threshold; runs shorter than minPoints are
// discarded as transitions/melisma. Tuning is then scored on note MEDIANS
// only, against a per-take tuning offset inferred from the notes themselves.
const NOTE_SEGMENTATION = {
  contourStepSeconds: 0.05,
  maxGapSeconds: 0.12,
  maxJumpCents: 80,
  minPoints: 4,
  minNotesForTuning: 5,
  inTuneCents: 25,
};
const SLOPES = {
  minTakes: 6,
  minSpanYears: 1.5,
  minPairYears: 0.5,
};
const BOOTSTRAP = { resamples: 1000, level: 0.95 };

const DIMENSIONS = [
  { key: "noteCoreCentError", label: "note-core tuning error (cents)", direction: "down_good" },
  { key: "inTuneShare", label: "note time within ±25 cents", direction: "up_good" },
  { key: "rangeSemitones", label: "range used (semitones p05–p95)", direction: "neutral" },
  { key: "cpps", label: "voice clarity (CPPS dB)", direction: "up_good" },
  { key: "longestSustain", label: "longest sustained note (s)", direction: "up_good" },
  { key: "vibratoDistanceFrom5p5", label: "vibrato rate distance from 5.5 Hz", direction: "down_good" },
];

class JourneysError extends Error {}

function parseArgs(argv) {
  const [maybeCommand, ...rest] = argv;
  const hasCommand = maybeCommand && !maybeCommand.startsWith("--") && maybeCommand !== "help";
  const command = hasCommand ? maybeCommand : "analyze";
  const args = hasCommand ? rest : argv;
  const options = {
    analyzedAt: null,
    command,
    dryRun: false,
    featureDir: DEFAULT_FEATURE_DIR,
    features: DEFAULT_FEATURES,
    lyrics: DEFAULT_LYRICS,
    out: DEFAULT_OUT,
    seed: 2026,
  };
  while (args.length > 0) {
    const next = args.shift();
    if (next === "--analyzed-at") {
      options.analyzedAt = requireValue(args, next);
    } else if (next === "--dry-run") {
      options.dryRun = true;
    } else if (next === "--feature-dir") {
      options.featureDir = requireValue(args, next);
    } else if (next === "--features") {
      options.features = requireValue(args, next);
    } else if (next === "--lyrics") {
      options.lyrics = requireValue(args, next);
    } else if (next === "--out") {
      options.out = requireValue(args, next);
    } else if (next === "--seed") {
      options.seed = parseNonNegativeInteger(requireValue(args, next), next);
    } else if (next === "--help" || next === "help") {
      options.help = true;
    } else {
      throw new JourneysError(`unsupported argument: ${next}`);
    }
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new JourneysError(`${flag} requires a value`);
  return value;
}

function parseNonNegativeInteger(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) throw new JourneysError(`${flag} must be a non-negative integer`);
  return parsed;
}

function printHelp() {
  process.stdout.write(`Voice Journey same-song journey analysis.

Usage:
  voice-journey-journeys analyze --dry-run [--features PATH] [--lyrics PATH]
  voice-journey-journeys analyze [--features PATH] [--lyrics PATH] [--feature-dir PATH] [--out PATH] [--analyzed-at ISO] [--seed N]

Reads the committed features + lyric manifests plus the gitignored local
feature detail store (stored f0 contours — no audio bytes are read). Segments
each take's contour into note cores, scores note-core tuning against a
per-take inferred tuning offset, builds per-cluster song journeys, and
aggregates a same-song improvement index (Theil–Sen slopes per cluster,
seeded bootstrap confidence intervals across clusters). Writes only numeric
aggregates and generic cluster labels to the repo-safe output manifest.
Host-executed because the feature detail store is gitignored; dry-run reads
only committed manifests.
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

function round(value, places = 3) {
  return Number.isFinite(value) ? Number(value.toFixed(places)) : null;
}

function centsOf(hz) {
  return 1200 * Math.log2(hz / 440);
}

function medianOf(values) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((left, right) => left - right);
  if (!sorted.length) return null;
  return sorted[Math.floor((sorted.length - 1) * 0.5)];
}

function quantileOf(values, p) {
  const sorted = values.filter((value) => Number.isFinite(value)).sort((left, right) => left - right);
  if (!sorted.length) return null;
  return sorted[Math.floor((sorted.length - 1) * p)];
}

function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function segmentNotes(contour, config = NOTE_SEGMENTATION) {
  const runs = [];
  let current = [];
  for (const point of contour ?? []) {
    if (!Array.isArray(point) || point.length < 2 || !Number.isFinite(point[1]) || point[1] <= 0) continue;
    const sample = { t: point[0], cents: centsOf(point[1]) };
    const previous = current.at(-1);
    if (!previous) {
      current.push(sample);
      continue;
    }
    const gap = sample.t - previous.t;
    const jump = Math.abs(sample.cents - previous.cents);
    if (gap <= config.maxGapSeconds && jump <= config.maxJumpCents) {
      current.push(sample);
    } else {
      if (current.length >= config.minPoints) runs.push(current);
      current = [sample];
    }
  }
  if (current.length >= config.minPoints) runs.push(current);
  return runs.map((run) => ({
    startSeconds: round(run[0].t, 2),
    durationSeconds: round(run.at(-1).t - run[0].t + config.contourStepSeconds, 2),
    medianCents: medianOf(run.map((sample) => sample.cents)),
    points: run.length,
  }));
}

function tuningOffsetFor(notes) {
  let sumSin = 0;
  let sumCos = 0;
  for (const note of notes) {
    const angle = ((note.medianCents % 100) + 100) % 100 * (2 * Math.PI / 100);
    sumSin += Math.sin(angle) * note.durationSeconds;
    sumCos += Math.cos(angle) * note.durationSeconds;
  }
  return Math.atan2(sumSin, sumCos) * (100 / (2 * Math.PI));
}

function noteCoreTuning(notes, config = NOTE_SEGMENTATION) {
  if (notes.length < config.minNotesForTuning) {
    return { notes: notes.length, noteTimeSeconds: round(notes.reduce((sum, note) => sum + note.durationSeconds, 0), 2), tuningOffsetCents: null, centErrorMedian: null, inTuneShare: null, meanNoteSeconds: null, longestNoteSeconds: null };
  }
  const offset = tuningOffsetFor(notes);
  const weighted = notes.map((note) => {
    const deviation = ((note.medianCents - offset + 50) % 100 + 100) % 100 - 50;
    return { absDeviation: Math.abs(deviation), weight: note.durationSeconds };
  }).sort((left, right) => left.absDeviation - right.absDeviation);
  const totalWeight = weighted.reduce((sum, entry) => sum + entry.weight, 0);
  let cumulative = 0;
  let weightedMedian = weighted.at(-1).absDeviation;
  for (const entry of weighted) {
    cumulative += entry.weight;
    if (cumulative >= totalWeight / 2) {
      weightedMedian = entry.absDeviation;
      break;
    }
  }
  const inTuneWeight = weighted.filter((entry) => entry.absDeviation <= config.inTuneCents).reduce((sum, entry) => sum + entry.weight, 0);
  return {
    notes: notes.length,
    noteTimeSeconds: round(totalWeight, 2),
    tuningOffsetCents: round(offset, 1),
    centErrorMedian: round(weightedMedian, 1),
    inTuneShare: round(inTuneWeight / totalWeight, 3),
    meanNoteSeconds: round(totalWeight / notes.length, 2),
    longestNoteSeconds: round(Math.max(...notes.map((note) => note.durationSeconds)), 2),
  };
}

function theilSenSlope(points, minPairYears = SLOPES.minPairYears) {
  const slopes = [];
  for (let i = 0; i < points.length; i += 1) {
    for (let j = i + 1; j < points.length; j += 1) {
      const dx = points[j].x - points[i].x;
      if (Math.abs(dx) < minPairYears) continue;
      slopes.push((points[j].y - points[i].y) / dx);
    }
  }
  return slopes.length ? medianOf(slopes) : null;
}

function bootstrapCi(values, seed, resamples = BOOTSTRAP.resamples, level = BOOTSTRAP.level) {
  if (values.length < 2) return null;
  const random = mulberry32(seed);
  const medians = [];
  for (let draw = 0; draw < resamples; draw += 1) {
    const sample = [];
    for (let pick = 0; pick < values.length; pick += 1) {
      sample.push(values[Math.floor(random() * values.length)]);
    }
    medians.push(medianOf(sample));
  }
  const alpha = (1 - level) / 2;
  return [round(quantileOf(medians, alpha), 3), round(quantileOf(medians, 1 - alpha), 3)];
}

function verdictFor(direction, ci) {
  if (!ci) return "insufficient_data";
  const [lo, hi] = ci;
  if (direction === "down_good") {
    if (hi < 0) return "improving";
    if (lo > 0) return "declining";
    return "flat";
  }
  if (direction === "up_good") {
    if (lo > 0) return "improving";
    if (hi < 0) return "declining";
    return "flat";
  }
  if (lo > 0) return "trending_up";
  if (hi < 0) return "trending_down";
  return "flat";
}

function yearFractionOf(capturedAt) {
  const parsed = Date.parse(capturedAt);
  return Number.isFinite(parsed) ? parsed / 31557600000 + 1970 : null;
}

function reliableFeatureTakes(featuresManifest) {
  return (featuresManifest.results ?? []).filter((row) => {
    if (row.status !== "completed" || !row.features || !row.capturedAt) return false;
    const voicing = row.features.voicing ?? {};
    return ((voicing.rejectedOutlierShare ?? 0) + (voicing.rejectedGlobalOutlierShare ?? 0)) <= RELIABLE_REJECTION_MAX;
  });
}

function takeMetricsFor(row, noteCore) {
  const vibratoRate = row.features.vibrato?.meanRateHz ?? null;
  return {
    noteCoreCentError: noteCore?.centErrorMedian ?? null,
    inTuneShare: noteCore?.inTuneShare ?? null,
    rangeSemitones: row.features.pitch?.rangeSemitonesP05P95 ?? null,
    cpps: row.features.quality?.cpps ?? null,
    longestSustain: row.features.phrasing?.longestSustainedSeconds ?? null,
    vibratoDistanceFrom5p5: vibratoRate === null ? null : round(Math.abs(vibratoRate - 5.5), 2),
  };
}

function eligibleClusters(lyricsManifest, takesById) {
  return (lyricsManifest.clusters ?? [])
    .map((cluster) => {
      const takes = (cluster.recordingIds ?? []).map((id) => takesById.get(id)).filter(Boolean);
      const fractions = takes.map((take) => take.yearFraction).filter((value) => value !== null);
      const span = fractions.length ? Math.max(...fractions) - Math.min(...fractions) : 0;
      return { cluster, takes, span };
    })
    .filter(({ takes, span }) => takes.length >= SLOPES.minTakes && span >= SLOPES.minSpanYears);
}

function buildJourneysDryRun(featuresManifest, lyricsManifest, options) {
  const reliable = reliableFeatureTakes(featuresManifest);
  const takesById = new Map(reliable.map((row) => [row.recordingId, { yearFraction: yearFractionOf(row.capturedAt) }]));
  const eligible = eligibleClusters(lyricsManifest, takesById);
  return {
    dryRun: true,
    operation: "same-song-journey-analysis",
    architecture: {
      slug: "kickoff-corpus-flow",
      reads: ["voice-feature-manifest", "lyric-indicator-manifest", "local-voice-feature-store"],
      writes: ["song-journeys-manifest"],
    },
    featuresPath: options.features,
    lyricsPath: options.lyrics,
    localFeatureDir: options.featureDir,
    outputPath: options.out,
    scope: {
      reliableTakes: reliable.length,
      clustersTotal: lyricsManifest.clusters?.length ?? 0,
      clustersEligible: eligible.length,
      eligibilityRule: `>= ${SLOPES.minTakes} reliable takes spanning >= ${SLOPES.minSpanYears} years`,
    },
    method: journeysMethod(options),
    readScope: journeysReadScope(),
  };
}

function journeysMethod(options) {
  return {
    noteSegmentation: { ...NOTE_SEGMENTATION, id: "contour_note_cores.v1" },
    tuning: { id: "duration_weighted_circular_offset_mod_100.v1", scoredOn: "note medians only (transitions and melisma excluded by the stability criterion)" },
    slopes: { id: "theil_sen_per_cluster.v1", ...SLOPES },
    bootstrap: { id: "cluster_resample_median.v1", ...BOOTSTRAP, seed: options.seed },
    reliability: `takes with total f0 frame rejection <= ${RELIABLE_REJECTION_MAX}`,
    clusterVerificationNote: "clusters come from transcript-shingle similarity; acoustic verification/splitting is a planned upgrade and would need its own gated audio pass",
  };
}

function journeysReadScope() {
  return {
    committedManifests: true,
    localFeatureDetailContours: true,
    audioBytes: false,
    transcriptText: false,
    lyricText: false,
    upload: false,
    corpusMutation: false,
  };
}

async function analyzeJourneys(featuresManifest, lyricsManifest, options, hooks = {}) {
  const readDetail = hooks.readDetail ?? (async (recordingId) => {
    try {
      return await readJson(path.join(options.featureDir, `${recordingId}.json`));
    } catch (error) {
      if (error?.code === "ENOENT") return null;
      throw error;
    }
  });

  const reliable = reliableFeatureTakes(featuresManifest);
  const noteCoreTakes = [];
  const takesById = new Map();
  let missingDetails = 0;
  for (const row of reliable) {
    const detail = await readDetail(row.recordingId);
    let noteCore = null;
    if (detail?.detail?.f0ContourVoiced50ms) {
      noteCore = noteCoreTuning(segmentNotes(detail.detail.f0ContourVoiced50ms));
    } else {
      missingDetails += 1;
    }
    const metrics = takeMetricsFor(row, noteCore);
    const take = {
      id: row.recordingId,
      year: row.year ?? (row.capturedAt ? row.capturedAt.slice(0, 4) : "unknown"),
      yearFraction: yearFractionOf(row.capturedAt),
      quarter: row.capturedAt ? `${row.capturedAt.slice(0, 4)}-Q${Math.ceil(Number(row.capturedAt.slice(5, 7)) / 3)}` : null,
      noteCore,
      metrics,
    };
    takesById.set(row.recordingId, take);
    if (noteCore) {
      noteCoreTakes.push({
        id: take.id,
        year: take.year,
        quarter: take.quarter,
        notes: noteCore.notes,
        noteTimeSeconds: noteCore.noteTimeSeconds,
        tuningOffsetCents: noteCore.tuningOffsetCents,
        centErrorMedian: noteCore.centErrorMedian,
        inTuneShare: noteCore.inTuneShare,
        meanNoteSeconds: noteCore.meanNoteSeconds,
        longestNoteSeconds: noteCore.longestNoteSeconds,
      });
    }
  }

  const eligible = eligibleClusters(lyricsManifest, takesById);
  const journeys = eligible.map(({ cluster, takes }) => {
    const perYear = {};
    for (const take of takes) {
      if (!perYear[take.year]) perYear[take.year] = [];
      perYear[take.year].push(take);
    }
    const perYearRollup = {};
    for (const year of Object.keys(perYear).sort()) {
      const group = perYear[year];
      perYearRollup[year] = {
        n: group.length,
        noteCoreCentError: round(medianOf(group.map((take) => take.metrics.noteCoreCentError)), 1),
        inTuneShare: round(medianOf(group.map((take) => take.metrics.inTuneShare)), 3),
        rangeSemitones: round(medianOf(group.map((take) => take.metrics.rangeSemitones)), 2),
        cpps: round(medianOf(group.map((take) => take.metrics.cpps)), 2),
        longestSustain: round(medianOf(group.map((take) => take.metrics.longestSustain)), 2),
      };
    }
    const slopes = {};
    for (const dimension of DIMENSIONS) {
      const points = takes
        .filter((take) => take.yearFraction !== null && take.metrics[dimension.key] !== null)
        .map((take) => ({ x: take.yearFraction, y: take.metrics[dimension.key] }));
      slopes[dimension.key] = points.length >= SLOPES.minTakes ? round(theilSenSlope(points), 4) : null;
    }
    const fractions = takes.map((take) => take.yearFraction).filter((value) => value !== null);
    return {
      clusterId: cluster.clusterId,
      label: cluster.label ?? cluster.clusterId,
      takesUsed: takes.length,
      firstYear: String(Math.floor(Math.min(...fractions))),
      lastYear: String(Math.floor(Math.max(...fractions))),
      perYear: perYearRollup,
      slopesPerYear: slopes,
    };
  });

  const improvementIndex = DIMENSIONS.map((dimension, index) => {
    const clusterSlopes = journeys.map((journey) => journey.slopesPerYear[dimension.key]).filter((value) => value !== null);
    const ci = bootstrapCi(clusterSlopes, options.seed + index);
    return {
      key: dimension.key,
      label: dimension.label,
      direction: dimension.direction,
      medianSlopePerYear: round(medianOf(clusterSlopes), 4),
      ci95: ci,
      clustersUsed: clusterSlopes.length,
      verdict: verdictFor(dimension.direction, ci),
    };
  });

  const analyzedAt = options.analyzedAt ?? new Date().toISOString();
  const manifest = {
    schemaVersion: MANIFEST_SCHEMA_VERSION,
    generatedAt: analyzedAt,
    updatedAt: analyzedAt,
    sources: {
      features: {
        schemaVersion: featuresManifest.schemaVersion ?? null,
        generatedAt: featuresManifest.generatedAt ?? null,
        updatedAt: featuresManifest.updatedAt ?? null,
        recordingCount: featuresManifest.results?.length ?? null,
      },
      lyrics: {
        schemaVersion: lyricsManifest.schemaVersion ?? null,
        generatedAt: lyricsManifest.generatedAt ?? null,
        clusterCount: lyricsManifest.clusters?.length ?? null,
      },
    },
    method: journeysMethod(options),
    readScope: journeysReadScope(),
    repoSafe: {
      containsContourArrays: false,
      containsAudioBytes: false,
      containsTranscriptText: false,
      containsLyricText: false,
      genericClusterLabelsOnly: true,
    },
    totals: {
      reliableTakes: reliable.length,
      takesWithNoteCore: noteCoreTakes.length,
      missingDetailFiles: missingDetails,
      clustersEligible: journeys.length,
    },
    noteCore: { takes: noteCoreTakes },
    journeys,
    improvementIndex,
  };
  await writeJson(options.out, manifest);
  return manifest;
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (!options.command || options.help) {
    printHelp();
    return 0;
  }
  if (options.command === "analyze") {
    const featuresManifest = await readJson(options.features);
    const lyricsManifest = await readJson(options.lyrics);
    if (options.dryRun) {
      await writeJson("-", buildJourneysDryRun(featuresManifest, lyricsManifest, options));
      return 0;
    }
    await analyzeJourneys(featuresManifest, lyricsManifest, options);
    return 0;
  }
  throw new JourneysError(`unsupported command: ${options.command}`);
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
  analyzeJourneys,
  bootstrapCi,
  buildJourneysDryRun,
  noteCoreTuning,
  run,
  segmentNotes,
  theilSenSlope,
};
