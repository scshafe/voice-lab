import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import { capturedAtFromFilename, run } from "../src/host-access.mjs";

async function captureRun(args) {
  let stdout = "";
  let stderr = "";
  const originalStdoutWrite = process.stdout.write;
  const originalStderrWrite = process.stderr.write;
  process.stdout.write = (chunk) => {
    stdout += String(chunk);
    return true;
  };
  process.stderr.write = (chunk) => {
    stderr += String(chunk);
    return true;
  };
  try {
    const code = await run(args);
    return { code, stdout, stderr };
  } finally {
    process.stdout.write = originalStdoutWrite;
    process.stderr.write = originalStderrWrite;
  }
}

async function withTempCorpus(callback) {
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-"));
  try {
    return await callback(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("describe is read-only and inspectable", async () => {
  const { code, stdout } = await captureRun(["--corpus-root", "/tmp/example", "describe"]);
  assert.equal(code, 0);
  const payload = JSON.parse(stdout);
  assert.deepEqual(payload.operations, ["describe", "list", "metadata", "read-handle"]);
  assert.equal(payload.privacy.uploadsAudio, false);
  assert.equal(payload.privacy.mutatesCorpus, false);
  assert.equal(payload.privacy.readsAudioBytes, false);
  assert.ok(payload.refusedOperations.includes("delete"));
});

test("list dry-run does not require an existing corpus", async () => {
  const { code, stdout } = await captureRun(["--corpus-root", "/does/not/exist", "list", "--dry-run"]);
  assert.equal(code, 0);
  const payload = JSON.parse(stdout);
  assert.equal(payload.dryRun, true);
  assert.equal(payload.operation, "list");
  assert.equal(payload.willReadAudioBytes, false);
});

test("list returns stable metadata for m4a files only", async () => {
  await withTempCorpus(async (root) => {
    await writeFile(path.join(root, "20190301 090000-0A1B2C3D.m4a"), "not real audio");
    await writeFile(path.join(root, "notes.txt"), "ignore me");

    const { code, stdout } = await captureRun(["--corpus-root", root, "list"]);

    assert.equal(code, 0);
    const payload = JSON.parse(stdout);
    assert.equal(payload.count, 1);
    assert.equal(payload.recordings[0].filename, "20190301 090000-0A1B2C3D.m4a");
    assert.equal(payload.recordings[0].capturedAt, "2019-03-01T09:00:00Z");
    assert.equal("hostPath" in payload.recordings[0], false);
  });
});

test("read-handle requires approval and includes no audio", async () => {
  await withTempCorpus(async (root) => {
    const filename = "20190301 090000-0A1B2C3D.m4a";
    await writeFile(path.join(root, filename), "not real audio");

    await assert.rejects(
      () => run(["--corpus-root", root, "read-handle", filename]),
      /requires --approval/u,
    );

    const { code, stdout } = await captureRun([
      "--corpus-root",
      root,
      "read-handle",
      filename,
      "--approval",
      "operator-approved sample",
    ]);

    assert.equal(code, 0);
    const payload = JSON.parse(stdout);
    assert.equal(payload.handle.readOnly, true);
    assert.equal(payload.handle.audioBytesIncluded, false);
    assert.equal(payload.handle.approval, "operator-approved sample");
  });
});

test("refuses mutation or upload commands", async () => {
  await assert.rejects(() => run(["delete", "anything"]), /refusing unsupported/u);
  await assert.rejects(() => run(["--corpus-root", "/tmp/example", "upload"]), /refusing unsupported/u);
});

test("captures Voice Memos timestamp from filename", () => {
  assert.equal(capturedAtFromFilename("20190301 090000-0A1B2C3D.m4a"), "2019-03-01T09:00:00Z");
  assert.equal(capturedAtFromFilename("not-a-voice-memo.m4a"), null);
});
