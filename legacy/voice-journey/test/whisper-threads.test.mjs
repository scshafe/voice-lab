import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { constants } from "node:fs";
import { access, chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const SCRIPT = fileURLToPath(new URL("../src/local-stt.mjs", import.meta.url));

// A fake ffmpeg (writes the wav) and a fake whisper-cli (logs its argv, writes
// the -of outputs), so the real STT stage runs without audio or a model.
const FAKE_FFMPEG = `#!/bin/sh
for a; do last="$a"; done
case "$1" in -version) echo fake-ffmpeg; exit 0;; esac
: > "$last"
`;
const FAKE_WHISPER = `#!/bin/sh
case "$1" in --version) echo fake-whisper; exit 0;; esac
printf '%s\\n' "$*" >> "$WHISPER_ARGS_LOG"
while [ $# -gt 0 ]; do [ "$1" = "-of" ] && base="$2"; shift; done
printf 'x' > "$base.txt"
printf '{"result":{"language":"en"},"transcription":[{"text":"x","offsets":{"from":0,"to":1000}}]}' > "$base.json"
`;

// The fakes must be executable from the temp dir; a noexec /tmp (docker
// `--tmpfs /tmp`, hardened hosts) cannot run them, so those tests skip there.
async function tmpdirAllowsExec() {
  const dir = await mkdtemp(path.join(tmpdir(), "vj-exec-probe-"));
  try {
    const probe = path.join(dir, "probe");
    await writeFile(probe, "#!/bin/sh\n");
    await chmod(probe, 0o755);
    await access(probe, constants.X_OK);
    return true;
  } catch {
    return false;
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}
const SKIP = (await tmpdirAllowsExec()) ? false : "temp dir is mounted noexec";

async function runStage(extraEnv, extraArgs = []) {
  const root = await mkdtemp(path.join(tmpdir(), "vj-threads-"));
  try {
    const ffmpeg = path.join(root, "ffmpeg");
    const whisper = path.join(root, "whisper-cli");
    await writeFile(ffmpeg, FAKE_FFMPEG);
    await writeFile(whisper, FAKE_WHISPER);
    await chmod(ffmpeg, 0o755);
    await chmod(whisper, 0o755);
    const index = path.join(root, "index.json");
    await writeFile(index, JSON.stringify({
      schemaVersion: "voice-journey.recording-index.v1",
      generatedAt: "2026-07-16T00:00:00.000Z",
      totals: { recordings: 1 },
      recordings: [{ recordingId: "a", filename: "a.m4a", capturedAt: "2019-01-01T00:00:00Z", sourceRef: { seam: "s", selector: "a", filename: "a.m4a" } }],
    }));
    const argsLog = path.join(root, "args.log");
    const env = { ...process.env, WHISPER_CPP_BIN: whisper, FFMPEG_BIN: ffmpeg, WHISPER_ARGS_LOG: argsLog, ...extraEnv };
    delete env.VOICE_JOURNEY_DATA;
    if (!("WHISPER_CPP_THREADS" in extraEnv)) delete env.WHISPER_CPP_THREADS;
    const child = spawn(process.execPath, [
      SCRIPT, "transcribe-index", "--index", index, "--corpus-root", root, "--approval", "test",
      "--out", path.join(root, "out.json"), "--transcript-dir", path.join(root, "t"), "--model", "m.bin", ...extraArgs,
    ], { env, stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.stdout.resume();
    const code = await new Promise((resolve) => child.on("close", resolve));
    let args = null;
    try { args = (await readFile(argsLog, "utf8")).trim(); } catch { /* whisper never ran */ }
    return { code, stderr, args };
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("WHISPER_CPP_THREADS unset: whisper-cli gets no -t (current behaviour)", { skip: SKIP }, async () => {
  const { code, stderr, args } = await runStage({});
  assert.equal(code, 0, stderr);
  assert.ok(args);
  assert.doesNotMatch(args, /(^| )-t( |$)/u);
});

test("WHISPER_CPP_THREADS=2: whisper-cli is invoked with -t 2", { skip: SKIP }, async () => {
  const { code, stderr, args } = await runStage({ WHISPER_CPP_THREADS: "2" });
  assert.equal(code, 0, stderr);
  assert.match(args, /(^| )-t 2( |$)/u);
});

test("--threads wins over WHISPER_CPP_THREADS", { skip: SKIP }, async () => {
  const { code, stderr, args } = await runStage({ WHISPER_CPP_THREADS: "2" }, ["--threads", "3"]);
  assert.equal(code, 0, stderr);
  assert.match(args, /(^| )-t 3( |$)/u);
  assert.doesNotMatch(args, /-t 2/u);
});

test("a non-integer WHISPER_CPP_THREADS is refused before whisper runs", async () => {
  const { code, stderr, args } = await runStage({ WHISPER_CPP_THREADS: "four" });
  assert.notEqual(code, 0);
  assert.match(stderr, /WHISPER_CPP_THREADS must be a positive integer/u);
  assert.equal(args, null);
});
