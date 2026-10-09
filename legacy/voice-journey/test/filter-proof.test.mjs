import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import { analyzeDryRun, analyzeIndexDryRun, analyzeIndexManifest, buildResultsManifest, classifyFeatures, run, selectSample } from "../src/filter-proof.mjs";

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
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-filter-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function fakeIndex() {
  const recordings = [];
  for (const year of ["2019", "2020", "2021"]) {
    for (const [suffix, sizeBytes] of [["A", 100], ["B", 500], ["C", 900]]) {
      recordings.push({
        recordingId: `${year}-${suffix}`,
        filename: `${year}0101 010203-${suffix}.m4a`,
        capturedAt: `${year}-01-01T01:02:03Z`,
        file: { sizeBytes, extension: ".m4a", modifiedAt: "2026-01-01T00:00:00.000Z" },
        sourceRef: { seam: "voice-journey-corpus", selector: `${year}-${suffix}` },
        contentClassification: { singingStatus: "unknown" },
      });
    }
  }
  return { schemaVersion: "voice-journey.recording-index.v1", totals: { recordings: recordings.length }, recordings };
}

function twoRecordingIndex() {
  const index = fakeIndex();
  const recordings = index.recordings.slice(0, 2);
  return { ...index, totals: { recordings: recordings.length }, recordings };
}

test("selectSample chooses year and size representatives without audio", () => {
  const sample = selectSample(fakeIndex(), { generatedAt: "2026-07-16T00:00:00.000Z", max: 6, perYear: 2 });
  assert.equal(sample.schemaVersion, "voice-journey.filtering-sample.v1");
  assert.equal(sample.sample.length, 6);
  assert.deepEqual(sample.sample.map((entry) => entry.recordingId), [
    "2019-A",
    "2019-C",
    "2020-A",
    "2020-C",
    "2021-A",
    "2021-C",
  ]);
  assert.equal(sample.selectionStrategy.audioBytesRead, false);
  assert.equal(sample.releaseGateRequiredForAnalysis, true);
});

test("classifyFeatures covers non-singing, clean singing, and contamination buckets", () => {
  assert.equal(classifyFeatures({ activeRatio: 0.01, voicedRatio: 0, meanPitchConfidence: 0, pitchStability: null, meanZeroCrossingRate: 0.01, clippingRatio: 0 }).finalBucket, "non_singing");
  assert.equal(classifyFeatures({ activeRatio: 0.5, voicedRatio: 0.5, meanPitchConfidence: 0.7, pitchStability: 0.2, meanZeroCrossingRate: 0.08, clippingRatio: 0 }).finalBucket, "clean_singing");
  assert.equal(classifyFeatures({ activeRatio: 0.5, voicedRatio: 0.5, meanPitchConfidence: 0.45, pitchStability: 0.2, meanZeroCrossingRate: 0.35, clippingRatio: 0 }).finalBucket, "noise_contaminated_singing");
  assert.equal(classifyFeatures({ activeRatio: 0.95, voicedRatio: 0.9, meanPitchConfidence: 0.75, pitchStability: 0.03, meanZeroCrossingRate: 0.08, clippingRatio: 0 }).finalBucket, "music_contaminated_singing");
});

test("analyze dry-run reports sample-only audio read scope", () => {
  const sample = selectSample(fakeIndex(), { generatedAt: "2026-07-16T00:00:00.000Z", max: 2, perYear: 1 });
  const dryRun = analyzeDryRun(sample, { corpusRoot: "/tmp/corpus", ffmpegBin: "/opt/homebrew/bin/ffmpeg", out: "manifests/filter-results.json" });
  assert.equal(dryRun.sampleCount, 2);
  assert.equal(dryRun.readScope.sampleRecordingsOnly, true);
  assert.equal(dryRun.readScope.audioBytes, true);
  assert.equal(dryRun.readScope.retainedAudioBytes, false);
  assert.equal(dryRun.readScope.waveformImages, false);
  assert.equal(dryRun.readScope.upload, false);
});

test("select-sample command writes a repo-safe manifest", async () => {
  await withTempDir(async (root) => {
    const indexPath = path.join(root, "index.json");
    const out = path.join(root, "sample.json");
    await writeFile(indexPath, JSON.stringify(fakeIndex()), "utf8");
    const { code } = await captureRun(["select-sample", "--index", indexPath, "--out", out, "--generated-at", "2026-07-16T00:00:00.000Z", "--max", "4"]);
    assert.equal(code, 0);
    const manifest = JSON.parse(await readFile(out, "utf8"));
    assert.equal(manifest.sample.length, 4);
    assert.equal(manifest.selectionStrategy.audioBytesRead, false);
  });
});

test("results manifest records tool choices and no retained audio", () => {
  const sample = selectSample(fakeIndex(), { generatedAt: "2026-07-16T00:00:00.000Z", max: 1, perYear: 1 });
  const manifest = buildResultsManifest(sample, [], { generatedAt: "2026-07-16T00:00:00.000Z", approval: "release-gate-test", toolVersions: { ffmpeg: "ffmpeg version test" } });
  assert.equal(manifest.schemaVersion, "voice-journey.filter-results.v1");
  assert.equal(manifest.readScope.audioBytes, true);
  assert.equal(manifest.readScope.retainedAudioBytes, false);
  assert.ok(manifest.toolChoices.some((tool) => tool.name === "ffmpeg" && tool.openSource && tool.version === "ffmpeg version test"));
  assert.ok(manifest.toolChoices.some((tool) => tool.name === "afinfo/afconvert" && !tool.openSource));
});

test("analyze-index dry-run reports full-corpus resumable scope", () => {
  const dryRun = analyzeIndexDryRun(twoRecordingIndex(), { corpusRoot: "/tmp/corpus", ffmpegBin: "/opt/homebrew/bin/ffmpeg", out: "manifests/full-corpus-filter-results.json" });
  assert.equal(dryRun.operation, "full-corpus-classification");
  assert.equal(dryRun.recordingCount, 2);
  assert.equal(dryRun.readScope.allIndexedRecordings, true);
  assert.equal(dryRun.readScope.audioBytes, true);
  assert.equal(dryRun.readScope.retainedAudioBytes, false);
  assert.equal(dryRun.resumeBehavior.enabled, true);
  assert.equal(dryRun.resumeBehavior.key, "recordingId");
  assert.ok(dryRun.extractedFeatures.includes("frameCount"));
});

test("analyze-index resumes by skipping completed recordingIds", async () => {
  await withTempDir(async (root) => {
    const index = twoRecordingIndex();
    const out = path.join(root, "full-results.json");
    await writeFile(out, JSON.stringify({
      schemaVersion: "voice-journey.full-corpus-filter-results.v1",
      results: [{
        recordingId: index.recordings[0].recordingId,
        filename: index.recordings[0].filename,
        classification: { finalBucket: "non_singing" },
      }],
    }), "utf8");

    const analyzed = [];
    const manifest = await analyzeIndexManifest(index, {
      approval: "release-gate-test",
      corpusRoot: "/tmp/corpus",
      ffmpegBin: "/opt/homebrew/bin/ffmpeg",
      out,
    }, {
      toolVersions: { ffmpeg: "ffmpeg version test" },
      analyzeRecording: async (entry) => {
        analyzed.push(entry.recordingId);
        return {
          recordingId: entry.recordingId,
          filename: entry.filename,
          capturedAt: entry.capturedAt,
          year: entry.capturedAt.slice(0, 4),
          sourceRef: entry.sourceRef,
          features: { durationSeconds: 1, activeRatio: 0.01, voicedRatio: 0, meanRmsDb: -40, meanZeroCrossingRate: 0.01, meanPitchHz: null, meanPitchConfidence: 0, pitchStability: null, clippingRatio: 0, frameCount: 1 },
          classification: classifyFeatures({ activeRatio: 0.01, voicedRatio: 0, meanPitchConfidence: 0, pitchStability: null, meanZeroCrossingRate: 0.01, clippingRatio: 0 }),
          humanSpotCheck: { recommended: false, status: "not_reviewed", reviewedBy: null, notes: null },
        };
      },
    });

    assert.deepEqual(analyzed, [index.recordings[1].recordingId]);
    assert.equal(manifest.results.length, 2);
    assert.equal(manifest.resume.skippedCompletedCount, 1);
    assert.equal(manifest.resume.completedCount, 2);
    assert.equal(manifest.resume.remainingCount, 0);
    const written = JSON.parse(await readFile(out, "utf8"));
    assert.equal(written.results.length, 2);
    assert.equal(written.readScope.retainedAudioBytes, false);
  });
});
