# The Breathing Studio — rig & continuous-processing plan

**Status:** approved by operator 2026-08-12 · Phase D implementation started same day
**Visual version** (rig + loop diagrams): https://claude.ai/code/artifact/af41fbb5-123f-4b4e-9381-8063c846f17c

Turn Voice Journey from a static dump-and-analyze corpus into a breathing system:
every take (either chain) is ingested automatically, digested incrementally, published
to the browser, and answered with a practice plan.

## The rig

Two capture chains into one machine:

- **Measurement chain (new):** singer at fixed 35 cm → miniDSP UMIK-1 (omni, per-serial
  factory cal file, 48 kHz/24-bit, no DSP) → USB → CoreAudio (input slider PINNED and
  logged; capture refuses to run if moved) → `vj-capture` (ffmpeg/avfoundation) →
  raw WAV + `session.json` (chain id, slider, distance, cal-file hash, segment map).
- **Phone chain (continuity, never retired):** iPhone → Voice Memos (AGC + AAC,
  uncontrolled) → iCloud → group container on the mini (seam-only reads; TCC held by
  `voice-journey-launcher`). Both chains hear every performance — the overlap is the
  dataset that estimates the phone chain's per-metric bias and rehabilitates history.

Upgrade path = **logged chain eras**, never silent swaps: SPL calibrator (94 dB check),
era 3: Line Audio OM1 + MOTU M2 (lower noise floor), era 4: head-mounted mic.

Room discipline: same corner every session; 10 s silence recorded per session → noise
floor becomes a logged covariate; absorption panel behind the mic.

## The loop

Capture → Ingest → Process → Publish → Coach → Practice → Capture.
The watcher makes ingest event-driven; every downstream stage is already
incremental/resumable by design (STT-pattern). The only non-automatable station sings.

## Phases

- **A — Session capture & spine ritual** *(gear-gated)*: `npm run capture -- session
  --chain umik1`; keypress-segmented ritual (silence → cal tone → /a/ soft+loud → glide
  → anchor song → free); `manifests/chains.json` chain registry; slider-pin guard.
  Done when: a real session produces segmented WAVs + session.json, and a nudged
  slider refuses to record.
- **B — Ingest & dual-chain pairing** *(after A)*: `measurement` source type in the
  index; `npm run pair-chains` matches takes across chains by wall-clock overlap →
  `manifests/chain-pairs.json`; per-metric chain-bias report.
- **C — Feature contract v2** *(after A)*: apply cal file; SPL percentiles + phrase
  dynamics, spectral tilt / H1–H2, singer's-formant ratio, revived HNR, session noise
  floor — null for uncalibrated chains, no backfilled fiction;
  `manifests/calibration-spine.json`; a "Spine" journey tab.
- **D — The breathing loop** *(no gear needed — STARTED)*: watcher + incremental
  pipeline + ntfy notifications + weekly digest. Details below.
- **E — Coaching layer** *(due-songs can start now; sharpens after C)*: Practice view;
  due-for-a-take planner (spaced re-recording ranked by evidence gain — CI tightening /
  span extension); frontier detector ("one step away" flags with why-chips: p95 pitch
  1–2 st under working top, vibrato 4.5–5.2 Hz on sustains, sustains within 15% of PB,
  note-cores at 25–35¢); goals as pre-registered n=1 experiments; loudness-matched
  referee playback.

## Phase D design decisions (as implemented)

1. **The watcher lives inside the browser service process**, not a separate daemon.
   TCC attributes corpus reads to the service's responsible process
   (`voice-journey-launcher`, already FDA-granted). A separate `vj-watchd` launchd
   agent would need its own compiled launcher and a second Full Disk Access trip.
   Inside the existing process, the watch loop and its child pipeline stages inherit
   the grant. Cost: pipeline shares the server's lifecycle — acceptable because every
   stage is resumable and the loop re-arms on restart.
2. **Polling + size-stability instead of launchd WatchPaths.** WatchPaths semantics on
   a TCC-protected group container are uncertain, and iCloud delivers files in bursts
   (partial files mid-sync). The loop scans on an interval (default 60 s), and only
   triggers when two consecutive scans agree (same file set, same sizes) AND differ
   from the last indexed state — a built-in debounce that also rides out partial syncs.
3. **Stages are a declarative table** (argv + label + timeout), run sequentially as
   child processes under a lock file; a failing stage aborts the run, notifies, and
   the next quiet scan retries. Stage list: index → filter apply → stt
   transcribe-index → lyric analyze → reclassify apply → features extract-index →
   journeys analyze.
4. **Notifications via the existing tailnet ntfy node** (`--ntfy <url>`, off unless
   flagged). Messages carry counts and stage names only — never transcript or lyric
   text (privacy norms).
5. **Weekly digest** is a watchd side-task: a snapshot diff of manifest stats
   (takes, buckets, clusters, verdicts) pushed via ntfy on the configured weekday.

## Guardrails

- Everything local (whisper.cpp, parselmouth); no transcript/audio content leaves the
  machine; manifests stay repo-safe; playback stays release-gated.
- Chain id is a covariate everywhere; v2 fields never cross chain eras uncorrected.
- Every writer idempotent + locked + resumable; failures notify, never silently skip.
- No verdict shopping: "evidence-thin" framing, never "one more take flips the chart".

## Open questions

- Due-interval policy: fixed 90-day floor vs adaptive from CI width (start fixed?).
- Digest cadence: weekly always, or monthly outside active eras?
- Should the watcher also refresh the referee pool / re-run reclassify, or stay
  operator-triggered? (v1: reclassify yes — deterministic; referee pool no.)
- T4: fold stages onto mission-pipeline after the loop runs standalone for a month.
