import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

import { buildSttDryRun, normalizeTranscript, run, transcribeIndexManifest } from "../src/local-stt.mjs";

function whisperSegment(text, fromMs, toMs) {
  return {
    text,
    offsets: { from: fromMs, to: toMs },
    timestamps: { from: msToCommaTimestamp(fromMs), to: msToCommaTimestamp(toMs) },
  };
}

function msToCommaTimestamp(ms) {
  const seconds = Math.floor(ms / 1000);
  const pad = (value, width = 2) => String(value).padStart(width, "0");
  return `${pad(Math.floor(seconds / 3600))}:${pad(Math.floor((seconds % 3600) / 60))}:${pad(seconds % 60)},${pad(ms % 1000, 3)}`;
}

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
  const root = await mkdtemp(path.join(process.cwd(), "local-artifacts", "stt-test-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function fakeIndex() {
  return {
    schemaVersion: "voice-journey.recording-index.v1",
    generatedAt: "2026-07-16T00:00:00.000Z",
    totals: { recordings: 2 },
    recordings: [
      recording("a", "20190101 000000-A.m4a", "2019-01-01T00:00:00Z"),
      recording("b", "20200101 000000-B.m4a", "2020-01-01T00:00:00Z"),
    ],
  };
}

function recording(recordingId, filename, capturedAt) {
  return {
    recordingId,
    filename,
    capturedAt,
    sourceRef: { seam: "voice-journey-corpus", selector: recordingId, filename },
  };
}

test("STT dry-run reports full-corpus scope without reading audio", () => {
  const dryRun = buildSttDryRun(fakeIndex(), {
    corpusRoot: "/tmp/corpus",
    ffmpegBin: "/opt/homebrew/bin/ffmpeg",
    language: "auto",
    model: "/opt/homebrew/opt/whisper-cpp/share/whisper-cpp/models/ggml-medium.bin",
    out: "manifests/local-stt-transcripts.json",
    transcriptDir: "local-artifacts/stt-transcripts",
    whisperBin: "/opt/homebrew/bin/whisper-cli",
  });
  assert.equal(dryRun.operation, "local-stt-transcription");
  assert.equal(dryRun.recordingCount, 2);
  assert.equal(dryRun.readScope.audioBytes, true);
  assert.equal(dryRun.readScope.transcriptTextCommittedToRepo, false);
  assert.equal(dryRun.sttTool.name, "whisper.cpp");
  assert.equal(dryRun.resumeBehavior.key, "recordingId");
});

test("STT dry-run command prints repo-safe scope", async () => {
  await withLocalArtifactDir(async (root) => {
    const indexPath = path.join(root, "index.json");
    await writeFile(indexPath, JSON.stringify(fakeIndex()), "utf8");
    const { code, stdout } = await captureRun(["transcribe-index", "--index", indexPath, "--dry-run"]);
    const payload = JSON.parse(stdout);
    assert.equal(code, 0);
    assert.equal(payload.dryRun, true);
    assert.equal(payload.recordingCount, 2);
    assert.equal(payload.readScope.upload, false);
    assert.equal(payload.localTranscriptDir, "local-artifacts/stt-transcripts");
  });
});

test("normalizeTranscript extracts aggregate metadata without requiring text in manifest", () => {
  const normalized = normalizeTranscript({
    result: { language: "en", language_probability: 0.91 },
    transcription: [
      { text: "hello world", timestamps: { from: "00:00:00.000", to: "00:00:02.000" }, avg_logprob: -0.2, no_speech_prob: 0.01, compression_ratio: 1.1 },
      { text: "again", timestamps: { from: "00:00:02.000", to: "00:00:03.000" }, avg_logprob: -0.3, no_speech_prob: 0.02, compression_ratio: 1.2 },
    ],
  }, { language: "auto" });
  assert.equal(normalized.language.detected, "en");
  assert.equal(normalized.durationProcessedSeconds, 3);
  assert.equal(normalized.counts.words, 3);
  assert.equal(normalized.confidenceStats.meanNoSpeechProbability, 0.015);
  assert.equal(normalized.derivedFeatures.hasTranscribedWords, true);
});

test("normalizeTranscript parses whisper.cpp comma timestamps and prefers millisecond offsets", () => {
  const commaOnly = normalizeTranscript({
    result: { language: "en" },
    transcription: [
      { text: "one two three", timestamps: { from: "00:00:00,000", to: "00:01:05,500" } },
    ],
  }, { language: "auto" });
  assert.equal(commaOnly.durationProcessedSeconds, 65.5);
  assert.equal(commaOnly.derivedFeatures.wordsPerMinute, 2.75);
  assert.equal(commaOnly.derivedFeatures.segmentDensityPerMinute, 0.92);

  const offsetsWin = normalizeTranscript({
    result: { language: "en" },
    transcription: [
      { text: "la la", offsets: { from: 0, to: 15500 }, timestamps: { from: "00:00:00,000", to: "00:00:99,000" } },
    ],
  }, { language: "auto" });
  assert.equal(offsetsWin.durationProcessedSeconds, 15.5);
});

test("backfill-manifest fills durations and rates from local transcript JSONs without re-running STT", async () => {
  await withLocalArtifactDir(async (root) => {
    const transcriptDir = path.join(root, "transcripts");
    await mkdir(transcriptDir, { recursive: true });
    await writeFile(path.join(transcriptDir, "a.json"), JSON.stringify({
      result: { language: "en" },
      transcription: [
        whisperSegment("silver river fixture phrase", 0, 20000),
        whisperSegment("silver river fixture phrase again", 20000, 30000),
      ],
    }), "utf8");
    const manifestPath = path.join(root, "stt-manifest.json");
    const outPath = path.join(root, "stt-manifest-backfilled.json");
    await writeFile(manifestPath, JSON.stringify({
      schemaVersion: "voice-journey.local-stt-transcripts.v1",
      updatedAt: "2026-07-16T00:00:00.000Z",
      repoSafeManifest: { containsTranscriptText: false },
      results: [
        {
          recordingId: "a",
          status: "completed",
          durationProcessedSeconds: null,
          confidenceStats: { meanLogProbability: null, meanNoSpeechProbability: null, meanCompressionRatio: null },
          counts: { words: 30, segments: 2, characters: 100 },
          derivedFeatures: { wordsPerMinute: null, averageWordsPerSegment: 15, segmentDensityPerMinute: null, hasTranscribedWords: true },
        },
        {
          recordingId: "b",
          status: "completed",
          durationProcessedSeconds: null,
          confidenceStats: { meanLogProbability: null, meanNoSpeechProbability: null, meanCompressionRatio: null },
          counts: { words: 5, segments: 1, characters: 20 },
          derivedFeatures: { wordsPerMinute: null, averageWordsPerSegment: 5, segmentDensityPerMinute: null, hasTranscribedWords: true },
        },
      ],
    }), "utf8");

    const first = await captureRun([
      "backfill-manifest",
      "--manifest", manifestPath,
      "--transcript-dir", transcriptDir,
      "--out", outPath,
      "--backfilled-at", "2026-07-30T00:00:00.000Z",
    ]);
    assert.equal(first.code, 0);
    const written = JSON.parse(await readFile(outPath, "utf8"));
    const rowA = written.results.find((row) => row.recordingId === "a");
    assert.equal(rowA.durationProcessedSeconds, 30);
    assert.equal(rowA.derivedFeatures.wordsPerMinute, 60);
    assert.equal(rowA.derivedFeatures.segmentDensityPerMinute, 4);
    assert.equal(rowA.derivedFeatures.averageWordsPerSegment, 15);
    assert.deepEqual(rowA.confidenceStats, { meanLogProbability: null, meanNoSpeechProbability: null, meanCompressionRatio: null });
    const rowB = written.results.find((row) => row.recordingId === "b");
    assert.equal(rowB.durationProcessedSeconds, null);
    assert.equal(written.durationBackfill.rowsExamined, 2);
    assert.equal(written.durationBackfill.rowsUpdatedDuration, 1);
    assert.equal(written.durationBackfill.rowsMissingTranscriptJson, 1);
    assert.deepEqual(written.durationBackfill.missingRecordingIds, ["b"]);
    assert.equal(written.durationBackfill.transcriptTextWrittenToManifest, false);
    assert.equal(written.updatedAt, "2026-07-30T00:00:00.000Z");
    assert.doesNotMatch(JSON.stringify(written), /silver river fixture/u);

    const firstBytes = await readFile(outPath, "utf8");
    const outPath2 = path.join(root, "stt-manifest-backfilled-2.json");
    const second = await captureRun([
      "backfill-manifest",
      "--manifest", manifestPath,
      "--transcript-dir", transcriptDir,
      "--out", outPath2,
      "--backfilled-at", "2026-07-30T00:00:00.000Z",
    ]);
    assert.equal(second.code, 0);
    assert.equal(await readFile(outPath2, "utf8"), firstBytes);
  });
});

test("backfill-manifest dry-run reads only the manifest and reports null-field scope", async () => {
  await withLocalArtifactDir(async (root) => {
    const manifestPath = path.join(root, "stt-manifest.json");
    await writeFile(manifestPath, JSON.stringify({
      schemaVersion: "voice-journey.local-stt-transcripts.v1",
      results: [
        {
          recordingId: "a",
          status: "completed",
          durationProcessedSeconds: null,
          confidenceStats: { meanLogProbability: null, meanNoSpeechProbability: null, meanCompressionRatio: null },
          derivedFeatures: { wordsPerMinute: null, segmentDensityPerMinute: null },
        },
      ],
    }), "utf8");
    const { code, stdout } = await captureRun([
      "backfill-manifest",
      "--manifest", manifestPath,
      "--transcript-dir", path.join(root, "missing-dir"),
      "--dry-run",
    ]);
    const payload = JSON.parse(stdout);
    assert.equal(code, 0);
    assert.equal(payload.dryRun, true);
    assert.equal(payload.operation, "local-stt-duration-backfill");
    assert.equal(payload.rows.total, 1);
    assert.equal(payload.rows.durationProcessedSecondsNull, 1);
    assert.equal(payload.rows.confidenceStatsAllNull, 1);
    assert.equal(payload.readScope.audioBytes, false);
    assert.equal(payload.method.rerunsStt, false);
  });
});

test("transcribe-index resumes by skipping completed recordingIds and keeps text local", async () => {
  await withLocalArtifactDir(async (root) => {
    const index = fakeIndex();
    const out = path.join(root, "stt-manifest.json");
    const transcriptDir = path.join(root, "transcripts");
    await writeFile(out, JSON.stringify({
      schemaVersion: "voice-journey.local-stt-transcripts.v1",
      results: [{
        recordingId: "a",
        filename: "20190101 000000-A.m4a",
        status: "completed",
        localArtifacts: { transcriptTextPath: "local-artifacts/stt-test/a.txt", textIncludedInManifest: false },
      }],
    }), "utf8");

    const analyzed = [];
    const manifest = await transcribeIndexManifest(index, {
      approval: "release-gate-test",
      corpusRoot: "/tmp/corpus",
      ffmpegBin: "/opt/homebrew/bin/ffmpeg",
      language: "auto",
      model: "/opt/homebrew/opt/whisper-cpp/share/whisper-cpp/models/ggml-medium.bin",
      out,
      transcriptDir,
      whisperBin: "/opt/homebrew/bin/whisper-cli",
    }, {
      toolVersions: { ffmpeg: "ffmpeg fixture", whisperCpp: "whisper.cpp fixture" },
      transcribeRecording: async (entry) => {
        analyzed.push(entry.recordingId);
        return {
          transcriptText: "secret lyric phrase stays local",
          rawTranscript: {
            text: "secret lyric phrase stays local",
            language: "en",
            durationSeconds: 4,
            segments: [{ text: "secret lyric phrase stays local", start: 0, end: 4, avg_logprob: -0.1, no_speech_prob: 0.01, compression_ratio: 1.05 }],
          },
        };
      },
    });

    assert.deepEqual(analyzed, ["b"]);
    assert.equal(manifest.results.length, 2);
    assert.equal(manifest.resume.skippedCompletedCount, 1);
    assert.equal(manifest.resume.completedCount, 2);
    assert.equal(manifest.resume.remainingCount, 0);
    const written = JSON.parse(await readFile(out, "utf8"));
    assert.equal(written.repoSafeManifest.containsTranscriptText, false);
    assert.doesNotMatch(JSON.stringify(written), /secret lyric phrase/u);
    assert.match(await readFile(path.join(transcriptDir, "b.txt"), "utf8"), /secret lyric phrase stays local/u);
  });
});
