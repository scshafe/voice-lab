# Plan: in-app Speak page — type text, hear it in the fine-tuned voice

**Goal:** A second page in the native app, **Speak**, where Cole types text, picks a model (latest fine-tune, base, or an earlier run) and a reference clip, presses Speak, hears the result, and can save the WAV. The Record page is untouched.
**Out of scope:** Anything the corpus-first pipeline plan delivers (`2026-09-15-corpus-first-app-pipeline.md`, a prerequisite: it adds `finetune_runtime_root` to the app settings and the fine-tune runs the picker lists). Streaming or long-form synthesis (one utterance of at most 2,000 characters per request). Voice conversion. Any transfer of generated audio off the machine; the lab's consent and promotion rules for rendering are unchanged.

## Context

- Synthesis exists as a lab function: `synthesize_finetune_samples` in `src/cole_voice_lab/finetune.py` runs `resources/f5_synthesize.py` in the pinned F5 runtime; it takes a checkpoint, a reference clip plus its exact text, and texts, and writes 24 kHz mono 16-bit WAVs plus a `FineTuneSampleSet` record. `latest` model resolution and reference selection are the only new logic.
- Playback to reuse: `RecordingPlaybackSource(takeID:sourceSHA256:assetRoot:relativePath:frameCount:)` + `RecordingPlaybackController.select/playPause/seek` (`apps/macos/Sources/RecordingCore/RecordingPlaybackController.swift`), backend `AVFoundationRecordingPlaybackBackend.swift`. Check first whether the source accepts 24 kHz 16-bit input; prefer teaching it the format over transcoding in the synthesizer.
- App entry: `apps/macos/Sources/RecordingApp/RecordingApp.swift:14` shows `RecordingWorkspace(model:)` in a `WindowGroup` with a toolbar `Picker` for appearance. The workspace's fixed regions (`RecordingLayout.regions`) are checked by the workspace/accessibility suites, so the page switch must not resize the workspace: a toolbar segmented control, not a tab bar inside the window.
- The playback/capture interlock (no playback while recording; capture owns the audio session) is enforced in `RecordingPlaybackController.beginCapture/endCapture`; the Speak page must respect it.
- Reference clips: the pilot catalog flags four `reference_candidate` prompts; the default reference is the first of them with a clip in the chosen run's dataset.
- Verification as in the pipeline plan; the `speak` native suite is new.

## Steps

- [x] **Step 1: Synthesis contract and service handler**
  - `recording_contracts.py`: `SynthesizeCommand` (operation_id, text 1–2,000 chars, model `latest`|`base`|run_id, reference_prompt_id optional, nfe_steps 4–64) and `SynthesisEvent` (operation_id, state queued|running|finished|failed, run_id, relative_path, sha256, frames, sample_rate_hz, seconds, error); add both to the unions and the conformance fixtures (invalid: finished without a path, text over the bound).
  - `recording_service.py`: resolve the model (`latest` = newest `derived/finetune/*/run.json` with status finished; `base` = pinned weights; explicit run id), resolve the reference (`reference_prompt_id` or the first `reference_candidate` prompt with a clip in that run's dataset), call `synthesize_finetune_samples` off the control thread, one synthesis at a time with a 120 s deadline, output under `derived/finetune/<run-id>/speech/<operation-id>/`, events queued → running → finished/failed.
  - Tests in `tests/test_recording_service.py`: `latest` resolution order, base fallback, missing runtime refusal, text bounds, one-at-a-time, reference default.
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_recording_contracts.py tests/test_recording_service.py -k "synthes or contract" && .venv/bin/python -I -B -m cole_voice_lab.schema --check && scripts/verify-native-recording --suite contracts`

- [ ] **Step 2: Swift client and Speak model**
  - `RecordingServiceClient.synthesize(operationID:text:model:referencePromptID:nfeSteps:)`. `RecordingUI/RecordingSpeakModel.swift`: text, model choice (list built from the service's finished runs plus Base), reference choice, generating flag with cancel, this session's clips (path, sha256, seconds, text), playback through a second `RecordingPlaybackController` that yields to capture (Speak is disabled while recording and cannot start while a take is being saved).
  - Tests: `apps/macos/Tests/RecordingWorkspaceTests/RecordingSpeakModelTests.swift` (event application, phase transitions, interlock, model list ordering).
  - Verify: `cd apps/macos && swift test --filter RecordingSpeakModelTests`

- [ ] **Step 3: Speak view and page switch**
  - `RecordingUI/RecordingSpeakView.swift`: multi-line text editor with placeholder (⌘↩ = Speak), model picker, reference picker (reference prompts only), `Speak` button with `Generating…` and cancel, a player for the newest clip (play/pause, elapsed/total, scrub), the session's clip list with text and duration, `Save As…` (NSSavePanel, WAV copy). In `RecordingApp.swift`: a segmented `Picker("Page")` in the window toolbar, `Record` | `Speak`; the workspace keeps its size and geometry; keyboard shortcuts stay window-scoped per page (R/Space/Return on Record; ⌘↩ on Speak).
  - Accessibility: labels on every control, VoiceOver reads generation state changes, Reduce Motion respected, contrast per `RecordingPalette`.
  - Verify: `cd apps/macos && swift test --filter RecordingSpeakTests && scripts/verify-native-recording --suite accessibility` (extended with the Speak page)

- [ ] **Step 4: Native `speak` verification suite**
  - Add `speak` to `scripts/verify_native_recording.py`/`verify_native_workspace.py`: synthesis against the stub runtime, `SynthesisEvent` sequence observed, the clip's sha256 re-read by Python, playback source accepted, interlock while a synthetic recording is active, cancel mid-generation, failure message shown verbatim.
  - Verify: `scripts/verify-native-recording --suite speak`

- [ ] **Step 5: Docs and decision record**
  - `docs/NATIVE_RECORDING_UX.md`: Speak page section (text bounds, model/reference choice, playback interlock, Save As, failure rows in the §10 matrix: runtime missing, synthesis deadline, no finished run yet). `docs/FINETUNE.md`: "Speak page". ~~`DECISIONS.md` D-018:~~ `DECISIONS.md` D-019 (D-017 is the corpus store's and D-018 the corpus-first pipeline's): in-app synthesis is a listening tool; generated audio stays local and is not a reference, dataset or promotion artifact.
  - Verify: `.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy src/cole_voice_lab`

- [ ] **Step 6: Build, physical check, install**
  - Build on top of the corpus-first pipeline release (same settings, which carry no catalog: the app loads the active corpus-store catalog from the service), release verification, `codesign --strict --deep`, full `scripts/verify`. Physical check with Cole: switch to Speak, type a sentence, hear it from the latest fine-tune and from Base, Save As to a folder, confirm Record still records. Install by copytree per the handoff procedure.
  - Verify: Cole hears the fine-tuned voice from the Speak page in the installed app; `speech/<operation-id>/samples.json` validates

## Done criteria

`scripts/verify` passes with the `speak` native suite; the installed app plays typed text in the fine-tuned voice from the Speak page and the Record page is byte-identical in geometry; committed on `capture-console` as `feat: in-app Speak page` after Cole's go-ahead.

**Amended 2026-09-16:** the decision record is renumbered D-019, because the corpus store (`2026-09-15-catalog-in-database-corpus-store.md`) took D-017 and the corpus-first pipeline plan now takes D-018. Since corpus-store Step 4 the app settings carry no catalog (`native-application-settings/v2` with a required `purpose`); the prompt list, including the `reference_candidate` prompts Step 1 resolves references from, comes from the active corpus-store catalog the service pins, so a Speak build needs no catalog in its settings.

**Amended 2026-09-16 (before execution):** a code survey found five statements the steps must not rely on, and the orchestrator settled the open design points. (1) Playback accepts only the recording format (48 kHz/24-bit/mono), so `RecordingPlaybackSource` gains an explicit audio format and the backend checks the declared one; speech clips are 24 kHz/16-bit/mono, written 0600 and checked from the WAV header before a `finished` event. (2) The app scene is a single `Window` without a toolbar: the page switch is a `RecordingPageContainer` in RecordingUI with a segmented `Record | Speak` toolbar control and View-menu items (⌘1/⌘2), disabled while recording or saving; the Record workspace keeps its frame and its keyboard monitor lives only while it is shown; `RecordingWindowLifecycle` moves to the container. (3) The synthesizer is `src/cole_voice_lab/resources/f5_synthesize.py`. (4) A reference clip needs a finished run: `base` uses the pinned base weights with the reference from the latest finished run; with no finished run every choice is refused with `FINETUNE.NO_FINISHED_RUN`. Clips go to `derived/speech/<operation-id>/` (`speech-01.wav` and a `speech.json` record with relative paths only), so finished runs stay unchanged. `FineTuneStatusEvent` gains `finished_runs` (newest first, at most 32) for the model picker. (5) The frozen service never overrides the pretrained pin, so the `speak` native suite reuses the pipeline's test-only service wrapper and stub runtime. Execution: synthesis is a second, in-memory job kind of the fine-tune supervisor, one at a time, sharing one GPU slot with fine-tuning (each refuses the other with `FINETUNE.JOB_ACTIVE`), with a 120 s per-request deadline (`FINETUNE.SYNTHESIS_DEADLINE`) and cancel by process group; runtime and checkpoint digests are cached by file identity so a request does not re-hash gigabytes. The decision record is D-019. Session notes: `.claude/work/speak-decisions.md`, `.claude/work/speak-codemap.md`.
