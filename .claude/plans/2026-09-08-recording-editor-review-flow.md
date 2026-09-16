# Plan: Recording editor review flow

**Goal:** Make recording, comparing takes, trimming, and keeping the next prompt a cohesive native desktop workflow, addressing every supplied UX suggestion.
**Out of scope:** Capture/playback backend changes, processing or deleting original audio, automatic speech-boundary detection, model training, or microphone use during verification.

## Context

The canonical UI is `src/cole_voice_lab/desktop_window.py`; the original-frame waveform is `desktop_waveform.py`. Existing trims are reversible derived copies, preview already honors pending bounds, and Keep saves a current take's pending trim before advancing. Preserve these behaviors, recording/playback interlocks, explicit room-tone instructions, and the user's uncommitted work.

## Steps

- [x] **Step 1: Improve waveform manipulation and feedback**
  - Add clearer excluded-region shading, larger grips, hover time/cursor, and double-click playback requests.
  - Provide explicitly labeled display-only amplitude fitting for quiet recordings; never modify PCM or its time coordinates.
  - Verify: `.venv/bin/python -m pytest tests/test_desktop_waveform.py -q`
  - Result: 62 synthetic waveform cases pass; source Ruff and strict mypy pass.
- [x] **Step 2: Group prompt, takes, editor, and actions**
  - Use palette-aware sections, a compact visible takes list beside the waveform, stable left/right numeric trim fields, central larger playback controls, and a fixed workflow footer.
  - Keep delivery instructions with the prompt (not the audio asset); retain explicit blue Save trim plus Save & next rather than silent persistence.
  - Verify: `.venv/bin/python -m pytest tests/test_desktop_window.py -q`
  - Result: 139 GUI cases pass in the full gate's GUI stage; compact control ordering is checked in both palettes.
- [x] **Step 3: Verify state-based review and target safety**
  - Switch visual emphasis from Record/Stop to waveform/playback/Retake after capture; add bounded skip controls and safe double-click play.
  - Ensure Save & next cannot approve a different, unseen take when historical audio is selected; preserve confirmation and playback/capture locks.
  - Verify: targeted GUI tests plus offscreen light/dark, empty/review/room-tone screenshots at laptop and larger sizes, using synthetic audio only.
  - Result: 201 focused GUI/waveform cases pass. Synthetic screenshots at 1060×700 and 1440×980 show distinct, non-overlapping cards and controls. A scrollable-prompt size hint prevents Qt from squeezing the editor below its content height.
- [x] **Step 4: Document decisions and run project verification**
  - Update desktop usage and a feedback decision matrix covering every item and justified adaptations/rejections.
  - Verify: `scripts/verify`; inspect installed launcher's checkout binding without interrupting the user's app.
  - Result: Full gate passed 1141 tests in 401.41 seconds, 91.43% branch-aware coverage; format/lint, strict mypy, build, schemas, and private-asset guard passed. The installed launcher's isolated Python imports these updated source modules. No app restart, real microphone, playback, or private-asset mutation was performed.

## Done criteria

All steps verified; full project gate passes; hand off the changed UX, how to load it, and the complete incorporated/adapted/rejected rationale. Do not commit or restart an active recording session automatically.
