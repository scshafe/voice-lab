# The library vault: an encrypted durable copy of the whole library

The vault is a second copy of the recording library — takes, records, the prompt database,
edits, datasets and finished fine-tune runs — encrypted on the Mac and stored somewhere the
Mac can reach. It exists so that the library survives the Mac, and so that a finished
fine-tune run can leave the disk and come back. Every byte is sealed before it leaves, with a
key never sent to the vault (kept in Keychain and an offline recovery sheet).

Modules: `src/cole_voice_lab/storage_ports.py` (`@canonical storage_ports`),
`storage_contracts.py`, `storage_local.py`, `storage_sealing.py`, `storage_http_client.py`,
`storage_server.py`, `storage_binding.py` (`@canonical storage_binding`, the composition
root), `library_classification.py`, `library_replication.py`, `library_generations.py` and
`library_replication_service.py` (`@canonical vault_supervisor`). Tests:
`tests/storage_conformance.py`, `tests/test_storage_*.py`, `tests/test_replication_sources.py`,
`tests/test_library_replication*.py`, `tests/test_vault_cli.py`, `tests/test_finetune_archive.py`.

The implementation, visible app status, container templates and D-020 policy are built.
Nothing is deployed yet: there is no vault host, no real binding and no copy of Cole's
library anywhere but the Mac. [RUNBOOK.md](RUNBOOK.md#9-library-vault) covers the Mac
procedures, and [deploy/vault/README.md](../deploy/vault/README.md) covers the host.
See "Deployment still outstanding" below for Step 15's operator gates.

## What the vault is, and what it is not

It **is** content-addressed storage for encrypted bytes, plus a handful of named pointers. It
applies no lab rule, sees no plaintext, and is never trusted: every answer it gives is checked
against a digest the Mac authored.

It is **not**:

- a filesystem the library runs on. Every lab process opens files by path under one local
  asset root and depends on local POSIX behaviour — `flock` for every writer, SQLite in WAL
  mode, `RENAME_EXCL` publishes, and records that name a file's device and inode. A network
  filesystem breaks all of that, so the Mac always keeps the complete working library and the
  vault is only ever reached through ports;
- a live catalog. The prompt database travels as an encrypted copy; the Mac's database stays
  authoritative. Serving prompts from a remote store would let whoever controls the host
  change them, and is deliberately out of scope;
- a way to free disk. Only `voice-lab finetune archive --remove-local-copy` removes anything
  from the Mac, one run at a time, after the copy has been verified by download;
- a second off-site copy. It is one copy on one disk.

## The ports, and the one place that picks an implementation

Two ports, in `storage_ports.py`. Neither has a list or a delete, and neither takes a
namespace: a namespace is fixed when an adapter is built.

| Port | Methods | Rules |
| --- | --- | --- |
| `BlobStore` | `put(data) -> BlobRef`, `has(ref) -> bool`, `get(ref) -> bytes` | the ref is the SHA-256 **and size** of the stored bytes; a blob is at most `MAX_BLOB_BYTES` (16 MiB) and a larger `put` is `STORAGE.TOO_LARGE` before anything is written; putting the same bytes twice is harmless; `get` checks the digest and size before returning |
| `RefStore` | `read(name) -> RefValue \| None`, `swap(name, expected_version=…, target=…)` | compare-and-set; `expected_version=None` means "must not exist yet"; versions start at 1 and are consecutive; a mismatch is `STORAGE.CONFLICT` |

Each library's pointer is `libraries/<library_id>/head`.

Failures are `StorageError(code, message)`, with `retryable` read from a fixed table; the three
integrity codes (`STORAGE.INTEGRITY`, `STORAGE.IDENTITY_MISMATCH`, `STORAGE.UNAUTHORIZED`)
raise `StorageIntegrityError`, which is also a `SecurityError`. The twelve codes are
`UNAVAILABLE` and `TIMEOUT` (the only retryable ones), `UNAUTHORIZED`, `NOT_FOUND`, `CONFLICT`,
`TOO_LARGE`, `QUOTA`, `INTEGRITY`, `IDENTITY_MISMATCH`, `NOT_APPROVED`, `NOT_BOUND` and
`PROTOCOL`. Adapters wrap every transport failure — `OSError`, TLS and HTTP errors, timeouts,
parse errors — so a command prints a refusal, never a traceback, and no secret reaches a
message.

Four more codes come from the copying side rather than from a store, and are what `vault push`,
`vault restore` and `finetune archive` print when the library itself is the problem:
`REPLICATION.SOURCE_CHANGED` (an owner's file moved or vanished while it was read; retryable —
the next pass simply does the work) and `REPLICATION.SOURCE_REFUSED` (an owner's file is
unsafe, hard-linked, or contradicts the record that names it), both raised as
`SourceChangedError`; `REPLICATION.PUSH_RUNNING` (`PushRunningError`: another push holds
`.push.lock`; retryable); and `REPLICATION.RESTORE_REFUSED` (`RestoreRefusedError`, with
`problem` `unsafe-target`, `target-not-empty`, `insufficient-space` or `unrestorable-path`).

Three implementations exist: `storage_local` (a private directory), `storage_http_client` (a
tailnet vault) and `storage_sealing` (encryption, wrapping either of the other two).
`tests/storage_conformance.py` runs the same checks against `local`, `sealed-local`, `http` and
`sealed-http`, so an implementation that passes it can replace another.

**`storage_binding` is the only module that builds an adapter for the lab.** Its entry points
are `open_catalog_reader(layout)` / `open_catalog_editor(layout, create=…)` for the prompt store
and `open_library_vault(layout) -> LibraryVault | None` for the vault; `None` means the library
is bound to nothing ("Vault · off"). `tests/test_storage_import_boundary.py` fails if any other
module — apart from the adapters themselves, the vault server (which builds the local store it
serves) and the two native fixture seeders — imports an adapter, including a function-local or
`importlib` import. Every adapter import inside `storage_binding` is itself inside a function,
so the recording service can open a catalog without loading `cryptography` or the HTTP client.

The catalog ports are documented in `docs/CORPUS_STORE.md`; this document covers the storage
side.

## Sealing, and the library key

`SealedBlobStore` wraps any `BlobStore` and is what the composition root always returns:
nothing a binding can say turns encryption off.

Sealed format v1, 33 bytes (`SEALING_OVERHEAD`) longer than its plaintext:

```
b"CVLS" | 0x01 | nonce (12 random bytes) | AES-256-GCM ciphertext | tag (16 bytes)
```

The associated data is `b"cole-voice-lab.sealed-blob.v1\x00" + library_id`, so a blob sealed
for one library never opens as another's. A fresh random nonce per `put` means the same
plaintext gets an unrelated ciphertext digest each time, so a digest never shows that two
blobs hold the same bytes; de-duplication happens on the Mac instead. Because the ref is the
digest of the *ciphertext*, the largest plaintext a sealed store takes is
`MAX_SEALED_PLAINTEXT_BYTES` = 16 MiB − 33 bytes, and that is the chunk size a push uses. Any
failure to open — a short blob, another magic or version, a wrong key or library id, a changed
nonce, ciphertext or tag — is `StorageIntegrityError` naming only the ciphertext ref.

**Lengths are not hidden.** A sealed blob is exactly 33 bytes longer than its plaintext, so
the host learns each chunk's size and the time it arrived. Equal-size blobs can be linked as
probably the same file; a host that already holds a candidate file of distinctive size (a WAV
file's size fixes its duration) can guess that it is stored; a file uploaded again repeats the
sizes its chunks had. This is accepted, not padded — the host is Cole's own machine and the
content stays confidential — and the format's version byte leaves room for padding later. The
decision record is D-020.

`LibraryKey` holds exactly 32 bytes, redacts its `repr`, and refuses to be pickled or copied.
It lives in the login Keychain under `private.cole.voice-lab.vault.<library_id>.key`, reached
only through `/usr/bin/security` with an absolute path, a sanitized environment and a timeout,
with the secret on stdin and never in an argument list. `voice-lab vault key create` also
renders a **recovery sheet**: a plain-text page naming the format, the library id, the key
fingerprint, the key as 16 groups of 4 lowercase hex and a checksum. It goes to a new 0600 file
outside the library and the repository (`--sheet-out`), or to a real terminal
(`--print-to-terminal`, refused when stdout is not a TTY). Keep it offline. **If both the
Keychain item and the sheet are lost, what is in the vault is lost.**

## What is copied, and what stays on the Mac

`library_classification.py` holds the one table that decides, as data: 74 ordered rules, each
with a pattern, a disposition, the owner that writes those files, a reason and an example the
rule must classify. The first matching rule wins, and a rule may shadow a later one only where
it says so. A path no rule covers refuses the whole pass with `STORAGE.PROTOCOL` naming the
path — a new writer's files must be classified before anything can be copied. A test proves
every path in the fixture libraries is classified exactly once and every rule is reachable.

Five copied classes:

| Class | What it holds |
| --- | --- |
| `records` | take heads, seals and reservations, the take-store head, operations, cursors and analysis heads; groups; edits (native, desktop and legacy); analysis jobs; packages and package origin; capture sessions and continuity; consents and their history; the diagnostic root; pilot gates; headphone reviews; transfer approvals; fine-tune job requests and terminals; transfer plans |
| `prompt-database` | the corpus database (one logical entry, taken through SQLite's online backup) and `provenance/corpus/snapshots/**` |
| `recordings` | native sealed originals, capture sessions once READY, rejected takes, `raw/**` and `references/**` |
| `derived-edits` | rendered edits under `derived/recording-edits/**` |
| `training` | `derived/datasets/**` and finished `derived/finetune/<run>/**`, including its archive records |

`training` is the separate opt-in: a binding copies it only when it lists the class, and a
tailnet destination's approval must cover it too. The run's hard-linked
`checkpoints/pretrained_*.safetensors` is never uploaded; it is recorded as a `pin` entry
naming the run's `runtime.pretrained_sha256`, and a restore re-links it from a runtime.

What stays on the Mac, and why it is safe to leave:

- **every lock and lease** (`.*.lock`, `.*.lease`), the take commit journal
  `.pending.json`, a native capture's `capture.partial.wav` and `native-recovery.json`, and
  every interrupted atomic write `.*.tmp-<hex16>`;
- **the recording index projection** `provenance/recording-library/**` and its quarantines —
  it rebuilds itself from the copied records;
- **the discovery checkpoints** `provenance/recording-library-discovery/**` and the
  **prepared capture gates** `provenance/recording-groups/.prepared-gates/**` — both bind this
  Mac's file identities, so they are invalid after a restore and are made again;
- **Speak clips** `derived/speech/**` — synthetic speech stays on the Mac (D-019, Decision 4);
- **unfinished fine-tune runs**, the fine-tune queue head and `speech-active.json`, and a run's
  `samples/*/synthesis-tmp/**`;
- **the waveform cache**, `incoming/`, `runtimes/`, `models/`, `experiments/`, `evaluations/`
  and every staging directory under `tmp/`;
- **the corpus database's WAL, shm and journal files** as files — the database travels only
  through its backup entry;
- **`provenance/vault/**` itself** — the binding, approvals, state, ledger, manifest copies
  and request log describe *this Mac's* link to the vault. They are never copied and never
  restored, and a manifest that names them is refused at the model level and again at restore.

## Reading the library without taking a writer's lock

A pass must never make a Save fail. The take store takes its metadata lock with `LOCK_NB`, so
a shared lock held by a copier would be a refusal, not a delay. `library_replication.py`
therefore takes **no lock any lab writer takes**, and opens no lock file at all — not even
through `state_revision`. Its only lock is on its own scratch directory under `tmp/.vault-*`.

One `ReplicationSource` per owner lists that owner's files; `library_sources()` orders them
referrers before referents, so a record is always read before the file it names. Each file is
read through a `ReplicationReader` that opens with `O_NOFOLLOW`, records the file's identity
(device, inode, size, mtime, ctime), streams bounded chunks while hashing, and re-checks the
identity on close. Write-once files are read directly; replaced files are re-checked and the
read retried. Every item's bytes must match the digest its seal or record names.

An owner whose files moved during the pass is reported `changed`; one whose files are unsafe or
contradict their records is `refused`. Either way nothing it read counts as fresh and the next
generation keeps that owner's previous entries, so a generation never pairs fresh records with
the older files they name. A `refused` owner does not stop the push — but the result lists it,
`vault push` exits 1, and the app's status says it is waiting. A file the Mac removed on
purpose (an archived run's checkpoints) is offered as a `CarriedPath`: there are no bytes to
read, and the generation keeps the entry of the generation that holds it.

The prompt database is copied with SQLite's online backup API over a `mode=ro` connection into
a private scratch file under `tmp/.vault-<hex>/`, never by opening the live database file and
never through `open_catalog_reader` (which takes the lock and the lease). The scratch copy is
hashed, consumed and deleted. A test proves the backup completes while a writer is committing
and that the copy passes `PRAGMA integrity_check`.

## Generations

A generation is one complete, consistent view of the library at a moment. `push_generation`
in `library_generations.py` is its only writer, and runs one at a time under
`provenance/vault/.push.lock` (`LOCK_NB`; a second push is told one is already running).

1. **The head must be the one this Mac left.** `state.json` names the head's version and
   target; its manifest must have the recorded digest and generation and chain from the
   previous one. Anything else — older, newer, a different target, undecryptable, a digest
   mismatch, a broken chain — is `StorageIntegrityError` with the check named
   (`missing`, `undecryptable`, `not-an-envelope`, `foreign-library`, `digest-mismatch`,
   `not-a-manifest`, `rollback`, `fork`, `chain`), and nothing is uploaded.
2. **A pass runs**, filtered to the destination's classes.
3. **A `ManifestPlan` is built**: generation + 1, the previous manifest's digest, the classes,
   and one entry per file with its path, class, kind, SHA-256, size, mode and pin. If the plan
   equals the head's except for its time and number, nothing is written (`up-to-date`).
4. **Dedup.** An entry whose content the previous generation already holds reuses that entry's
   chunk refs and is not uploaded. `--full` skips this, which is the fix when `verify` finds a
   blob the vault has lost.
5. **The `upload-started` ledger entry is written before the first new blob leaves**, naming
   the plan's payload digest (`manifest_payload_digest` — the plan without chunk refs, which
   only exist after sealing), the destination, the approval digest, the classes and the new
   content planned.
6. **New content is read again**, proven to reproduce the planned digest and size, and put in
   pieces of at most `MAX_SEALED_PLAINTEXT_BYTES`. A file that moved in between stops the push
   (`source-changed`); the started entry with no completion records that, and the next push
   simply does the work.
7. **The manifest is stored**: its canonical JSON is chunked and sealed, and a small sealed
   `ManifestEnvelope` (library id, generation, manifest digest, size, chunk refs) is the blob
   the head points at. A manifest is capped at `MAX_MANIFEST_BYTES` (256 MiB).
8. **The head is swapped** with `expected_version` from the state — a `STORAGE.CONFLICT` here
   is a fork and stops the push — and then the local manifest copy, `state.json` and the
   `upload-completed` entry (the manifest digest and the new head version) are written.

**The ledger** is `provenance/vault/ledger/<sequence:08d>.json`, write-once canonical JSON,
each entry naming the previous entry's digest. Appending re-reads and verifies the whole chain
— sequence contiguity, digests, started/completed pairing — and refuses to append to a broken
one. The chain's own last entry is covered by nothing, so `state.json` also records
`ledger_tail_sha256`, and a push refuses a state whose ledger tail no longer matches; it
likewise refuses a state whose `destination_sha256` is not the binding's destination. A
`local-directory` destination needs no approval, so its entries carry none.

**Two crash windows, both settled by the next push.** If the Mac dies after the head swap but
before `state.json`, the next push sees a head exactly one version ahead that names the
generation the ledger's last `upload-started` entry started and chains from the state's
manifest; only this Mac could have sealed that manifest, so it is adopted and finished rather
than refused as a fork. If it dies after `state.json` but before the `upload-completed` entry,
`state.json` names the started entry — which is the completed entry's
`previous_entry_sha256` — and the next push settles the ledger. Anything else one version
ahead is a fork. Under `--dry-run` an interrupted push is reported as a fork rather than
finished, because finishing it would write.

## Verify

`voice-lab vault verify` downloads and decrypts **every chunk** of every file entry (or of the
paths and classes named), reassembles each file and checks its plaintext digest and size. A
file counts as present only after that: `has()` is never trusted for presence. Each entry is
reported `present`, `missing` (a chunk is not there), `mismatch` (a chunk failed its digest or
did not open, or the file did not reassemble — `detail` says which) or `pinned` (a pin entry,
which the vault never holds). The command exits 1 if anything is not present or pinned, or if
a requested path names no entry.

## Restore

`restore_library` writes into an empty asset root, and only after the checks pass:

- **Naming the generation.** Either `--generation N --manifest-sha256 D` (the head's manifest
  must be exactly that, or an older one must be reachable through the manifest copies this Mac
  keeps) or `--accept-latest`, whose result carries the generation, its time and its per-class
  entry counts. Where this Mac's records exist, the head is held against `state.json` first,
  with the same rollback, digest and fork checks a push makes; a Mac that keeps no records has
  no such anchor, which is why `vault push` prints the generation and manifest digest and the
  runbook asks Cole to keep that line offline.
- **Free space.** The entries' bytes plus a tenth plus 256 MiB must be free, or the restore is
  refused before anything is written.
- **Staging.** Everything goes into a private sibling `<target>.restoring-<random>` (0700) and
  is renamed into place at the end, so a partial library is never at the target path. On any
  failure the staging directory is removed.
- **Modes.** All ten asset directories are created. Files are written 0600, fsynced, checked
  against their digest, then given the entry's mode (0400 or 0600); directories under a
  read-only tree (`raw/`, `references/`, `derived/datasets/`, `derived/transfer-plans/`,
  `provenance/pilot-gates/`) become 0500, all others 0700.
- **Pins.** A pin entry is hard-linked (or copied) from `--runtime-root` only when the file
  there has the pinned digest; otherwise it is reported missing and the rest is still restored.
- **`provenance/vault/**` is never written.**

Before the rename the take store is opened once in write mode, which adds only its Mac-only
scaffolding — its metadata lock and its empty operation, cursor, analysis and prepared-gate
directories — which the read-only readers (`prompts coverage-report`, `finetune corpus`) need.
Nothing is pending in a restored tree, so that open repairs nothing.

**What a restored library does not have.** The recording index rebuilds itself on first open.
Discovery starts again from nothing, and groups prepare their capture gates again, because both
bind file identities this Mac no longer has. Records that name a file's device and inode
(edit and analysis job records, package records) are restored as history, but a job that was
still in flight cannot be resumed against the restored files and must be run again. Speak clips
are not in the vault at all. And `provenance/vault/**` is not restored: a new Mac runs
`vault init --library-id <the library's id>`, imports the key from its recovery sheet and the
read token, and restores through the explicit endpoint route. This does not recreate the
old Mac's upload state or ledger. Simply binding the restored root to the populated
namespace will refuse its existing head as a fork; `--reset-state` cannot adopt it.
Resuming copies therefore needs a separately approved fresh namespace, distinct tokens,
new approval and binding, retaining the old namespace and a private recovery credential.
Trusted original Mac-only vault records, if separately available, require their own
reviewed recovery procedure; there is no automatic state/ledger recovery helper.
See [RUNBOOK.md](RUNBOOK.md#9-library-vault).

## The vault server

`storage_server.py` serves one `LocalBlobStore` and one `LocalRefStore` per namespace over
plain HTTP; the Tailscale sidecar terminates TLS. There is no console script — a second one
breaks real capture-kit installs — so it runs as
`python -I -B -m cole_voice_lab.storage_server serve --config <file> --data-root <dir>
--access-log <file> [--listen-internal] [--port 8080]`. It binds `127.0.0.1` unless
`--listen-internal` is given. `--provision` is a one-time operator step: without it a missing
namespace directory fails closed. The same module carries the two subcommands that write that
configuration on the host: `new-token` prints one fresh `cvlv_` token on standard output, and
`hash-token` reads one token from standard input and prints its SHA-256, which is what the
configuration holds. Tokens made on the Mac with `vault token create` need neither.

- **Namespaces.** `<data-root>/namespaces/<ns>/` is the root of both stores; every directory is
  0700 and owned by the server's user. The data root is absolute and symlink-free and is not an
  `AssetLayout` (the container runs with `HOME=/nonexistent`).
- **Tokens.** The configuration (`VaultServerConfig`) holds **only SHA-256 hashes** — a read
  hash and a write hash per namespace, all distinct — plus, per namespace, its `quota_bytes`
  and an optional list of allowed `Tailscale-User-Login` values, and, for the server as a
  whole, `max_workers` and `request_timeout_seconds`. A
  token is `cvlv_` plus 43 base64url characters; it works in one namespace at one scope; the
  write scope also reads. A wrong namespace or scope is 401, identical to a missing token, so
  nothing reveals which namespaces exist.
- **Order of checks.** Request line, path grammar, exactly one `Bearer` token compared as
  hashes in constant time, the optional login, the method, the scope, then the framing: no
  `Transfer-Encoding`, exactly one ASCII-decimal `Content-Length` compared as a string against
  the limit before `int()` (16 MiB for a blob, 64 KiB for JSON). An unauthenticated request
  whose declared body is within the limit has that body drained before the 401, so the proxy
  delivers the refusal rather than a reset.
- **Limits.** A bounded thread pool answers 503 `STORAGE.UNAVAILABLE` immediately when
  saturated rather than queueing. `request_timeout_seconds` is one wall-clock deadline for a
  request's whole read phase, not a per-read socket timeout, which is what closes slowloris: at
  the default of 120 seconds a 16 MiB blob must average about 140 KB/s, and the ceiling of 600
  seconds allows about 28 KB/s.
- **Writes.** An uploaded blob must hash to the digest in its path, is write-once, and is
  refused with `STORAGE.QUOTA` past the namespace's `quota_bytes`. Ref updates are
  compare-and-set and fsynced.
- **The access log** appends one `VaultAccessRecord` per answered request: time, namespace,
  scope, operation, ref, bytes, status, code and the proxy's forwarded headers. Never the
  `Authorization` header, never a token hash, never a byte of a body. It is opened once and
  appended without fsync, so it must be rotated by copy-truncate, never by rename.
- **Health.** `GET /healthz` takes no credential and answers `ok`.
- `/healthz` aside, every refusal is `{"code": "STORAGE.…", "message": "…"}`, and no message
  names a path on the server, a token or any content. A test asserts the server's import
  closure holds no recording, capture, catalog, consent, fine-tune, sealing, client or binding
  module.

## The client, the binding and the approval

**The client** (`storage_http_client.py`) resolves the endpoint through an injected
`TailnetResolver` before the first request and again whenever the last check is older than a
minute. The peer must carry the approved node's stable id, the approved tag among its tags, a
DNS name equal to the endpoint, and the endpoint must end in the approved tailnet suffix;
anything else is `STORAGE.IDENTITY_MISMATCH` and nothing is sent. It then connects to exactly
the IP the check returned — so a stolen MagicDNS name reaches nothing — verifies the
certificate for the endpoint *name* with the system trust store, and sends `Host: <endpoint>`.
Nothing is pinned to a certificate. The token goes in one `Authorization` header and never in a
URL, a log, an exception or a repr. Every answer is bounded and re-checked: a blob against its
digest and size, JSON against its contract, a `RefValue` against the ref that was asked for.
Retries are made only for retryable codes, with exponential backoff and jitter; a `swap` is
retried too, and a `CONFLICT` after a retry is reconciled by reading the ref, because the lost
answer may have been the winning one. One `VaultRequestRecord` per attempt is appended to
`provenance/vault/requests/<UTC date>.jsonl`.

**The approval** (`VaultDestinationApproval`) is Cole's standing authorization for one
destination: the endpoint FQDN, the tailnet suffix, the node's stable Tailscale ID, the tag
`tag:voice-lab-vault`, the namespace, the library id, the classes it covers, `approved_at`, a
mandatory `expires_at` at most 366 days later, `approved_by` and a statement. It binds the
destination to a *node*, not to a name, because names and addresses can be taken over. It is
written **write-once at 0400** under `provenance/vault/approvals/<approval_id>.json`: an
approval is never replaced, only superseded by a new one with another id. Authoring it is a
two-step ceremony — `vault approve --preview <fields>` prints the record and its digest and
writes nothing; `vault approve --reviewed-approval-sha256 D --i-approve-this-destination
<fields>` recomputes the record, requires the digest to match, and only then writes. It names
no consent record: D-020 permits ciphertext storage on Cole's own host, which never
runs a model or synthesizes. It does not authorize hosted GPU transfer or decide D-019's
separate open question about consent for local listening.

`--approved-at` remains optional in the preview: if omitted, that preview generates the
timestamp. Confirmation must explicitly reuse that exact printed timestamp as well as
all other fields and the digest. Omitting it again creates a different approval and is
refused. Supplying the same timestamp to both invocations is also supported. See the
complete commands in [RUNBOOK.md](RUNBOOK.md#9-library-vault).

**The binding** (`provenance/vault/binding.json`) names the backend (`tailnet-vault` or
`local-directory`), the endpoint, the namespace, the library id, the classes copied there, the
Keychain item names derived from the library id, and — for a tailnet vault — the approval's id
**and the digest of its file**, so an approval edited after the bind is refused when the vault
opens. It holds no secret and no path to one. Switching destinations is `voice-lab vault bind`
plus a relaunch; there is no rebuild and no setting in the app.

**Every refusal `open_library_vault` makes**, in the order it makes them — so a binding with
two faults is reported by the first row that matches, not the worst one:

| Situation | Code |
| --- | --- |
| `provenance/vault` is not a private directory of this user | `STORAGE.INTEGRITY` |
| `state.json` records copies but the binding is gone | `STORAGE.NOT_BOUND` |
| the binding is unreadable, invalid, or of an unknown backend | `STORAGE.NOT_BOUND` |
| no library record, or it names another library than the binding | `STORAGE.NOT_BOUND` |
| the approval file is missing, unreadable, or records another id | `STORAGE.NOT_APPROVED` |
| the approval's digest is not the one the binding reviewed | `STORAGE.NOT_APPROVED` |
| the approval is not yet effective, or has expired | `STORAGE.NOT_APPROVED` |
| the approval names another library, endpoint, namespace or node | `STORAGE.NOT_APPROVED` |
| the binding copies a class the approval does not cover | `STORAGE.NOT_APPROVED` |
| the login Keychain has no library key | `STORAGE.NOT_BOUND` |
| a local endpoint this Mac refuses (see below) | `STORAGE.NOT_BOUND` |
| the login Keychain has no vault token for the scope | `STORAGE.NOT_BOUND` |
| the node answering is not the approved one (at connect time) | `STORAGE.IDENTITY_MISMATCH` |
| no binding at all (looked for first of all) | not an error: `None`, "Vault · off" |

The approval rows come before the Keychain and endpoint rows because the stores are opened
last: a tailnet binding whose approval has expired *and* whose key is missing reports
`STORAGE.NOT_APPROVED`.

`bind` itself refuses more: a tailnet binding without an existing, effective approval covering
its library, endpoint, namespace and classes; an existing binding without `--replace`; a
rebind to a destination other than the one `state.json` records, without `--reset-state`
(which renames `state.json` and `manifests/` aside as `*-superseded-<n>` and never deletes
them); and a `local-directory` endpoint that is not absolute and normalized, is the user's home,
is fewer than four components deep, or is the library or the repository, or holds one, or is
inside one. It does all of this under `.push.lock`, so a rebind during a push is a named
refusal rather than a reset the push quietly undoes.

## Background copying in the app

`library_replication_service.py` owns a `VaultSupervisor` thread, started by the recording
service for the workspace and takes backends. **It does nothing at all, and publishes nothing,
while the library is bound to no vault.**

The backend calls `notify_commit()` after every command that commits durably (open context,
continue a legacy recording, reserve, seal, save, edit, decision, review, retake, navigate,
recover, fine-tune and its cancel — `recording_service.COMMITTING_COMMANDS` is the list), and
the service's background owners do the same. A pass starts once the library has
stood still for 60 seconds, and in any case at least every 15 minutes so that evidence the
command line writes is copied too. It never starts while a fine-tune job or a Speak request is
running, and never while a pass is already running.

Each pass runs in a `python -I -B -m cole_voice_lab.library_replication_service --worker`
subprocess in a session of its own, with a sanitized environment that still carries `HOME`
(the Keychain needs it) and its stdin held open, so EOF kills the worker's process group. It
answers with exactly one JSON frame. The service process itself never reads audio, never seals
anything and never takes a lab lock.

The status the app is shown, held in memory only:

| State | Meaning |
| --- | --- |
| `off` | no binding |
| `waiting` | files are waiting, or a retryable refusal (a changed or refused owner, the push lock held); the message names it |
| `copying` | a pass is running |
| `up-to-date` | the head holds this Mac's view |
| `unreachable` | consecutive `STORAGE.UNAVAILABLE` / `STORAGE.TIMEOUT` failures, with `unreachable_since`; retried with backoff from 30 s to 30 min |
| `failed` | a non-retryable storage refusal (identity, approval, integrity). Passes stop until the app is relaunched |

The supervisor pins the binding at startup. Each worker reopens and checks it against that
pin, so rebinding takes effect at the next launch, as `voice-lab vault bind` prints.
Storage codes travel in the `vault-status` event's own
field and never join `ErrorCode`; supervisor failures go on the retained `vault-supervisor`
error lane, like the fine-tune supervisor's.

The sidebar's last text (`library.vault-status`) displays off, checking, copying,
up to date, waiting with a count when known, or `Vault unreachable since HH:mm` in
local time. Failures use the service's message verbatim. It adds no layout region
and does not block recording or Save. The native `vault` suite checks this actual
hosted text across a stopped/restarted synthetic loopback server and independently
verifies a newly saved take's downloaded bytes.

## Fine-tune runs in the vault

`voice-lab finetune archive --run R` refuses unless the binding (and, for a tailnet vault, the
approval) covers `training`, the run's `run.json` says `finished`, and no fine-tune job or
Speak request is active. It then pushes a generation, verifies **that generation's** run
entries by downloading and decrypting every chunk, writes `archived.json` beside `run.json`
(write-once, 0600: the run id, the library id, the destination digest, the generation, the
manifest digest and every file's digest), and pushes once more so the record itself is in the
vault.

`--remove-local-copy` removes **only `checkpoints/**`**, and only after a second verification of
the run in the generation just pushed, in the same invocation — so the run is downloaded and
decrypted twice. It re-checks that no job or Speak request is active, re-hashes
every recorded file, and writes `local-removal.json` **before** the first unlink, so a crash
leaves a record and some files, which both a re-run and `restore-run` handle. `run.json`,
`dataset/**`, `samples/**` and the summary records are never removed. Later generations keep
naming the removed files: the fine-tune source carries their entries forward from the
generation the removal record names, through the manifest copies under
`provenance/vault/manifests/`. (`vault bind --reset-state` moves those copies aside, after
which the removed files appear in `vault push`'s waiting list.)

`voice-lab finetune restore-run --run R` checks free space, re-downloads and re-checks each
removed file, writes it 0600 and renames it into place, re-links the pinned base weights from
`--runtime-root` when their digest matches the run's pin, and finally renames
`local-removal.json` to `local-removal-restored-<n>.json`, keeping the history.

`fine-tune-status` reports each run's `location` (`local` or `archived` — archived only while a
removal record exists *and* a file it names is missing, so a restored library reports its runs
`local`) and `in_vault` (whether `archived.json` is there). `finetune train|run|dataset|
synthesize` and Speak refuse an archived run with `FINETUNE.RUN_ARCHIVED`, naming
`voice-lab finetune restore-run --run <id>`. Speak appends ` · Archived` to Latest
and named archived model labels and disables generation with that restore instruction.
Base remains available because its pinned untuned weights use local reference clips;
removing checkpoints never removes the dataset.

## The command surface

```sh
voice-lab vault init [--library-id ID]          # record which library this Mac holds (once)
voice-lab vault key create --sheet-out PATH     # make the library key; write the recovery sheet
voice-lab vault key import --sheet PATH         # restore the key from its sheet ("-" = stdin)
voice-lab vault key show                        # the key's fingerprint, never the key
voice-lab vault token create [--namespace N]    # make both vault tokens; print only their hashes
voice-lab vault token import --scope write|read # keep one token read from standard input
voice-lab vault approve --preview …             # print the approval and its digest; write nothing
voice-lab vault approve --reviewed-approval-sha256 D --i-approve-this-destination …
voice-lab vault bind --backend … --endpoint … --namespace … --classes … [--replace --reset-state]
voice-lab vault show [--json]                   # binding, approval, key/token presence, state
voice-lab vault status [--json]                 # one line for the terminal; runs no pass
voice-lab vault push [--dry-run] [--full]       # copy the current files as the next generation
voice-lab vault verify [--generation N] [--path P …] [--class C …]
voice-lab vault restore --target DIR (--accept-latest | --generation N --manifest-sha256 D)
                                     [--runtime-root P]
voice-lab vault audit --access-log FILE [--namespace N]
voice-lab finetune archive --run R [--remove-local-copy]
voice-lab finetune restore-run --run R [--runtime-root P]
```

- `init` writes `provenance/vault/library.json` once, with a fresh `lib-<yyyymmdd>-<12 hex>` id
  or an existing library's id to adopt (a restore onto another Mac). Every other command reads
  it.
- `key create` needs either `--sheet-out` or a real terminal; `token create` prints only the two
  SHA-256 values the server's configuration needs and the namespace, never a `cvlv_` token.
- `status` prints the state using the sidebar's wording; it runs no pass, so its
  `waiting_files` is always `null`, and it cannot know the supervisor's first-outage time.
  `push --dry-run` is what counts the files that are waiting.
- `push --dry-run` runs the head check and a pass and reports what it would send, including how
  many files are waiting, without reading a file again, uploading or writing a record.
  `push --full` re-uploads everything. A push exits 1 if it stopped on a changed source or any
  owner was refused, and prints the generation and manifest digest — **keep that line offline**,
  because `restore --generation N --manifest-sha256 D` checks an independent anchor,
  while `--accept-latest` trusts the host's head. On a new Mac the explicit endpoint
  route can restore that pair only while it matches the current head. Historical
  selection needs the bound-library route and the required local manifest chain;
  a first-push pair alone cannot recover an older generation after the head advances.
- `verify` exits 1 on any missing or mismatched entry, or an unknown requested path.
- `restore` uses the binding on this Mac; on a Mac with no binding, add `--endpoint
  --namespace --node-stable-id --tailnet-suffix` (the library id comes from `library.json`, and
  the key and read token from the Keychain), and `--backend local-directory` for a local vault,
  since the default is `tailnet-vault`. `--runtime-root` is where a pinned base-weights file is
  re-linked from.
- `audit` holds the vault's access log against this Mac's request log for one namespace. A
  refused attempt is paired on operation, ref and status only — bytes are not compared on
  either side, because the server records `len(data)` for a refusal found after the body is read
  and 0 for a pre-read 401. Anything the vault answered that this Mac did not send is
  unexplained and fails the audit; requests the vault has no record of are counted as
  unanswered, not refused.

## Under `provenance/vault/`

Mac-only, never copied and never restored:

| Path | Mode | What it is |
| --- | --- | --- |
| `library.json` | 0600 | `LibraryIdentity`: which library this Mac holds; written once |
| `binding.json` | 0600 | `StorageBinding`: where this library copies to and what it copies; replaced atomically |
| `approvals/<approval_id>.json` | 0400 | `VaultDestinationApproval`; write-once, never replaced |
| `state.json` | 0600 | `ReplicationState`: the newest generation pushed, its head, destination digest and ledger tail |
| `state-superseded-<n>.json` | 0600 | a state `bind --reset-state` moved aside |
| `ledger/<sequence:08d>.json` | 0600 | `LedgerEntry`; write-once, chained |
| `manifests/<generation:08d>.json` | 0600 | the plaintext manifest of each pushed generation; write-once |
| `manifests-superseded-<n>/` | 0700 | manifest copies `bind --reset-state` moved aside |
| `requests/<UTC date>.jsonl` | 0600 | one `VaultRequestRecord` per request attempt; no token |
| `.push.lock` | 0600 | held by one push at a time; no lab writer takes it |

A scratch directory lives under `tmp/.vault-<hex>/` — a pass makes one for the prompt
database's backup, a push for its spooled copy — and is removed once consumed. Making one
first sweeps the scratch directories a crash left behind, which are the ones whose
`.scratch.lock` no process still holds.

## How to try it locally

No tailnet, no container and no Keychain ceremony beyond one key: a `local-directory` binding
on a scratch asset root behaves exactly like a real vault, sealed and ledgered, with the
approval step skipped because a local directory needs none. Use a throwaway library and a
directory on another disk — never the real library or anything inside the repository, both of
which `bind` refuses anyway.

```sh
LAB=<a scratch asset root you are happy to delete>
VAULT=<an absolute directory outside the library and the repository>

voice-lab assets init --asset-root "$LAB"   # every vault command needs the ten directories
voice-lab vault init  --asset-root "$LAB"
voice-lab vault key create --asset-root "$LAB" --sheet-out <a new 0600 file outside both>
voice-lab vault bind  --asset-root "$LAB" \
    --backend local-directory --endpoint "$VAULT" --namespace scratch \
    --classes records prompt-database recordings derived-edits

voice-lab vault push   --asset-root "$LAB" --dry-run   # what it would send, and what is waiting
voice-lab vault push   --asset-root "$LAB"             # note the generation and manifest digest
voice-lab vault verify --asset-root "$LAB"             # every chunk downloaded and decrypted
voice-lab vault status --asset-root "$LAB"
voice-lab vault restore --asset-root "$LAB" --target <an empty directory> --accept-latest
```

The restored root should give the same `prompts coverage-report` and `finetune corpus` results
as the original; that is what `tests/test_library_replication.py` checks. Add `training` to
`--classes` to exercise `finetune archive` against the same local vault. Deleting `$VAULT`, the
scratch library and the recovery sheet leaves nothing behind but the key's login-Keychain item
(service `private.cole.voice-lab.vault.<library_id>.key`, account `library-key`), which
Keychain Access removes.

## Deployment still outstanding

Steps 12–14 provide the sidebar line and archived Speak blocker, the synthetic native
`vault` suite, the hardened container and deployment templates, the offline container
verifier, D-020 and the Mac operator procedures. Container verification uses a local
loopback test namespace only; it starts no Tailscale sidecar and proves no real peer,
Keychain or deployment behavior.

Step 15 still requires Cole: decide the pending combined app build/install, select the
host's large-disk path and training opt-in, review the tailnet tag/access policy, choose
and pin the sidecar image, join with a single-use auth key, reserve the endpoint in Cole's
inventory, and approve how this unpushed source or image reaches the host. Each remote
or tailnet action requires approval. Cole creates the key and tokens privately and stores
the recovery sheet before the first push, full verify and empty-root restore drill.
Archiving or removing any real run is a separate per-run choice. No deployment is implied
by the templates or the synthetic gates; see [the host runbook](../deploy/vault/README.md).
