# Plan: Keep Stop visibly red during recording

**Goal:** Background recording-status checks do not make the Stop button flash
between red and its disabled appearance.
**Out of scope:** Capture behavior, command serialization, recordings, and app restart.

## Context

The serialized status poll briefly disables controls via `_submit`. Stop inherits
the generic disabled button palette, although the microphone is still recording.
Its recording appearance should follow capture state; an actual Stop operation
should retain the existing disabled treatment and input interlocks.

## Steps

- [x] **Step 1: Preserve the active recording button appearance through polling.**
  - Verify: delayed-poll widget tests sample rendered button colors in light/dark
    palettes for speech and silence, plus delayed actual-stop checks.
- [x] **Step 2: Verify and document the appearance correction.**
  - Verify: `scripts/verify` and `git diff --check`.

## Done criteria

The Stop button remains red during active capture and polling. Actual stopping
still shows a disabled control. Full verification passes without touching the
operator's running app or recordings.

## Verification evidence

- Before the fix, all four rendered-color regressions reproduced the pale
  Stop button during delayed polls; both actual-shutdown checks passed.
- After the fix, eight focused checks passed, including repeated polls in both
  palettes for speech/silence, actual shutdown, timer freezing, and poll failure.
- All 171 desktop-window tests passed during full verification.
- Full `scripts/verify` passed: 1,173 tests, 91.45% branch-aware coverage, Ruff,
  strict mypy, package build, schema validation, and private-asset guard.
- `git diff --check` passed. Full verification log:
  `/tmp/cole-voice-lab-stop-button-verify.log`.
- Existing capture/input interlocks are unchanged. The installed app loads this
  checkout on reopen; no operator app restart or existing audio changes were made.
