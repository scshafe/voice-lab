# Testing

Native capture continuity tests use fake Audio Queue bindings and generated PCM;
they never implicitly open a microphone. They cover stable device UID selection,
native ABI/layouts, multiple buffers, sample-time gaps/overlaps, bounded writer
overflow, write/control/diagnostic failures, stop/dispose cleanup, exact WAV/progress
frame agreement, and private partial retention. Desktop discovery tests preserve
the native backend/UID without starting capture; historical backend evidence
remains parseable. Portable PCM tests distinguish repeated buffer-boundary joins
and short internal zero runs from smooth harmonics, fades, noise, impulses, and
quantized silence, including detector read-boundary invariance. These automated
checks do not replace a separately announced hardware capture and headphone review.

Native desktop tests use real Qt widgets with `QT_QPA_PLATFORM=offscreen`, set by
`scripts/verify`. They drive synthetic capture and history through the UI without
opening a microphone. Install the dev extra, or run the hash-pinned desktop
bootstrap before verifying an older development environment. Recording-library
tests exercise durable per-take dates, history, retries, interrupted sessions,
forensic recovery, changed device indices, and tamper rejection. Physical
microphone/headphone acceptance remains in [docs/DESKTOP.md](docs/DESKTOP.md).

Desktop experience tests cover the single large script inspector, optional
sortable history columns, canonical batch advancement including retakes/room
tone, read-only script scrolling, and window-scoped R/Space/Enter controls.
Editable fields, dialogs, popups, modifiers, and auto-repeat must not trigger
transport. Fake native audio bridges exercise playback, pause, exact PCM slices,
native-clock seeking, errors, and close sequencing without playing sound. Layout
checks keep visible controls inside 1320x860, 1060x760, and 1060x700 laptop windows.
Desktop entrypoint/packaging tests verify the shared application/window icon,
the Finder ICNS resource and plist entry, malformed/missing artwork failures,
and preservation of exclusive creation and shell-quoted launcher arguments.
Public PNG corners must remain transparent. Waveform tests cover raw extrema,
clipped sparse-bin painting, narrow/one-frame handle ranges, mouse/keyboard seeking,
and disabled/read-only drag cancellation. Recording-edit tests cover frame bounds,
hash/path/symlink validation, source preservation, restore, and downstream exclusion.
Desktop tests cover selected-prompt recording, pending review, historical trimming,
native playback interlocks, and recoverable take management. Synthetic real-backend
acceptance additionally exercises cross-batch selection and original-byte retention.

Editor-review regressions cover state-based Record/Retake emphasis, expanded numbered
takes, loaded/selected current-take approval identity, deferred-selection races,
historical QC labeling, bounded skips, double-click playback, per-take cursor memory,
paused seek clocks, and paused Save/Reset preview reloads. Waveform checks include
larger grips, hover cleanup, retained/excluded double-click behavior, and explicitly
labeled view-only fitting that preserves PCM and source-frame coordinates. Compact
layout checks assert ordered, non-overlapping editor sections in both palettes,
not just that buttons remain inside the window. Screenshot QA uses synthetic audio
and no hardware capture or private assets.

Silent-room-tone presentation tests cover both pilot tones, category-driven custom
tasks/durations, separate instruction versus script surfaces, accessible instruction
content, and light/dark contrast at compact window sizes. Record/review/Keep,
selection, and resume must retain the actual target's silent/spoken treatment;
these presentation changes must not alter canonical prompt text or catalog hashes.

Capture-clock regressions use confirmed PCM frame counts, including delayed first
audio, stalls despite advancing wall time, fractional durations, and resets. The
FFmpeg adapter's bounded packet-size reader is tested for malformed/missing output,
exit/drain/timeout cleanup, correct output-option placement, and mismatch against
final WAV frames before publication. UI tests cover immediate startup/stop cues,
frozen clocks during slow stop/abort, safe error recovery, and preserved prompt
locks. The independent abandoned-take watchdog remains wall-clock based.

Runtime integrity unit tests use temporary interpreter/formula files and mocked
OS probes. The production lock verifier still checks their complete bytes and
rejects changed interpreter, dependency, import-path, and OS evidence. Running
these tests does not require the mini's exact installed Homebrew Python.

`scripts/verify` is the authoritative local gate. It runs:

- Ruff format check and lint
- strict mypy
- pytest with branch coverage
- package build
- exact durable-schema set and schema drift check
- repository asset-leak guard

Tests generate small synthetic PCM WAV fixtures in pytest-owned temporary
directories. They never require a microphone, model download, Tailnet peer, or
human recording. Real runtime probes and renders are opt-in and write immutable
evidence beneath the private asset root.

Capture-console coverage is fully synthetic. A controlled process exercises
start, stop, accept, retake, resume, interruption, and the ten-minute cap with
frozen clocks and explicit mode-`0700` fixture directories. An ephemeral
loopback server is driven through `http.client` to verify its token, exact Host
and Origin/fetch-site policy, response headers, request bounds, transitions,
concurrent 409 response, 24-to-16-bit in-memory preview, real finalization to
`READY`, and shutdown. Resource tests enforce the strict CSP shape, local-only
assets, keyboard/focus contracts, and absence of browser recording APIs. These
tests do not open a real browser, request microphone permission, invoke capture
hardware, or touch the machine's private asset root. Safari/Chrome playback and
the physical UMIK-1 path remain the unchecked runbook acceptance gate.

The synthetic audition suite verifies all five candidate routes without loading
weights: immutable input/source hashes, path confinement, fresh-process worker
invocations, structured failures, normalization evidence, and exact-digest
resume. Real smoke results are a separate operator gate, never a unit-test side
effect.
