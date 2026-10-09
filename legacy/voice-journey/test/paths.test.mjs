import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { test } from "node:test";

import { assertBindAllowed, GENERATED_MANIFESTS, INPUT_MANIFESTS, isInsideArtifacts, resolvePaths, resolveTools } from "../src/paths.mjs";

const execFileAsync = promisify(execFile);
const repoRoot = fileURLToPath(new URL("..", import.meta.url));

test("without a data root the layout is the historical local-dev one", () => {
  const paths = resolvePaths({});
  assert.equal(paths.dataRoot, null);
  assert.equal(paths.manifest("recording-index.json"), path.join("manifests", "recording-index.json"));
  assert.equal(paths.artifact("stt-transcripts"), path.join("local-artifacts", "stt-transcripts"));
  assert.match(paths.phoneCorpusRoot, /Group Containers\/group\.com\.apple\.VoiceMemos\.shared\/Recordings$/u);
  assert.equal(paths.chains, path.join("manifests", "chains.json"));
});

test("a data root owns corpus, artifacts, generated manifests and staging; chains stay in the source tree", () => {
  const paths = resolvePaths({ VOICE_JOURNEY_DATA: "/data" });
  assert.equal(paths.phoneCorpusRoot, "/data/corpus/phone");
  assert.equal(paths.measurementDir, "/data/corpus/measurement");
  assert.equal(paths.manifest("song-journeys.json"), "/data/manifests/song-journeys.json");
  assert.equal(paths.artifact("voice-features"), "/data/artifacts/voice-features");
  assert.equal(paths.stagingDir, "/data/staging");
  assert.equal(paths.chains, path.join("manifests", "chains.json"));
  assert.equal(resolvePaths({ VOICE_JOURNEY_DATA: "" }).dataRoot, null);
});

test("explicit env overrides win over the data root", () => {
  const paths = resolvePaths({ VOICE_JOURNEY_DATA: "/data", VOICE_JOURNEY_CORPUS_ROOT: "/mnt/phone", VOICE_JOURNEY_CHAINS: "/etc/chains.json" });
  assert.equal(paths.phoneCorpusRoot, "/mnt/phone");
  assert.equal(paths.chains, "/etc/chains.json");
});

test("tool paths come from the environment and default to PATH names, never Homebrew", () => {
  const tools = resolveTools({});
  assert.equal(tools.ffmpegBin, "ffmpeg");
  assert.equal(tools.whisperBin, "whisper-cli");
  assert.doesNotMatch(JSON.stringify(tools), /homebrew/u);
  const set = resolveTools({ FFMPEG_BIN: "/usr/bin/ffmpeg", WHISPER_CPP_BIN: "/usr/local/bin/whisper-cli", WHISPER_CPP_MODEL: "/m.bin", VOICE_JOURNEY_PYTHON: "/opt/venv/bin/python" });
  assert.deepEqual(set, { ffmpegBin: "/usr/bin/ffmpeg", whisperBin: "/usr/local/bin/whisper-cli", whisperModel: "/m.bin", whisperThreads: null, venvPython: "/opt/venv/bin/python" });
  assert.equal(resolveTools({ VOICE_JOURNEY_DATA: "/data" }).whisperModel, "/data/models/ggml-medium.bin");
});

test("WHISPER_CPP_THREADS: unset or empty is null, a positive integer is a number, anything else is refused", () => {
  assert.equal(resolveTools({}).whisperThreads, null);
  assert.equal(resolveTools({ WHISPER_CPP_THREADS: "" }).whisperThreads, null);
  assert.equal(resolveTools({ WHISPER_CPP_THREADS: "2" }).whisperThreads, 2);
  for (const bad of ["0", "-1", "2.5", "two", "4 ", "0x4", "1e1", "04"]) {
    assert.throws(() => resolveTools({ WHISPER_CPP_THREADS: bad }), /WHISPER_CPP_THREADS must be a positive integer/u, bad);
  }
});

test("the manifest split is explicit and disjoint", () => {
  assert.deepEqual([...INPUT_MANIFESTS], ["chains.json"]);
  for (const name of INPUT_MANIFESTS) assert.ok(!GENERATED_MANIFESTS.includes(name));
});

test("isInsideArtifacts follows the artifacts dir", () => {
  assert.equal(isInsideArtifacts(path.join(process.cwd(), "local-artifacts", "x"), {}), true);
  assert.equal(isInsideArtifacts("/tmp/x", {}), false);
  assert.equal(isInsideArtifacts("/data/artifacts/stt/a.txt", { VOICE_JOURNEY_DATA: "/data" }), true);
  assert.equal(isInsideArtifacts("/data/manifests/a.json", { VOICE_JOURNEY_DATA: "/data" }), false);
});

test("loopback-only binding outside the container, any address inside it", () => {
  assert.doesNotThrow(() => assertBindAllowed("127.0.0.1", {}));
  assert.throws(() => assertBindAllowed("0.0.0.0", {}), /loopback only/u);
  assert.throws(() => assertBindAllowed("100.64.0.1", {}), /loopback only/u);
  assert.doesNotThrow(() => assertBindAllowed("0.0.0.0", { VOICE_JOURNEY_CONTAINER: "1" }));
});

async function runBrowser(env, args = []) {
  return execFileAsync(process.execPath, ["src/corpus-browser.mjs", "--port", "0", "--once", ...args], {
    cwd: repoRoot,
    env: { PATH: process.env.PATH, ...env },
  });
}

test("server on an empty data root starts, reports its stores under the data root, and refuses a wide bind outside the container", async () => {
  const data = await mkdtemp(path.join(tmpdir(), "voice-journey-data-"));
  try {
    const { stdout } = await runBrowser({ VOICE_JOURNEY_DATA: data });
    const started = JSON.parse(stdout);
    assert.equal(started.rows, 0);
    assert.equal(started.statePath, path.join(data, "artifacts", "corpus-browser-state.json"));
    assert.equal(started.transcriptText.enabled, true);

    await assert.rejects(runBrowser({ VOICE_JOURNEY_DATA: data, VOICE_JOURNEY_HOST: "0.0.0.0" }), /loopback only/u);
    const wide = await runBrowser({ VOICE_JOURNEY_DATA: data, VOICE_JOURNEY_HOST: "0.0.0.0", VOICE_JOURNEY_CONTAINER: "1" });
    assert.match(JSON.parse(wide.stdout).endpoints[0], /^http:\/\/0\.0\.0\.0:\d+\/$/u);
  } finally {
    await rm(data, { recursive: true, force: true });
  }
});

test("healthz answers without identity", async () => {
  const data = await mkdtemp(path.join(tmpdir(), "voice-journey-data-"));
  try {
    const { createRequestHandler } = await import("../src/corpus-browser.mjs");
    const http = await import("node:http");
    const server = http.createServer(createRequestHandler({ rows: [], reviewQueue: [] }, {}));
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    try {
      const response = await fetch(`http://127.0.0.1:${server.address().port}/healthz`);
      assert.equal(response.status, 200);
      assert.deepEqual(await response.json(), { ok: true });
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }
  } finally {
    await rm(data, { recursive: true, force: true });
  }
});
