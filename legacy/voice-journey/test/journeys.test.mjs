import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  analyzeJourneys,
  bootstrapCi,
  noteCoreTuning,
  run,
  segmentNotes,
  theilSenSlope,
} from "../src/journeys.mjs";

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
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-journeys-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function contourFor(segments) {
  const points = [];
  let t = 0;
  for (const segment of segments) {
    for (let i = 0; i < segment.points; i += 1) {
      points.push([Number(t.toFixed(2)), segment.hz]);
      t += 0.05;
    }
    t += segment.gap ?? 0;
  }
  return points;
}

function centsAboveA4(cents) {
  return 440 * Math.pow(2, cents / 1200);
}

test("segmentNotes splits on jumps and gaps and drops short transitions", () => {
  const contour = contourFor([
    { hz: 220, points: 10 },
    { hz: 330, points: 8, gap: 0 },
    { hz: 262, points: 2, gap: 0.3 },
    { hz: 440, points: 6, gap: 0.3 },
  ]);
  const notes = segmentNotes(contour);
  assert.equal(notes.length, 3);
  assert.ok(Math.abs(notes[0].medianCents - (-1200)) < 5);
  assert.ok(Math.abs(notes[1].medianCents - (-498)) < 8);
  assert.ok(Math.abs(notes[2].medianCents - 0) < 5);
  assert.equal(notes[0].points, 10);
});

test("noteCoreTuning infers the take's own offset and scores deviations from it", () => {
  const offsets = [20, 18, 22, 20, 19, 21];
  const notes = offsets.map((cents, index) => ({
    startSeconds: index,
    durationSeconds: 0.5,
    medianCents: -1200 + index * 100 + cents,
    points: 10,
  }));
  const tuning = noteCoreTuning(notes);
  assert.ok(Math.abs(tuning.tuningOffsetCents - 20) < 1.5, `offset=${tuning.tuningOffsetCents}`);
  assert.ok(tuning.centErrorMedian <= 2, `error=${tuning.centErrorMedian}`);
  assert.equal(tuning.inTuneShare, 1);
  assert.equal(tuning.notes, 6);

  const tooFew = noteCoreTuning(notes.slice(0, 3));
  assert.equal(tooFew.centErrorMedian, null);
});

test("theilSenSlope recovers a clean linear trend and bootstrapCi is seed-deterministic", () => {
  const points = [0, 1, 2, 3, 4].map((x) => ({ x: 2019 + x, y: 30 - 2 * x }));
  assert.equal(theilSenSlope(points), -2);
  const ciA = bootstrapCi([-2.1, -1.9, -2.4, -1.7, -2.2], 7);
  const ciB = bootstrapCi([-2.1, -1.9, -2.4, -1.7, -2.2], 7);
  assert.deepEqual(ciA, ciB);
  assert.ok(ciA[1] < 0);
});

function journeyFixture(root) {
  const featureRows = [];
  const clusterIds = { improving: [], flat: [] };
  const detailFiles = new Map();
  let serial = 0;
  const addTake = (year, month, clusterKey, centOffsetSpread) => {
    const id = `take-${serial += 1}`;
    const capturedAt = `${year}-${String(month).padStart(2, "0")}-10T00:00:00Z`;
    featureRows.push({
      recordingId: id,
      capturedAt,
      year: String(year),
      status: "completed",
      features: {
        durationSeconds: 40,
        voicing: { voicedShare: 0.7, frameCount: 4000, rejectedOutlierShare: 0.01, rejectedGlobalOutlierShare: 0 },
        pitch: { f0Hz: { p05: 110, p25: 150, p50: 200, p75: 260, p95: 330 }, rangeSemitonesP05P95: 12, madSemitones: 1 },
        tuning: { offsetCents: 0, medianAbsCentError: 22, within25CentsShare: 0.5 },
        vibrato: { sustainedSegmentCount: 3, vibratoSegmentCount: 2, meanRateHz: 4, meanExtentCents: 45, vibratoTimeShare: 0.3 },
        quality: { jitterLocal: 0.01, shimmerLocal: 0.07, meanHnrDb: 15, cpps: 10 + (year - 2019) * (clusterKey === "improving" ? 0.6 : 0), spectral: null },
        spectral: { centroidHzMean: 1400, centroidHzSd: 300, rolloffHzMean: 2800, singerFormantRatio: 0.04 },
        dynamics: { voicedRmsDbP10: -32, voicedRmsDbP50: -25, voicedRmsDbP90: -19, dynamicSpreadDb: 13 },
        phrasing: { longestSustainedSeconds: 2 + (year - 2019) * (clusterKey === "improving" ? 0.3 : 0), meanVoicedSegmentSeconds: 1.5, pauseRatePerMinute: 8 },
      },
    });
    clusterIds[clusterKey].push(id);
    const spread = centOffsetSpread;
    const notes = [0, 1, 2, 3, 4, 5].map((noteIndex) => ({
      hz: centsAboveA4(-1200 + noteIndex * 200 + (noteIndex % 2 === 0 ? spread : -spread)),
      points: 8,
      gap: 0.3,
    }));
    detailFiles.set(id, {
      recordingId: id,
      methodId: "voice-journey.feature-extract.v1",
      summary: {},
      detail: { f0ContourVoiced50ms: contourFor(notes), vibratoSegments: [] },
    });
  };
  for (const year of [2019, 2020, 2021, 2022]) {
    for (const month of [2, 8]) {
      addTake(year, month, "improving", Math.max(4, 36 - (year - 2019) * 10));
      addTake(year, month, "flat", 18);
    }
  }
  return { featureRows, clusterIds, detailFiles };
}

test("analyzeJourneys builds journeys, note-core tuning, and a verdicted improvement index", async () => {
  await withTempDir(async (root) => {
    const { featureRows, clusterIds, detailFiles } = journeyFixture(root);
    const featuresManifest = { schemaVersion: "voice-journey.local-voice-features.v1", generatedAt: "2026-07-31T00:00:00.000Z", results: featureRows };
    const lyricsManifest = {
      schemaVersion: "voice-journey.local-lyric-indicators.v1",
      clusters: [
        { clusterId: "cluster-improving", label: "recurring_lyric_cluster_A", recordingIds: clusterIds.improving },
        { clusterId: "cluster-flat", label: "recurring_lyric_cluster_B", recordingIds: clusterIds.flat },
        { clusterId: "cluster-tiny", label: "recurring_lyric_cluster_C", recordingIds: clusterIds.flat.slice(0, 2) },
      ],
      results: [],
    };
    const outPath = path.join(root, "song-journeys.json");
    const manifest = await analyzeJourneys(featuresManifest, lyricsManifest, {
      featureDir: root,
      out: outPath,
      analyzedAt: "2026-07-31T00:00:00.000Z",
      seed: 2026,
    }, {
      readDetail: async (recordingId) => detailFiles.get(recordingId) ?? null,
    });

    assert.equal(manifest.totals.reliableTakes, 16);
    assert.equal(manifest.totals.takesWithNoteCore, 16);
    assert.equal(manifest.totals.clustersEligible, 2);
    const improving = manifest.journeys.find((journey) => journey.clusterId === "cluster-improving");
    assert.ok(improving.slopesPerYear.noteCoreCentError < -5, `slope=${improving.slopesPerYear.noteCoreCentError}`);
    assert.ok(improving.slopesPerYear.cpps > 0.4);
    const flat = manifest.journeys.find((journey) => journey.clusterId === "cluster-flat");
    assert.ok(Math.abs(flat.slopesPerYear.noteCoreCentError) < 2);
    const centDim = manifest.improvementIndex.find((dimension) => dimension.key === "noteCoreCentError");
    assert.equal(centDim.clustersUsed, 2);
    const written = await readFile(outPath, "utf8");
    assert.doesNotMatch(written, /f0ContourVoiced50ms/u);
    assert.ok(JSON.parse(written).noteCore.takes.length === 16);

    const outPath2 = path.join(root, "song-journeys-2.json");
    await analyzeJourneys(featuresManifest, lyricsManifest, {
      featureDir: root,
      out: outPath2,
      analyzedAt: "2026-07-31T00:00:00.000Z",
      seed: 2026,
    }, { readDetail: async (recordingId) => detailFiles.get(recordingId) ?? null });
    assert.equal(await readFile(outPath2, "utf8"), written);
  });
});

test("journeys dry-run reads only committed manifests and reports eligibility", async () => {
  await withTempDir(async (root) => {
    const { featureRows, clusterIds } = journeyFixture(root);
    const featuresPath = path.join(root, "features.json");
    const lyricsPath = path.join(root, "lyrics.json");
    await writeFile(featuresPath, JSON.stringify({ schemaVersion: "voice-journey.local-voice-features.v1", results: featureRows }), "utf8");
    await writeFile(lyricsPath, JSON.stringify({
      schemaVersion: "voice-journey.local-lyric-indicators.v1",
      clusters: [{ clusterId: "cluster-improving", label: "recurring_lyric_cluster_A", recordingIds: clusterIds.improving }],
      results: [],
    }), "utf8");
    const { code, stdout } = await captureRun(["analyze", "--features", featuresPath, "--lyrics", lyricsPath, "--feature-dir", path.join(root, "missing"), "--dry-run"]);
    const payload = JSON.parse(stdout);
    assert.equal(code, 0);
    assert.equal(payload.operation, "same-song-journey-analysis");
    assert.equal(payload.scope.reliableTakes, 16);
    assert.equal(payload.scope.clustersEligible, 1);
    assert.equal(payload.readScope.audioBytes, false);
  });
});
