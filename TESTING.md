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

The synthetic audition suite verifies all five candidate routes without loading
weights: immutable input/source hashes, path confinement, fresh-process worker
invocations, structured failures, normalization evidence, and exact-digest
resume. Real smoke results are a separate operator gate, never a unit-test side
effect.
