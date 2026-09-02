# Security and biometric-data policy

Cole's natural recordings, derived speaker representations, cloned-model
weights, and recognizable synthetic outputs are biometric assets.

## Required controls

- Asset roots and session staging directories use mode `0700`; regular private
  files use `0600` until raw intake makes them read-only.
- File names are relative POSIX paths. Absolute paths, `..`, empty components,
  control characters, alternate separators, symlinks, sockets, devices, and
  undeclared files are rejected.
- The intake re-computes SHA-256, audio metadata, and QC from opened file
  descriptors; capture-side QC is advisory. It publishes from fresh inodes,
  revalidates all file identities/digests at the final boundary, and binds both
  source and intake QC digests in the v2 receipt.
- `scripts/verify` invokes `scripts/guard-private-assets`; it scans regular
  worktree files, Git index blobs (including staged content that differs from the
  worktree), private evidence JSON patterns, common audio/model/embedding
  formats and signatures, and wheel contents. Only structurally valid generated
  schemas inside `schemas/*.schema.json` bypass instance inspection; an
  arbitrary `$schema`, `properties`, or `required` field cannot hide approved
  consent or provenance. No Git hook is installed automatically.
- Reference audio and consent evidence are bound by digest in every experiment.
  Consent validation also requires the exact execution boundary; offline model
  workers require `tailnet-local-host`, so capture-only authorization cannot be
  reused for synthesis.
- The default network policy is local/Tailnet-only. Model downloads may use the
  public network only with an explicit download flag; human audio never does.
- Capture sync is bound to the reviewed mini Tailnet destination, private remote
  account, and intake root. Live execution requires the digest of a separately
  reviewed plan in addition to the network and execution flags, then requires
  both rsync commands to equal the canonical full argument vectors.
- Hosted GPU use requires a new approval containing destination, exact payload
  digest, purpose, retention terms, and expiry.
- Synthetic output must retain provenance and be disclosed as synthetic when it
  leaves private evaluation.
- Capture-kit creation is offline by default. Public-network dependency fetching
  requires `--allow-network`; runtime dependencies and the application wheel are
  exact-versioned and hash-locked inside the resulting bundle.
- The capture console is a foreground-only loopback controller. It binds only
  `127.0.0.1` on an ephemeral port and requires a fresh 256-bit per-run token,
  an exact loopback `Host`, and an exact same-origin `Origin` or
  `Sec-Fetch-Site` check for every API request. Its page uses a restrictive
  Content Security Policy, never accesses browser audio-capture APIs, and serves
  only in-memory 16-bit playback previews; preview bytes never touch disk.
- The capture-kit `SHA256SUMS` digest is evidence that must travel separately
  from the bundle. Bootstrap verifies that digest before trusting any bundled
  manifest, wheel, requirements file, example configuration, or bootstrap copy.
- MacBook bootstrap installs into a private sibling staging root and publishes
  the final capture root by rename only after verification. A digest-bound
  `.INSTALLING` receipt permits recovery with the same bundle without silently
  accepting stale installation state.
- The mini intake LaunchAgent runs only through a durable deployment launcher.
  Its v2 receipt binds the verified application wheel, the exact installed
  application package, every entry in the release tree, the console script, and
  its resolved Python interpreter. The launcher re-hashes that complete closure
  on every scan and sanitizes Python path/user-site variables; a changed module,
  added `.pth`, or repository `.venv` fails closed. All release symlinks are
  rejected except Python launcher aliases that resolve to the separately hashed
  interpreter; symlinked import/path-hook content is never accepted.
- `config/site.example.toml` is documentation, not an operative configuration or
  policy mechanism.

## Deliberately avoided

- Voice-authentication phrases in the recording script.
- Browser capture, automatic gain control, denoising, echo cancellation, or
  destructive cleanup of raw audio.
- Network-exposed, background, or auto-started capture-console services.
- Long-running synthesis endpoints.
- Secrets, OAuth tokens, host credentials, or private host keys in configuration.

## Revocation

Revocation prevents new experiments and exports. It does not silently delete
evidence or backups. Deletion is a separate, explicit, audited operation.
