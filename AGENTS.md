# voice-lab agent guide

## Mission

Build a private, reproducible laboratory for recording Cole's voice, accepting
recordings onto the Mac mini, comparing local voice-cloning systems, and
speaking with the one voice release Cole designates, for callers he registers
(D-044 to D-047).

## Invariants

- Human recordings, derived audio, model weights, consent records (history since
  D-050), and run outputs never enter Git.
- Runtime assets live under an explicit, permission-restricted asset root. The
  code repository must remain usable without access to those assets.
- Raw recordings are accepted only after every regular file, declared path,
  byte count, audio format, and SHA-256 digest has been verified. Symlinks,
  undeclared files, path traversal, and partial sessions fail closed.
- Accepted raw recordings are content-addressed and write-protected. Derived
  audio is always written elsewhere.
- Only Cole's own voice is synthesized, under his standing decision (D-050): his
  voice may be used for fine-tuning, listening (Speak), assistant speech and
  benchmarks on his own hardware. The subject is pinned in code to `cole-self`
  (`models.COLE_SUBJECT`, the speech configuration's `subject_id`,
  `release.SPEECH_SUBJECT`); designation, the speech API and job runner, reference
  publication, dataset building and the offline workers refuse any other subject.
  Zero-shot rendering needs a checksum-bound reference. No consent record is
  required or read; those already on disk stay as history.
- No human audio is sent to a hosted service or GPU host without a separate,
  explicit transfer approval naming the destination and payload digest. D-020
  permits encrypted library storage on Cole's own approved vault: a standing
  `VaultDestinationApproval` binds the library, endpoint, namespace, stable
  Tailscale node ID, tag, tailnet suffix and file classes, with a 12-month expiry
  (at most 366 days). A ledger entry naming the plaintext manifest-plan payload
  digest must precede each generation's first new blob upload. `training` is a
  separate opt-in class. The vault never receives plaintext or executes a model.
- Free, and on Cole's own hardware (D-028): no cloud service and nothing paid,
  anywhere. No object storage (ciphertext included), no hosted or rented GPU, no
  paid model, API, signing or notarization, no hosted CI beyond what is free for a
  public repository. GitHub, PyPI, flagged downloads of public weights at pinned
  revisions, and the fleet's free tailnet are allowed; none carries a private byte.
- Model code and weights are identified by immutable revisions. A mutable
  branch, tag, package range, or model alias is not experiment evidence.
- Incompatible model stacks run in separate environments and communicate
  through the versioned JSON worker contract.
- Bake-offs use identical held-out text, references, seeds where supported,
  normalization, and scoring rules. Voice conversion is reported separately
  from direct text-to-speech.
- Blind listening is the primary decision signal. Automatic metrics are
  diagnostics, not a substitute for listening.
- voice-lab is the speech engine; ChloeSpeech is retired (D-044). Speech outside
  the lab's bake-off and listening tools uses only Cole's current voice release: a
  checksum-bound record of one finished fine-tuned run of his own voice that Cole
  designates by CLI (D-045). Never the base weights, a zero-shot reference, another
  subject or an undesignated run.
- Speech synthesis over the API is a queued job in the one GPU slot for registered
  tailnet callers, completed asynchronously (polling, a per-job event stream of finished
  sentence segments, or a signed terminal webhook); never an always-on synthesis endpoint,
  audio streamed while it is synthesized, or a public endpoint. Sentences render
  sequentially with derived seeds, so reruns are byte-identical. Rendered audio leaves
  the PC only to a registered caller, is deleted after its retention, and every render
  is in the append-only render ledger, which holds the text's digest, never the text
  (D-046, D-050; docs/SPEECH_API.md).

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
3. Merge your own PR with a merge commit, one change at a time: `gh pr merge <N> --merge --subject "Merge #<N>: <title>"`. Never squash or rebase (both are off on the repository), and pass `--subject`: `gh pr merge` does not make the `Merge #N: <title>` subject by itself.

The project's agent may merge its own PR and push `main`; there is no approval gate.

Merging deploys to production ([deploy] lane `runner`: `.github/workflows/deploy.yml` verifies on the `voice-lab-verify` runner, deploys through the host entrypoint on the `voice-lab-prod` self-hosted runner, then checks health).
Watch the run yourself with `gh run list -w deploy`, `gh run watch <id>` and `gh run view <id> --log` (a public repository's deploy log is a summary only); say in your reply what the run did, naming the merge commit.
Roll back by merging a `git revert`, or by dispatching `deploy.yml` with `sha=<older commit on main>` and `allow_rollback=true` (`gh workflow run deploy.yml -f sha=<sha> -f allow_rollback=true`).
<!-- scshafe-dev:end landing -->
