# Cole Voice Lab agent guide

## Mission

Build a private, reproducible laboratory for recording Cole's voice, accepting
recordings onto the Mac mini, comparing local voice-cloning systems, and
promoting a winning, hash-pinned voice into ChloeSpeech.

## Invariants

- Human recordings, derived audio, model weights, consent evidence, and run
  outputs never enter Git.
- Runtime assets live under an explicit, permission-restricted asset root. The
  code repository must remain usable without access to those assets.
- Raw recordings are accepted only after every regular file, declared path,
  byte count, audio format, and SHA-256 digest has been verified. Symlinks,
  undeclared files, path traversal, and partial sessions fail closed.
- Accepted raw recordings are content-addressed and write-protected. Derived
  audio is always written elsewhere.
- Recognizable-human synthesis requires a checksum-bound reference and an
  approved, checksum-bound consent record with a matching subject and purpose.
- No human audio is sent to a hosted service or GPU host without a separate,
  explicit transfer approval naming the destination and payload digest. (The
  library vault is built but not deployed; a standing destination approval for
  it is proposed in D-020, library-vault plan Step 14. Until that decision is
  recorded, this rule stands exactly as written.)
- Model code and weights are identified by immutable revisions. A mutable
  branch, tag, package range, or model alias is not experiment evidence.
- Incompatible model stacks run in separate environments and communicate
  through the versioned JSON worker contract.
- Bake-offs use identical held-out text, references, seeds where supported,
  normalization, and scoring rules. Voice conversion is reported separately
  from direct text-to-speech.
- Blind listening is the primary decision signal. Automatic metrics are
  diagnostics, not a substitute for listening.
- ChloeSpeech remains the production rendering boundary. This lab exports
  manifests; it does not mutate ChloeSpeech or a consumer's catalog.

## Working agreement

- Python 3.12, strict Pydantic contracts, strict mypy, Ruff, and pytest branch
  coverage are the default implementation stack.
- Prefer standard-library capture and transfer primitives. Optional model and
  metric dependencies belong in isolated runtime environments. Library-vault
  encryption is the one exception, to be recorded in D-020 (library-vault plan
  Step 14): `cryptography` is an optional `vault` extra at an exact version,
  hash-locked with its closure in `requirements/vault-macos.txt`, and only the
  sealing module imports it.
- Storage and catalog access go through ports, never a named implementation.
  Only the composition root builds an adapter for the lab; the vault server
  builds the local store it serves, and the two fixture seeders build their own.
  An import-boundary test fails if anything else does.
- Keep commands non-destructive by default. Downloads, remote deployment,
  hosted transfer, model eviction, and raw-asset removal require explicit flags.
- Preserve user changes and keep commits reversible.

## Required verification

Run `scripts/verify` before declaring an increment complete. Real model smoke
tests are opt-in and must record the exact code revision, weight revision,
environment fingerprint, reference digest, request, duration, and output digest.
