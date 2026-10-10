import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

import { analyzeLyricIndicators, buildDryRun, repetitionSignal, run, tokenize, wordlessSignal } from "../src/lyric-indicators.mjs";

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
  const root = await mkdtemp(path.join(process.cwd(), "local-artifacts", "lyric-test-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function fakeTranscriptManifest() {
  return {
    schemaVersion: "voice-journey.local-stt-transcripts.v1",
    generatedAt: "2026-07-16T00:00:00.000Z",
    updatedAt: "2026-07-16T01:00:00.000Z",
    localTranscriptStore: { root: "local-artifacts/stt-transcripts", committedToRepo: false },
    results: [
      transcriptRow("a", "2019", 17, "en"),
      transcriptRow("b", "2020", 16, "en"),
      transcriptRow("c", "2021", 14, "en"),
      transcriptRow("d", "2022", 0, "en"),
      transcriptRow("e", "2023", 4, "ko"),
    ],
  };
}

function transcriptRow(recordingId, year, words, language) {
  return {
    recordingId,
    filename: `${year}0101 000000-${recordingId}.m4a`,
    capturedAt: `${year}-01-01T00:00:00Z`,
    year,
    status: "completed",
    language: { requested: "auto", detected: language, probability: null },
    counts: { words, segments: 1, characters: words * 5 },
    localArtifacts: {
      transcriptTextPath: `local-artifacts/stt-transcripts/${recordingId}.txt`,
      transcriptJsonPath: `local-artifacts/stt-transcripts/${recordingId}.json`,
      textIncludedInManifest: false,
    },
  };
}

test("lyric dry-run declares local-only scope without reading text", () => {
  const dryRun = buildDryRun(fakeTranscriptManifest(), {
    out: "manifests/local-lyric-indicators.json",
    transcriptDir: "local-artifacts/stt-transcripts",
    clusterThreshold: 0.22,
    minClusterSize: 2,
  });
  assert.equal(dryRun.operation, "local-lyric-indicators");
  assert.equal(dryRun.recordingCount, 5);
  assert.equal(dryRun.readScope.localTranscriptText, true);
  assert.equal(dryRun.readScope.localTranscriptTextReadInDryRun, false);
  assert.equal(dryRun.readScope.externalLyricDatabases, false);
  assert.equal(dryRun.strategy.rawLyricTextInOutput, false);
});

test("dry-run command succeeds even when transcript files are absent", async () => {
  await withLocalArtifactDir(async (root) => {
    const manifestPath = path.join(root, "stt.json");
    await writeFile(manifestPath, JSON.stringify(fakeTranscriptManifest()), "utf8");
    const { code, stdout } = await captureRun(["analyze", "--transcripts", manifestPath, "--dry-run"]);
    const payload = JSON.parse(stdout);
    assert.equal(code, 0);
    assert.equal(payload.dryRun, true);
    assert.equal(payload.readScope.localTranscriptTextReadInDryRun, false);
  });
});

test("analyze marks recurring local transcript clusters without raw lyric text", async () => {
  await withLocalArtifactDir(async (root) => {
    const transcriptDir = path.join(root, "transcripts");
    await mkdir(transcriptDir);
    const manifest = fakeTranscriptManifest();
    await writeFile(path.join(transcriptDir, "a.txt"), "silver river carries me home bright moon silver river carries me home", "utf8");
    await writeFile(path.join(transcriptDir, "b.txt"), "bright moon silver river carries me home tonight", "utf8");
    await writeFile(path.join(transcriptDir, "c.txt"), "plain spoken memo about errands and groceries", "utf8");
    await writeFile(path.join(transcriptDir, "d.txt"), "", "utf8");
    await writeFile(path.join(transcriptDir, "e.txt"), "la la vocal tone", "utf8");

    const output = await analyzeLyricIndicators(manifest, {
      clusterThreshold: 0.2,
      minClusterSize: 2,
      transcriptDir,
      generatedAt: "2026-07-16T00:00:00.000Z",
    });

    assert.equal(output.schemaVersion, "voice-journey.local-lyric-indicators.v1");
    assert.equal(output.readScope.externalLyricDatabases, false);
    assert.equal(output.privacy.containsRawLyricText, false);
    assert.equal(output.clusters.length, 1);
    assert.deepEqual(output.clusters[0].recordingIds, ["a", "b"]);
    assert.equal(output.results.find((row) => row.recordingId === "a").lyricMatch.matchedCluster, "lyric-cluster-001");
    assert.equal(output.results.find((row) => row.recordingId === "c").lyricMatch.marked, false);
    assert.equal(output.results.find((row) => row.recordingId === "d").lyricMatch.signals.wordlessVocalise.marked, true);
    assert.equal(output.results.find((row) => row.recordingId === "e").lyricMatch.signals.wordlessVocalise.marked, true);
    assert.doesNotMatch(JSON.stringify(output), /silver river|bright moon|groceries|vocal tone/u);
  });
});

test("repetition and wordless helpers expose indicator signals", () => {
  const tokens = tokenize("turn around sing it again turn around sing it again turn around sing it again");
  const repetition = repetitionSignal(tokens);
  assert.equal(repetition.marked, true);
  assert.ok(repetition.repeatedNgramCount > 0);
  assert.deepEqual(wordlessSignal({ counts: { words: 0 }, language: { detected: "en" } }).marked, true);
  assert.deepEqual(wordlessSignal({ counts: { words: 4 }, language: { detected: "haw" } }).marked, true);
  assert.deepEqual(wordlessSignal({ counts: { words: 4 }, language: { detected: "en" } }).marked, false);
});

test("analyze command writes repo-safe manifest", async () => {
  await withLocalArtifactDir(async (root) => {
    const manifestPath = path.join(root, "stt.json");
    const out = path.join(root, "lyric.json");
    const transcriptDir = path.join(root, "transcripts");
    await mkdir(transcriptDir);
    const manifest = fakeTranscriptManifest();
    await writeFile(manifestPath, JSON.stringify(manifest), "utf8");
    for (const row of manifest.results) await writeFile(path.join(transcriptDir, `${row.recordingId}.txt`), "same local refrain same local refrain same local refrain", "utf8");

    const { code } = await captureRun(["analyze", "--transcripts", manifestPath, "--transcript-dir", transcriptDir, "--out", out, "--cluster-threshold", "0.2"]);
    assert.equal(code, 0);
    const written = JSON.parse(await readFile(out, "utf8"));
    assert.equal(written.privacy.containsTranscriptText, false);
    assert.equal(written.readScope.audioBytes, false);
    assert.equal(written.readScope.upload, false);
    assert.doesNotMatch(JSON.stringify(written), /same local refrain/u);
  });
});
