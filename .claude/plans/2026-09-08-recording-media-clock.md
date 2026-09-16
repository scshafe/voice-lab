# Plan: Match the recording timer to captured audio

**Goal:** The recording clock measures captured media, excludes microphone startup
and stop/QC delay, and agrees with the verified saved WAV duration after stopping.
**Out of scope:** Editing existing recordings, gain/noise processing, a new capture
backend, corpus/schema migrations, remote transfers, or restarting the operator app.

## Context

- The live engine reports wall time since subprocess launch, before AVFoundation
  has necessarily delivered audio. The desktop extrapolates that time even while
  Stop is waiting for capture shutdown and QC. Review uses actual WAV frames.
- Preserve the canonical FFmpeg capture path, safety watchdog, immutable evidence,
  native playback clock, room-tone instructions, and stop/close recovery behavior.
- Use confirmed media progress, never a guessed two-second subtraction. Startup
  and finishing must be explicit; unknown/failed timing must not look like capture.
- The fresh room-tone take is assessed read-only using digest-verified local PCM.

## Steps

- [x] **Step 1: Bind live capture timing to encoded media.**
  - Verify: synthetic process tests cover delayed first audio, progression/stalls,
    malformed progress/failure cleanup, watchdog behavior, and final frame duration.
  - Notes: backend worker owns capture.py and test_capture.py; root adapts other
    synthetic process fixtures and the recording-library startup acknowledgment.
- [x] **Step 2: Show truthful startup, recording, and finishing states.**
  - Verify: native offscreen tests cover startup wait, no wall-clock extrapolation,
    immediate Stop freeze, failures, prompt locks, room-tone and speech, and review.
  - Notes: UI worker owns desktop_window.py and test_desktop_window.py. Playback
    retains its existing native media clock; saved evidence timestamps stay intact.
- [x] **Step 3: Verify the integrated path and document behavior.**
  - Verify: a real FFmpeg synthetic-only media-clock check; `scripts/verify`;
    `git diff --check`; compact light/dark UI inspection and updated operator docs.
  - Notes: no real microphone, audible playback, private-asset writes, or app restart.

## Done criteria

The full gate passes with delayed-start/stop regressions. The user receives the
new room-tone assessment and safe reopen instructions, with original audio intact.

**Amended 2026-09-08:** Review found startup error windows before the engine's first
state and the desktop wrapper's final state acknowledgment. Both must stop/reap
capture, preserve partials, and expose cleanup errors before reporting failed Start.
The root owns recording_library.py and its regression tests for the second window.

**Verified 2026-09-08:** `scripts/verify` passed 928 tests in 338.32 seconds at
90.96% branch-aware coverage, plus format/lint, strict types, build, schema checks,
and the private-asset guard. Real FFmpeg with generated sine audio exercised a
two-second process-start delay, slow shutdown, and exact saved-frame agreement;
a separate timestamp-offset input confirmed packet counts exclude that offset.
Offscreen Qt screenshots at 1060x700 verified startup, recording, stopping, and
review in light/dark palettes. Independent review confirmed startup cleanup at
both acknowledgment boundaries. No operator app restart or private audio changes.
