# Decision log

## D-001 — Sibling lab, not ChloeSpeech core

Capture, biometric storage, dataset construction, training, and comparative
evaluation remain in `cole-voice-lab`. ChloeSpeech receives only a validated,
hash-pinned promotion manifest. This preserves ChloeSpeech's provider-neutral
runtime scope.

## D-002 — Local-first, explicit asset root

The repository has no default writable media directory. Commands that handle
private data require an asset root argument or `COLE_VOICE_LAB_ASSET_ROOT`.

## D-003 — Native PCM and immutable originals

Production capture uses CoreAudio through FFmpeg and writes mono 48 kHz PCM WAV,
preferably 24-bit. Calibration information is retained as metadata and is never
baked into raw recordings. Accepted originals become write-protected.

## D-004 — Worker isolation

Each incompatible model stack executes out-of-process using a versioned JSON
contract. Shared caches may deduplicate weights, but Python dependency graphs do
not leak into the core CLI.

## D-005 — Two experiment tracks

Qwen3-TTS, Chatterbox, and F5-TTS participate in the direct-TTS track. Seed-VC
participates in a performed-source voice-conversion track and is not silently
ranked as if it generated prosody from text.

## D-006 — Pilot gate before corpus

The first artifact is a roughly five-minute pilot. The full 90-minute training
corpus is conditional on recording quality and the zero-shot result.

## D-007 — Evaluation engine identity is not production-provider identity

MLX Chatterbox outputs are explicitly unwatermarked and are not mislabeled as
outputs from ChloeSpeech's official upstream Chatterbox adapter. If an MLX
candidate wins, promotion requires either a dedicated, reviewed ChloeSpeech MLX
provider or a separately evidenced official-runtime reproduction. The lab never
fabricates Perth-watermark evidence.

## D-008 — Sequential candidate residency

The 16 GiB mini runs one candidate in one fresh subprocess at a time. Model
process exit is the Metal-memory release boundary. Weight caches may also need
sequential explicit lifecycle management because the 10 GiB free-space floor
prevents all quality profiles from coexisting on the current disk.

## D-009 — Loopback capture control and fail-closed resume

A foreground, loopback-only capture console may drive the native FFmpeg
recorder. The browser controls recording but never captures audio. An
unfinalized session may be resumed only by re-inspecting every completed
canonical take; partial recordings are retained as rejected forensic evidence
outside the session directory and are never promoted to canonical takes.
