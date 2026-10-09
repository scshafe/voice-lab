import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  bradleyTerryByYear,
  buildCorpusRows,
  loudnessMatchGainsDb,
  buildRefereePairs,
  buildVoiceTrends,
  closeServers,
  filterRows,
  mergeVerdictsIntoResults,
  paginateRows,
  projectRows,
  run,
  selectReviewQueue,
  sortRows,
  startServers,
} from "../src/corpus-browser.mjs";

function featureRow(recordingId, capturedAt, f0, overrides = {}) {
  return {
    recordingId,
    filename: `${recordingId}.m4a`,
    capturedAt,
    year: capturedAt.slice(0, 4),
    status: overrides.status ?? "completed",
    features: overrides.status === "failed" ? null : {
      durationSeconds: overrides.durationSeconds ?? 60,
      voicing: {
        voicedShare: 0.7,
        frameCount: 6000,
        rejectedOutlierShare: overrides.rejectedOutlierShare ?? 0.01,
        rejectedGlobalOutlierShare: overrides.rejectedGlobalOutlierShare ?? 0,
      },
      pitch: { f0Hz: { p05: f0.p05, p25: f0.p25, p50: f0.p50, p75: f0.p75, p95: f0.p95 }, rangeSemitonesP05P95: 11, madSemitones: 1.1 },
      tuning: { offsetCents: 0, medianAbsCentError: 21, within25CentsShare: 0.55 },
      vibrato: { sustainedSegmentCount: 3, vibratoSegmentCount: 1, meanRateHz: overrides.vibRate ?? 4.1, meanExtentCents: overrides.vibExtent ?? 45, vibratoTimeShare: 0.3 },
      quality: { jitterLocal: 0.01, shimmerLocal: 0.07, meanHnrDb: overrides.hnr ?? 15, cpps: overrides.cpps ?? 11 },
      spectral: { centroidHzMean: 1400, centroidHzSd: 300, rolloffHzMean: 2800, singerFormantRatio: 0.04 },
      dynamics: { voicedRmsDbP10: -32, voicedRmsDbP50: -25, voicedRmsDbP90: -19, dynamicSpreadDb: 13 },
      phrasing: { longestSustainedSeconds: overrides.sustain ?? 2.8, meanVoicedSegmentSeconds: 1.7, pauseRatePerMinute: 9 },
    },
  };
}

function fakeFeatures() {
  const results = [];
  for (let i = 0; i < 12; i += 1) {
    const month = String((i % 6) + 1).padStart(2, "0");
    results.push(featureRow(`y19-${i}`, `2019-${month}-10T00:00:00Z`, { p05: 90, p25: 110, p50: 150, p75: 190, p95: 260 }));
    results.push(featureRow(`y21-${i}`, `2021-${month}-10T00:00:00Z`, { p05: 100, p25: 150, p50: 310, p75: 350, p95: 390 }));
  }
  results.push(featureRow("flagged", "2021-02-11T00:00:00Z", { p05: 80, p25: 120, p50: 200, p75: 500, p95: 900 }, { rejectedOutlierShare: 0.2 }));
  results.push(featureRow("failed", "2021-03-11T00:00:00Z", {}, { status: "failed" }));
  return {
    schemaVersion: "voice-journey.local-voice-features.v1",
    generatedAt: "2026-07-31T00:00:00.000Z",
    updatedAt: "2026-07-31T00:00:00.000Z",
    results,
  };
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

async function withTempDir(callback) {
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-browser-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function fakeIndex() {
  return {
    schemaVersion: "voice-journey.recording-index.v1",
    totals: { recordings: 7 },
    recordings: [
      recording("a", "20190101 000000-A.m4a", "2019-01-01T00:00:00Z", 1000),
      recording("b", "20200101 000000-B.m4a", "2020-01-01T00:00:00Z", 2000),
      recording("c", "20200201 000000-C.m4a", "2020-02-01T00:00:00Z", 3000),
      recording("d", "20210101 000000-D.m4a", "2021-01-01T00:00:00Z", 4000),
      recording("e", "20220101 000000-E.m4a", "2022-01-01T00:00:00Z", 5000),
      recording("f", "20230101 000000-F.m4a", "2023-01-01T00:00:00Z", 6000),
      recording("g", "20240101 000000-G.m4a", "2024-01-01T00:00:00Z", 7000),
    ],
  };
}

function recording(recordingId, filename, capturedAt, sizeBytes) {
  return { recordingId, filename, capturedAt, durationSeconds: null, file: { sizeBytes } };
}

function fakeResults() {
  return {
    schemaVersion: "voice-journey.full-corpus-filter-results.v1",
    results: [
      result("a", "20190101 000000-A.m4a", "2019", 10, "clean_singing", 0.91, "not_reviewed"),
      result("b", "20200101 000000-B.m4a", "2020", 20, "uncertain_manual_review", 0.42, "not_reviewed"),
      result("c", "20200201 000000-C.m4a", "2020", 30, "non_singing", 0.8, "reviewed"),
      result("d", "20210101 000000-D.m4a", "2021", 40, "uncertain_manual_review", 0.31, "not_reviewed"),
      result("e", "20220101 000000-E.m4a", "2022", 50, "noise_contaminated_singing", 0.72, "not_reviewed"),
      result("f", "20230101 000000-F.m4a", "2023", 60, "music_contaminated_singing", 0.64, "not_reviewed"),
      result("g", "20240101 000000-G.m4a", "2024", 70, "clean_singing", 0.88, "not_reviewed"),
    ],
  };
}

function result(recordingId, filename, year, durationSeconds, bucket, confidence, spotCheckStatus) {
  return {
    recordingId,
    filename,
    capturedAt: `${year}-01-01T00:00:00Z`,
    year,
    features: { durationSeconds },
    classification: { finalBucket: bucket, confidence, rationale: `${bucket} rationale` },
    humanSpotCheck: { recommended: bucket === "uncertain_manual_review", status: spotCheckStatus, reviewedBy: null, notes: null },
  };
}

function fakeState() {
  return {
    schemaVersion: "voice-journey.corpus-browser-state.v1",
    updatedAt: "2026-07-16T00:00:00.000Z",
    verdicts: [{ recordingId: "b", verdict: "clean_singing", note: "fixture", reviewedBy: "tester", reviewedAt: "2026-07-16T00:00:00.000Z", source: "corpus-browser" }],
  };
}

function fakeTranscripts() {
  return {
    schemaVersion: "voice-journey.local-stt-transcripts.v1",
    results: [
      transcript("a", "completed", "en", 42, true),
      transcript("b", "completed", "en", 0, true),
      transcript("d", "failed", null, null, false),
    ],
  };
}

function transcript(recordingId, status, language, words, hasText) {
  return {
    recordingId,
    status,
    language: { detected: language },
    counts: { words, segments: words === null ? null : Math.max(1, Math.ceil(words / 10)), characters: words === null ? null : words * 5 },
    derivedFeatures: { hasTranscribedWords: words === null ? null : words > 0 },
    localArtifacts: hasText ? { transcriptTextPath: `local-artifacts/stt-transcripts/${recordingId}.txt`, localOnly: true, textIncludedInManifest: false } : null,
  };
}

function fakeLyrics() {
  return {
    schemaVersion: "voice-journey.local-lyric-indicators.v1",
    totals: { recordings: 3 },
    clusters: [
      { clusterId: "lyric-cluster-001", label: "recurring_lyric_cluster_001", method: "cross_recording_shingle_similarity.v1", recordingIds: ["a", "b"] },
    ],
    results: [
      lyric("a", true, "lyric-cluster-001", false, "2019-01-01T00:00:00Z"),
      lyric("b", true, null, true, "2020-01-01T00:00:00Z"),
      lyric("c", false, null, false, "2020-02-01T00:00:00Z"),
    ],
  };
}

function lyric(recordingId, marked, clusterId, wordless, capturedAt = null) {
  return {
    recordingId,
    capturedAt,
    lyricMatch: {
      marked,
      status: marked ? "marked_indicator" : "not_marked",
      method: marked ? "cross_recording_similarity" : "none",
      confidence: marked ? 0.91 : 0.35,
      matchedCluster: clusterId,
      matchedClusterLabel: clusterId ? "recurring_lyric_cluster_001" : null,
      signals: {
        wordlessVocalise: { marked: wordless, confidence: wordless ? 0.8 : 0, detectedLanguage: "en" },
      },
    },
  };
}

test("buildCorpusRows joins manifests and overlays local verdict state", () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { state: fakeState(), queueSize: 5 });
  assert.equal(payload.schemaVersion, "voice-journey.corpus-browser-rows.v1");
  assert.equal(payload.rows.length, 7);
  assert.equal(payload.source.readScope.audioBytes, false);
  assert.equal(payload.source.readScope.verdictWrites, true);
  assert.equal(payload.rows.find((row) => row.recordingId === "b").spotCheckStatus, "reviewed");
  assert.equal(payload.rows.find((row) => row.recordingId === "b").humanVerdict, "clean_singing");
  assert.equal(payload.rows.filter((row) => row.reviewQueueSelected).length, 5);
});

test("buildCorpusRows tolerates absent transcript and lyric manifests", () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
  assert.equal(payload.source.transcripts.available, false);
  assert.equal(payload.source.lyrics.available, false);
  assert.equal(payload.rows.find((row) => row.recordingId === "a").transcriptStatus, "not_available");
  assert.equal(payload.rows.find((row) => row.recordingId === "a").lyricMatchStatus, "not_available");
});

test("buildCorpusRows joins partial and complete transcript and lyric manifests", () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5, transcriptsManifest: fakeTranscripts(), lyricsManifest: fakeLyrics() });
  const completed = payload.rows.find((row) => row.recordingId === "a");
  assert.equal(payload.source.transcripts.available, true);
  assert.equal(payload.source.lyrics.available, true);
  assert.equal(completed.transcriptStatus, "completed");
  assert.equal(completed.transcriptLanguage, "en");
  assert.equal(completed.transcriptWordCount, 42);
  assert.equal(completed.transcriptTextLocalOnly, true);
  assert.equal(completed.lyricMatchStatus, "marked_indicator");
  assert.equal(completed.lyricMatchCluster, "lyric-cluster-001");
  assert.equal(completed.lyricMatchClusterLabel, "recurring_lyric_cluster_001");
  assert.equal(completed.wordlessVocaliseMarked, false);
  const zeroWord = payload.rows.find((row) => row.recordingId === "b");
  assert.equal(zeroWord.wordlessVocaliseMarked, true);
  const missing = payload.rows.find((row) => row.recordingId === "g");
  assert.equal(missing.transcriptStatus, "not_available");
  assert.equal(missing.lyricMatchStatus, "not_available");
});

test("selectReviewQueue includes uncertain rows and bucket controls", () => {
  const rows = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 7 }).rows;
  const queue = selectReviewQueue(rows, 56).map(({ row }) => row.bucket);
  assert.ok(queue.includes("uncertain_manual_review"));
  assert.ok(queue.includes("clean_singing"));
  assert.ok(queue.includes("noise_contaminated_singing"));
  assert.ok(queue.includes("non_singing"));
});

test("filterRows and sortRows support browser table axes", () => {
  const rows = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 }).rows;
  assert.deepEqual(filterRows(rows, { bucket: "uncertain_manual_review" }).map((row) => row.recordingId), ["b", "d"]);
  assert.deepEqual(filterRows(rows, { year: "2020", spotCheckStatus: "reviewed" }).map((row) => row.recordingId), ["c"]);
  assert.ok(filterRows(rows, { reviewQueue: "selected" }).length > 0);
  assert.deepEqual(sortRows(rows, "confidence", "desc").slice(0, 2).map((row) => row.recordingId), ["a", "g"]);
});

const FIXTURE_APP_JS = "/* fixture bundle */ console.log(1);\n";
const FIXTURE_APP_CSS = "/* fixture styles */ body{}\n";

async function makeWebDistFixture() {
  const dir = await mkdtemp(path.join(tmpdir(), "vj-webdist-"));
  await writeFile(path.join(dir, "app.js"), FIXTURE_APP_JS, "utf8");
  await writeFile(path.join(dir, "app.css"), FIXTURE_APP_CSS, "utf8");
  return dir;
}

test("server exposes HTML, summary, review queue, and filtered row JSON", async () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
  const webDist = await makeWebDistFixture();
  const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: "unused-state.json", webDist });
  try {
    const [{ port }] = servers;
    const html = await fetch(`http://127.0.0.1:${port}/`).then((response) => response.text());
    assert.match(html, /<div id="app"><\/div>/u);
    assert.match(html, /src="\/assets\/app\.js"/u);
    assert.match(html, /href="\/assets\/app\.css"/u);
    const js = await fetch(`http://127.0.0.1:${port}/assets/app.js`);
    assert.equal(js.status, 200);
    assert.equal((js.headers.get("content-type") ?? "").startsWith("text/javascript"), true);
    assert.equal(await js.text(), FIXTURE_APP_JS);
    const css = await fetch(`http://127.0.0.1:${port}/assets/app.css`).then((response) => response.text());
    assert.equal(css, FIXTURE_APP_CSS);
    const summary = await fetch(`http://127.0.0.1:${port}/api/summary`).then((response) => response.json());
    assert.equal(summary.playback.enabled, false);
    assert.equal(summary.reviewQueue.selectedCount, 5);
    const queue = await fetch(`http://127.0.0.1:${port}/api/review-queue`).then((response) => response.json());
    assert.equal(queue.rows.length, 5);
    const rows = await fetch(`http://127.0.0.1:${port}/api/rows?bucket=non_singing`).then((response) => response.json());
    assert.equal(rows.rows.length, 1);
    assert.equal(rows.rows[0].recordingId, "c");
  } finally {
    await closeServers(servers);
  }
});


test("page routes serve the not-built notice when web/dist is absent", async () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
  const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: "unused-state.json", webDist: path.join(tmpdir(), "vj-missing-dist") });
  try {
    const [{ port }] = servers;
    for (const route of ["/", "/journey", "/referee"]) {
      const html = await fetch(`http://127.0.0.1:${port}${route}`).then((response) => response.text());
      assert.match(html, /web bundle is not built/u);
    }
    const asset = await fetch(`http://127.0.0.1:${port}/assets/app.js`);
    assert.equal(asset.status, 404);
  } finally {
    await closeServers(servers);
  }
});

test("paginateRows slices only when paging params are present and clamps bad input", () => {
  const rows = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 }).rows;
  const unpaged = paginateRows(rows, new URLSearchParams(""));
  assert.equal(unpaged.page, null);
  assert.equal(unpaged.rows.length, 7);
  const paged = paginateRows(rows, new URLSearchParams("limit=2&offset=1"));
  assert.deepEqual(paged.page, { offset: 1, limit: 2, returned: 2, totalFiltered: 7 });
  assert.deepEqual(paged.rows.map((row) => row.recordingId), ["b", "c"]);
  const tail = paginateRows(rows, new URLSearchParams("limit=5&offset=5"));
  assert.deepEqual(tail.page, { offset: 5, limit: 5, returned: 2, totalFiltered: 7 });
  const clamped = paginateRows(rows, new URLSearchParams("limit=9999&offset=-3"));
  assert.equal(clamped.page.limit, 500);
  assert.equal(clamped.page.offset, 0);
  const defaulted = paginateRows(rows, new URLSearchParams("offset=abc"));
  assert.deepEqual(defaulted.page, { offset: 0, limit: 100, returned: 7, totalFiltered: 7 });
});

test("projectRows lean list projection drops heavy fields and rejects unknown projections", () => {
  const rows = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 }).rows;
  const full = projectRows(rows, null);
  assert.equal(full[0].rationale, "clean_singing rationale");
  const lean = projectRows(rows, "list");
  assert.equal(lean.length, 7);
  assert.equal(lean[0].recordingId, "a");
  assert.equal(lean[0].bucket, "clean_singing");
  assert.equal("rationale" in lean[0], false);
  assert.equal("sizeBytes" in lean[0], false);
  assert.throws(() => projectRows(rows, "bogus"), (error) => error.statusCode === 400);
});

test("rows endpoint pages, projects, and serves single rows by id", async () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
  const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: "unused-state.json" });
  try {
    const [{ port }] = servers;
    const base = `http://127.0.0.1:${port}`;
    const paged = await fetch(`${base}/api/rows?limit=3&offset=2&fields=list&sort=capturedAt&direction=asc`).then((response) => response.json());
    assert.deepEqual(paged.page, { offset: 2, limit: 3, returned: 3, totalFiltered: 7 });
    assert.deepEqual(paged.rows.map((row) => row.recordingId), ["c", "d", "e"]);
    assert.equal("rationale" in paged.rows[0], false);
    assert.equal(paged.totals.rows, 7, "totals summarize the whole filtered set, not the page");
    const filteredPage = await fetch(`${base}/api/rows?bucket=uncertain_manual_review&limit=1&offset=1`).then((response) => response.json());
    assert.deepEqual(filteredPage.page, { offset: 1, limit: 1, returned: 1, totalFiltered: 2 });
    assert.equal(filteredPage.rows[0].recordingId, "d");
    assert.equal(filteredPage.rows[0].rationale, "uncertain_manual_review rationale");
    const unpaged = await fetch(`${base}/api/rows`).then((response) => response.json());
    assert.equal("page" in unpaged, false);
    assert.equal(unpaged.rows.length, 7);
    const badProjection = await fetch(`${base}/api/rows?fields=bogus`);
    assert.equal(badProjection.status, 400);
    const single = await fetch(`${base}/api/row/d`).then((response) => response.json());
    assert.equal(single.row.recordingId, "d");
    assert.equal(single.row.rationale, "uncertain_manual_review rationale");
    assert.equal(single.schemaVersion, payload.schemaVersion);
    const missing = await fetch(`${base}/api/row/not-indexed`);
    assert.equal(missing.status, 404);
  } finally {
    await closeServers(servers);
  }
});

test("transcript route serves only indexed local text by recordingId", async () => {
  await withTempDir(async (root) => {
    const transcriptDir = path.join(root, "stt-transcripts");
    await mkdir(transcriptDir);
    await writeFile(path.join(transcriptDir, "a.txt"), "fixture transcript text", "utf8");
    await writeFile(path.join(root, "secret.txt"), "outside transcript dir", "utf8");
    const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5, transcriptsManifest: fakeTranscripts(), lyricsManifest: fakeLyrics() });
    const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: path.join(root, "state.json"), transcriptDir });
    try {
      const [{ port }] = servers;
      const rows = await fetch(`http://127.0.0.1:${port}/api/rows`).then((response) => response.json());
      assert.equal(JSON.stringify(rows).includes("fixture transcript text"), false);
      assert.equal(rows.rows.find((row) => row.recordingId === "a").transcriptWordCount, 42);
      const transcriptResponse = await fetch(`http://127.0.0.1:${port}/api/transcript/a`);
      assert.equal(transcriptResponse.status, 200);
      const transcriptPayload = await transcriptResponse.json();
      assert.equal(transcriptPayload.text, "fixture transcript text");
      assert.equal(transcriptPayload.repoSafe.transcriptTextCommittedToRepo, false);
      const missingText = await fetch(`http://127.0.0.1:${port}/api/transcript/b`);
      assert.equal(missingText.status, 404);
      const unknown = await fetch(`http://127.0.0.1:${port}/api/transcript/not-indexed`);
      assert.equal(unknown.status, 404);
      const traversal = await fetch(`http://127.0.0.1:${port}/api/transcript/..%2Fsecret`);
      assert.equal(traversal.status, 404);
    } finally {
      await closeServers(servers);
    }
  });
});

test("transcript route is disabled without local transcript directory", async () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5, transcriptsManifest: fakeTranscripts() });
  const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: "unused-state.json" });
  try {
    const [{ port }] = servers;
    const response = await fetch(`http://127.0.0.1:${port}/api/transcript/a`);
    assert.equal(response.status, 403);
    assert.match(await response.text(), /transcript_dir_not_configured/u);
  } finally {
    await closeServers(servers);
  }
});

test("playback route is disabled until launch includes approval", async () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
  const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: "unused-state.json" });
  try {
    const [{ port }] = servers;
    const response = await fetch(`http://127.0.0.1:${port}/api/audio/a`);
    assert.equal(response.status, 403);
    assert.match(await response.text(), /playback_requires_release_gate_approval/u);
  } finally {
    await closeServers(servers);
  }
});

test("playback streams only indexed fixture audio through approved seam handle", async () => {
  await withTempDir(async (root) => {
    const corpusRoot = path.join(root, "corpus");
    await mkdir(corpusRoot);
    await writeFile(path.join(corpusRoot, "20190101 000000-A.m4a"), "fixture-audio", "utf8");
    const recordingId = "b74e7f7075c282e2";
    const index = { ...fakeIndex(), recordings: [recording(recordingId, "20190101 000000-A.m4a", "2019-01-01T00:00:00Z", 13)] };
    const results = { ...fakeResults(), results: [result(recordingId, "20190101 000000-A.m4a", "2019", 10, "clean_singing", 0.9, "not_reviewed")] };
    const payload = buildCorpusRows(index, results, { queueSize: 1 });
    const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: path.join(root, "state.json"), corpusRoot, playbackApproval: "fixture approval" });
    try {
      const [{ port }] = servers;
      const response = await fetch(`http://127.0.0.1:${port}/api/audio/${recordingId}`);
      assert.equal(response.status, 200);
      assert.equal(response.headers.get("content-type"), "audio/mp4");
      assert.equal(await response.text(), "fixture-audio");
      const missing = await fetch(`http://127.0.0.1:${port}/api/audio/not-indexed`);
      assert.equal(missing.status, 404);
    } finally {
      await closeServers(servers);
    }
  });
});

test("playback maps corpus-root permission denials to a distinct 503", async () => {
  await withTempDir(async (root) => {
    const corpusRoot = path.join(root, "corpus");
    await mkdir(corpusRoot);
    await writeFile(path.join(corpusRoot, "20190101 000000-A.m4a"), "fixture-audio", "utf8");
    const recordingId = "b74e7f7075c282e2";
    const index = { ...fakeIndex(), recordings: [recording(recordingId, "20190101 000000-A.m4a", "2019-01-01T00:00:00Z", 13)] };
    const results = { ...fakeResults(), results: [result(recordingId, "20190101 000000-A.m4a", "2019", 10, "clean_singing", 0.9, "not_reviewed")] };
    const payload = buildCorpusRows(index, results, { queueSize: 1 });
    const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: path.join(root, "state.json"), corpusRoot, playbackApproval: "fixture approval" });
    try {
      // The seam child's readdir fails EACCES on a 000 root — the same
      // operator-fixable denial class as macOS TCC's EPERM on the real corpus.
      await chmod(corpusRoot, 0o000);
      const [{ port }] = servers;
      const response = await fetch(`http://127.0.0.1:${port}/api/audio/${recordingId}`);
      assert.equal(response.status, 503);
      assert.match(await response.text(), /corpus_access_denied/u);
    } finally {
      await chmod(corpusRoot, 0o755);
      await closeServers(servers);
    }
  });
});

test("watch status route reports disabled without a watcher and proxies a live one", async () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
  let servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: "unused-state.json" });
  try {
    const [{ port }] = servers;
    const disabled = await (await fetch(`http://127.0.0.1:${port}/api/watch/status`)).json();
    assert.equal(disabled.enabled, false);
  } finally {
    await closeServers(servers);
  }
  servers = await startServers(payload, {
    hosts: ["127.0.0.1"],
    port: 0,
    state: "unused-state.json",
    watchd: { statusSnapshot: () => ({ enabled: true, marker: "wd" }) },
  });
  try {
    const [{ port }] = servers;
    const live = await (await fetch(`http://127.0.0.1:${port}/api/watch/status`)).json();
    assert.equal(live.enabled, true);
    assert.equal(live.marker, "wd");
  } finally {
    await closeServers(servers);
  }
});

test("serve-state reload swaps freshly loaded manifests into the running server", async () => {
  await withTempDir(async (root) => {
    const indexPath = path.join(root, "recording-index.json");
    const resultsPath = path.join(root, "results.json");
    await writeFile(indexPath, JSON.stringify(fakeIndex()));
    await writeFile(resultsPath, JSON.stringify(fakeResults()));
    const payloadReload = { fn: null };
    const options = {
      hosts: ["127.0.0.1"],
      port: 0,
      state: path.join(root, "state.json"),
      index: indexPath,
      results: resultsPath,
      transcripts: path.join(root, "missing-transcripts.json"),
      lyrics: path.join(root, "missing-lyrics.json"),
      features: path.join(root, "missing-features.json"),
      journeys: path.join(root, "missing-journeys.json"),
      queueSize: 5,
      payloadReload,
    };
    const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
    const servers = await startServers(payload, options);
    try {
      const [{ port }] = servers;
      const before = await (await fetch(`http://127.0.0.1:${port}/api/rows`)).json();
      assert.equal(before.rows.length, 7);

      const grownIndex = fakeIndex();
      grownIndex.recordings.push(recording("h", "20250101 000000-H.m4a", "2025-01-01T00:00:00Z", 8000));
      const grownResults = fakeResults();
      grownResults.results.push(result("h", "20250101 000000-H.m4a", "2025", 80, "clean_singing", 0.9, "not_reviewed"));
      await writeFile(indexPath, JSON.stringify(grownIndex));
      await writeFile(resultsPath, JSON.stringify(grownResults));

      assert.equal(typeof payloadReload.fn, "function", "startServers wires the reload hook");
      await payloadReload.fn();

      const after = await (await fetch(`http://127.0.0.1:${port}/api/rows`)).json();
      assert.equal(after.rows.length, 8);
      assert.ok(after.rows.some((row) => row.recordingId === "h"));
      const detail = await (await fetch(`http://127.0.0.1:${port}/api/row/h`)).json();
      assert.equal(detail.row.recordingId, "h");
    } finally {
      await closeServers(servers);
    }
  });
});

test("loudness-match gains are symmetric, capped, and default to zero", () => {
  assert.deepEqual(loudnessMatchGainsDb(-25, -31), { aGainDb: -3, bGainDb: 3 });
  assert.deepEqual(loudnessMatchGainsDb(-10, -60), { aGainDb: -12, bGainDb: 12 });
  assert.deepEqual(loudnessMatchGainsDb(null, -20), { aGainDb: 0, bGainDb: 0 });
  const lyrics = { clusters: [{ clusterId: "c1", label: "c1", recordingIds: ["x", "y"] }] };
  const features = {
    results: [
      { recordingId: "x", capturedAt: "2020-01-01T00:00:00Z", year: "2020", status: "completed", features: { voicing: {}, dynamics: { voicedRmsDbP50: -20 } } },
      { recordingId: "y", capturedAt: "2021-01-01T00:00:00Z", year: "2021", status: "completed", features: { voicing: {}, dynamics: { voicedRmsDbP50: -26 } } },
    ],
  };
  const pool = buildRefereePairs(lyrics, features, { seed: 7 });
  assert.equal(pool.length, 1);
  assert.equal(pool[0].aGainDb + pool[0].bGainDb, 0);
  assert.equal(Math.abs(pool[0].aGainDb), 3);
});

test("goals routes pre-register, evaluate against features, and abandon", async () => {
  await withTempDir(async (root) => {
    const goalsState = path.join(root, "goals-state.json");
    const featuresManifest = {
      results: [
        { recordingId: "t1", capturedAt: "2999-01-01T00:00:00Z", status: "completed", features: { vibrato: { meanRateHz: 5.6 } } },
        { recordingId: "t2", capturedAt: "2999-01-02T00:00:00Z", status: "completed", features: { vibrato: { meanRateHz: 5.8 } } },
        { recordingId: "t3", capturedAt: "2999-01-03T00:00:00Z", status: "completed", features: { vibrato: { meanRateHz: 5.7 } } },
      ],
    };
    const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
    const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: path.join(root, "state.json"), goalsState, featuresManifest });
    try {
      const [{ port }] = servers;
      const base = `http://127.0.0.1:${port}`;
      const bad = await fetch(`${base}/api/goals`, { method: "POST", body: JSON.stringify({ metric: "nope", direction: "at_least", target: 1 }) });
      assert.equal(bad.status, 400);

      const created = await (await fetch(`${base}/api/goals`, {
        method: "POST",
        body: JSON.stringify({ metric: "vibratoRateHz", direction: "at_least", target: 5.5, takesRequired: 3 }),
      })).json();
      assert.equal(created.ok, true);
      assert.match(created.goal.goalId, /^[0-9a-f]{16}$/u);

      const listed = await (await fetch(`${base}/api/goals`)).json();
      assert.equal(listed.goals.length, 1);
      assert.equal(listed.goals[0].derivedStatus, "achieved");
      assert.equal(listed.goals[0].progress.takesCounted, 3);
      assert.equal(listed.goals[0].progress.runningMedian, 5.7);

      const abandoned = await (await fetch(`${base}/api/goals/abandon`, { method: "POST", body: JSON.stringify({ goalId: created.goal.goalId }) })).json();
      assert.equal(abandoned.ok, true);
      const after = await (await fetch(`${base}/api/goals`)).json();
      assert.equal(after.goals[0].derivedStatus, "abandoned");
      assert.equal(after.goals[0].progress, null);
    } finally {
      await closeServers(servers);
    }
  });
});

test("coach API serves recommendations from the live serve state", async () => {
  const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
  const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: "unused-state.json" });
  try {
    const [{ port }] = servers;
    const coach = await (await fetch(`http://127.0.0.1:${port}/api/coach`)).json();
    assert.ok(Array.isArray(coach.dueSongs));
    assert.ok(Array.isArray(coach.frontier));
    assert.deepEqual(coach.dueSongs, [], "no lyric clusters in the fixture rows means nothing is due");
    assert.equal(coach.readScope.transcriptText, false);
    assert.equal(coach.readScope.audioBytes, false);
    assert.equal(coach.params.dueAfterDays, 90);
  } finally {
    await closeServers(servers);
  }
});

test("verdict API writes local state only", async () => {
  await withTempDir(async (root) => {
    const statePath = path.join(root, "state.json");
    const payload = buildCorpusRows(fakeIndex(), fakeResults(), { queueSize: 5 });
    const servers = await startServers(payload, { hosts: ["127.0.0.1"], port: 0, state: statePath });
    try {
      const [{ port }] = servers;
      const response = await fetch(`http://127.0.0.1:${port}/api/verdicts`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ recordingId: "b", verdict: "clean_singing", note: "sounds clean", reviewedBy: "tester", reviewedAt: "2026-07-16T00:00:00.000Z" }),
      });
      assert.equal(response.status, 200);
      const state = JSON.parse(await readFile(statePath, "utf8"));
      assert.equal(state.verdicts.length, 1);
      assert.equal(state.verdicts[0].recordingId, "b");
      const rows = await fetch(`http://127.0.0.1:${port}/api/rows?spotCheckStatus=reviewed`).then((result) => result.json());
      assert.ok(rows.rows.some((row) => row.recordingId === "b"));
    } finally {
      await closeServers(servers);
    }
  });
});

test("mergeVerdictsIntoResults writes repo-safe humanSpotCheck metadata", () => {
  const merged = mergeVerdictsIntoResults(fakeResults(), fakeState());
  const result = merged.results.find((entry) => entry.recordingId === "b");
  assert.equal(merged.humanSpotCheckMerge.mergedCount, 1);
  assert.equal(result.humanSpotCheck.status, "reviewed");
  assert.equal(result.humanSpotCheck.verdict, "clean_singing");
  assert.equal(result.humanSpotCheck.notes, "fixture");
});

test("run --once starts and closes a manifest server", async () => {
  await withTempDir(async (root) => {
    const indexPath = path.join(root, "index.json");
    const resultsPath = path.join(root, "results.json");
    await writeFile(indexPath, JSON.stringify(fakeIndex()), "utf8");
    await writeFile(resultsPath, JSON.stringify(fakeResults()), "utf8");
    const { code, stdout } = await captureRun(["--index", indexPath, "--results", resultsPath, "--state", path.join(root, "state.json"), "--host", "127.0.0.1", "--port", "0", "--once"]);
    assert.equal(code, 0);
    const output = JSON.parse(stdout);
    assert.equal(output.rows, 7);
    assert.equal(output.playback.enabled, false);
    assert.match(output.endpoints[0], /^http:\/\/127\.0\.0\.1:\d+\/$/u);
  });
});

test("merge-verdicts command writes output manifest", async () => {
  await withTempDir(async (root) => {
    const resultsPath = path.join(root, "results.json");
    const statePath = path.join(root, "state.json");
    const outPath = path.join(root, "merged.json");
    await writeFile(resultsPath, JSON.stringify(fakeResults()), "utf8");
    await writeFile(statePath, JSON.stringify(fakeState()), "utf8");
    const { code, stdout } = await captureRun(["merge-verdicts", "--results", resultsPath, "--state", statePath, "--out", outPath]);
    assert.equal(code, 0);
    assert.equal(JSON.parse(stdout).mergedCount, 1);
    const merged = JSON.parse(await readFile(outPath, "utf8"));
    assert.equal(merged.results.find((entry) => entry.recordingId === "b").humanSpotCheck.verdict, "clean_singing");
  });
});

test("buildVoiceTrends aggregates quarters, filters flagged takes, and computes the working-top headline", () => {
  const rows = [{ capturedAt: "2019-01-05T00:00:00Z" }, { capturedAt: "2019-01-20T00:00:00Z" }, { capturedAt: "2021-02-10T00:00:00Z" }];
  const trends = buildVoiceTrends(fakeFeatures(), rows);
  assert.equal(trends.available, true);
  assert.equal(trends.headline.takesAnalyzed, 25);
  assert.equal(trends.headline.reliableTakes, 24);
  assert.equal(trends.headline.flaggedTakes, 1);
  assert.equal(trends.headline.workingTop.baselineYear, "2019");
  assert.equal(trends.headline.workingTop.peakYear, "2021");
  assert.equal(trends.headline.workingTop.gainSemitones, 7);
  const q1of2019 = trends.quarters.find((quarter) => quarter.quarter === "2019-Q1");
  assert.equal(q1of2019.nReliable, 6);
  assert.equal(q1of2019.envelope.hiHz, 260);
  assert.equal(q1of2019.registerShares.low, 1);
  const q1of2021 = trends.quarters.find((quarter) => quarter.quarter === "2021-Q1");
  assert.equal(q1of2021.registerShares.high, 1);
  assert.deepEqual(trends.monthlyCadence, [{ month: "2019-01", count: 2 }, { month: "2021-02", count: 1 }]);
  assert.doesNotMatch(JSON.stringify(trends), /f0ContourVoiced50ms|vibratoSegments/u);
});

test("buildVoiceTrends reports unavailable without a features manifest but still carries cadence", () => {
  const trends = buildVoiceTrends(null, [{ capturedAt: "2020-05-01T00:00:00Z" }]);
  assert.equal(trends.available, false);
  assert.equal(trends.reason, "features_manifest_not_loaded");
  assert.deepEqual(trends.monthlyCadence, [{ month: "2020-05", count: 1 }]);
});

test("journey page and trends endpoint serve over HTTP", async () => {
  await withTempDir(async (root) => {
    const rowsPayload = buildCorpusRows(fakeIndex(), fakeResults());
    const voiceTrends = buildVoiceTrends(fakeFeatures(), rowsPayload.rows);
    const servers = await startServers(rowsPayload, { port: 0, state: path.join(root, "state.json"), voiceTrends });
    const base = `http://127.0.0.1:${servers[0].port}`;
    const trends = await fetch(`${base}/api/trends`).then((response) => response.json());
    assert.equal(trends.available, true);
    assert.ok(trends.quarters.length >= 2);
    assert.equal(trends.readScope.featureArrays, false);
    const journey = await fetch(`${base}/journey`).then((response) => response.text());
    assert.match(journey, /<div id="app"><\/div>|web bundle is not built/u);
    const rowsResponse = await fetch(`${base}/api/rows`).then((response) => response.json());
    assert.equal(rowsResponse.rows.length, 7);
    await closeServers(servers);
  });
});

test("trends endpoint degrades gracefully when trends were not computed", async () => {
  await withTempDir(async (root) => {
    const rowsPayload = buildCorpusRows(fakeIndex(), fakeResults());
    const servers = await startServers(rowsPayload, { port: 0, state: path.join(root, "state.json") });
    const base = `http://127.0.0.1:${servers[0].port}`;
    const trends = await fetch(`${base}/api/trends`).then((response) => response.json());
    assert.equal(trends.available, false);
    await closeServers(servers);
  });
});

test("buildVoiceTrends carries spreads, all-takes rollups, per-take vibrato points, eras, and cluster threads", () => {
  const rows = [];
  for (const [month, count] of [["2021-01", 50], ["2021-02", 45], ["2021-04", 44], ["2022-06", 5]]) {
    for (let i = 0; i < count; i += 1) rows.push({ capturedAt: `${month}-10T00:00:00Z` });
  }
  const trends = buildVoiceTrends(fakeFeatures(), rows, { lyricsManifest: fakeLyrics() });
  const q1 = trends.quarters.find((quarter) => quarter.quarter === "2021-Q1");
  const q1All = trends.quartersAllTakes.find((quarter) => quarter.quarter === "2021-Q1");
  assert.equal(q1.nReliable, 6);
  assert.equal(q1All.nReliable, 7);
  assert.ok(Array.isArray(q1.spread.rate) && q1.spread.rate.length === 2);
  assert.ok(Array.isArray(q1.spread.cpps));
  assert.equal(trends.vibratoTakes.length, 25);
  const flagged = trends.vibratoTakes.find((take) => take.id === "flagged");
  assert.equal(flagged.reliable, false);
  assert.equal(trends.eras.length, 1);
  assert.equal(trends.eras[0].start, "2021-01");
  assert.equal(trends.eras[0].end, "2021-04");
  assert.equal(trends.eras[0].totalTakes, 139);
  assert.equal(trends.eras[0].sampleRecordingIds.length, 3);
  assert.equal(trends.clusters.length, 1);
  assert.equal(trends.clusters[0].id, "lyric-cluster-001");
  assert.equal(trends.clusters[0].size, 2);
  assert.equal(trends.clusters[0].first, "2019-01");
  assert.deepEqual(trends.clusters[0].perYear, { 2019: 1, 2020: 1 });
});

test("feature-detail endpoint gates on --feature-dir and serves local detail with row metadata", async () => {
  await withTempDir(async (root) => {
    const featureDir = path.join(root, "voice-features");
    await mkdir(featureDir, { recursive: true });
    await writeFile(path.join(featureDir, "a.json"), JSON.stringify({
      recordingId: "a",
      methodId: "voice-journey.feature-extract.v1",
      summary: { pitch: { f0Hz: { p50: 220 }, rangeSemitonesP05P95: 10 } },
      detail: { f0ContourVoiced50ms: [[0, 220.1], [0.05, 221.0]], vibratoSegments: [] },
    }), "utf8");
    const rowsPayload = buildCorpusRows(fakeIndex(), fakeResults());
    const gated = await startServers(rowsPayload, { port: 0, state: path.join(root, "state.json") });
    const gatedBase = `http://127.0.0.1:${gated[0].port}`;
    const denied = await fetch(`${gatedBase}/api/feature-detail/a`);
    assert.equal(denied.status, 403);
    assert.equal((await denied.json()).error, "feature_dir_not_configured");
    await closeServers(gated);

    const servers = await startServers(rowsPayload, { port: 0, state: path.join(root, "state.json"), featureDir });
    const base = `http://127.0.0.1:${servers[0].port}`;
    const ok = await fetch(`${base}/api/feature-detail/a`);
    assert.equal(ok.status, 200);
    const payload = await ok.json();
    assert.equal(payload.filename, "20190101 000000-A.m4a");
    assert.equal(payload.detail.f0ContourVoiced50ms.length, 2);
    assert.equal(payload.repoSafe.audioBytes, false);
    const missing = await fetch(`${base}/api/feature-detail/b`);
    assert.equal(missing.status, 404);
    assert.equal((await missing.json()).error, "feature_detail_missing");
    const unknown = await fetch(`${base}/api/feature-detail/zz`);
    assert.equal(unknown.status, 404);
    const summary = await fetch(`${base}/api/summary`).then((response) => response.json());
    assert.equal(summary.featureDetail.enabled, true);
    await closeServers(servers);
  });
});

test("journeys endpoint serves the wrapped manifest and degrades gracefully", async () => {
  await withTempDir(async (root) => {
    const rowsPayload = buildCorpusRows(fakeIndex(), fakeResults());
    const songJourneys = {
      available: true,
      totals: { clustersEligible: 1 },
      noteCore: { takes: [{ id: "a", year: "2019", quarter: "2019-Q1", centErrorMedian: 18.2, inTuneShare: 0.64 }] },
      journeys: [{ clusterId: "lyric-cluster-001", label: "recurring_lyric_cluster_001", takesUsed: 6, firstYear: "2019", lastYear: "2021", perYear: {}, slopesPerYear: { noteCoreCentError: -1.2, cpps: 0.3 } }],
      improvementIndex: [{ key: "noteCoreCentError", label: "note-core tuning error (cents)", direction: "down_good", medianSlopePerYear: -1.2, ci95: [-2.0, -0.4], clustersUsed: 1, verdict: "improving" }],
    };
    const servers = await startServers(rowsPayload, { port: 0, state: path.join(root, "state.json"), songJourneys });
    const base = `http://127.0.0.1:${servers[0].port}`;
    const journeys = await fetch(`${base}/api/journeys`).then((response) => response.json());
    assert.equal(journeys.available, true);
    assert.equal(journeys.improvementIndex[0].verdict, "improving");
    await closeServers(servers);

    const bare = await startServers(rowsPayload, { port: 0, state: path.join(root, "state.json") });
    const unavailable = await fetch(`http://127.0.0.1:${bare[0].port}/api/journeys`).then((response) => response.json());
    assert.equal(unavailable.available, false);
    await closeServers(bare);
  });
});

function refereeLyrics() {
  return {
    schemaVersion: "voice-journey.local-lyric-indicators.v1",
    clusters: [
      { clusterId: "cluster-song-1", label: "recurring_lyric_cluster_S1", recordingIds: ["y19-0", "y19-1", "y19-2", "y21-0", "y21-1", "y21-2", "flagged", "failed"] },
      { clusterId: "cluster-song-2", label: "recurring_lyric_cluster_S2", recordingIds: ["y19-3", "y21-3"] },
    ],
    results: [],
  };
}

test("buildRefereePairs is seeded-deterministic, cross-year only, reliable only, and capped", () => {
  const poolA = buildRefereePairs(refereeLyrics(), fakeFeatures(), { seed: 7, pairsPerCluster: 4 });
  const poolB = buildRefereePairs(refereeLyrics(), fakeFeatures(), { seed: 7, pairsPerCluster: 4 });
  assert.deepEqual(poolA, poolB);
  assert.ok(poolA.length >= 2 && poolA.length <= 5, `pool=${poolA.length}`);
  for (const pair of poolA) {
    assert.notEqual(pair.aYear, pair.bYear);
    assert.ok(!["flagged", "failed"].includes(pair.aId));
    assert.ok(!["flagged", "failed"].includes(pair.bId));
    assert.match(pair.pairId, /^[0-9a-f]{16}$/u);
  }
  const clusterOne = poolA.filter((pair) => pair.clusterId === "cluster-song-1");
  assert.ok(clusterOne.length <= 4);
  const poolC = buildRefereePairs(refereeLyrics(), fakeFeatures(), { seed: 8, pairsPerCluster: 4 });
  assert.notDeepEqual(poolA.map((pair) => pair.pairId), poolC.map((pair) => pair.pairId));
});

test("bradleyTerryByYear ranks the winning year above and flags chained clusters", () => {
  const judgments = [];
  for (let i = 0; i < 8; i += 1) judgments.push({ pairId: `p${i}`, aYear: "2021", bYear: "2019", clusterId: "c1", choice: "a" });
  for (let i = 0; i < 2; i += 1) judgments.push({ pairId: `q${i}`, aYear: "2019", bYear: "2021", clusterId: "c1", choice: "a" });
  judgments.push({ pairId: "t1", aYear: "2021", bYear: "2019", clusterId: "c1", choice: "too_close" });
  judgments.push({ pairId: "n1", aYear: "2021", bYear: "2019", clusterId: "cluster-chained", choice: "not_same_song" });
  judgments.push({ pairId: "s1", aYear: "2021", bYear: "2019", clusterId: "c1", choice: "skip" });
  const fit = bradleyTerryByYear(judgments);
  const y2021 = fit.years.find((row) => row.year === "2021");
  const y2019 = fit.years.find((row) => row.year === "2019");
  assert.ok(y2021.strengthLog2 > 0.5, `2021=${y2021.strengthLog2}`);
  assert.equal(y2019.strengthLog2, 0);
  assert.equal(y2021.wins, 8);
  assert.equal(y2021.losses, 2);
  assert.equal(y2021.ties, 1);
  assert.deepEqual(fit.notSameSongFlags, [{ clusterId: "cluster-chained", count: 1 }]);
  assert.equal(fit.byChoice.skip, 1);
  const again = bradleyTerryByYear(judgments);
  assert.equal(again.years.find((row) => row.year === "2021").strengthLog2, y2021.strengthLog2);
});

test("referee endpoints gate on playback, keep trials blind, and persist judgments", async () => {
  await withTempDir(async (root) => {
    const rowsPayload = buildCorpusRows(fakeIndex(), fakeResults());
    const refereePool = buildRefereePairs(refereeLyrics(), fakeFeatures(), { seed: 7, pairsPerCluster: 4 });
    const refereeState = path.join(root, "referee-state.json");

    const gated = await startServers(rowsPayload, { port: 0, state: path.join(root, "state.json"), refereePool, refereeState });
    const gatedNext = await fetch(`http://127.0.0.1:${gated[0].port}/api/referee/next`).then((response) => response.json());
    assert.equal(gatedNext.enabled, false);
    await closeServers(gated);

    const servers = await startServers(rowsPayload, {
      port: 0,
      state: path.join(root, "state.json"),
      refereePool,
      refereeState,
      corpusRoot: "/tmp/fixture-corpus",
      playbackApproval: "fixture approval",
    });
    const base = `http://127.0.0.1:${servers[0].port}`;
    const next = await fetch(`${base}/api/referee/next`).then((response) => response.json());
    assert.equal(next.enabled, true);
    assert.ok(next.pair.pairId);
    assert.match(next.pair.a.audioUrl, /^\/api\/audio\//u);
    assert.equal(JSON.stringify(next.pair).includes("Year"), false);
    assert.equal(JSON.stringify(next.pair).includes("cluster"), false);

    const verdict = await fetch(`${base}/api/referee/verdicts`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pairId: next.pair.pairId, choice: "a", judgedAt: "2026-07-31T00:00:00.000Z" }),
    }).then((response) => response.json());
    assert.equal(verdict.ok, true);
    assert.ok(verdict.reveal.aYear);
    assert.equal(verdict.progress.judged, 1);

    const state = JSON.parse(await readFile(refereeState, "utf8"));
    assert.equal(state.judgments.length, 1);
    assert.equal(state.judgments[0].choice, "a");

    const nextAfter = await fetch(`${base}/api/referee/next`).then((response) => response.json());
    assert.notEqual(nextAfter.pair?.pairId, next.pair.pairId);

    const badChoice = await fetch(`${base}/api/referee/verdicts`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pairId: next.pair.pairId, choice: "nope" }),
    });
    assert.equal(badChoice.status, 400);
    const badPair = await fetch(`${base}/api/referee/verdicts`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pairId: "ffffffffffffffff", choice: "a" }),
    });
    assert.equal(badPair.status, 404);

    const results = await fetch(`${base}/api/referee/results`).then((response) => response.json());
    assert.equal(results.judgedTotal, 1);
    assert.equal(results.enabled, true);

    await closeServers(servers);
  });
});
