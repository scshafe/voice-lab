import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildSessionManifest,
  findAudioDevice,
  parseAvfoundationDevices,
  planSession,
  resolveChain,
  runSession,
  sessionIdFor,
  SPINE_SEGMENTS,
  updateChainPinnedVolume,
  volumeGuard,
  wavDurationSeconds,
} from "../src/capture.mjs";

const FFMPEG_LIST_STDERR = `
[AVFoundation indev @ 0x158704080] AVFoundation video devices:
[AVFoundation indev @ 0x158704080] [0] FaceTime HD Camera
[AVFoundation indev @ 0x158704080] AVFoundation audio devices:
[AVFoundation indev @ 0x158704080] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x158704080] [1] Umik-1  Gain: 18dB
: Input/output error
`;

function chainFixture(overrides = {}) {
  return {
    chainId: "umik1",
    kind: "measurement",
    controlled: true,
    deviceName: "umik",
    sampleRate: 48000,
    distanceCm: 35,
    pinnedInputVolume: 70,
    calFile: "local-artifacts/calibration/umik1-cal.txt",
    ...overrides,
  };
}

function registryFixture(chain = chainFixture()) {
  return { schemaVersion: "voice-journey.chains.v1", chains: [{ chainId: "iphone-voicememos" }, chain] };
}

test("avfoundation device listing parses audio devices only, and matching is fuzzy", () => {
  const devices = parseAvfoundationDevices(FFMPEG_LIST_STDERR);
  assert.deepEqual(devices.audio.map((device) => device.index), [0, 1]);
  assert.match(devices.audio[1].name, /Umik-1/u);
  assert.equal(findAudioDevice(devices, "umik").index, 1);
  assert.equal(findAudioDevice(devices, "UMIK-1").index, 1);
  assert.equal(findAudioDevice(devices, "sm7b"), null);
  assert.equal(findAudioDevice(devices, ""), null);
});

test("chain resolution validates schema and id", () => {
  assert.equal(resolveChain(registryFixture(), "umik1").chainId, "umik1");
  assert.throws(() => resolveChain(registryFixture(), "nope"), /unknown chain: nope/u);
  assert.throws(() => resolveChain({ schemaVersion: "wrong" }, "umik1"), /unexpected schema/u);
});

test("the volume guard refuses unpinned and drifted sliders", () => {
  assert.throws(() => volumeGuard(chainFixture({ pinnedInputVolume: null }), 70), /no pinned input volume/u);
  assert.throws(() => volumeGuard(chainFixture(), 71), /pinned at 70/u);
  assert.doesNotThrow(() => volumeGuard(chainFixture(), 70));
});

test("session planning orders and numbers segments, honoring the flags", () => {
  const chain = chainFixture();
  const plain = planSession({ chain, options: {}, sessionId: "s-x" });
  assert.deepEqual(plain.segments.map((segment) => segment.key), [
    "room-silence", "vowel-soft", "vowel-loud", "glide", "anchor-song", "free-practice",
  ]);
  assert.equal(plain.segments[0].file, "01-room-silence.wav");
  assert.ok(plain.outDir.endsWith("s-x"));

  const withCal = planSession({ chain, options: { calTone: true, skipFree: true }, sessionId: "s-y" });
  assert.deepEqual(withCal.segments.map((segment) => segment.key), [
    "room-silence", "cal-tone", "vowel-soft", "vowel-loud", "glide", "anchor-song",
  ]);
  assert.equal(withCal.segments[1].file, "02-cal-tone.wav");
  assert.equal(SPINE_SEGMENTS.length, 7);
});

test("session ids and wav duration math behave", () => {
  assert.match(sessionIdFor(Date.parse("2026-08-13T19:30:05")), /^s-20260813-193005$/u);
  assert.equal(wavDurationSeconds(44 + 48000 * 3 * 2, 48000), 2);
  assert.equal(wavDurationSeconds(44, 48000), null);
  assert.equal(wavDurationSeconds(Number.NaN, 48000), null);
});

test("pin-volume updates the registry and reports the previous value", () => {
  const registry = registryFixture(chainFixture({ pinnedInputVolume: null }));
  const first = updateChainPinnedVolume(registry, "umik1", 70);
  assert.equal(first.previous, null);
  assert.equal(first.pinned, 70);
  const second = updateChainPinnedVolume(registry, "umik1", 65);
  assert.equal(second.previous, 70);
  assert.equal(resolveChain(registry, "umik1").pinnedInputVolume, 65);
});

test("runSession records every segment, stops manual ones, and writes the manifest", async () => {
  const chain = chainFixture();
  const plan = planSession({ chain, options: { skipFree: true }, sessionId: "s-t" });
  const prompts = [];
  const stops = [];
  const recorded = [];
  let written = null;
  let clock = Date.parse("2026-08-13T20:00:00Z");

  const manifest = await runSession({
    chain,
    options: {},
    plan,
    deps: {
      prompt: async (message) => { prompts.push(message); },
      startRecording: async ({ file, seconds }) => {
        recorded.push({ file, seconds });
        return { done: Promise.resolve(), stop: async () => { stops.push(file); } };
      },
      statFile: async () => ({ size: 44 + 48000 * 3 * 3 }),
      writeManifest: async (payload) => { written = payload; },
      inputVolume: 70,
      calFileSha256: "abc123",
      now: () => (clock += 1000),
      log: () => {},
    },
  });

  assert.equal(manifest.status, "completed");
  assert.equal(manifest.segments.length, 5);
  assert.equal(manifest.segments[0].key, "room-silence");
  assert.equal(manifest.segments[0].durationSeconds, 3);
  assert.equal(manifest.capture.inputVolume, 70);
  assert.equal(manifest.capture.calFileSha256, "abc123");
  assert.equal(manifest.capture.distanceCm, 35);
  assert.equal(recorded[0].seconds, 10, "timed segment carries its duration");
  assert.equal(recorded[1].seconds, null, "manual segments run until stopped");
  assert.equal(stops.length, 4, "every manual segment gets stopped; the timed one does not");
  assert.equal(prompts.filter((message) => message.includes("Enter to stop")).length, 4);
  assert.equal(written.schemaVersion, "voice-journey.capture-session.v1");
});

test("a failing recording aborts the session but still writes a truthful manifest", async () => {
  const chain = chainFixture();
  const plan = planSession({ chain, options: { skipFree: true }, sessionId: "s-f" });
  let written = null;
  let started = 0;

  await assert.rejects(
    runSession({
      chain,
      options: {},
      plan,
      deps: {
        prompt: async () => {},
        startRecording: async () => {
          started += 1;
          if (started === 3) throw new Error("device vanished");
          return { done: Promise.resolve(), stop: async () => {} };
        },
        statFile: async () => ({ size: 44 + 48000 * 3 }),
        writeManifest: async (payload) => { written = payload; },
        inputVolume: 70,
        calFileSha256: null,
        now: () => Date.parse("2026-08-13T20:00:00Z"),
        log: () => {},
      },
    }),
    /session aborted after 2\/5 segments: device vanished/u,
  );
  assert.equal(written.status, "aborted");
  assert.equal(written.error, "device vanished");
  assert.equal(written.segments.length, 2);
});

test("session manifests stay metadata-only", () => {
  const manifest = buildSessionManifest({
    plan: planSession({ chain: chainFixture(), options: {}, sessionId: "s-m" }),
    chain: chainFixture(),
    results: [{ key: "room-silence", file: "01-room-silence.wav", startedAt: "x", sizeBytes: 1, durationSeconds: 0.1 }],
    inputVolume: 70,
    calFileSha256: null,
    startedAt: "a",
    endedAt: "b",
  });
  assert.deepEqual(
    Object.keys(manifest).sort(),
    ["capture", "chainId", "chainKind", "controlled", "endedAt", "schemaVersion", "segments", "sessionId", "startedAt", "status"],
  );
});
