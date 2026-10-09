import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import { executiveVerdict, run } from "../src/evidence-report.mjs";

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
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-report-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function fixtureFiles() {
  const results = {
    schemaVersion: "voice-journey.full-corpus-filter-results.v1",
    results: [
      { recordingId: "a", capturedAt: "2019-02-01T00:00:00Z", classification: { finalBucket: "clean_singing" } },
      { recordingId: "b", capturedAt: "2021-02-01T00:00:00Z", classification: { finalBucket: "clean_singing" } },
      { recordingId: "c", capturedAt: "2021-03-01T00:00:00Z", classification: { finalBucket: "non_singing" } },
    ],
  };
  const featureRow = (id, capturedAt, p95) => ({
    recordingId: id,
    capturedAt,
    year: capturedAt.slice(0, 4),
    status: "completed",
    features: {
      durationSeconds: 60,
      voicing: { voicedShare: 0.7, frameCount: 6000, rejectedOutlierShare: 0.01, rejectedGlobalOutlierShare: 0 },
      pitch: { f0Hz: { p05: 100, p25: 140, p50: 190, p75: 240, p95 }, rangeSemitonesP05P95: 11, madSemitones: 1 },
      tuning: { offsetCents: 0, medianAbsCentError: 21, within25CentsShare: 0.55 },
      vibrato: { sustainedSegmentCount: 3, vibratoSegmentCount: 1, meanRateHz: 4.1, meanExtentCents: 45, vibratoTimeShare: 0.3 },
      quality: { jitterLocal: 0.01, shimmerLocal: 0.07, meanHnrDb: 15, cpps: 11 },
      spectral: { centroidHzMean: 1400, centroidHzSd: 300, rolloffHzMean: 2800, singerFormantRatio: 0.04 },
      dynamics: { voicedRmsDbP10: -32, voicedRmsDbP50: -25, voicedRmsDbP90: -19, dynamicSpreadDb: 13 },
      phrasing: { longestSustainedSeconds: 2.8, meanVoicedSegmentSeconds: 1.7, pauseRatePerMinute: 9 },
    },
  });
  const features = {
    schemaVersion: "voice-journey.local-voice-features.v1",
    generatedAt: "2026-07-31T00:00:00.000Z",
    results: [],
  };
  for (let i = 0; i < 12; i += 1) {
    features.results.push(featureRow(`a${i}`, `2019-0${(i % 6) + 1}-10T00:00:00Z`, 260));
    features.results.push(featureRow(`b${i}`, `2021-0${(i % 6) + 1}-10T00:00:00Z`, 390));
  }
  const journeys = {
    schemaVersion: "voice-journey.song-journeys.v1",
    totals: { reliableTakes: 24, takesWithNoteCore: 24, clustersEligible: 1 },
    method: { slopes: { id: "theil_sen_per_cluster.v1" }, bootstrap: { resamples: 1000, seed: 2026 } },
    noteCore: { takes: [
      { id: "a0", year: "2019", quarter: "2019-Q1", centErrorMedian: 20.1, inTuneShare: 0.6 },
      { id: "b0", year: "2021", quarter: "2021-Q1", centErrorMedian: 17.4, inTuneShare: 0.66 },
    ] },
    journeys: [{ clusterId: "lyric-cluster-001", label: "recurring_lyric_cluster_001", takesUsed: 12, firstYear: "2019", lastYear: "2021", perYear: { 2019: { n: 6, noteCoreCentError: 20.1 }, 2021: { n: 6, noteCoreCentError: 17.4 } }, slopesPerYear: { noteCoreCentError: -1.3, cpps: 0.2 } }],
    improvementIndex: [
      { key: "noteCoreCentError", label: "note-core tuning error (cents)", direction: "down_good", medianSlopePerYear: -1.3, ci95: [-2.1, -0.5], clustersUsed: 1, verdict: "improving" },
      { key: "cpps", label: "voice clarity (CPPS dB)", direction: "up_good", medianSlopePerYear: 0.05, ci95: [-0.2, 0.3], clustersUsed: 1, verdict: "flat" },
    ],
  };
  const refereeState = {
    schemaVersion: "voice-journey.referee-state.v1",
    updatedAt: "2026-07-31T00:00:00.000Z",
    judgments: Array.from({ length: 32 }, (_, index) => ({
      pairId: `p${index}`,
      aId: "x",
      bId: "y",
      clusterId: "lyric-cluster-001",
      aYear: "2021",
      bYear: "2019",
      choice: index % 4 === 3 ? "too_close" : "a",
      judgedAt: "2026-07-31T00:00:00.000Z",
    })),
  };
  return { results, features, journeys, refereeState };
}

test("executiveVerdict names improving dimensions and the ear verdict", () => {
  const sentences = executiveVerdict({
    trends: { headline: { workingTop: { baselineYear: "2019", baselineTopHz: 265, peakYear: "2021", peakTopHz: 392, gainSemitones: 6.7 } } },
    journeys: { totals: { clustersEligible: 3 }, improvementIndex: [{ verdict: "improving", label: "note-core tuning error (cents)" }] },
    bt: { judgedTotal: 40, years: [{ year: "2019", strengthLog2: 0 }, { year: "2021", strengthLog2: 0.6 }] },
  });
  const joined = sentences.join(" ");
  assert.match(joined, /C4 to G4/u);
  assert.match(joined, /improvement in note-core tuning error/u);
  assert.match(joined, /blind ear disagrees/u);
});

test("generate writes a self-contained report and is deterministic", async () => {
  await withTempDir(async (root) => {
    const { results, features, journeys, refereeState } = fixtureFiles();
    const paths = {
      results: path.join(root, "results.json"),
      features: path.join(root, "features.json"),
      journeys: path.join(root, "journeys.json"),
      refereeState: path.join(root, "referee.json"),
    };
    await writeFile(paths.results, JSON.stringify(results), "utf8");
    await writeFile(paths.features, JSON.stringify(features), "utf8");
    await writeFile(paths.journeys, JSON.stringify(journeys), "utf8");
    await writeFile(paths.refereeState, JSON.stringify(refereeState), "utf8");
    const outA = path.join(root, "report-a.html");
    const outB = path.join(root, "report-b.html");
    const argsFor = (out) => [
      "generate",
      "--results", paths.results,
      "--features", paths.features,
      "--lyrics", path.join(root, "missing-lyrics.json"),
      "--journeys", paths.journeys,
      "--referee-state", paths.refereeState,
      "--out", out,
      "--generated-at", "2026-07-31T12:00:00.000Z",
    ];
    const first = await captureRun(argsFor(outA));
    assert.equal(first.code, 0);
    const html = await readFile(outA, "utf8");
    assert.match(html, /Evidence Report/u);
    assert.match(html, /Did I get better/u);
    assert.match(html, /same-song controlled/u);
    assert.match(html, /note-core tuning error \(cents\)/u);
    assert.match(html, /chip improving/u);
    assert.match(html, /32 blind judgments|32 judgments|judgedTotal/u);
    assert.match(html, /Perceived quality by year/u);
    assert.doesNotMatch(html, /<script>/u);
    assert.doesNotMatch(html, /f0ContourVoiced50ms/u);
    await captureRun(argsFor(outB));
    assert.equal(await readFile(outB, "utf8"), html);
  });
});

test("generate without journeys or judgments degrades to honest placeholders", async () => {
  await withTempDir(async (root) => {
    const { results, features } = fixtureFiles();
    const resultsPath = path.join(root, "results.json");
    const featuresPath = path.join(root, "features.json");
    await writeFile(resultsPath, JSON.stringify(results), "utf8");
    await writeFile(featuresPath, JSON.stringify(features), "utf8");
    const out = path.join(root, "report.html");
    const { code } = await captureRun([
      "generate",
      "--results", resultsPath,
      "--features", featuresPath,
      "--lyrics", path.join(root, "missing.json"),
      "--journeys", path.join(root, "missing2.json"),
      "--referee-state", path.join(root, "missing3.json"),
      "--out", out,
      "--generated-at", "2026-07-31T12:00:00.000Z",
    ]);
    assert.equal(code, 0);
    const html = await readFile(out, "utf8");
    assert.match(html, /No judgments yet/u);
    assert.match(html, /npm run journeys/u);
  });
});

test("report dry-run reports inputs without writing", async () => {
  await withTempDir(async (root) => {
    const { results, features } = fixtureFiles();
    const resultsPath = path.join(root, "results.json");
    const featuresPath = path.join(root, "features.json");
    await writeFile(resultsPath, JSON.stringify(results), "utf8");
    await writeFile(featuresPath, JSON.stringify(features), "utf8");
    const out = path.join(root, "report.html");
    const { code, stdout } = await captureRun([
      "generate", "--dry-run",
      "--results", resultsPath,
      "--features", featuresPath,
      "--lyrics", path.join(root, "m1.json"),
      "--journeys", path.join(root, "m2.json"),
      "--referee-state", path.join(root, "m3.json"),
      "--out", out,
    ]);
    const payload = JSON.parse(stdout);
    assert.equal(code, 0);
    assert.equal(payload.operation, "evidence-report-generation");
    assert.equal(payload.inputs.refereeJudgments, 0);
    assert.equal(payload.readScope.audioBytes, false);
    await assert.rejects(() => readFile(out, "utf8"), /ENOENT/u);
  });
});
