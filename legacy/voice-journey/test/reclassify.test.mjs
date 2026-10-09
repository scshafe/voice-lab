import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import { classifyFeatures } from "../src/filter-proof.mjs";
import {
  applyReclassification,
  buildReclassifyDryRun,
  musicScoreFor,
  noiseScoreFor,
  run,
} from "../src/reclassify.mjs";

async function captureRun(args) {
  let stdout = "";
  const originalStdoutWrite = process.stdout.write;
  process.stdout.write = (chunk) => {
    stdout += String(chunk);
    return true;
  };
  try {
    const code = await run(args);
    return { code, stdout };
  } finally {
    process.stdout.write = originalStdoutWrite;
  }
}

async function withTempDir(callback) {
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-reclassify-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function features(overrides = {}) {
  return {
    durationSeconds: 60,
    activeRatio: 0.6,
    voicedRatio: 0.35,
    meanRmsDb: -25,
    meanZeroCrossingRate: 0.1,
    meanPitchHz: 220,
    meanPitchConfidence: 0.4,
    pitchStability: 0.5,
    clippingRatio: 0,
    frameCount: 1875,
    ...overrides,
  };
}

function resultRow(recordingId, bucket, overrides = {}) {
  return {
    recordingId,
    filename: `${recordingId}.m4a`,
    capturedAt: "2021-01-01T00:00:00Z",
    year: "2021",
    features: features(overrides.features),
    classification: {
      method: "voice-journey.local-heuristic-proof.v1",
      contentLabel: overrides.contentLabel ?? (bucket === "uncertain_manual_review" ? "uncertain" : "singing"),
      contaminationLabel: overrides.contaminationLabel ?? "uncertain",
      finalBucket: bucket,
      confidence: overrides.confidence ?? 0.45,
      rationale: "fixture",
    },
    humanSpotCheck: { recommended: true, status: "not_reviewed", reviewedBy: null, notes: null },
  };
}

function fakeResults(rows) {
  return {
    schemaVersion: "voice-journey.full-corpus-filter-results.v1",
    generatedAt: "2026-07-16T00:00:00.000Z",
    updatedAt: "2026-07-16T00:00:00.000Z",
    results: rows,
  };
}

function lyricRow(recordingId, { cluster = null, wordless = null, repetition = null } = {}) {
  return {
    recordingId,
    lyricMatch: {
      marked: Boolean(cluster || wordless || repetition),
      signals: {
        crossRecordingCluster: cluster ?? { marked: false, clusterId: null, clusterSize: 0, confidence: 0 },
        repetitionStructure: repetition ?? { marked: false, repeatedNgramCount: 0, confidence: 0.35 },
        wordlessVocalise: wordless ?? { marked: false, wordCount: 10, detectedLanguage: "en" },
      },
    },
  };
}

function fakeLyrics(rows) {
  return {
    schemaVersion: "voice-journey.local-lyric-indicators.v1",
    generatedAt: "2026-07-16T00:00:00.000Z",
    results: rows,
  };
}

test("recurring-cluster evidence resolves uncertain rows and preserves the v1 classification", () => {
  const results = fakeResults([
    resultRow("a", "uncertain_manual_review", { contentLabel: "singing", features: { meanPitchConfidence: 0.45, meanZeroCrossingRate: 0.16 } }),
  ]);
  const lyrics = fakeLyrics([
    lyricRow("a", { cluster: { marked: true, clusterId: "lyric-cluster-007", clusterLabel: "recurring_lyric_cluster_007", clusterSize: 30, confidence: 0.99 } }),
  ]);
  const { counters } = applyReclassification(results, lyrics, { results: "r.json", lyrics: "l.json", reclassifiedAt: "2026-07-30T00:00:00.000Z" });
  const row = results.results[0];
  assert.equal(row.classification.method, "voice-journey.evidence-join-reclassify.v2");
  assert.equal(row.classification.contentLabel, "singing");
  assert.equal(row.classification.finalBucket, "clean_singing");
  assert.equal(row.previousClassification.method, "voice-journey.local-heuristic-proof.v1");
  assert.equal(row.previousClassification.finalBucket, "uncertain_manual_review");
  assert.equal(row.evidence.rule, "recurring_cluster");
  assert.equal(row.evidence.signals.clusterId, "lyric-cluster-007");
  assert.equal(row.evidence.sources.audioBytes, false);
  assert.equal(counters.resolvedByRule.recurring_cluster, 1);
  assert.equal(counters.uncertainAfter, 0);
  assert.equal(results.reclassification.counters.resolvedToBucket.clean_singing, 1);
});

test("evidence-backed rows keep v1 noise/music contamination flags", () => {
  const results = fakeResults([
    resultRow("noisy", "uncertain_manual_review", { contentLabel: "singing", features: { meanZeroCrossingRate: 0.35, meanPitchConfidence: 0.4 } }),
  ]);
  const lyrics = fakeLyrics([
    lyricRow("noisy", { cluster: { marked: true, clusterId: "lyric-cluster-001", clusterLabel: "recurring_lyric_cluster_001", clusterSize: 60, confidence: 0.99 } }),
  ]);
  applyReclassification(results, lyrics, { results: "r.json", lyrics: "l.json", reclassifiedAt: "2026-07-30T00:00:00.000Z" });
  const row = results.results[0];
  assert.equal(row.classification.contaminationLabel, "noise_contaminated");
  assert.equal(row.classification.finalBucket, "noise_contaminated_singing");
  assert.ok(row.evidence.contaminationScores.noiseScore >= 0.55);
});

test("wordless vocalise and repetition rules resolve when voiced enough", () => {
  const results = fakeResults([
    resultRow("hum", "uncertain_manual_review", { contentLabel: "uncertain", features: { voicedRatio: 0.5, meanPitchConfidence: 0.5 } }),
    resultRow("chorus", "uncertain_manual_review", { contentLabel: "uncertain", features: { voicedRatio: 0.4, meanPitchConfidence: 0.45 } }),
    resultRow("quiet", "uncertain_manual_review", { contentLabel: "uncertain", features: { voicedRatio: 0.25 } }),
  ]);
  const lyrics = fakeLyrics([
    lyricRow("hum", { wordless: { marked: true, wordCount: 0, detectedLanguage: "unknown" } }),
    lyricRow("chorus", { repetition: { marked: true, repeatedNgramCount: 5, confidence: 0.75 } }),
    lyricRow("quiet", { wordless: { marked: true, wordCount: 0, detectedLanguage: "unknown" } }),
  ]);
  const { counters } = applyReclassification(results, lyrics, { results: "r.json", lyrics: "l.json", reclassifiedAt: "2026-07-30T00:00:00.000Z" });
  assert.equal(results.results[0].classification.finalBucket, "clean_singing");
  assert.equal(results.results[0].evidence.rule, "wordless_vocalise");
  assert.equal(results.results[1].evidence.rule, "repetition_structure");
  assert.equal(results.results[2].classification.finalBucket, "uncertain_manual_review");
  assert.equal(results.results[2].previousClassification, undefined);
  assert.equal(counters.uncertainBefore, 3);
  assert.equal(counters.uncertainAfter, 1);
});

test("decided rows are untouched and non_singing rows in clusters get conflict flags", () => {
  const results = fakeResults([
    resultRow("kept", "clean_singing", { contentLabel: "singing", contaminationLabel: "clean", confidence: 0.8 }),
    resultRow("spoken", "non_singing", { contentLabel: "non_singing", contaminationLabel: "not_applicable", confidence: 0.7 }),
  ]);
  const lyrics = fakeLyrics([
    lyricRow("kept", { cluster: { marked: true, clusterId: "lyric-cluster-002", clusterLabel: "recurring_lyric_cluster_002", clusterSize: 57, confidence: 0.99 } }),
    lyricRow("spoken", { cluster: { marked: true, clusterId: "lyric-cluster-003", clusterLabel: "recurring_lyric_cluster_003", clusterSize: 53, confidence: 0.99 } }),
  ]);
  const { counters } = applyReclassification(results, lyrics, { results: "r.json", lyrics: "l.json", reclassifiedAt: "2026-07-30T00:00:00.000Z" });
  assert.equal(results.results[0].classification.method, "voice-journey.local-heuristic-proof.v1");
  assert.equal(results.results[0].evidenceConflict, undefined);
  assert.equal(results.results[1].classification.finalBucket, "non_singing");
  assert.equal(results.results[1].evidenceConflict.type, "non_singing_in_recurring_cluster");
  assert.equal(results.results[1].humanSpotCheck.recommended, true);
  assert.equal(counters.conflictsFlagged, 1);
  assert.equal(counters.decidedRowsUntouched, 2);
});

test("contamination score formulas agree with the v1 classifier", () => {
  const noisy = features({ voicedRatio: 0.5, meanPitchConfidence: 0.5, pitchStability: 0.2, meanZeroCrossingRate: 0.4 });
  const v1Noisy = classifyFeatures(noisy);
  assert.equal(v1Noisy.contentLabel, "singing");
  assert.equal(v1Noisy.contaminationLabel, "noise_contaminated");
  assert.ok(noiseScoreFor(noisy) >= 0.55);

  const clean = features({ voicedRatio: 0.5, meanPitchConfidence: 0.7, pitchStability: 0.2, meanZeroCrossingRate: 0.1 });
  const v1Clean = classifyFeatures(clean);
  assert.equal(v1Clean.contaminationLabel, "clean");
  assert.ok(noiseScoreFor(clean) < 0.55);
  assert.ok(musicScoreFor(clean) < 0.62);
});

test("apply command dry-run computes counters without writing and real runs are deterministic", async () => {
  await withTempDir(async (root) => {
    const resultsPath = path.join(root, "results.json");
    const lyricsPath = path.join(root, "lyrics.json");
    await writeFile(resultsPath, JSON.stringify(fakeResults([
      resultRow("a", "uncertain_manual_review", { contentLabel: "singing" }),
      resultRow("b", "uncertain_manual_review", { contentLabel: "uncertain" }),
    ])), "utf8");
    await writeFile(lyricsPath, JSON.stringify(fakeLyrics([
      lyricRow("a", { cluster: { marked: true, clusterId: "lyric-cluster-005", clusterLabel: "recurring_lyric_cluster_005", clusterSize: 37, confidence: 0.99 } }),
      lyricRow("b"),
    ])), "utf8");
    const before = await readFile(resultsPath, "utf8");

    const dry = await captureRun(["apply", "--results", resultsPath, "--lyrics", lyricsPath, "--dry-run"]);
    const payload = JSON.parse(dry.stdout);
    assert.equal(dry.code, 0);
    assert.equal(payload.dryRun, true);
    assert.equal(payload.counters.uncertainBefore, 2);
    assert.equal(payload.counters.uncertainAfter, 1);
    assert.equal(payload.readScope.audioBytes, false);
    assert.equal(await readFile(resultsPath, "utf8"), before);

    const outA = path.join(root, "out-a.json");
    const outB = path.join(root, "out-b.json");
    const runA = await captureRun(["apply", "--results", resultsPath, "--lyrics", lyricsPath, "--out", outA, "--reclassified-at", "2026-07-30T00:00:00.000Z"]);
    const runB = await captureRun(["apply", "--results", resultsPath, "--lyrics", lyricsPath, "--out", outB, "--reclassified-at", "2026-07-30T00:00:00.000Z"]);
    assert.equal(runA.code, 0);
    assert.equal(runB.code, 0);
    const writtenA = await readFile(outA, "utf8");
    assert.equal(writtenA, await readFile(outB, "utf8"));
    const written = JSON.parse(writtenA);
    assert.equal(written.reclassification.method, "voice-journey.evidence-join-reclassify.v2");
    assert.equal(written.reclassification.counters.resolvedByRule.recurring_cluster, 1);
    assert.equal(written.updatedAt, "2026-07-30T00:00:00.000Z");
  });
});

test("dry-run builder leaves the input manifest object unmodified", () => {
  const results = fakeResults([
    resultRow("a", "uncertain_manual_review", { contentLabel: "singing" }),
  ]);
  const lyrics = fakeLyrics([
    lyricRow("a", { cluster: { marked: true, clusterId: "lyric-cluster-009", clusterLabel: "recurring_lyric_cluster_009", clusterSize: 26, confidence: 0.99 } }),
  ]);
  const snapshot = JSON.stringify(results);
  const payload = buildReclassifyDryRun(results, lyrics, { results: "r.json", lyrics: "l.json", outSet: false, out: "r.json" });
  assert.equal(payload.counters.uncertainBefore, 1);
  assert.equal(JSON.stringify(results), snapshot);
});
