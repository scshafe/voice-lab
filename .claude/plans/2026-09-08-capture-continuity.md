# Plan: Preserve microphone capture continuity

**Goal:** Locate the live buffer-loss mechanism, prevent silent capture discontinuities,
and expose suspect recordings before the operator records more training material.
**Out of scope:** Editing/removing existing takes, noise suppression, silence insertion,
gain changes, hosted audio transfer, model work, or restarting the operator app.

## Context

- Read-only inspection found repeated sharp joins aligned with 512-frame boundaries
  in original speech and room-tone WAVs; this predates the media-clock change.
- FFmpeg's AVFoundation input retains only the latest unread audio buffer. Its
  demux polling delay can lose input before the encoded-frame counter sees it.
- Existing QC catches long exact-zero runs but not missing chunks joined together.
- The user approved live diagnosis and a capture fix. Any bounded live probe is
  announced, automatically stopped, kept private, and excluded from the corpus.

## Steps

- [x] **Step 1: Trace live microphone input timing.**
  - Verify: bounded timestamp/sample-count probe; compare source timestamp span with
    received frames; retain private diagnostics without changing an existing take.
  - Notes: root owns diagnosis. Check no FFmpeg capture is active before opening mic.
- [x] **Step 2: Replace the confirmed loss mechanism.**
  - Verify: synthetic delayed-consumer, timestamp gap, overflow, stop/abort, device
    binding, exact WAV-frame and metadata checks; one bounded hardware confirmation.
  - Notes: compare repairing FFmpeg ingestion with a native macOS continuous capture
    adapter behind the existing process contract; decide from evidence before coding.
- [x] **Step 3: Flag suspicious saved discontinuities.**
  - Verify: generated clean/removed-buffer/noisy/impulsive/quiet PCM tests with
    conservative findings; no transformation of recorded audio or existing manifests.
  - Notes: worker owns audio.py and test_audio.py; root coordinates evidence fields.
- [x] **Step 4: Verify and document the integrated workflow.**
  - Verify: `scripts/verify`, `git diff --check`, safe installed-launcher/reopen check,
    independent failure-path review, and precise implementation/acceptance claims.
  - Notes: root owns integration/docs; human listening remains a separate acceptance.

## Done criteria

The implemented recorder passes continuity/failure regressions and full repository
verification. The user receives hardware evidence, any remaining acceptance step,
and safe reopen instructions. Existing audio remains untouched and private.

**Amended 2026-09-08:** The bounded live FFmpeg probe confirmed input timestamp
gaps. Chosen implementation is an isolated Audio Queue worker behind the existing
process contract, with stable UID selection and no global input-device changes.
Native synthetic tests and an announced UMIK-1 hardware probe passed; the latter
had continuous timestamps, matching written/progress/WAV frames, and no repeated
buffer-boundary QC finding. Private diagnostics stay in the explicit asset root.
Independent review added cleanup-phase interruption handling, finite timestamp
checks, preserved device metadata, ancestor-symlink rejection, and exact WAV hash
binding before publication.

**Verified 2026-09-08:** `scripts/verify` passed all 1080 tests in 345.08 seconds
at 91.31% branch-aware coverage, plus strict types, format/lint, package build,
schemas and the private-asset guard. `git diff --check` is clean. The installed
Applications launcher points to this checkout; no reinstall or automatic restart
is needed. The operator must reopen, start a new native session, and review a short
spoken take on headphones before resuming corpus recording. No commit requested.
