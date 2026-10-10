// One place that decides where Voice Journey reads and writes.
//
// Local dev (VOICE_JOURNEY_DATA unset): exactly the historical layout, all
// relative to the working directory — generated manifests in `manifests/`,
// gitignored artifacts in `local-artifacts/`, the phone corpus through the
// Voice Memos container.
//
// Container / server (VOICE_JOURNEY_DATA=/data): one data root owns every
// mutable thing, and the app never writes into its source tree:
//
//   $D/corpus/phone/        phone chain mirror (the Mini writes it; the app only reads)
//   $D/corpus/measurement/  accepted capture sessions, <sessionId>/…
//   $D/artifacts/           what local-artifacts/ is today (transcripts, feature
//                           store, app state, report, models-independent scratch)
//   $D/manifests/           GENERATED manifests (index, results, stt, lyrics, features, journeys…)
//   $D/staging/             intake uploads in flight (same volume as corpus/ so
//                           the finalize rename is atomic; never scanned)
//   $D/models/              whisper model (fetched once, sha256-pinned)
//
// INPUT manifests (authored, reviewed, versioned in the repo) never move:
// `manifests/chains.json`. Override with VOICE_JOURNEY_CHAINS.

import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const MAC_PHONE_CORPUS = path.join(homedir(), "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings");

// Generated manifests: outputs of the pipeline, written under manifests/ in
// local dev and under $VOICE_JOURNEY_DATA/manifests in the container.
export const GENERATED_MANIFESTS = Object.freeze([
  "recording-index.json",
  "filtering-sample.json",
  "filter-results.json",
  "full-corpus-filter-results.json",
  "local-stt-transcripts.json",
  "local-lyric-indicators.json",
  "local-voice-features.json",
  "song-journeys.json",
]);

// Input manifests: authored, stay in the repo (source tree, read-only).
export const INPUT_MANIFESTS = Object.freeze(["chains.json"]);

export function resolveDataRoot(env = process.env) {
  const value = env.VOICE_JOURNEY_DATA;
  return value && value.trim() !== "" ? path.resolve(value) : null;
}

export function isContainer(env = process.env) {
  return env.VOICE_JOURNEY_CONTAINER === "1";
}

// Paths are relative (cwd-based) in local dev to keep historical output and
// the "gitignored local path" checks identical; absolute under a data root.
export function resolvePaths(env = process.env) {
  const data = resolveDataRoot(env);
  const manifestsDir = data ? path.join(data, "manifests") : "manifests";
  const artifactsDir = data ? path.join(data, "artifacts") : "local-artifacts";
  const corpusDir = data ? path.join(data, "corpus") : path.join(artifactsDir, "corpus");
  return {
    dataRoot: data,
    manifestsDir,
    artifactsDir,
    manifest: (name) => path.join(manifestsDir, name),
    artifact: (...parts) => path.join(artifactsDir, ...parts),
    // Input manifests are always read from the source tree (or an explicit override).
    chains: env.VOICE_JOURNEY_CHAINS ?? path.join("manifests", "chains.json"),
    phoneCorpusRoot: env.VOICE_JOURNEY_CORPUS_ROOT ?? (data ? path.join(corpusDir, "phone") : MAC_PHONE_CORPUS),
    measurementDir: env.VOICE_JOURNEY_MEASUREMENT_DIR ?? path.join(corpusDir, "measurement"),
    stagingDir: env.VOICE_JOURNEY_STAGING_DIR ?? (data ? path.join(data, "staging") : path.join(artifactsDir, "staging")),
    modelsDir: data ? path.join(data, "models") : null,
  };
}

// WHISPER_CPP_THREADS: a positive integer passed to whisper-cli as `-t <n>`.
// Unset (or empty) keeps whisper.cpp's own default. Anything else that is not
// a plain positive integer is refused, not silently ignored.
export function parseWhisperThreads(value) {
  if (value === undefined || value === "") return null;
  if (!/^[1-9][0-9]{0,3}$/u.test(String(value))) {
    throw new Error(`WHISPER_CPP_THREADS must be a positive integer (1-9999), got ${JSON.stringify(String(value))}`);
  }
  return Number(value);
}

// Tool binaries come from the environment; bare names resolve through PATH,
// so there is no Homebrew (or any other) location baked in.
export function resolveTools(env = process.env, p = resolvePaths(env)) {
  return {
    ffmpegBin: env.FFMPEG_BIN ?? "ffmpeg",
    whisperBin: env.WHISPER_CPP_BIN ?? "whisper-cli",
    whisperModel: env.WHISPER_CPP_MODEL ?? (p.modelsDir ? path.join(p.modelsDir, "ggml-medium.bin") : "models/ggml-medium.bin"),
    whisperThreads: parseWhisperThreads(env.WHISPER_CPP_THREADS),
    venvPython: env.VOICE_JOURNEY_PYTHON ?? path.join("analysis", ".venv", "bin", "python"),
  };
}

// True when filePath is inside the artifacts dir (the gitignored / out-of-tree
// store); the manifests record this as `gitignoredExpected`.
export function isInsideArtifacts(filePath, env = process.env) {
  const { artifactsDir } = resolvePaths(env);
  const relative = path.relative(path.resolve(artifactsDir), path.resolve(filePath));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);

// The loopback-only rule: outside the container the server binds loopback
// only. Inside the container (VOICE_JOURNEY_CONTAINER=1) the door and the
// tailnet sidecar reach it over the compose network, so it may bind wider.
export function assertBindAllowed(host, env = process.env) {
  if (LOOPBACK_HOSTS.has(host) || isContainer(env)) return;
  throw new Error(`refusing to bind ${host}: Voice Journey binds loopback only outside the container (set VOICE_JOURNEY_CONTAINER=1 in the image)`);
}
