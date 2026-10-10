#!/usr/bin/env node
// capture.mjs — phase A of docs/breathing-studio-plan.md: the measurement
// chain's session recorder and spine ritual.
//
// Records raw WAV (48 kHz / 24-bit mono, no DSP, cal file applied at analysis
// time, never at capture) from the chain's USB measurement mic via ffmpeg's
// avfoundation input, one file per keypress-marked segment, plus a session.json
// carrying everything a future analyst needs to trust the numbers: chain id,
// pinned input volume, mouth distance, cal-file hash, per-segment timing.
//
// The discipline lives in the guards: capture REFUSES to run if the chain's
// input-volume slider was never pinned or has drifted from the pinned value —
// the macOS input slider is a hidden gain stage, and a nudged slider silently
// invalidates the SPL reference. `pin-volume` records the deliberate value.
//
// Local-only: writes under local-artifacts/capture/ (gitignored). The repo
// stays zero-dependency — node core + the host ffmpeg binary.

import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { execFile } from "node:child_process";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { createInterface } from "node:readline/promises";
import { promisify } from "node:util";
import { pathToFileURL } from "node:url";
import { resolvePaths, resolveTools } from "./paths.mjs";

const execFileAsync = promisify(execFile);

const VJ = resolvePaths();
const TOOLS = resolveTools(process.env, VJ);
const DEFAULT_CHAINS = VJ.chains;
const DEFAULT_CAPTURE_DIR = VJ.artifact("capture");
const DEFAULT_FFMPEG = TOOLS.ffmpegBin;
const CHAINS_SCHEMA_VERSION = "voice-journey.chains.v1";
const SESSION_SCHEMA_VERSION = "voice-journey.capture-session.v1";
const WAV_BYTES_PER_SAMPLE = 3; // pcm_s24le mono

class CaptureError extends Error {}

// The spine ritual, in order. Timed segments run unattended; manual segments
// stop on a keypress. cal-tone only exists once a calibrator does.
export const SPINE_SEGMENTS = Object.freeze([
  { key: "room-silence", label: "room silence — stand still, say nothing (noise floor)", mode: "timed", seconds: 10 },
  { key: "cal-tone", label: "94 dB calibrator tone on the capsule", mode: "timed", seconds: 10, requiresFlag: "calTone" },
  { key: "vowel-soft", label: "sustained /a/ — comfortable pitch, soft", mode: "manual" },
  { key: "vowel-loud", label: "sustained /a/ — same pitch, full voice", mode: "manual" },
  { key: "glide", label: "slow glide — bottom of range to top and back", mode: "manual" },
  { key: "anchor-song", label: "the anchor song — one verse, same song every session", mode: "manual" },
  { key: "free-practice", label: "free practice — everything else, mic keeps rolling", mode: "manual", skipFlag: "skipFree" },
]);

export function parseAvfoundationDevices(stderrText) {
  const audio = [];
  let inAudio = false;
  for (const line of String(stderrText ?? "").split("\n")) {
    if (/AVFoundation audio devices/u.test(line)) { inAudio = true; continue; }
    if (/AVFoundation video devices/u.test(line)) { inAudio = false; continue; }
    const match = /\[(\d+)\]\s+(.+)$/u.exec(line);
    if (inAudio && match) audio.push({ index: Number(match[1]), name: match[2].trim() });
  }
  return { audio };
}

export function findAudioDevice(devices, needle) {
  const wanted = String(needle ?? "").toLowerCase();
  if (!wanted) return null;
  return (devices?.audio ?? []).find((device) => device.name.toLowerCase().includes(wanted)) ?? null;
}

export function resolveChain(registry, chainId) {
  if (registry?.schemaVersion !== CHAINS_SCHEMA_VERSION) {
    throw new CaptureError(`chain registry has unexpected schema: ${registry?.schemaVersion ?? "missing"}`);
  }
  const chain = (registry.chains ?? []).find((candidate) => candidate.chainId === chainId);
  if (!chain) {
    const known = (registry.chains ?? []).map((candidate) => candidate.chainId).join(", ");
    throw new CaptureError(`unknown chain: ${chainId} (registry has: ${known})`);
  }
  return chain;
}

export function volumeGuard(chain, currentVolume) {
  if (!Number.isInteger(chain.pinnedInputVolume)) {
    throw new CaptureError(
      `chain ${chain.chainId} has no pinned input volume — set the slider deliberately once, then run: npm run capture -- pin-volume --chain ${chain.chainId}`,
    );
  }
  if (currentVolume !== chain.pinnedInputVolume) {
    throw new CaptureError(
      `input volume is ${currentVolume} but chain ${chain.chainId} is pinned at ${chain.pinnedInputVolume} — a moved slider invalidates the SPL reference. Restore the slider, or re-pin deliberately (a logged era event) with pin-volume.`,
    );
  }
}

export function planSession({ chain, options = {}, sessionId }) {
  const segments = [];
  let index = 0;
  for (const segment of SPINE_SEGMENTS) {
    if (segment.requiresFlag && !options[segment.requiresFlag]) continue;
    if (segment.skipFlag && options[segment.skipFlag]) continue;
    index += 1;
    segments.push({
      key: segment.key,
      label: segment.label,
      mode: segment.mode,
      seconds: segment.seconds ?? null,
      file: `${String(index).padStart(2, "0")}-${segment.key}.wav`,
    });
  }
  const outDir = path.join(options.out ?? DEFAULT_CAPTURE_DIR, sessionId);
  return { sessionId, chainId: chain.chainId, sampleRate: chain.sampleRate ?? 48000, outDir, segments };
}

export function sessionIdFor(nowMs) {
  const date = new Date(nowMs);
  const pad = (value) => String(value).padStart(2, "0");
  return `s-${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

export function wavDurationSeconds(sizeBytes, sampleRate, { bytesPerSample = WAV_BYTES_PER_SAMPLE, channels = 1, headerBytes = 44 } = {}) {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= headerBytes || !sampleRate) return null;
  return Number(((sizeBytes - headerBytes) / (sampleRate * bytesPerSample * channels)).toFixed(2));
}

export function buildSessionManifest({ plan, chain, results, inputVolume, calFileSha256, startedAt, endedAt, status = "completed", error = null }) {
  return {
    schemaVersion: SESSION_SCHEMA_VERSION,
    sessionId: plan.sessionId,
    chainId: chain.chainId,
    chainKind: chain.kind ?? null,
    controlled: Boolean(chain.controlled),
    startedAt,
    endedAt,
    status,
    ...(error ? { error } : {}),
    capture: {
      sampleRate: plan.sampleRate,
      bitDepth: 24,
      channels: 1,
      inputVolume,
      distanceCm: chain.distanceCm ?? null,
      calFile: chain.calFile ?? null,
      calFileSha256,
    },
    segments: results,
  };
}

export function updateChainPinnedVolume(registry, chainId, volume) {
  const chain = resolveChain(registry, chainId);
  const previous = chain.pinnedInputVolume ?? null;
  chain.pinnedInputVolume = volume;
  return { registry, previous, pinned: volume };
}

async function listAudioDevicesReal(ffmpegBin) {
  try {
    await execFileAsync(ffmpegBin, ["-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]);
    return { audio: [] };
  } catch (error) {
    // ffmpeg exits nonzero after listing; the devices are on stderr.
    if (error?.stderr) return parseAvfoundationDevices(error.stderr);
    throw new CaptureError(`could not list avfoundation devices via ${ffmpegBin}: ${error.message}`);
  }
}

async function readInputVolumeReal() {
  const { stdout } = await execFileAsync("/usr/bin/osascript", ["-e", "input volume of (get volume settings)"]);
  const volume = Number(String(stdout).trim());
  if (!Number.isInteger(volume)) throw new CaptureError(`could not read input volume (osascript said: ${String(stdout).trim()})`);
  return volume;
}

function startRecordingReal({ ffmpegBin, deviceIndex, file, seconds, sampleRate }) {
  const args = [
    "-hide_banner", "-loglevel", "error",
    "-f", "avfoundation", "-i", `:${deviceIndex}`,
    "-ac", "1", "-ar", String(sampleRate), "-c:a", "pcm_s24le",
    ...(seconds ? ["-t", String(seconds)] : []),
    "-y", file,
  ];
  const child = spawn(ffmpegBin, args, { stdio: ["pipe", "ignore", "pipe"] });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const done = new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", async (code) => {
      if (code === 0) return resolve();
      // A manual stop can end ffmpeg with a nonzero code after the file is
      // already written and finalized; accept a non-trivial output file.
      try {
        const stats = await stat(file);
        if (stats.size > 1024) return resolve();
      } catch { /* fall through to reject */ }
      reject(new CaptureError(`ffmpeg exit ${code} for ${path.basename(file)}: ${stderr.slice(-300)}`));
    });
  });
  return {
    done,
    stop: async () => {
      try { child.stdin.write("q\n"); } catch { /* stdin may be gone */ }
      setTimeout(() => { try { child.kill("SIGINT"); } catch { /* already dead */ } }, 3000).unref();
    },
  };
}

// The orchestrator, with every side effect injectable so tests can drive the
// full ritual without hardware, a terminal, or ffmpeg.
export async function runSession({ chain, options, plan, deps }) {
  const { prompt, startRecording, statFile, writeManifest, now, log } = deps;
  const startedAt = new Date(now()).toISOString();
  const results = [];
  let status = "completed";
  let failure = null;
  try {
    for (const segment of plan.segments) {
      const action = segment.mode === "timed" ? `record ${segment.seconds}s automatically` : "start recording";
      await prompt(`▶ ${segment.label}\n  Enter to ${action}`);
      const segmentStartedAt = new Date(now()).toISOString();
      const file = path.join(plan.outDir, segment.file);
      const recording = await startRecording({ file, seconds: segment.seconds, sampleRate: plan.sampleRate });
      if (segment.mode === "manual") {
        await prompt("  … recording — Enter to stop");
        await recording.stop();
      }
      await recording.done;
      const stats = await statFile(file);
      results.push({
        key: segment.key,
        file: segment.file,
        startedAt: segmentStartedAt,
        sizeBytes: stats.size,
        durationSeconds: wavDurationSeconds(stats.size, plan.sampleRate),
      });
      log(`  ✓ ${segment.key} (${results[results.length - 1].durationSeconds ?? "?"} s)`);
    }
  } catch (error) {
    status = "aborted";
    failure = error.message;
  }
  const manifest = buildSessionManifest({
    plan,
    chain,
    results,
    inputVolume: deps.inputVolume,
    calFileSha256: deps.calFileSha256,
    startedAt,
    endedAt: new Date(now()).toISOString(),
    status,
    error: failure,
  });
  await writeManifest(manifest);
  if (failure) throw new CaptureError(`session aborted after ${results.length}/${plan.segments.length} segments: ${failure}`);
  return manifest;
}

function parseArgs(argv) {
  const args = [...argv];
  const options = {
    command: null,
    chain: null,
    device: null,
    out: null,
    chains: DEFAULT_CHAINS,
    ffmpegBin: DEFAULT_FFMPEG,
    calTone: false,
    skipFree: false,
    dryRun: false,
  };
  while (args.length > 0) {
    const next = args.shift();
    if (!next.startsWith("--") && !options.command) options.command = next;
    else if (next === "--chain") options.chain = requireValue(args, next);
    else if (next === "--device") options.device = requireValue(args, next);
    else if (next === "--out") options.out = requireValue(args, next);
    else if (next === "--chains") options.chains = requireValue(args, next);
    else if (next === "--ffmpeg-bin") options.ffmpegBin = requireValue(args, next);
    else if (next === "--cal-tone") options.calTone = true;
    else if (next === "--skip-free") options.skipFree = true;
    else if (next === "--dry-run") options.dryRun = true;
    else if (next === "--help" || next === "help") options.command = "help";
    else throw new CaptureError(`unsupported argument: ${next}`);
  }
  return options;
}

function requireValue(args, flag) {
  const value = args.shift();
  if (!value || value.startsWith("--")) throw new CaptureError(`${flag} requires a value`);
  return value;
}

function printHelp() {
  process.stdout.write(`Voice Journey session capture — the measurement chain's spine ritual.

Usage:
  voice-journey-capture devices [--ffmpeg-bin PATH]
  voice-journey-capture pin-volume --chain ID [--chains PATH]
  voice-journey-capture session --chain ID [--dry-run] [--cal-tone] [--skip-free] [--device NAME] [--out DIR] [--chains PATH] [--ffmpeg-bin PATH]

session records the spine ritual segment by segment (keypress-driven) as raw
WAV 48k/24 mono plus a session.json under local-artifacts/capture/. It
refuses to run when the chain's input volume is unpinned or drifted
(pin-volume records the deliberate value). Raw audio stays local and
uncorrected; the cal file applies at analysis time.
`);
}

async function readChains(filePath) {
  return JSON.parse(await readFile(filePath, "utf8"));
}

async function sha256OfFile(filePath) {
  try {
    return createHash("sha256").update(await readFile(filePath)).digest("hex");
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function run(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (!options.command || options.command === "help") {
    printHelp();
    return 0;
  }

  if (options.command === "devices") {
    const devices = await listAudioDevicesReal(options.ffmpegBin);
    process.stdout.write(`${JSON.stringify(devices, null, 2)}\n`);
    return 0;
  }

  if (options.command === "pin-volume") {
    if (!options.chain) throw new CaptureError("pin-volume requires --chain");
    const registry = await readChains(options.chains);
    const volume = await readInputVolumeReal();
    const { previous, pinned } = updateChainPinnedVolume(registry, options.chain, volume);
    await writeFile(options.chains, `${JSON.stringify(registry, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify({ chainId: options.chain, pinned, previous, note: previous !== null && previous !== pinned ? "re-pin recorded — treat as a chain era event" : "pinned" }, null, 2)}\n`);
    return 0;
  }

  if (options.command !== "session") throw new CaptureError(`unsupported command: ${options.command}`);
  if (!options.chain) throw new CaptureError("session requires --chain");

  const registry = await readChains(options.chains);
  const chain = resolveChain(registry, options.chain);
  const sessionId = sessionIdFor(Date.now());
  const plan = planSession({ chain, options, sessionId });
  const calFilePath = chain.calFile ?? null;
  const calFileSha256 = calFilePath ? await sha256OfFile(calFilePath) : null;

  if (options.dryRun) {
    process.stdout.write(`${JSON.stringify({
      dryRun: true,
      operation: "capture-session",
      chainId: chain.chainId,
      outDir: plan.outDir,
      segments: plan.segments.map(({ key, mode, seconds }) => ({ key, mode, seconds })),
      wouldCheck: {
        device: options.device ?? chain.deviceName,
        pinnedInputVolume: chain.pinnedInputVolume,
        calFile: { path: calFilePath, present: Boolean(calFileSha256) },
      },
      readScope: { recordsAudio: false, writes: [] },
    }, null, 2)}\n`);
    return 0;
  }

  const devices = await listAudioDevicesReal(options.ffmpegBin);
  const device = findAudioDevice(devices, options.device ?? chain.deviceName);
  if (!device) {
    throw new CaptureError(
      `audio device matching "${options.device ?? chain.deviceName}" not found — is the mic plugged in? (saw: ${devices.audio.map((entry) => entry.name).join(", ") || "none"})`,
    );
  }
  const inputVolume = await readInputVolumeReal();
  volumeGuard(chain, inputVolume);
  if (!calFileSha256) {
    process.stderr.write(`warning: cal file missing at ${calFilePath} — SPL-referenced analysis will be uncalibrated for this session\n`);
  }

  await mkdir(plan.outDir, { recursive: true });
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  process.stdout.write(`session ${sessionId} · chain ${chain.chainId} · device [${device.index}] ${device.name} · input volume ${inputVolume} (pinned)\n`);
  try {
    const manifest = await runSession({
      chain,
      options,
      plan,
      deps: {
        prompt: (message) => rl.question(`${message} `),
        startRecording: ({ file, seconds, sampleRate }) => startRecordingReal({ ffmpegBin: options.ffmpegBin, deviceIndex: device.index, file, seconds, sampleRate }),
        statFile: (file) => stat(file),
        writeManifest: (manifest) => writeFile(path.join(plan.outDir, "session.json"), `${JSON.stringify(manifest, null, 2)}\n`),
        inputVolume,
        calFileSha256,
        now: () => Date.now(),
        log: (line) => process.stdout.write(`${line}\n`),
      },
    });
    process.stdout.write(`${JSON.stringify({ sessionId: manifest.sessionId, outDir: plan.outDir, segments: manifest.segments.length, status: manifest.status }, null, 2)}\n`);
  } finally {
    rl.close();
  }
  return 0;
}

async function main() {
  try {
    process.exitCode = await run();
  } catch (error) {
    if (error instanceof CaptureError) {
      process.stderr.write(`error: ${error.message}\n`);
      process.exitCode = 1;
      return;
    }
    throw error;
  }
}

const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  await main();
}
