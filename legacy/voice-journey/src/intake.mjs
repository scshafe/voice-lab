// Intake API for the native capture client (voice-journey-capture).
//
//   POST /api/intake/sessions                       open (or resume) a session with its manifest
//   GET  /api/intake/sessions/<id>                  status: per-file bytes received
//   PUT  /api/intake/sessions/<id>/files/<name>     upload one file (whole, or resumable via Content-Range)
//   POST /api/intake/sessions/<id>/finalize         verify everything, then publish atomically
//
// Fail-closed: uploads land in the staging dir, which the watcher never
// scans. Finalize re-verifies every size and sha256 and, only if all of it
// holds, renames the whole session directory into corpus/measurement/<id>/ in
// one step (same volume, so the rename is atomic). Anything less leaves
// corpus/measurement untouched. The server never logs file names' contents,
// audio or transcript text — counts and ids only.
//
// Identity comes from the door (oauth2-proxy) headers; the app must only be
// reachable through it. No identity, no intake.

import { createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, readFile, readdir, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

export const INTAKE_SCHEMA_VERSION = "voice-journey.intake-session.v1";

const SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$/u;
const FILE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.wav$/u;
const SHA256_RE = /^[0-9a-f]{64}$/u;
const MAX_MANIFEST_BYTES = 256 * 1024;
const RESERVED_NAMES = new Set(["session.json"]);

export const DEFAULT_LIMITS = Object.freeze({
  maxFiles: 64,
  maxFileBytes: 1024 ** 3,
  maxSessionBytes: 4 * 1024 ** 3,
  maxOpenSessions: 8,
  staleMs: 7 * 24 * 60 * 60 * 1000,
});

class IntakeError extends Error {
  constructor(status, code, detail = {}) {
    super(code);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function sendJson(response, status, payload) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  response.end(`${JSON.stringify(payload)}\n`);
}

export function identityFromHeaders(headers) {
  const clean = (value) => {
    const text = Array.isArray(value) ? value[0] : value;
    return typeof text === "string" && text.trim() !== "" && text.length <= 256 ? text.trim() : null;
  };
  const user = clean(headers["x-forwarded-user"]);
  const email = clean(headers["x-forwarded-email"]);
  return user || email ? { user, email } : null;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256Hex(text) {
  return createHash("sha256").update(text).digest("hex");
}

async function sha256OfFile(filePath) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filePath)) hash.update(chunk);
  return hash.digest("hex");
}

async function statOrNull(filePath) {
  try {
    return await stat(filePath);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

function isInteger(value) {
  return Number.isInteger(value) && Number.isSafeInteger(value);
}

// Validates a manifest against the chain registry and limits; returns the
// normalized manifest that is stored. Throws IntakeError(422/…) on any defect.
export function validateManifest(input, registry, limits = DEFAULT_LIMITS) {
  const bad = (code, detail) => new IntakeError(422, code, detail);
  if (!input || typeof input !== "object" || Array.isArray(input)) throw bad("manifest_not_object");
  const { sessionId, chainId } = input;
  if (typeof sessionId !== "string" || !SESSION_ID_RE.test(sessionId)) throw bad("invalid_session_id");
  const chain = (registry?.chains ?? []).find((candidate) => candidate.chainId === chainId);
  if (!chain) throw bad("unknown_chain", { chainId: String(chainId).slice(0, 64) });
  if (chain.kind !== "measurement" || !/umik/iu.test(chain.deviceName ?? "")) {
    throw bad("chain_not_accepted", { chainId, reason: "only UMIK-1 measurement chains are accepted at launch" });
  }
  for (const key of ["startedAt", "endedAt"]) {
    if (typeof input[key] !== "string" || Number.isNaN(Date.parse(input[key]))) throw bad("invalid_timestamp", { field: key });
  }
  const capture = input.capture;
  if (!capture || typeof capture !== "object") throw bad("capture_required");
  if (!Number.isFinite(capture.inputVolume)) throw bad("input_level_required");
  if (!Number.isFinite(capture.distanceCm) || capture.distanceCm <= 0) throw bad("distance_required");
  if (capture.calFileSha256 !== null && !(typeof capture.calFileSha256 === "string" && SHA256_RE.test(capture.calFileSha256))) {
    throw bad("invalid_cal_file_hash", { hint: "64 lowercase hex, or null when the cal file is missing" });
  }
  if (capture.sampleRate !== undefined && chain.sampleRate && capture.sampleRate !== chain.sampleRate) {
    throw bad("sample_rate_mismatch", { expected: chain.sampleRate });
  }
  const files = input.files;
  if (!Array.isArray(files) || files.length === 0 || files.length > limits.maxFiles) throw bad("invalid_file_list", { max: limits.maxFiles });
  const names = new Set();
  let total = 0;
  const normalizedFiles = files.map((file) => {
    if (!file || typeof file.name !== "string" || !FILE_NAME_RE.test(file.name) || RESERVED_NAMES.has(file.name)) throw bad("invalid_file_name");
    if (names.has(file.name)) throw bad("duplicate_file_name");
    names.add(file.name);
    if (typeof file.sha256 !== "string" || !SHA256_RE.test(file.sha256)) throw bad("invalid_file_sha256");
    if (!isInteger(file.sizeBytes) || file.sizeBytes <= 0) throw bad("invalid_file_size");
    if (file.sizeBytes > limits.maxFileBytes) throw bad("file_too_large", { maxFileBytes: limits.maxFileBytes });
    total += file.sizeBytes;
    return { name: file.name, sha256: file.sha256, sizeBytes: file.sizeBytes };
  });
  if (total > limits.maxSessionBytes) throw bad("session_too_large", { maxSessionBytes: limits.maxSessionBytes });
  const segments = input.segments;
  if (!Array.isArray(segments) || segments.length === 0) throw bad("segment_map_required");
  const normalizedSegments = segments.map((segment) => {
    if (!segment || typeof segment.key !== "string" || typeof segment.file !== "string") throw bad("invalid_segment");
    if (!names.has(segment.file)) throw bad("segment_file_not_listed", { key: segment.key.slice(0, 64) });
    return { ...segment };
  });
  return {
    schemaVersion: INTAKE_SCHEMA_VERSION,
    sessionId,
    chainId,
    chainKind: chain.kind,
    controlled: Boolean(chain.controlled),
    startedAt: input.startedAt,
    endedAt: input.endedAt,
    capture: { ...capture },
    segments: normalizedSegments,
    files: normalizedFiles,
  };
}

function parseContentRange(header, declaredSize) {
  const match = /^bytes (\d+)-(\d+)\/(\d+)$/u.exec(header ?? "");
  if (!match) throw new IntakeError(400, "invalid_content_range");
  const [start, end, total] = match.slice(1).map(Number);
  if (total !== declaredSize || end < start || end >= total) throw new IntakeError(416, "content_range_out_of_bounds", { sizeBytes: declaredSize });
  return { start, end, total };
}

export function createIntake({
  measurementDir,
  stagingDir,
  chainsPath,
  limits = {},
  log = (line) => process.stderr.write(`[intake] ${line}\n`),
  now = () => Date.now(),
} = {}) {
  if (!measurementDir || !stagingDir || !chainsPath) throw new Error("createIntake requires measurementDir, stagingDir and chainsPath");
  const lim = { ...DEFAULT_LIMITS, ...limits };
  const busy = new Set();

  const sessionDir = (id) => path.join(stagingDir, id);
  const filesDir = (id) => path.join(sessionDir(id), "files");

  async function loadRegistry() {
    return JSON.parse(await readFile(chainsPath, "utf8"));
  }

  async function readStaged(id) {
    try {
      return JSON.parse(await readFile(path.join(sessionDir(id), "manifest.json"), "utf8"));
    } catch (error) {
      if (error?.code === "ENOENT") return null;
      throw error;
    }
  }

  async function readAccepted(id) {
    try {
      return JSON.parse(await readFile(path.join(measurementDir, id, "session.json"), "utf8"));
    } catch (error) {
      if (error?.code === "ENOENT") return null;
      throw error;
    }
  }

  async function progress(manifest) {
    const files = [];
    for (const file of manifest.files) {
      const done = await statOrNull(path.join(filesDir(manifest.sessionId), file.name));
      const part = done ? null : await statOrNull(path.join(filesDir(manifest.sessionId), `${file.name}.part`));
      files.push({
        name: file.name,
        sizeBytes: file.sizeBytes,
        receivedBytes: done ? done.size : part ? part.size : 0,
        complete: Boolean(done),
      });
    }
    return files;
  }

  async function withLock(id, work) {
    if (busy.has(id)) throw new IntakeError(409, "session_busy");
    busy.add(id);
    try {
      return await work();
    } finally {
      busy.delete(id);
    }
  }

  async function purgeStale() {
    let names;
    try {
      names = await readdir(stagingDir);
    } catch (error) {
      if (error?.code === "ENOENT") return 0;
      throw error;
    }
    let open = 0;
    for (const name of names) {
      const info = await statOrNull(path.join(stagingDir, name));
      if (!info?.isDirectory()) continue;
      if (now() - info.mtimeMs > lim.staleMs && !busy.has(name)) {
        await rm(path.join(stagingDir, name), { recursive: true, force: true });
      } else {
        open += 1;
      }
    }
    return open;
  }

  async function openSession(request) {
    const body = await readBody(request);
    let parsed;
    try {
      parsed = JSON.parse(body);
    } catch {
      throw new IntakeError(400, "invalid_json");
    }
    const manifest = validateManifest(parsed, await loadRegistry(), lim);
    const digest = sha256Hex(canonicalJson(manifest));
    return withLock(manifest.sessionId, async () => {
      const accepted = await readAccepted(manifest.sessionId);
      if (accepted) {
        if (accepted.intake?.manifestSha256 === digest) return { status: 200, payload: { sessionId: manifest.sessionId, state: "accepted" } };
        throw new IntakeError(409, "session_already_accepted");
      }
      const staged = await readStaged(manifest.sessionId);
      if (staged) {
        if (sha256Hex(canonicalJson(staged)) !== digest) throw new IntakeError(409, "session_manifest_conflict");
        return { status: 200, payload: { sessionId: manifest.sessionId, state: "open", files: await progress(staged) } };
      }
      if ((await purgeStale()) >= lim.maxOpenSessions) throw new IntakeError(429, "too_many_open_sessions");
      await mkdir(filesDir(manifest.sessionId), { recursive: true });
      await writeFile(path.join(sessionDir(manifest.sessionId), "manifest.json"), `${JSON.stringify(manifest)}\n`);
      log(`session ${manifest.sessionId} opened files=${manifest.files.length}`);
      return { status: 201, payload: { sessionId: manifest.sessionId, state: "open", files: await progress(manifest), limits: { maxFileBytes: lim.maxFileBytes } } };
    });
  }

  async function sessionStatus(id) {
    const accepted = await readAccepted(id);
    if (accepted) return { status: 200, payload: { sessionId: id, state: "accepted" } };
    const staged = await readStaged(id);
    if (!staged) throw new IntakeError(404, "session_not_found");
    return { status: 200, payload: { sessionId: id, state: "open", files: await progress(staged) } };
  }

  async function putFile(request, id, name) {
    const staged = await readStaged(id);
    if (!staged) {
      if (await readAccepted(id)) throw new IntakeError(409, "session_already_accepted");
      throw new IntakeError(404, "session_not_found");
    }
    const file = staged.files.find((candidate) => candidate.name === name);
    if (!file) throw new IntakeError(404, "file_not_in_manifest");
    return withLock(`${id}/${name}`, async () => {
      const dir = filesDir(id);
      const finalPath = path.join(dir, name);
      const partPath = `${finalPath}.part`;
      const existing = await statOrNull(finalPath);
      if (existing) {
        // Idempotent re-upload: a verified file is never rewritten.
        request.resume();
        return { status: 200, payload: { name, state: "already_received", sizeBytes: existing.size } };
      }
      const declaredLength = Number(request.headers["content-length"]);
      if (!isInteger(declaredLength) || declaredLength <= 0) throw new IntakeError(411, "content_length_required");
      if (declaredLength > file.sizeBytes) throw new IntakeError(413, "file_too_large", { sizeBytes: file.sizeBytes });

      let start = 0;
      if (request.headers["content-range"] !== undefined) {
        const range = parseContentRange(request.headers["content-range"], file.sizeBytes);
        const have = (await statOrNull(partPath))?.size ?? 0;
        if (range.start !== have) throw new IntakeError(409, "range_not_contiguous", { receivedBytes: have });
        if (range.end - range.start + 1 !== declaredLength) throw new IntakeError(400, "content_length_range_mismatch");
        start = range.start;
      } else {
        if (declaredLength !== file.sizeBytes) throw new IntakeError(422, "size_mismatch", { sizeBytes: file.sizeBytes });
        await rm(partPath, { force: true });
      }

      const written = await streamTo(request, partPath, { append: start > 0, expected: declaredLength });
      if (written !== declaredLength) {
        if (start === 0) await rm(partPath, { force: true });
        throw new IntakeError(400, "incomplete_body", { receivedBytes: start > 0 ? (await statOrNull(partPath))?.size ?? 0 : 0 });
      }
      const partSize = (await stat(partPath)).size;
      if (partSize < file.sizeBytes) {
        return { status: 202, payload: { name, state: "partial", receivedBytes: partSize } };
      }
      const digest = await sha256OfFile(partPath);
      if (digest !== file.sha256) {
        await rm(partPath, { force: true });
        throw new IntakeError(422, "checksum_mismatch", { name });
      }
      await rename(partPath, finalPath);
      return { status: 200, payload: { name, state: "received", sizeBytes: file.sizeBytes } };
    });
  }

  async function finalize(request, id) {
    request.resume();
    return withLock(id, async () => {
      const accepted = await readAccepted(id);
      if (accepted) return { status: 200, payload: { sessionId: id, state: "accepted" } };
      const staged = await readStaged(id);
      if (!staged) throw new IntakeError(404, "session_not_found");
      const dir = filesDir(id);

      const missing = [];
      const mismatched = [];
      for (const file of staged.files) {
        const info = await statOrNull(path.join(dir, file.name));
        if (!info) {
          missing.push(file.name);
        } else if (info.size !== file.sizeBytes || (await sha256OfFile(path.join(dir, file.name))) !== file.sha256) {
          mismatched.push(file.name);
        }
      }
      if (mismatched.length > 0) {
        // On-disk bytes disagree with the manifest: drop them so the client re-sends; nothing is published.
        for (const name of mismatched) await rm(path.join(dir, name), { force: true });
        throw new IntakeError(422, "checksum_mismatch", { files: mismatched });
      }
      if (missing.length > 0) throw new IntakeError(409, "incomplete", { missing });

      const expected = new Set([...staged.files.map((file) => file.name)]);
      for (const entry of await readdir(dir)) {
        if (!expected.has(entry)) await rm(path.join(dir, entry), { recursive: true, force: true });
      }

      const stagedStat = await stat(stagingDir);
      await mkdir(measurementDir, { recursive: true });
      if (stagedStat.dev !== (await stat(measurementDir)).dev) {
        throw new IntakeError(500, "staging_not_on_corpus_volume");
      }
      const record = {
        ...staged,
        intake: {
          acceptedAt: new Date(now()).toISOString(),
          acceptedBy: identityFromHeaders(request.headers)?.user ?? identityFromHeaders(request.headers)?.email ?? null,
          manifestSha256: sha256Hex(canonicalJson(staged)),
        },
      };
      await writeFile(path.join(dir, "session.json"), `${JSON.stringify(record, null, 2)}\n`);
      try {
        await rename(dir, path.join(measurementDir, id));
      } catch (error) {
        if (error?.code === "EEXIST" || error?.code === "ENOTEMPTY") throw new IntakeError(409, "session_already_accepted");
        throw error;
      }
      await rm(sessionDir(id), { recursive: true, force: true });
      const bytes = staged.files.reduce((sum, file) => sum + file.sizeBytes, 0);
      log(`session ${id} accepted files=${staged.files.length} bytes=${bytes}`);
      return { status: 200, payload: { sessionId: id, state: "accepted", files: staged.files.length, bytes } };
    });
  }

  // Returns true when the request belonged to the intake API.
  async function handle(request, response, url) {
    if (!url.pathname.startsWith("/api/intake/")) return false;
    try {
      if (!identityFromHeaders(request.headers)) throw new IntakeError(401, "identity_required");
      const parts = url.pathname.split("/").slice(3); // after /api/intake/
      if (parts[0] !== "sessions") throw new IntakeError(404, "not_found");
      const id = parts[1] === undefined ? null : decodeURIComponent(parts[1]);
      if (id !== null && !SESSION_ID_RE.test(id)) throw new IntakeError(400, "invalid_session_id");
      let result;
      if (parts.length === 1) {
        if (request.method !== "POST") throw new IntakeError(405, "method_not_allowed");
        result = await openSession(request);
      } else if (parts.length === 2) {
        if (request.method !== "GET") throw new IntakeError(405, "method_not_allowed");
        result = await sessionStatus(id);
      } else if (parts.length === 3 && parts[2] === "finalize") {
        if (request.method !== "POST") throw new IntakeError(405, "method_not_allowed");
        result = await finalize(request, id);
      } else if (parts.length === 4 && parts[2] === "files") {
        if (request.method !== "PUT") throw new IntakeError(405, "method_not_allowed");
        const name = decodeURIComponent(parts[3]);
        if (!FILE_NAME_RE.test(name)) throw new IntakeError(400, "invalid_file_name");
        result = await putFile(request, id, name);
      } else {
        throw new IntakeError(404, "not_found");
      }
      sendJson(response, result.status, result.payload);
    } catch (error) {
      if (error instanceof IntakeError) {
        if (!request.readableEnded) request.resume();
        sendJson(response, error.status, { error: error.code, ...error.detail });
      } else {
        log(`internal error: ${error?.code ?? "unknown"}`);
        sendJson(response, 500, { error: "internal_error" });
      }
    }
    return true;
  }

  return { handle, purgeStale, limits: lim };
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_MANIFEST_BYTES) {
        reject(new IntakeError(413, "manifest_too_large"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    request.on("error", reject);
  });
}

// Streams the request body to disk, refusing more than `expected` bytes.
function streamTo(request, filePath, { append, expected }) {
  return new Promise((resolve, reject) => {
    const out = createWriteStream(filePath, { flags: append ? "a" : "w", mode: 0o640 });
    let received = 0;
    let failed = false;
    const fail = (error) => {
      if (failed) return;
      failed = true;
      out.destroy();
      reject(error);
    };
    request.on("data", (chunk) => {
      received += chunk.length;
      if (received > expected) {
        fail(new IntakeError(413, "body_exceeds_content_length"));
        return;
      }
      if (!out.write(chunk)) {
        request.pause();
        out.once("drain", () => request.resume());
      }
    });
    request.on("aborted", () => fail(new IntakeError(400, "upload_aborted")));
    request.on("error", fail);
    request.on("end", () => {
      if (failed) return;
      out.end(() => resolve(received));
    });
    out.on("error", fail);
  });
}
