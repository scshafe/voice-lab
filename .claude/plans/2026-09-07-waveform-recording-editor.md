# Plan: Waveform recording editor and direct prompt selection

**Goal:** Make the native recorder feel like an audio editor: truthful waveform,
seek/pause transport, reversible head/tail trims and removal, direct selected-prompt
recording, and recognizable semantic icons.
**Out of scope:** Hosted audio, new dependency downloads, model training, changing
accepted raw bytes, purging originals, automatic microphone capture, or restarting
an operator-owned recording app. Icon-border cleanup amends the installation plan.

## Design and safety decisions

- Preserve prior uncommitted work. Baseline: 692 tests, 90.90% coverage.
- Keep canonical catalog/path order and mandatory room tones for finalization,
  but allow desktop execution order to follow selected rows. Default console
  prefix semantics remain unchanged. Existing v1 provenance must still open.
- What if a row changes during recording? Disable selection. What if a review is
  pending? Preserve it, never silently discard or assign its audio to another row.
  When switching between batches, end/preserve the old session before opening one.
- What if an older accepted take is wrong? Reversible exclusion is enforced at
  the shared accepted-utterance eligibility boundary, not only hidden in the UI.
- What if a trimmed file is tampered with? Validate source and derivative hashes,
  paths and frame bounds; fail clearly. Original packages stay immutable.
- Trims are separate frame-addressed PCM derivatives, with an explicit recipe.
  Existing raw-bound reference/dataset contracts are not silently relabeled as
  trimmed training data; future export must opt into the derivative identity.
- Choose native AVAudioPlayer with a thin standard-library bridge over adding
  QtMultimedia or an approximate `afplay` seek timer. Qt polls the real audio clock;
  trim previews contain only the selected PCM frames.
- Draw the full original min/max waveform in Qt, with playhead and start/end
  handles plus precise time fields. No second microphone stream for visualization.
- Use native transport/refresh/trash icons with tooltips and accessible names;
  status icons retain readable text and meaningful color.

## Steps

- [x] **Step 1: Add safe selection and recording edits.**
  - Verify: synthetic backend tests cover old v1 resume, arbitrary order, prompt
    identity, trim/reset, retained original hashes, remove/restore and eligibility.
  - Notes: backend owner `recording_edit_contract`; root coordinates UI APIs.
- [x] **Step 2: Add exact PCM playback and waveform controls.**
  - Verify: fake-bridge native-audio tests, frame-exact slices, waveform bins,
    Qt drag/keyboard tests, pause/seek/end/error guards; no audible test playback.
  - Notes: audio owner `native_desktop_window`; waveform owner `recording_edit_contract`;
    root owns GUI integration.
- [x] **Step 3: Integrate selected-prompt editing and semantic icons.**
  - Verify: offscreen workflow tests for selecting/recording, review/history trim,
    delete/restore/retake, playback conflicts, and compact light/dark layouts.
- [x] **Step 4: Verify, document, and hand off safely.**
  - Verify: `scripts/verify`, `git diff --check`, synthetic real-backend workflow,
    packaged icon inspection and screenshot QA.
  - Notes: no app termination; user safely reopens to load code and Dock changes.

## Done criteria

All requested controls are implemented and tested, private originals remain
unchanged, icon has true transparent outside corners, and physical microphone/
headphone checks are explicitly left to the operator. No commits or transfers.

## Verification evidence

- Backend/edit/reference/dataset focused suite: 169 passed; new edit schema checked.
- Native audio: 55 synthetic/fake-bridge tests; waveform: 28 offscreen tests;
  GUI: 74 offscreen tests. Root also exercised AVAudioPlayer loading/seeking/stopping
  synthetic 24-bit PCM without ever calling play or opening a microphone.
- Real-backend synthetic Qt acceptance passed: selected Record, trim/apply with
  unchanged original bytes, cropped seek/pause, Keep, outside-batch selection,
  recoverable removal/restore, and exact-prompt retake. Compact 1060x700 and
  larger 1320x860 screenshots were inspected in light/dark palettes.
  A further retake batch recorded the selected utterance before both required
  room tones, correctly focused each new take, and finalized to READY while
  retaining the earlier dated history.
- Review fixes preserve the session on failed mic startup, release a new session
  on prompt-selection failure, select the exact new take for review, retain failed
  Keep edits, and avoid racing initial device scan with waveform loading.
- Public icon exterior is transparent; rebuilt installed ICNS matches repository
  bytes and Finder export was visually inspected. Operator app was not restarted.
- Full `scripts/verify` passed: 850 tests in 262.22 seconds, 90.77% branch-aware
  coverage; Ruff, strict mypy, package build, schemas, and private-asset guard pass.
  Built wheel editor modules and cleaned icon bytes match the working sources.
  `git diff --check` passed. QA scripts/screenshots/logs are in
  `/tmp/cole-voice-editor.KmDy12/`. Hardware capture/listening and safe reopening
  remain operator acceptance; no operator recording or application was interrupted.
