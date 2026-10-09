import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

import { buildFeaturesDryRun, extractIndexManifest, run, selectRecordings } from "../src/features.mjs";

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

async function withLocalArtifactDir(callback) {
  await mkdir(path.join(process.cwd(), "local-artifacts"), { recursive: true });
  const root = await mkdtemp(path.join(process.cwd(), "local-artifacts", "features-test-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function recording(recordingId, filename, capturedAt) {
  return {
    recordingId,
    filename,
    capturedAt,
    sourceRef: { seam: "voice-journey-corpus", selector: recordingId, filename },
  };
}

function fakeIndex() {
  return {
    schemaVersion: "voice-journey.recording-index.v1",
    generatedAt: "2026-07-16T00:00:00.000Z",
    totals: { recordings: 3 },
    recordings: [
      recording("a", "20210101 000000-A.m4a", "2021-01-01T00:00:00Z"),
      recording("b", "20220101 000000-B.m4a", "2022-01-01T00:00:00Z"),
      recording("c", "20230101 000000-C.m4a", "2023-01-01T00:00:00Z"),
    ],
  };
}

function fakeResults() {
  return {
    schemaVersion: "voice-journey.full-corpus-filter-results.v1",
    generatedAt: "2026-07-16T00:00:00.000Z",
    updatedAt: "2026-07-30T00:00:00.000Z",
    results: [
      { recordingId: "a", classification: { finalBucket: "clean_singing" } },
      { recordingId: "b", classification: { finalBucket: "clean_singing" } },
      { recordingId: "c", classification: { finalBucket: "non_singing" } },
    ],
  };
}

function fakeSummary(recordingId) {
  return {
    durationSeconds: 42,
    voicing: { voicedShare: 0.7, frameCount: 4200 },
    pitch: { f0Hz: { p05: 180, p25: 210, p50: 240, p75: 280, p95: 330 }, rangeSemitonesP05P95: 10.5, madSemitones: 1.2 },
    tuning: { offsetCents: -12.5, medianAbsCentError: 18.2, within25CentsShare: 0.64 },
    vibrato: { sustainedSegmentCount: 4, vibratoSegmentCount: 2, meanRateHz: 5.4, meanExtentCents: 32.1, vibratoTimeShare: 0.41 },
    quality: { jitterLocal: 0.012, shimmerLocal: 0.08, meanHnrDb: 14.2, cpps: 11.8 },
    spectral: { centroidHzMean: 1450.2, centroidHzSd: 310.5, rolloffHzMean: 2900.1, singerFormantRatio: 0.041 },
    dynamics: { voicedRmsDbP10: -32.1, voicedRmsDbP50: -24.6, voicedRmsDbP90: -18.9, dynamicSpreadDb: 13.2 },
    phrasing: { longestSustainedSeconds: 6.4, meanVoicedSegmentSeconds: 1.8, pauseRatePerMinute: 9.5 },
    marker: recordingId,
  };
}

function fakeWorkerHooks(extractLog, { failIds = new Set() } = {}) {
  return {
    toolVersions: { ffmpeg: "ffmpeg fixture" },
    createWorker: async () => ({
      versions: { python: "3.12-fixture", parselmouth: "0.4.5-fixture", librosa: "0.11-fixture" },
      config: { methodId: "voice-journey.feature-extract.v1", fixture: true },
      extract: async (task) => {
        extractLog.push(task.recordingId);
        if (failIds.has(task.recordingId)) {
          return { recordingId: task.recordingId, status: "failed", error: "FixtureError: synthetic failure" };
        }
        return {
          recordingId: task.recordingId,
          status: "completed",
          summary: fakeSummary(task.recordingId),
          detail: { methodId: "voice-journey.feature-extract.v1", f0ContourVoiced50ms: [[0, 240.1]], vibratoSegments: [], config: {} },
        };
      },
      close: async () => {},
    }),
    decodeRecording: async (entry) => ({ wavPath: `/fixture/${entry.recordingId}.wav`, cleanup: async () => {} }),
  };
}

function baseOptions(root, overrides = {}) {
  return {
    approval: "release-gate-test",
    buckets: "clean_singing",
    corpusRoot: "/tmp/corpus",
    featureDir: path.join(root, "features"),
    ffmpegBin: "/opt/homebrew/bin/ffmpeg",
    generatedAt: "2026-07-30T00:00:00.000Z",
    index: "unused",
    limit: null,
    out: path.join(root, "voice-features.json"),
    results: "unused",
    sampleRate: 22050,
    venvPython: "analysis/.venv/bin/python",
    worker: "analysis/extract_features.py",
    ...overrides,
  };
}

test("selectRecordings filters by bucket and honors limit", () => {
  const selected = selectRecordings(fakeIndex(), fakeResults(), { buckets: "clean_singing", limit: null });
  assert.deepEqual(selected.map((item) => item.entry.recordingId), ["a", "b"]);
  const limited = selectRecordings(fakeIndex(), fakeResults(), { buckets: "clean_singing,non_singing", limit: 2 });
  assert.deepEqual(limited.map((item) => item.entry.recordingId), ["a", "b"]);
});

test("features dry-run reports selection and scope without reading audio", async () => {
  await withLocalArtifactDir(async (root) => {
    const indexPath = path.join(root, "index.json");
    const resultsPath = path.join(root, "results.json");
    await writeFile(indexPath, JSON.stringify(fakeIndex()), "utf8");
    await writeFile(resultsPath, JSON.stringify(fakeResults()), "utf8");
    const { code, stdout } = await captureRun(["extract-index", "--index", indexPath, "--results", resultsPath, "--dry-run"]);
    const payload = JSON.parse(stdout);
    assert.equal(code, 0);
    assert.equal(payload.operation, "voice-feature-extraction");
    assert.equal(payload.selection.selectedCount, 2);
    assert.deepEqual(payload.selection.byBucket, { clean_singing: 2 });
    assert.equal(payload.readScope.audioBytes, true);
    assert.equal(payload.readScope.featureArraysCommittedToRepo, false);
    assert.equal(payload.resumeBehavior.failedRowsRetriedOnRerun, true);
  });
});

test("buildFeaturesDryRun widens selection with buckets flag", () => {
  const payload = buildFeaturesDryRun(fakeIndex(), fakeResults(), {
    buckets: "clean_singing,non_singing",
    limit: null,
    out: "out.json",
    featureDir: "local-artifacts/voice-features",
    ffmpegBin: "ffmpeg",
    venvPython: "py",
    worker: "worker.py",
    sampleRate: 22050,
  });
  assert.equal(payload.selection.selectedCount, 3);
  assert.equal(payload.selection.byBucket.non_singing, 1);
});

test("extract-index writes local detail files, repo-safe rows, and continues past failures", async () => {
  await withLocalArtifactDir(async (root) => {
    const extractLog = [];
    const hooks = fakeWorkerHooks(extractLog, { failIds: new Set(["b"]) });
    const options = baseOptions(root);
    const manifest = await extractIndexManifest(fakeIndex(), fakeResults(), options, hooks);

    assert.deepEqual(extractLog, ["a", "b"]);
    assert.equal(manifest.results.length, 2);
    const rowA = manifest.results.find((row) => row.recordingId === "a");
    const rowB = manifest.results.find((row) => row.recordingId === "b");
    assert.equal(rowA.status, "completed");
    assert.equal(rowA.features.pitch.rangeSemitonesP05P95, 10.5);
    assert.equal(rowA.localArtifacts.arraysIncludedInManifest, false);
    assert.match(rowA.featuresDigest, /^[0-9a-f]{64}$/u);
    assert.equal(rowB.status, "failed");
    assert.match(rowB.error, /FixtureError/u);
    assert.equal(rowB.featuresDigest, null);
    assert.equal(manifest.resume.completedCount, 1);
    assert.equal(manifest.resume.failedCount, 1);

    const detail = JSON.parse(await readFile(path.join(options.featureDir, "a.json"), "utf8"));
    assert.equal(detail.detail.f0ContourVoiced50ms.length, 1);

    const written = await readFile(options.out, "utf8");
    assert.doesNotMatch(written, /f0ContourVoiced50ms/u);
    assert.doesNotMatch(written, /vibratoSegments/u);
    assert.equal(JSON.parse(written).analysisConfig.configFingerprint, rowA.configFingerprint);
  });
});

test("extract-index resumes by skipping completed rows and retrying failed rows", async () => {
  await withLocalArtifactDir(async (root) => {
    const firstLog = [];
    const options = baseOptions(root);
    await extractIndexManifest(fakeIndex(), fakeResults(), options, fakeWorkerHooks(firstLog, { failIds: new Set(["b"]) }));
    assert.deepEqual(firstLog, ["a", "b"]);

    const secondLog = [];
    const manifest = await extractIndexManifest(fakeIndex(), fakeResults(), options, fakeWorkerHooks(secondLog));
    assert.deepEqual(secondLog, ["b"]);
    assert.equal(manifest.resume.skippedCompletedCount, 1);
    assert.equal(manifest.resume.completedCount, 2);
    assert.equal(manifest.resume.failedCount, 0);
    assert.equal(manifest.resume.remainingCount, 0);
  });
});

test("extract-index runs are byte-deterministic with a pinned generated-at", async () => {
  await withLocalArtifactDir(async (root) => {
    const optionsA = baseOptions(root, { out: path.join(root, "out-a.json") });
    const optionsB = baseOptions(root, { out: path.join(root, "out-b.json") });
    await extractIndexManifest(fakeIndex(), fakeResults(), optionsA, fakeWorkerHooks([]));
    await extractIndexManifest(fakeIndex(), fakeResults(), optionsB, fakeWorkerHooks([]));
    assert.equal(await readFile(optionsA.out, "utf8"), await readFile(optionsB.out, "utf8"));
  });
});

test("extract-index requires approval and corpus root for non-dry-run", async () => {
  await withLocalArtifactDir(async (root) => {
    await assert.rejects(
      () => extractIndexManifest(fakeIndex(), fakeResults(), baseOptions(root, { approval: null }), fakeWorkerHooks([])),
      /requires --approval/u,
    );
    await assert.rejects(
      () => extractIndexManifest(fakeIndex(), fakeResults(), baseOptions(root, { corpusRoot: null }), fakeWorkerHooks([])),
      /requires --corpus-root/u,
    );
  });
});
