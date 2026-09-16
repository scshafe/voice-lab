# Plan: Native desktop recording library

**Goal:** A standalone Python desktop window shows the entire recording library,
completion, recording dates, and preserved take history, and controls native capture.
**Out of scope:** Real microphone recording by an agent, model provisioning,
hosted transfer, changing the model runtime locks, or generating the full training corpus.

## Context

The user explicitly wants a desktop window, superseding the browser-console UI
choice in D-009. Keep the canonical native FFmpeg take engine and exact-package
finalization. The bundled pilot has 36 prompts across eight categories. The full
training corpus remains gated by pilot review; the desktop can load explicit suites.
Current TakeEngine is sequential and only manifests a session timestamp. A catalog
selection becomes a new exact capture suite including room tone; new sessions
preserve old takes. Private sidecars bind per-take UTC timestamps and checksums.
Legacy recordings display their known session date, never an invented take date.
Qt Widgets supplies the native window, without browser or HTTP capture control.
The optional desktop dependency stays separate from the capture-kit/model locks.

## Steps

- [x] **Step 1: Implement a persistent recording library**
  - Verify: `.venv/bin/pytest tests/test_recording_library.py`
  - Notes: Full catalog rows; selected batches; safe resume; preserved timestamp,
    history, and original audio; checksum/path validation; serialized writers.
- [x] **Step 2: Build the native desktop recording window**
  - Verify: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/test_desktop_window.py`
  - Notes: Sort/filter table, dates and age, history playback, batch capture,
    resume, QC, and responsive native controls. No microphone opens during tests.
- [x] **Step 3: Provide a reproducible desktop launch path**
  - Verify: Desktop CLI tests, pinned dependency installation, native window smoke,
    and launcher generation/launch with no microphone capture.
  - Notes: Explicit download gates; a macOS application launcher; accurate docs.
- [x] **Step 4: Make existing runtime tests host independent**
  - Verify: The eleven previously failing runtime tests pass on this Mac with
    synthetic runtime evidence, retaining production integrity checks.
  - Notes: No model lock or installed Homebrew changes. Baseline was 561 passed,
    11 failed and 88.36% coverage due to a missing mini-specific Python prefix.
- [x] **Step 5: Review and verify the integrated increment**
  - Verify: `scripts/verify`; native window visual inspection and synthetic flow.
  - Notes: Register sidecar schemas, document the new primary desktop workflow,
    preserve earlier sessions, and record any remaining physical acceptance work.

## Done criteria

The desktop opens its own window and shows all 36 pilot prompts. Recording a
synthetic selected batch persists per-take dates across reopening; retakes add
history without replacing originals. `scripts/verify` passes. The local launcher
is available, and real microphone/listening acceptance remains clearly identified.

## Verification outcome

2026-09-07: `scripts/verify` passed 660 tests with 90.57% branch-aware coverage,
strict types, Ruff, package build, generated schemas, and private-asset guard.
Native widgets were visually checked at 1320×860 and 1060×760 and with a long
transcript. A synthetic UI-to-engine workflow finalized two batches across
reopening and verified both dated recordings remained in history. The local
macOS launcher was installed in the user's Applications directory. Review
findings for interrupted retries, abandoned write temporaries, and changing
microphone indices were fixed and regression-tested. The declared build tools
were added to the dev dependencies after the build stage exposed their absence.
