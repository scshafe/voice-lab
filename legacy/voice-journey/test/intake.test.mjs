import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import http from "node:http";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import { listMeasurementEntries } from "../src/host-access.mjs";
import { identityFromHeaders, createIntake, validateManifest } from "../src/intake.mjs";
import { buildManifest, run as runIndex } from "../src/index-recordings.mjs";
import { createWatchd } from "../src/watchd.mjs";

const REGISTRY = {
  schemaVersion: "voice-journey.chains.v1",
  chains: [
    { chainId: "iphone-voicememos", kind: "phone", controlled: false },
    { chainId: "umik1", kind: "measurement", controlled: true, deviceName: "umik", sampleRate: 48000 },
    { chainId: "other-mic", kind: "measurement", controlled: true, deviceName: "scarlett", sampleRate: 48000 },
  ],
};
const HEADERS = { "x-forwarded-user": "cole", "x-forwarded-email": "cole@example.test" };
const sha = (buffer) => createHash("sha256").update(buffer).digest("hex");

function wav(seed, size) {
  return Buffer.alloc(size, seed);
}

function makeManifest(overrides = {}, payloads = { "01-silence.wav": wav(1, 3000), "02-song.wav": wav(2, 5000) }) {
  const files = Object.entries(payloads).map(([name, buffer]) => ({ name, sha256: sha(buffer), sizeBytes: buffer.length }));
  return {
    sessionId: "s-20261003-101500",
    chainId: "umik1",
    startedAt: "2026-10-03T10:15:00.000Z",
    endedAt: "2026-10-03T10:25:00.000Z",
    capture: { sampleRate: 48000, bitDepth: 24, channels: 1, inputVolume: 62, distanceCm: 35, calFileSha256: "a".repeat(64) },
    segments: [
      { key: "silence", label: "room silence", file: "01-silence.wav", seconds: 10 },
      { key: "song", label: "anchor song", file: "02-song.wav" },
    ],
    files,
    ...overrides,
  };
}

async function withIntake(callback, options = {}) {
  const root = await mkdtemp(path.join(tmpdir(), "voice-journey-intake-"));
  const chainsPath = path.join(root, "chains.json");
  await writeFile(chainsPath, JSON.stringify(REGISTRY));
  const logs = [];
  const intake = createIntake({
    measurementDir: path.join(root, "corpus", "measurement"),
    stagingDir: path.join(root, "staging"),
    chainsPath,
    log: (line) => logs.push(line),
    ...options,
  });
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://x");
    if (!(await intake.handle(request, response, url))) {
      response.writeHead(404).end();
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = async (method, pathname, { body, headers = HEADERS, json } = {}) => {
    const response = await fetch(`${base}${pathname}`, {
      method,
      headers: { ...(json !== undefined ? { "content-type": "application/json" } : {}), ...headers },
      body: json !== undefined ? JSON.stringify(json) : body,
    });
    return { status: response.status, body: await response.json() };
  };
  try {
    return await callback({ root, call, logs, measurementDir: path.join(root, "corpus", "measurement"), stagingDir: path.join(root, "staging"), intake });
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
}

const PAYLOADS = { "01-silence.wav": wav(1, 3000), "02-song.wav": wav(2, 5000) };

async function uploadAll(call, id, payloads = PAYLOADS) {
  for (const [name, buffer] of Object.entries(payloads)) {
    const result = await call("PUT", `/api/intake/sessions/${id}/files/${name}`, { body: buffer });
    assert.equal(result.status, 200, JSON.stringify(result.body));
  }
}

test("a bearer-token identity is the same door headers: email only is enough, Authorization alone is not", async () => {
  // oauth2-proxy (--skip-jwt-bearer-tokens) validates the bearer JWT and sets
  // X-Forwarded-User / X-Forwarded-Email; the app never reads Authorization.
  await withIntake(async ({ call }) => {
    const viaBearer = await call("POST", "/api/intake/sessions", {
      json: makeManifest(),
      headers: { "x-forwarded-email": "cole@example.test" },
    });
    assert.equal(viaBearer.status, 201, JSON.stringify(viaBearer.body));
    const rawBearer = await call("GET", "/api/intake/sessions/s-20261003-101500", {
      headers: { authorization: "Bearer not-validated-by-the-app" },
    });
    assert.equal(rawBearer.status, 401);
  });
});

test("identity comes from the door headers; intake refuses without them", async () => {
  assert.deepEqual(identityFromHeaders({ "x-forwarded-user": "cole" }), { user: "cole", email: null });
  assert.equal(identityFromHeaders({}), null);
  assert.equal(identityFromHeaders({ "x-forwarded-user": "  " }), null);
  await withIntake(async ({ call }) => {
    const denied = await call("POST", "/api/intake/sessions", { json: makeManifest(), headers: {} });
    assert.equal(denied.status, 401);
    assert.equal(denied.body.error, "identity_required");
    assert.equal((await call("GET", "/api/intake/sessions/s-20261003-101500", { headers: {} })).status, 401);
  });
});

test("a good session is accepted atomically into corpus/measurement/<id>", async () => {
  await withIntake(async ({ call, measurementDir, stagingDir, logs }) => {
    const manifest = makeManifest();
    const opened = await call("POST", "/api/intake/sessions", { json: manifest });
    assert.equal(opened.status, 201);
    assert.equal(opened.body.state, "open");
    assert.deepEqual(opened.body.files.map((file) => file.receivedBytes), [0, 0]);

    await uploadAll(call, manifest.sessionId);
    // Nothing is visible to the watcher before finalize.
    assert.deepEqual(await readdir(measurementDir).catch(() => []), []);
    assert.deepEqual(await listMeasurementEntries(measurementDir), []);

    const done = await call("POST", `/api/intake/sessions/${manifest.sessionId}/finalize`);
    assert.equal(done.status, 200, JSON.stringify(done.body));
    assert.deepEqual(done.body, { sessionId: manifest.sessionId, state: "accepted", files: 2, bytes: 8000 });

    const dir = path.join(measurementDir, manifest.sessionId);
    assert.deepEqual((await readdir(dir)).sort(), ["01-silence.wav", "02-song.wav", "session.json"]);
    assert.deepEqual(await readFile(path.join(dir, "02-song.wav")), PAYLOADS["02-song.wav"]);
    const stored = JSON.parse(await readFile(path.join(dir, "session.json"), "utf8"));
    assert.equal(stored.chainId, "umik1");
    assert.equal(stored.capture.distanceCm, 35);
    assert.equal(stored.capture.calFileSha256, "a".repeat(64));
    assert.equal(stored.intake.acceptedBy, "cole");
    assert.deepEqual(await readdir(stagingDir), []);

    // Re-finalize and re-open are idempotent.
    assert.equal((await call("POST", `/api/intake/sessions/${manifest.sessionId}/finalize`)).body.state, "accepted");
    const reopened = await call("POST", "/api/intake/sessions", { json: manifest });
    assert.deepEqual(reopened.body, { sessionId: manifest.sessionId, state: "accepted" });
    const changed = await call("POST", "/api/intake/sessions", { json: makeManifest({ capture: { ...manifest.capture, distanceCm: 50 } }) });
    assert.equal(changed.status, 409);

    // Logs carry ids and counts only.
    assert.ok(logs.length > 0);
    for (const line of logs) assert.doesNotMatch(line, /silence|song|cole/u);
  });
});

test("a bad checksum on upload rejects the file, and the session can never be finalized with it", async () => {
  await withIntake(async ({ call, measurementDir }) => {
    const manifest = makeManifest();
    await call("POST", "/api/intake/sessions", { json: manifest });
    const corrupt = Buffer.from(PAYLOADS["01-silence.wav"]);
    corrupt[10] ^= 0xff;
    const bad = await call("PUT", `/api/intake/sessions/${manifest.sessionId}/files/01-silence.wav`, { body: corrupt });
    assert.equal(bad.status, 422);
    assert.equal(bad.body.error, "checksum_mismatch");
    await call("PUT", `/api/intake/sessions/${manifest.sessionId}/files/02-song.wav`, { body: PAYLOADS["02-song.wav"] });

    const early = await call("POST", `/api/intake/sessions/${manifest.sessionId}/finalize`);
    assert.equal(early.status, 409);
    assert.deepEqual(early.body.missing, ["01-silence.wav"]);
    assert.deepEqual(await readdir(measurementDir).catch(() => []), []);

    // The right bytes then complete it.
    const good = await call("PUT", `/api/intake/sessions/${manifest.sessionId}/files/01-silence.wav`, { body: PAYLOADS["01-silence.wav"] });
    assert.equal(good.status, 200);
    assert.equal((await call("POST", `/api/intake/sessions/${manifest.sessionId}/finalize`)).status, 200);
  });
});

test("finalize re-verifies bytes on disk and publishes nothing when one changed", async () => {
  await withIntake(async ({ call, measurementDir, stagingDir }) => {
    const manifest = makeManifest();
    await call("POST", "/api/intake/sessions", { json: manifest });
    await uploadAll(call, manifest.sessionId);
    const staged = path.join(stagingDir, manifest.sessionId, "files", "02-song.wav");
    await writeFile(staged, wav(9, 5000)); // same size, different bytes
    const result = await call("POST", `/api/intake/sessions/${manifest.sessionId}/finalize`);
    assert.equal(result.status, 422);
    assert.equal(result.body.error, "checksum_mismatch");
    assert.deepEqual(result.body.files, ["02-song.wav"]);
    assert.deepEqual(await readdir(measurementDir).catch(() => []), []);
    // The client re-sends only the dropped file and the session completes.
    const status = await call("GET", `/api/intake/sessions/${manifest.sessionId}`);
    assert.deepEqual(status.body.files.map((file) => file.complete), [true, false]);
    await call("PUT", `/api/intake/sessions/${manifest.sessionId}/files/02-song.wav`, { body: PAYLOADS["02-song.wav"] });
    assert.equal((await call("POST", `/api/intake/sessions/${manifest.sessionId}/finalize`)).status, 200);
  });
});

test("re-uploading a received file is idempotent, and a size mismatch is refused", async () => {
  await withIntake(async ({ call }) => {
    const manifest = makeManifest();
    await call("POST", "/api/intake/sessions", { json: manifest });
    const url = `/api/intake/sessions/${manifest.sessionId}/files/01-silence.wav`;
    assert.equal((await call("PUT", url, { body: PAYLOADS["01-silence.wav"] })).body.state, "received");
    const again = await call("PUT", url, { body: PAYLOADS["01-silence.wav"] });
    assert.equal(again.status, 200);
    assert.equal(again.body.state, "already_received");
    const short = await call("PUT", `/api/intake/sessions/${manifest.sessionId}/files/02-song.wav`, { body: wav(2, 100) });
    assert.equal(short.status, 422);
    assert.equal(short.body.error, "size_mismatch");
    const long = await call("PUT", `/api/intake/sessions/${manifest.sessionId}/files/02-song.wav`, { body: wav(2, 9000) });
    assert.equal(long.status, 413);
    const unlisted = await call("PUT", `/api/intake/sessions/${manifest.sessionId}/files/99-extra.wav`, { body: wav(1, 10) });
    assert.equal(unlisted.status, 404);
  });
});

test("uploads resume with Content-Range from the last byte the server holds", async () => {
  await withIntake(async ({ call }) => {
    const manifest = makeManifest();
    await call("POST", "/api/intake/sessions", { json: manifest });
    const url = `/api/intake/sessions/${manifest.sessionId}/files/02-song.wav`;
    const payload = PAYLOADS["02-song.wav"];
    const first = await call("PUT", url, { body: payload.subarray(0, 2000), headers: { ...HEADERS, "content-range": "bytes 0-1999/5000" } });
    assert.equal(first.status, 202);
    assert.equal(first.body.receivedBytes, 2000);
    const status = await call("GET", `/api/intake/sessions/${manifest.sessionId}`);
    assert.equal(status.body.files[1].receivedBytes, 2000);
    const gap = await call("PUT", url, { body: payload.subarray(3000), headers: { ...HEADERS, "content-range": "bytes 3000-4999/5000" } });
    assert.equal(gap.status, 409);
    assert.equal(gap.body.receivedBytes, 2000);
    const last = await call("PUT", url, { body: payload.subarray(2000), headers: { ...HEADERS, "content-range": "bytes 2000-4999/5000" } });
    assert.equal(last.status, 200);
    assert.equal(last.body.state, "received");
  });
});

test("a resumed upload whose bytes do not add up to the checksum is dropped", async () => {
  await withIntake(async ({ call }) => {
    const manifest = makeManifest();
    await call("POST", "/api/intake/sessions", { json: manifest });
    const url = `/api/intake/sessions/${manifest.sessionId}/files/02-song.wav`;
    await call("PUT", url, { body: wav(7, 2000), headers: { ...HEADERS, "content-range": "bytes 0-1999/5000" } });
    const last = await call("PUT", url, { body: wav(2, 3000), headers: { ...HEADERS, "content-range": "bytes 2000-4999/5000" } });
    assert.equal(last.status, 422);
    const status = await call("GET", `/api/intake/sessions/${manifest.sessionId}`);
    assert.equal(status.body.files[1].receivedBytes, 0);
  });
});

test("manifests are validated against the chain registry and limits", async () => {
  const registry = REGISTRY;
  const reject = (overrides, code) => assert.throws(() => validateManifest(makeManifest(overrides), registry), (error) => error.code === code, code);
  assert.doesNotThrow(() => validateManifest(makeManifest(), registry));
  reject({ chainId: "nope" }, "unknown_chain");
  reject({ chainId: "iphone-voicememos" }, "chain_not_accepted");
  reject({ chainId: "other-mic" }, "chain_not_accepted");
  reject({ sessionId: "../etc" }, "invalid_session_id");
  reject({ capture: { sampleRate: 48000, inputVolume: 62, distanceCm: 35, calFileSha256: null }, startedAt: "yesterday" }, "invalid_timestamp");
  reject({ capture: { sampleRate: 44100, inputVolume: 62, distanceCm: 35, calFileSha256: null } }, "sample_rate_mismatch");
  reject({ capture: { sampleRate: 48000, distanceCm: 35, calFileSha256: null } }, "input_level_required");
  reject({ capture: { sampleRate: 48000, inputVolume: 62, calFileSha256: null } }, "distance_required");
  reject({ capture: { sampleRate: 48000, inputVolume: 62, distanceCm: 35, calFileSha256: "xyz" } }, "invalid_cal_file_hash");
  reject({ segments: [] }, "segment_map_required");
  reject({ segments: [{ key: "x", file: "03-missing.wav" }] }, "segment_file_not_listed");
  reject({ files: [] }, "invalid_file_list");
  const base = makeManifest().files[0];
  reject({ files: [{ ...base, name: "../evil.wav" }] }, "invalid_file_name");
  reject({ files: [{ ...base, name: "session.json" }] }, "invalid_file_name");
  reject({ files: [{ ...base, name: "a.m4a" }] }, "invalid_file_name");
  reject({ files: [base, base] }, "duplicate_file_name");
  reject({ files: [{ ...base, sha256: "ABC" }] }, "invalid_file_sha256");
  reject({ files: [{ ...base, sizeBytes: 0 }] }, "invalid_file_size");
  assert.throws(() => validateManifest(makeManifest(), registry, { maxFiles: 64, maxFileBytes: 100, maxSessionBytes: 1000 }), (error) => error.code === "file_too_large");
  assert.throws(() => validateManifest(makeManifest(), registry, { maxFiles: 64, maxFileBytes: 5000, maxSessionBytes: 6000 }), (error) => error.code === "session_too_large");
});

test("HTTP surface: bad ids, bad names, wrong methods, conflicting manifests, open-session cap", async () => {
  await withIntake(async ({ call }) => {
    assert.equal((await call("POST", "/api/intake/sessions", { json: makeManifest({ chainId: "iphone-voicememos" }) })).status, 422);
    assert.equal((await call("POST", "/api/intake/sessions", { json: makeManifest({ sessionId: "x" }) })).status, 422);
    assert.equal((await call("GET", "/api/intake/sessions/s-20261003-999999")).status, 404);
    assert.equal((await call("PUT", "/api/intake/sessions/s-20261003-101500/files/..%2Fx.wav", { body: "x" })).status, 400);
    assert.equal((await call("DELETE", "/api/intake/sessions/s-20261003-101500")).status, 405);
    assert.equal((await call("GET", "/api/intake/other")).status, 404);
    const manifest = makeManifest();
    assert.equal((await call("POST", "/api/intake/sessions", { json: manifest })).status, 201);
    const again = await call("POST", "/api/intake/sessions", { json: manifest });
    assert.equal(again.status, 200);
    assert.equal(again.body.state, "open");
    const conflict = await call("POST", "/api/intake/sessions", { json: makeManifest({ endedAt: "2026-10-03T11:00:00.000Z" }) });
    assert.equal(conflict.status, 409);
  });
  await withIntake(async ({ call }) => {
    for (const id of ["s-20261003-000001", "s-20261003-000002"]) {
      assert.equal((await call("POST", "/api/intake/sessions", { json: makeManifest({ sessionId: id }) })).status, 201);
    }
    const capped = await call("POST", "/api/intake/sessions", { json: makeManifest({ sessionId: "s-20261003-000003" }) });
    assert.equal(capped.status, 429);
  }, { limits: { maxOpenSessions: 2 } });
});

test("the manifest body is size-limited", async () => {
  await withIntake(async ({ call }) => {
    const huge = makeManifest({ notes: "x".repeat(300 * 1024) });
    const result = await call("POST", "/api/intake/sessions", { json: huge }).catch((error) => ({ status: 413, error }));
    assert.equal(result.status, 413);
  });
});

test("stale staging sessions are purged, not accepted", async () => {
  let clock = Date.now();
  await withIntake(async ({ call, intake, stagingDir }) => {
    await call("POST", "/api/intake/sessions", { json: makeManifest() });
    assert.equal((await readdir(stagingDir)).length, 1);
    clock += 8 * 24 * 60 * 60 * 1000;
    await intake.purgeStale();
    assert.equal((await readdir(stagingDir)).length, 0);
  }, { now: () => clock, limits: { staleMs: 7 * 24 * 60 * 60 * 1000 } });
});

test("an accepted session is indexed as a measurement take and the watcher sees it", async () => {
  await withIntake(async ({ call, root, measurementDir }) => {
    const manifest = makeManifest();
    await call("POST", "/api/intake/sessions", { json: manifest });
    await uploadAll(call, manifest.sessionId);
    await call("POST", `/api/intake/sessions/${manifest.sessionId}/finalize`);

    const entries = await listMeasurementEntries(measurementDir);
    assert.equal(entries.length, 2);
    assert.equal(entries[0].filename, `${manifest.sessionId}/01-silence.wav`);
    assert.deepEqual(entries[0].source, { type: "measurement", chainId: "umik1", sessionId: manifest.sessionId, segmentKey: "silence" });
    assert.equal(entries[0].capturedAt, manifest.startedAt);

    // The index command lists phone and measurement takes together.
    const phone = path.join(root, "phone");
    await mkdir(phone, { recursive: true });
    await writeFile(path.join(phone, "20190301 090000-0A1B2C3D.m4a"), "x");
    const out = path.join(root, "manifests", "recording-index.json");
    const originalWrite = process.stdout.write;
    process.stdout.write = () => true;
    try {
      await runIndex(["--corpus-root", phone, "--measurement-root", measurementDir, "--out", out, "--generated-at", "2026-10-03T00:00:00.000Z"]);
    } finally {
      process.stdout.write = originalWrite;
    }
    const index = JSON.parse(await readFile(out, "utf8"));
    const kinds = index.recordings.map((row) => row.source?.type ?? "phone");
    assert.deepEqual(kinds.sort(), ["measurement", "measurement", "phone"]);
    assert.equal(index.totals.recordings, 3);

    // The watcher's default scan includes measurement sessions and passes the root to the index stage.
    const calls = [];
    const watchd = createWatchd({
      corpusRoot: phone,
      measurementRoot: measurementDir,
      approval: "approved",
      statePath: path.join(root, "state.json"),
      repoRoot: path.join(root, "empty-repo"),
      log: () => {},
    }, {
      runStage: async (stage) => { calls.push(stage); return {}; },
      notify: async () => {},
      now: () => 1_000_000_000_000,
    });
    assert.equal((await watchd.tick()).status, "unstable");
    assert.equal((await watchd.tick()).status, "ran");
    assert.equal(calls[0].key, "index");
    assert.ok(calls[0].args.includes("--measurement-root"));
  });
});

test("index rows built from a measurement entry carry their source", () => {
  const manifest = buildManifest([{ recordingId: "r1", filename: "s-1/01-x.wav", capturedAt: "2026-10-03T10:15:00.000Z", sizeBytes: 5, modifiedAt: "2026-10-03T10:30:00.000Z", source: { type: "measurement", chainId: "umik1", sessionId: "s-1", segmentKey: "x" } }], { generatedAt: "2026-10-03T00:00:00.000Z" });
  assert.equal(manifest.recordings[0].source.type, "measurement");
});
