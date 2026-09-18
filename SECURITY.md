# Security and biometric-data policy

Cole's natural recordings, derived speaker representations, cloned-model
weights, and recognizable synthetic outputs are biometric assets.

## Required controls

- Asset roots and session staging directories use mode `0700`; regular private
  files use `0600` until raw intake makes them read-only.
- The corpus store under `provenance/corpus/` keeps its directories at `0700` and
  its database, lock files and snapshots at `0600`, owned by the operator, never
  behind a symlink or hard link; snapshots are content-addressed, written once and
  never overwritten, and every reader checks the whole-file SHA-256 its reference
  names ([docs/CORPUS_STORE.md](docs/CORPUS_STORE.md)).
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

## Library vault (built, not yet deployed)

The vault is an encrypted durable copy of the library on Cole's own Linux host,
reached over the tailnet. The code is built and committed; the container, the
deployment and the rule changes it needs are not. No library is bound to a
vault and nothing has been uploaded.

- Everything is sealed on the Mac before it leaves. Each blob is encrypted with
  AES-256-GCM under a 256-bit library key kept in the login Keychain, whose
  only other copy is a recovery sheet Cole keeps offline. The key never leaves
  the Mac, and no binding can turn encryption off.
- The vault holds ciphertext blobs addressed by the digest of their ciphertext,
  plus named pointers. It applies no lab rule and is never trusted: a file
  counts as present only after its bytes have been downloaded and decrypted.
- A generation's manifest is itself sealed, chained to the digest of the
  previous manifest, and pinned by a head the Mac compares with the generation
  and digest it recorded locally. A head that is older, forks, fails to
  decrypt, or belongs to another library or destination is refused before
  anything is uploaded.
- Every upload is preceded by a ledger entry naming the destination, the file
  classes and the payload digest of the generation's manifest plan; a
  completion entry follows the head swap. The whole chain is re-read and
  checked before an entry is appended.
- Uploading to a tailnet vault requires a standing destination approval with a
  mandatory expiry, naming the node's stable Tailscale ID, its tag, the tailnet
  and the namespace. The client re-checks that identity before each session,
  connects to the address the check returned, and verifies TLS for the endpoint
  name. Nothing is pinned to a certificate.
- Fine-tune checkpoints and datasets are a separate file class that both the
  binding and the approval must list, so they are copied only on an opt-in.
- A local copy is removed only by an explicit per-run command, after that run's
  bytes have been fetched from the vault and decrypted again, and only the
  run's checkpoints are removed.
- The server keeps only the SHA-256 of each namespace's read and write tokens;
  the raw tokens stay in the Mac's login Keychain.

Two limits are accepted rather than hidden:

- Sealing does not hide sizes. A sealed blob is exactly 33 bytes longer than
  its contents, so the host learns each chunk's size and the time it arrived,
  can link blobs of equal size, and can guess that a file of a distinctive size
  is stored. The format's version byte leaves room for padding later.
- The rule changes this design needs — a standing destination approval instead
  of a per-transfer one — are still pending in D-020, at Step 14 of the
  library-vault plan. Until that decision is recorded and the vault is deployed
  (Step 15), the network and transfer rules above stand exactly as written.

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
