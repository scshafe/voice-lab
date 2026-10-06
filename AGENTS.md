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
  explicit transfer approval naming the destination and payload digest. D-020
  permits encrypted library storage on Cole's own approved vault: a standing
  `VaultDestinationApproval` binds the library, endpoint, namespace, stable
  Tailscale node ID, tag, tailnet suffix and file classes, with a 12-month expiry
  (at most 366 days). A ledger entry naming the plaintext manifest-plan payload
  digest must precede each generation's first new blob upload. `training` is a
  separate opt-in class. Storage needs no synthesis consent; the vault never
  receives plaintext or executes a model.
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
  encryption is the D-020 exception: `cryptography` is an optional `vault` extra
  at an exact version,
  hash-locked with its closure in `requirements/vault-macos.txt`, and only the
  sealing module imports it. The frozen Mac runtime carries that closure;
  the capture kit and vault server do not.
- Storage and catalog access go through ports, never a named implementation.
  Only the composition root builds an adapter for the lab; the vault server
  builds the local store it serves, and the two fixture seeders build their own.
  An import-boundary test fails if anything else does.
- Keep commands non-destructive by default. Downloads, remote deployment,
  hosted transfer, model eviction, and raw-asset removal require explicit flags.
  Vault approval and binding explicitly enable background encrypted copying.
  Removing local fine-tune checkpoints requires `finetune archive --run <id>
  --remove-local-copy` for each run, after download verification. Vault deployment
  remains a separate, Cole-approved action; documentation is not deployment.
- Preserve user changes and keep commits reversible.

## Required verification

Run `scripts/verify` before declaring an increment complete. Real model smoke
tests are opt-in and must record the exact code revision, weight revision,
environment fingerprint, reference digest, request, duration, and output digest.

<!-- scshafe-dev:begin landing -->
## Verify and landing

Managed by scshafe-dev: `dev adopt` and `dev update` refresh this section from `dev.toml`; change `dev.toml`, not these lines.

Before finishing, both of these must pass:

```sh
scripts/verify
dev check .
```

How a change lands:

1. Work on a branch and open a PR.
2. Run the two commands above. If the repository is private, GitHub Actions does not run for it: verify locally and say in the PR what you ran. If it is public, wait for CI to be green.
3. Merge the PR with a merge commit, one change at a time: `gh pr merge <N> --merge --subject "Merge #<N>: <title>"`. Never squash or rebase (both are off on the repository), and pass `--subject`: `gh pr merge` does not make the `Merge #N: <title>` subject by itself.
<!-- scshafe-dev:end landing -->
