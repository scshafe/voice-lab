# Testing

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
