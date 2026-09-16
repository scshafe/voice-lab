# Plan: Desktop recording experience

**Goal:** Apply Cole's interface feedback with a native two-pane library/inspector,
readable script, safe keyboard transport, clear state, and preserved take history.
**Out of scope:** Corpus changes, model/training work, audio transfers, actual
microphone recording, data/schema migrations, waveform capture, or terminating
an operator-owned app/session.

## Context

- Existing uncommitted native desktop and corpus notes belong to prior work;
  preserve them. Current verification baseline is 660 tests / 90.57% coverage.
- `src/cole_voice_lab/desktop_window.py` owns the single native UI and worker;
  keep using `DesktopRecordingSession`/`TakeEngine` for capture transitions.
- ~~Keep already advances the canonical batch. A global next-Missing selection
  would skip intended retakes and required room tone, so do not implement that.~~
  Superseded by Cole's explicit September 11 request: Save & next now skips
  recorded prompts across the catalog; unfinished batches remain resumable.
- Choose a horizontal library/inspector split over a restyled three-pane screen.
  Keep one full-size transcript, adaptive native palette and system typography.
- Preserve date/age/count sorting through optional library columns and dated
  inspector history. Active session context must not diverge from transport.
- Window-scoped R toggles record/stop; Space toggles playback/stop. Do not
  intercept typing, editable controls, popups, dialogs, or auto-repeat. Enter
  keeps only in safe review context. Never auto-record after Keep or Retry.
- Use real recording/playback state and an elapsed counter, not a simulated
  audio waveform. Disable conflicting actions during async work/playback.
- What if filters hide the queued prompt? Reveal/identify the active target.
  What if the operator clicks another row? Do not retarget active capture.
  What if script/history is long or the palette is dark? Preserve scrolling,
  readable contrast, and usable transport at laptop window sizes.

## Steps

- [x] **Step 1: Implement consolidated native inspector and transport.**
  - Verify: focused Qt tests, Ruff, and strict mypy.
  - Notes: source ownership `desktop_window.py`; no capture-backend changes.
- [x] **Step 2: Cover keyboard, queue, and playback state regressions.**
  - Verify: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/test_desktop_window.py`.
  - Notes: synthetic audio only; text focus, repeat, failures, filters, room tone,
    retakes, history, keep-next, completion, close/resume, and timer transitions.
- [x] **Step 3: Inspect rendered laptop layouts and update documentation.**
  - Verify: native offscreen screenshots in light/dark at 1320x860, 1060x760,
    and 1060x700 (added to accommodate laptop menu bars/Docks),
    long script/history and active/review states; inspect images and adjust.
  - Notes: update desktop guide/current status/decision notes; do not restart
    an operator-owned app to demonstrate the change.
- [x] **Step 4: Run full verification and hand off.**
  - Verify: `scripts/verify` and `git diff --check`.
  - Notes: report actual checks, physical audio acceptance still required,
    and safe close/reopen instructions for the installed local launcher.

## Done criteria

The requested workflow improvements are implemented, documented, independently
reviewed, visually checked, and pass repository verification. No private audio
was used or transferred. Hand off without committing or restarting the user's
running recording application.

## Verification evidence

- The independent state audit and source review retained the existing backend
  and canonical queue rather than introducing another capture state machine.
- Focused real-widget verification: 41 tests passed in 18.82 seconds after the
  final compact-window adjustment; Ruff and strict mypy also passed.
- Synthetic light/dark-palette screenshots cover browsing, ready, recording,
  review, playback, and long scripts at all three sizes. The recording/review
  controls fit; long scripts scroll without polling resetting their position.
  Dark appearance was palette-emulated, not an operator OS appearance change.
- A real `RecordingLibrary`/`TakeEngine` integration with synthetic microphone
  output exercised keyboard capture, two finalized batches 40 days apart,
  reopening, two retained dated takes, and validated playback paths. No audible
  playback or human recording was used.
- Local QA artifacts and logs are outside Git at
  `/tmp/cole-voice-desktop-experience.FsqrTm/`.
- Final `scripts/verify` passed 685 tests in 295.83 seconds with 90.88%
  branch-aware coverage, format/lint, strict types, package build, schema checks,
  and the private-asset guard. `git diff --check` passed. No changes were committed.
- Next operator action: stop/review any active take, finalize a complete batch
  or End session to preserve an unfinished batch, then close/reopen the installed
  local app. Its launcher already loads this checkout. Physical microphone and
  headphone acceptance remains an operator action; development did not restart
  the app or touch existing recordings.

**Amended 2026-09-11:** Cole requested that Save & next skip previously recorded
prompts throughout the library. The batch-only navigation decision is superseded
by `2026-09-11-save-next-unrecorded.md`; recording-package requirements are retained.
