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
- Reference audio is bound by digest in every experiment. Only Cole's own voice
  is synthesized (D-050): the offline model workers, reference publication and
  dataset building, voice release designation, the speech API and job runner, and
  the model comparison's benchmark each refuse any subject but the pinned
  `cole-self` (`models.COLE_SUBJECT`). No consent record is required or read; Cole's
  standing decision is the permission, and consent records already on disk stay as
  private history (the guard still treats an approved one as private evidence).
- The default network policy is local/Tailnet-only. Model downloads may use the
  public network only with an explicit download flag; human audio never does.
- Capture sync is bound to the reviewed mini Tailnet destination, private remote
  account, and intake root. Live execution requires the digest of a separately
  reviewed plan in addition to the network and execution flags, then requires
  both rsync commands to equal the canonical full argument vectors.
- Hosted GPU use requires a new approval containing destination, exact payload
  digest, purpose, retention terms, and expiry. Under D-028 the platform uses no
  hosted GPU and no cloud or paid service at all; the rule stays as a rule.
- Synthetic output must retain provenance and be disclosed as synthetic when it
  leaves private evaluation. Speech rendered for a registered caller is private use:
  it reaches only Cole's own devices, and its provenance is the render ledger and the
  output digest. No watermark is applied for now; this and disclosure are revisited
  before rendered audio could reach anyone else (D-046).
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
reached over the tailnet. The implementation, container templates and D-020
policy are built; deployment remains outstanding. No real library is bound to
a vault and none of Cole's library has been uploaded. The Mac keeps its working
database and files; this is storage, never a remote catalog or model service.
From phase 2 (D-023, D-034) the PC holds the working library and copies it to
several vaults on Cole's hardware (the laptop, then the Air), each destination
with its own binding, approval, records and tokens; what follows holds for each.

- Everything is sealed on the Mac before it leaves. Each blob is encrypted with
  AES-256-GCM under a 256-bit library key kept in the login Keychain (on the
  PC, as a root-owned encrypted systemd credential that only the vault unit
  receives, decrypted into its private read-only credential directory), whose
  only other copy is a recovery sheet Cole keeps offline. The key is never sent
  to the vault, and no binding can turn encryption off.
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
  12-month expiry (at most 366 days), naming the library, endpoint, namespace,
  file classes, node's stable Tailscale ID, `tag:voice-lab-vault` and MagicDNS
  suffix. The binding pins the approval's file digest. This D-020 exception
  replaces a separate per-upload approval only for encrypted storage on Cole's
  approved host. The client re-checks that identity before each session,
  connects to the address the check returned, and verifies TLS for the endpoint
  name. Nothing is pinned to a certificate.
- Fine-tune checkpoints and datasets are a separate file class that both the
  binding and the approval must list, so they are copied only on an opt-in.
- A local copy is removed only by an explicit per-run command, after that run's
  bytes have been fetched from the vault and decrypted again, and only the
  run's checkpoints are removed.
- The server keeps only the SHA-256 of each namespace's read and write tokens;
  the raw tokens stay in the copying host's keychain (the Mac's login Keychain,
  the PC's encrypted credentials). Each destination has its own pair, so two
  vaults never share a token, and the PC's unit for one destination imports
  only that destination's tokens.
- The server runs as UID/GID 10001 with a read-only root, dropped capabilities,
  no privilege gain and an internal-only network. A dedicated Tailscale sidecar
  supplies HTTPS; no host port is published and Funnel is absent. The deployment
  policy permits only Cole's Mac to reach the tagged node's port 443 and gives
  that tag no outbound grant; existing broad rules must be reviewed at deployment.
- Vault storage needs destination approval and no consent record (consent records
  are retired, D-050): Cole's own host holds opaque ciphertext and never
  synthesizes or runs a model. Hosted GPU use still requires its own approval.
  Speak clips (`derived/speech/**`), local vault records, locks, caches and
  unfinished runs stay on the Mac.
- D-020 permits the exact optional `vault` encryption dependency, hash-locked
  with `cffi` and `pycparser` in `requirements/vault-macos.txt`. Only
  `storage_sealing.py` imports `cryptography`. The frozen Mac service runtime
  includes the closure for its worker; the server and capture kit omit it.

Two limits are accepted rather than hidden:

- Sealing does not hide sizes. A sealed blob is exactly 33 bytes longer than
  its contents, so the host learns each chunk's size and the time it arrived,
  can link blobs of equal size, and can guess that a file of a distinctive size
  is stored. The format's version byte leaves room for padding later.
- The host can withhold or delete ciphertext. Encryption protects confidentiality
  and checks integrity, not availability. D-023 adds destinations (the laptop,
  then the Air offsite), but each is one copy that its host can lose. The host's privileged account is therefore
  outside the plaintext trust boundary, not a guarantee of durable storage.

Mac procedures are in [docs/RUNBOOK.md](docs/RUNBOOK.md#9-library-vault); host
hardening and deployment gates are in [deploy/vault/README.md](deploy/vault/README.md).

## Speech for registered callers (built, not yet deployed)

The speech API of D-046 ([docs/SPEECH_API.md](docs/SPEECH_API.md)) adds these controls:

- Only Cole's current voice release speaks (D-045), and only a release of the subject the
  speech configuration pins (`subject_id`, `cole-self`): designation refuses another
  subject's run (no caller can name another subject), and the API and the job runner refuse
  another subject's release. A changed run or model file refuses the render until Cole
  designates again; before each sentence the job runner checks that the release is still
  current, so a new designation stops a render at its next sentence. No consent record is
  read (D-050).
- Callers are listed in a root-owned caller registry with their token issuer, subject and
  audience, purposes, limits and webhook URL allowlist. A caller's machine token reaches
  the speech routes only, and a caller sees only its own jobs.
- Webhook URLs are `https` on hosts under the tailnet suffix only. The delivery process
  resolves the host, refuses any address outside the tailnet ranges, connects to the
  address it checked, follows no redirect, and uses short timeouts. Events carry no text
  and no audio, and are signed per caller (Standard Webhooks, HMAC-SHA256).
- Webhook secrets are root-owned files (0600) referenced by path; systemd hands them to the
  delivery unit alone as credentials (`LoadCredential=`), so no other process, the API and
  the job runner included, can read them, even as the `voice-lab` user. None is in
  configuration or Git. A webhook URL's path is compared with the allowlist after parsing,
  and one with a dot segment or a percent-encoded dot, slash or backslash is refused.
- A caller sees a failure's code and one fixed sentence; paths, a release's mismatch and
  worker errors stay in the job runner's journal.
- The API container only enqueues and serves finished files (whole sentences and the
  joined audio); the job runner and its resident model worker render with no network; the
  delivery unit reaches the tailnet only.
- A model comparison's benchmark (D-049) renders only a finished run of the pinned subject,
  as an unwritten release preview, and reads no consent record (D-050); its audio is hashed
  in the benchmark's own scratch under `state/speech/benchmark/` (never the lab) and deleted,
  and its record holds digests and timings only. Like the job runner it renders with no
  network and only the GPU's devices:
  on the PC it runs as `voice-lab-speech-benchmark.service` (deploy/host-manual/), whose
  fences are the runner's and which writes only the GPU slot, its own scratch under
  `state/speech/benchmark/` and `provenance/model-comparisons/`; never the lab's `tmp/`,
  which holds raw take originals. `voice-lab families record --benchmark` refuses to render
  unless the unit's marker (`VOICE_LAB_SANDBOX`) is set and no network interface but
  loopback is visible; `--i-render-outside-the-sandbox` is the operator's explicit
  acceptance of rendering Cole's voice in an unfenced shell, and the record keeps
  `sandboxed: false`. `sandboxed: true` means those two observations only, not proof of the
  unit's device and file fences.
- The job's text is deleted when the job ends, on every path, first. The job record and the
  render ledger name it only by a keyed digest (HMAC-SHA256 under a 32-byte key made once per
  install in the speech state, never in the asset root or the vault) and its length, so a
  short reply cannot be recovered by hashing guesses. Rendered audio is kept 7 days by
  default, outside the asset root, so the vault never copies it.

## Deliberately avoided

- Voice-authentication phrases in the recording script.
- Browser capture, automatic gain control, denoising, echo cancellation, or
  destructive cleanup of raw audio.
- Network-exposed, background, or auto-started capture-console services.
  The separately approved, tailnet-only vault stores ciphertext and is not a
  capture console or synthesis endpoint.
- An always-on synthesis endpoint, audio streamed while it is being synthesized, or a
  synthesis endpoint reachable beyond the tailnet. Synthesis is a queued job for
  registered callers whose finished sentences may be fetched as they complete (D-046),
  amending "long-running synthesis endpoints" here.
- Secrets, OAuth tokens, host credentials, or private host keys in configuration.
  Vault bindings name keychain or credential items; server configuration contains token hashes,
  never raw tokens or encryption keys.

## Revocation

There is no consent record to revoke (D-050). Cole withdraws his standing
permission by recording a new decision, together with the code change that
enforces it; until it lands, stopping the speech units (and not starting the
benchmark unit) stops every render for callers at once, and designating another
run stops the current release at its next sentence. Withdrawing permission does
not silently delete evidence or backups. Deletion is a separate, explicit,
audited operation.
An expired, missing or changed vault approval stops new uploads when the vault
opens. The storage ports expose no delete operation: removing host data is a
separate operator action. Destroying both the library key and its recovery sheet
makes every sealed copy unreadable; token rotation does not change that key.
