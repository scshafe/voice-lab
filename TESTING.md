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
- strict mypy (checked as macOS on every host)
- all synthetic native recording suites, including `vault` (macOS only)
- pytest with branch coverage (90% minimum), in parallel (`pytest-xdist`, `-n auto`), then
  the tests marked `serial` on their own: each runs a real service or native process
  against a deadline that a fully loaded machine can miss. Coverage is appended across the
  two passes and checked once. The desktop suite runs the same way; its serial pass may
  find no test (none is marked now). `serial` is for a real deadline, not a cure for a
  test that fails only under load: look for the race first (the two desktop failures of
  the PC's parallel pass were app bugs, a dialog reopened during its exit transition and
  a spool error raised on the GUI thread). A test must not
  depend on another's state or on a fixed port, and its parameters must be the same on
  every collection (a random value belongs in the test, not in its id).
- package build
- exact durable-schema set and schema drift check
- repository asset-leak guard

It runs on macOS and on Linux (the Arch PC; setup in the README). Off macOS the
native suites are skipped, and so are the tests that need macOS itself: each
carries a marker from `tests/macos_only.py` naming what it needs (the real
recording service, whose build identity binds the macOS Python runtime; Core
Audio; the Homebrew capture bootstrap; launchd; the macOS artifact locks; APFS
case-insensitivity). Tests of recording logic that only need *a* service build
identity get a deterministic, test-only stand-in off macOS (`tests/conftest.py`);
the production code still refuses to compute one there. The 90% branch-coverage
floor is the same on both hosts.

`pyproject.toml` sets `python_files` to `test_*.py` and `*_conformance.py`, so the
port conformance suites are collected with everything else. `tests/catalog_conformance.py`
is written only against the catalog port and obtains stores through the composition
root. `tests/storage_conformance.py` runs every check once per storage backend: the
local store, the sealed store over it, the vault client against a vault server on the
loopback, and the sealed store over that client, which is what the Mac actually pushes.
A check that needs a capability or a damage hook is its own test and starts by requiring
it, so a backend without one is reported skipped with a reason naming the capability;
no assertion is quietly dropped inside a passing test.

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

Library-vault tests cover the storage contracts and their error table
(`tests/test_storage_contracts.py`), the local store's root refusals, locks and
crash sweep (`test_storage_local.py`), sealing with the library key and its recovery
sheet (`test_storage_sealing.py`), the vault server's endpoints, refusals and access
log (`test_storage_server.py`), the client's identity check, retries and TLS path
(`test_storage_http_client.py`), binding and the `vault` commands
(`test_storage_binding.py`, `test_vault_cli.py`), path classification and replication
sources (`test_replication_sources.py`), the manifest chain with verify and restore
(`test_library_replication.py`), the service's background copying
(`test_library_replication_service.py`), and the fine-tune run archive round trip
(`test_finetune_archive.py`). Three helper modules are imported by those tests and
never collected themselves: `tests/vault_library_fixtures.py` (synthetic libraries and
a sealed local vault), `tests/vault_binding_fixtures.py` (a fake Keychain, a recorded
tailnet answer and a loopback vault) and `tests/vault_service_fixture.py` (a real
recording service with a vault it can reach).

`tests/test_storage_import_boundary.py` parses every module under `src/` and every
script in `scripts/`, including function-local imports and `importlib` names, and fails
if anything but the composition root, the adapters themselves, the vault server or the
two native fixture seeders imports an adapter; `cryptography` is allowed in the sealing
module alone. No test touches the real login Keychain, runs `tailscale`, or reaches a
network beyond the loopback. The Keychain, the tailnet resolver and the vault connection
are injected seams. The Keychain is a dictionary, or a fake `security` script in
`tmp_path` whose argv, standard input and environment the test inspects; an autouse guard
in that module refuses to run the real tool. Every other vault test injects a dictionary
Keychain through `tests/vault_binding_fixtures.install_vault_seams`. The resolver answers from
recorded JSON. Every server is one the test starts itself. The client's plain-HTTP mode
is a separate test-only constructor, and AST checks prove that no command, flag or
environment variable can reach it.

`scripts/verify-native-recording --suite vault` checks the actual hosted sidebar
text during copy, a stopped loopback server and recovery; saves synthetic audio and
independently downloads, decrypts and hashes the saved take; and checks the archived
Speak blocker, Base availability and unchanged workspace geometry. The fixture uses
injected credentials and identity, never the real Keychain or tailnet. Its private
listener/credential lifecycle also has failure-injection tests.

`scripts/verify-vault-container` is a separate offline gate, not part of
`scripts/verify`. It requires the reviewed cached base image and Linux wheelhouse,
uses the validated local Colima socket, builds with the legacy builder, and runs
storage conformance against a hardened container's test-only loopback port. The
`container-http` fixture accepts only literal loopback URLs and namespace `test`;
backing-store damage hooks are unavailable and explicitly skipped. It never starts
a Tailscale sidecar. See [deploy/vault/README.md](deploy/vault/README.md).

The full gate's direct Python commands, native dispatcher and ordinary synthetic
host helpers use `-I -B`, with explicit script/fixture module paths where needed.
Copied fixture services compute and check identity under the same explicit import
root; the synthetic F5 launcher preserves its emulated runtime arguments while
isolating its host interpreter. Production worker invocations and deliberate
startup-audit cases retain the runtime behavior they are testing; this is not a
claim that every interpreter descendant uses identical flags. Packaged-app rollback replay
runs only when `COLE_VOICE_ROLLBACK_RELEASE` explicitly names a release; the default
gate neither discovers nor stats the real Application Support release tree, and a
skipped rollback case is not reported as verified.
