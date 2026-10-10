import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import { buildManifest, run } from "../src/index-recordings.mjs";

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
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-index-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("dry-run describes exact metadata-only read scope", async () => {
  const { code, stdout } = await captureRun(["--corpus-root", "/tmp/corpus", "--out", "manifests/recording-index.json", "--dry-run"]);
  assert.equal(code, 0);
  const payload = JSON.parse(stdout);
  assert.equal(payload.operation, "rebuild-recording-index");
  assert.deepEqual(payload.architecture.readsVia, ["host-access-seam", "seam-feeds-indexer"]);
  assert.equal(payload.readScope.directoryEntries, true);
  assert.equal(payload.readScope.fileStatMetadata, true);
  assert.equal(payload.readScope.audioBytes, false);
  assert.equal(payload.readScope.durationExtraction, false);
  assert.ok(payload.manifestShape.classificationFields.includes("contentClassification.singingStatus"));
});

test("manifest includes classification placeholders and tool choices", () => {
  const manifest = buildManifest([
    {
      recordingId: "abc123",
      filename: "20190301 090000-0A1B2C3D.m4a",
      capturedAt: "2019-03-01T09:00:00Z",
      sizeBytes: 12,
      modifiedAt: "2026-01-01T00:00:00.000Z",
    },
  ], { generatedAt: "2026-07-16T00:00:00.000Z" });

  assert.equal(manifest.schemaVersion, "voice-journey.recording-index.v1");
  assert.equal(manifest.source.readScope.audioBytes, false);
  assert.equal(manifest.toolChoices[0].openSource, true);
  assert.equal(manifest.toolChoices[1].stage, "content-classification-placeholder");
  assert.equal(manifest.recordings[0].contentClassification.singingStatus, "unknown");
  assert.equal(manifest.recordings[0].contaminationClassification.noiseMusicStatus, "not_evaluated");
  assert.equal(manifest.recordings[0].durationSeconds, null);
});

test("rebuild writes deterministic index from fake corpus metadata", async () => {
  await withTempDir(async (root) => {
    const corpusRoot = path.join(root, "corpus");
    const out = path.join(root, "manifests", "recording-index.json");
    await mkdir(corpusRoot, { recursive: true });
    await writeFile(path.join(corpusRoot, "20200101 010203-AAAA.m4a"), "fake-audio");
    await writeFile(path.join(corpusRoot, "20190301 090000-0A1B2C3D.m4a"), "fake-audio");
    await writeFile(path.join(corpusRoot, "ignore.txt"), "ignore");

    const args = ["--corpus-root", corpusRoot, "--out", out, "--generated-at", "2026-07-16T00:00:00.000Z"];
    const first = await captureRun(args);
    const firstBody = await readFile(out, "utf8");
    const second = await captureRun(args);
    const secondBody = await readFile(out, "utf8");

    assert.equal(first.code, 0);
    assert.equal(second.code, 0);
    assert.equal(firstBody, secondBody);
    const manifest = JSON.parse(firstBody);
    assert.equal(manifest.totals.recordings, 2);
    assert.equal(manifest.totals.audioBytesIndexed, 0);
    assert.deepEqual(manifest.recordings.map((entry) => entry.filename), [
      "20190301 090000-0A1B2C3D.m4a",
      "20200101 010203-AAAA.m4a",
    ]);
  });
});

// ---- mirrored drop layout (server side): synthetic files only ----

import { listCorpusEntries } from "../src/host-access.mjs";

const MIRROR_A = "20240101 101010-AAAA1111.m4a";
const MIRROR_B = "20240102 101010-BBBB2222.m4a";
const MIRROR_C = "20240103 101010-CCCC3333.m4a";

async function drop(root, files, manifestFiles) {
  const phone = path.join(root, "corpus", "phone");
  await mkdir(path.join(phone, "_mirror"), { recursive: true });
  await mkdir(path.join(phone, ".rsync-partial"), { recursive: true });
  for (const [name, body] of Object.entries(files)) await writeFile(path.join(phone, name), body);
  await writeFile(path.join(phone, "CloudRecordings.db"), "db");
  await writeFile(path.join(phone, "CloudRecordings.db-wal"), "wal");
  await writeFile(path.join(phone, `.${MIRROR_A}.Ab12Cd`), "temp");
  await writeFile(path.join(phone, ".rsync-partial", MIRROR_A), "partial");
  await writeFile(path.join(phone, "_mirror", "stray.m4a"), "not a recording");
  if (manifestFiles) {
    await writeFile(
      path.join(phone, "_mirror", "latest.json"),
      JSON.stringify({
        schema: "voice-journey-mirror-manifest/1",
        runId: "r1",
        files: manifestFiles.map(([name, sizeBytes]) => ({ path: name, sizeBytes, sha256: "x" })),
      }),
    );
  }
  return phone;
}

test("the drop listing ignores rsync temp files, .rsync-partial/ and _mirror/, and writes nothing", async () => {
  await withTempDir(async (root) => {
    const phone = await drop(root, { [MIRROR_A]: "aaa", [MIRROR_B]: "bbbb" });
    const before = JSON.stringify(await (await import("node:fs/promises")).readdir(phone, { recursive: true }));
    const entries = await listCorpusEntries(phone, { mirrorManifest: true });
    assert.deepEqual(entries.map((entry) => entry.filename), [MIRROR_A, MIRROR_B]);
    assert.ok(entries.every((entry) => entry.inManifest === null)); // no manifest yet
    const after = JSON.stringify(await (await import("node:fs/promises")).readdir(phone, { recursive: true }));
    assert.equal(after, before);
  });
});

test("a size that differs from the mirror manifest is held back; unlisted files are kept", async () => {
  await withTempDir(async (root) => {
    const phone = await drop(root, { [MIRROR_A]: "aaa", [MIRROR_B]: "bbbbXX", [MIRROR_C]: "c" }, [[MIRROR_A, 3], [MIRROR_B, 4]]);
    const entries = await listCorpusEntries(phone, { mirrorManifest: true });
    assert.deepEqual(
      entries.map((entry) => [entry.filename, entry.inManifest]),
      [[MIRROR_A, true], [MIRROR_C, false]],
    );
    // Without the option the listing is the plain directory (the Mac corpus path).
    assert.equal((await listCorpusEntries(phone)).length, 3);
  });
});

test("index from the mirrored layout: recordings only, deleted-on-phone is marked and kept", async () => {
  await withTempDir(async (root) => {
    const phone = await drop(root, { [MIRROR_A]: "aaa", [MIRROR_B]: "bbbb" }, [[MIRROR_A, 3], [MIRROR_B, 4]]);
    const out = path.join(root, "manifests", "recording-index.json");
    const args = ["--corpus-root", phone, "--measurement-root", path.join(root, "none"), "--out", out];
    await captureRun(args);
    let rows = JSON.parse(await readFile(out, "utf8")).recordings;
    assert.deepEqual(rows.map((row) => [row.filename, row.phoneState]), [[MIRROR_A, "present"], [MIRROR_B, "present"]]);

    // The phone deletes B; the next mirror manifest no longer lists it. The file stays.
    await writeFile(
      path.join(phone, "_mirror", "latest.json"),
      JSON.stringify({ schema: "voice-journey-mirror-manifest/1", runId: "r2", files: [{ path: MIRROR_A, sizeBytes: 3 }] }),
    );
    await captureRun(args);
    rows = JSON.parse(await readFile(out, "utf8")).recordings;
    assert.deepEqual(rows.map((row) => [row.filename, row.phoneState]), [[MIRROR_A, "present"], [MIRROR_B, "deleted"]]);

    // Sticky across runs; a brand-new unlisted file is not called deleted.
    await writeFile(path.join(phone, MIRROR_C), "c");
    await captureRun(args);
    rows = JSON.parse(await readFile(out, "utf8")).recordings;
    assert.deepEqual(
      rows.map((row) => [row.filename, row.phoneState]),
      [[MIRROR_A, "present"], [MIRROR_B, "deleted"], [MIRROR_C, "unverified"]],
    );
  });
});
