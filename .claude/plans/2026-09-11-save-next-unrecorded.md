# Plan: Save and advance to an unrecorded prompt

**Goal:** Save & next skips existing usable recordings across the library, moves
forward in catalog order, and wraps to earlier missing prompts without recording.
**Out of scope:** Audio changes, package requirements, microphone capture, transfers.

## Context

The desktop currently accepts a take and uses the engine's first pending prompt
in the current batch. Each batch includes room tones, even when the library has
recorded them in earlier sessions. The user's requested catalog navigation
supersedes the old batch-only navigation decision. Preserve all existing work.

## Steps

- [x] **Step 1: Navigate to missing catalog prompts after saving.**
  - Verify: targeted native-window regression tests using synthetic audio.
  - Notes: Reuse existing prompt selection/session transitions. Preserve trims,
    accepted decisions, unfinished batches, and explicit finalization rules.
- [x] **Step 2: Cover skipped recordings, wrapping, completion, and failures.**
  - Verify: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/test_desktop_window.py`.
- [x] **Step 3: Document navigation and verify the increment.**
  - Verify: `scripts/verify` and `git diff --check`.
- [x] **Step 4: Keep the saved prompt visible during navigation.**
  - Verify: delayed snapshot and target-selection native-widget regressions;
    `scripts/verify` and `git diff --check`.
  - Notes: Publish the final target only. Preserve accurate recoverable state
    if finding or preparing the target fails after the take has been saved.

## Done criteria

Navigation and regression tests pass full verification. Existing recordings and
the operator's running application remain intact; reopening loads the fix.

## Verification evidence

- Focused navigation checks: 13 passed, covering existing room-tone and speech
  takes, catalog order, wrapping, rejected/removed takes, saved trims, completed
  recording coverage, and failed transitions without losing the saved take.
- All 159 desktop-window tests passed in the full verification run, including
  keyboard, playback interlocks, trim persistence, navigation, and finalization.
- Full `scripts/verify` passed: 1,161 tests, 91.44% branch-aware coverage, Ruff,
  strict mypy, package build, schema checks, and private-asset guard.
- `git diff --check` passed. Verification log:
  `/tmp/cole-voice-lab-save-next-verify.log`.
- Confirmed the installed `/Applications/Cole Voice Lab.app` launches this
  checkout's `.venv` and imports the edited source. Reopen to load the fix after
  saving the current take. The running application and existing audio were not touched.

**Amended 2026-09-11:** The user observed the engine's first pending room tone
flashing during Save & next. Keep the saved prompt visible until the final
unrecorded target is ready, including while library verification is delayed.

### Transition follow-up verification

- 21 targeted navigation and failure tests passed, including four deliberately
  paused snapshot/selection scenarios across current and replacement batches.
  These check the script, selected row, waveform, controls, and every script-text
  change during the transition, plus recovery after a saved take's navigation fails.
- Full `scripts/verify` passed: 1,167 tests, 91.45% branch-aware coverage, Ruff,
  strict mypy, package build, schema checks, and private-asset guard.
- `git diff --check` passed. Full log:
  `/tmp/cole-voice-lab-save-next-transition-verify.log`.
- The current prompt and waveform now remain visible through saving and target
  preparation; only the final target is displayed. Existing audio and the running
  app remain untouched. Reopen the app after saving the current take to load the fix.
