# Plan: library vault — an encrypted home for the database and recordings on `cole-ubuntu-pc`

**Goal:** The lab's prompt database, recordings, edits, datasets and finished fine-tune runs are stored in a Docker container (the "vault") on Cole's `cole-ubuntu-pc`, reached over the tailnet:
- everything is encrypted with a key that never leaves Cole's Mac;
- the library can be restored from the vault onto an empty Mac;
- finished fine-tune runs can leave the Mac and come back.

Every piece of code that stores or reads the lab's data depends on storage and catalog ports, not on a particular implementation. Any implementation that passes a port's conformance suite can replace the current one without changes to dependent code.

**Out of scope:**
- Serving the live prompt database from the vault (Decision 0). The catalog port makes this a later drop-in.
- Removing recordings from the Mac. This needs digest-based file identity in Python and Swift; recordings are about 100 MB today and would be about 1.2 GB for all 1,886 prompts. It gets its own follow-up plan.
- Recording, playback, Speak or training anywhere but the Mac.
- Several Macs or writers sharing one library.
- A second off-site copy (Decision 5).
- The 1,000-take acceptance track and the physical-test roots.
- The pending combined build, install and physical check of the corpus-store, pipeline and Speak plans. ~~That work comes before Step 1.~~ Cole chose on 2026-09-17 to start the vault first (the UMIK-1 was not attached); that build will be made later from the finished vault commit.

## Context

- **Why the Mac keeps a complete working library.** Every process opens library files by path under one local asset root. On the Python side that is `paths.AssetLayout` and `confined_path`, used at about 150 call sites in `src/`; Swift uses `assetRoot`. The contracts depend on local POSIX behaviour:
  - the recorder helper's `O_EXCL` partial file, fsync and `RENAME_EXCL` publish (`apps/macos/Sources/RecordingCore/NativeCaptureWriter.swift`);
  - `RecordingFileIdentity` (device, inode, mtime, ctime), produced by Python and re-checked by Swift (`recording_contracts.py:132-141`, `RecordingPrivateFile.swift`);
  - `flock` for every writer;
  - SQLite in WAL mode;
  - the owner, `0o077` and `nlink == 1` checks (`canonical.py:43-63`).

  A network filesystem breaks all of these. So the vault is a durable, encrypted copy that is reached only through ports, never a mounted disk.
- **Where the disk goes.**
  - The library is 3.9 GB. One finished fine-tune run under `derived/finetune/` takes 3.8 GB of that; recordings and evidence take about 100 MB.
  - Each real run adds about 3–4 GB, including 2.7 GB of checkpoints.
  - The Mac had 21–24 GB free on 2026-09-16.
  - Archiving finished runs is what frees disk. Copying everything else is what protects the library.
- **Catalog code today.** `CorpusStore` and `open_corpus_store` live at `corpus_store.py:908-929`. Callers depend on more than the store's methods:
  - `cli.py:1097-1104` imports `catalog_definition`, `validated_subject_id` and `CORPUS_DATABASE_NAME`, and `cli.py:1181` prints the database path.
  - `edit_prompt` uses the three-state `UNCHANGED` value (`corpus_store.py:218-230`) and returns plain dataclasses (`:234-270`).
  - Five modules import `corpus_store` for shared logic rather than for the store: `recording_discovery.py:35`, `recording_takes.py:33`, `recording_index.py:44`, `prompt_coverage.py:24` and `finetune.py:61`.

  Snapshots:
  - The contract fixes where a snapshot lives (`recording_contracts.py:199-200`).
  - `_write_snapshot` (`corpus_store.py:1281`) is the only snapshot writer, and it runs under `.corpus.lock`.
  - The store refuses to open when `provenance/corpus/` contains anything that is not one of its own files (`:1165-1174`).

  Any catalog implementation must keep these rules from `docs/CORPUS_STORE.md`:
  - one transaction per change, with every rule checked inside it;
  - writers serialized, so versions are consecutive;
  - snapshots written once and readable without the database;
  - the service pins one catalog per process;
  - the same error codes and security classes.
- **Writers don't wait for locks.** The take store takes its metadata lock with `LOCK_NB` (`recording_takes.py:546`). If the copier held a shared lock there, a Save would be refused, not just slowed down. So copying must read without taking any lock a writer takes.
- **What is copied to the vault.**
  - Take records: `provenance/recording-takes/**`, except `.metadata.lock` and `.pending.json`.
  - Groups, edits and analysis: `provenance/recording-groups/**`, `provenance/recording-edits/**` (except locks) and `provenance/recording-analysis/jobs/**`.
  - The prompt database: the corpus database, copied with SQLite's online backup, plus `provenance/corpus/snapshots/**`.
  - Packages and capture history: `provenance/recording-packages/**`, `provenance/capture-desktop/*.json`, `provenance/capture-continuity/**` and `provenance/recording-legacy-continuations/**`.
  - Consents, gates and approvals: `provenance/consents/**`, `provenance/diagnostic-root.json`, `provenance/pilot-gates/**`, `provenance/headphone-reviews/**` and `provenance/transfer-approvals/**`.
  - Fine-tune job records: `provenance/finetune-jobs/{requests,terminal}/**`.
  - Audio:
    - sealed `tmp/recording-takes/*/original.wav`;
    - `tmp/capture-*/**` once `READY`;
    - `tmp/capture-*.rejected/**`;
    - `raw/**`;
    - `references/**`.
  - Derived data: `derived/recording-edits/**`, `derived/datasets/**`, and finished `derived/finetune/<run>/**`. The pinned base weights inside a run are recorded by their pin, not uploaded.
- **What stays on the Mac only.**
  - Every lock and lease file, `.pending.json`, `capture.partial.wav` and `native-recovery.json`.
  - The recording index projection, the discovery checkpoints and `derived/recording-waveforms/**`, all of which can be rebuilt.
  - Staging directories, `incoming/`, `runtimes/`, `models/`, `experiments/` and `evaluations/`.
  - `provenance/finetune-jobs/{head.json,.owner.lock,speech-active.json}` and unfinished runs.
  - `derived/speech/**` (D-019; Decision 4).
  - `provenance/vault/**`: the binding, approvals, generation record, ledger and request log. These are never restored.
- **Security facts that shape the design.**
  - On `cole-ubuntu-pc`, the `cole` account has passwordless sudo, is in the `docker` group, and hosts other services that are deployed to it (`~/Projects/blob-royale/docs/operations/tailnet.md:16-19`). Disk encryption on that host does not protect data while it is running, so the Mac encrypts everything before it leaves.
  - Fine-tuned checkpoints plus the dataset's reference clips are enough to clone Cole's voice.
  - Node names, MagicDNS names and tailnet addresses can be taken over, and HTTPS certificates rotate. The vault's identity is therefore the node's stable Tailscale ID plus its tag.
  - The installed F5 runtime loads checkpoints with `weights_only=True`.
- **Rules this plan changes, with Cole's approval** (Decision 1):
  - AGENTS.md:22-23: "No human audio is sent to a hosted service or GPU host without a separate, explicit transfer approval naming the destination and payload digest."
  - AGENTS.md:43, which requires explicit flags for model eviction and raw-asset removal.
  - SECURITY.md:33-40 (the network policy and hosted-transfer approval), plus :75 and :77.
  - `ConsentRecord.hosted_transfer_allowed: Literal[False]` (`models.py:386`).
  - D-019's consent scope, which rests on "the Mac that holds Cole's library" (`DECISIONS.md:428-430`).
  - The closest precedent is capture sync to the mini, which is bound to a reviewed tailnet destination (SECURITY.md:35-38; `sync.py`).
- **Platform facts.**
  - `cole_voice_lab/__init__.py` imports `models`, which needs pydantic, so the container needs a hashed Linux lock file.
  - Python's standard library has no authenticated encryption, and the venv has no encryption package. This plan adds `cryptography` (Decision 7):
    - as an optional `vault` extra, at an exact version, hash-locked in `requirements/vault-macos.txt`;
    - imported only by the encryption module;
    - absent from the container and the capture kit.

    That is a recorded exception to AGENTS.md's preference for standard-library transfer primitives.
  - Docker on the Mac runs under Colima (an arm64 Ubuntu VM) without the compose plugin.
  - The Mac has `tailscale` 1.102.3 at `/usr/local/bin/tailscale`.
  - `cole-ubuntu-pc` is x86_64 Ubuntu 24.04 with Docker 29.6.1, reached with `ssh ubuntu-tailscale`.
  - Cole's usual tailnet deployment (`~/Projects/inbox-pipeline/TAILNET_DEPLOYMENT.md`): an app container plus its own Tailscale sidecar, the endpoint reserved in Mission Control's `tailnet_endpoints` registry, Funnel off. The hardening to copy is in `~/Projects/jobtrack/docker-compose.yml`.
- **Conventions.**
  - Python runs only as `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B`.
  - Contracts are strict Pydantic models registered in `schema.SCHEMA_MODELS`. Wire contracts get Swift twins, which `--suite contracts` round-trips (`scripts/verify_native_recording.py:376-410`).
  - Canonical modules carry `@canonical` tags, and error codes use the `DOMAIN.FAILURE` form.
  - Tests need no tailnet peer, and coverage stays at or above 90 %.
  - Each verified step is one commit on `capture-console`, which is not pushed.

## Design

**Storage ports** (`storage_ports.py`, `@canonical storage_ports`). The vault stores bytes and pointers and applies no lab rules.
- **`BlobStore`**
  - `put(data: bytes) -> BlobRef`. The ref is the SHA-256 of the stored bytes. A blob is at most 16 MiB, and putting the same bytes twice is harmless.
  - `has(ref) -> bool`.
  - `get(ref) -> bytes`. The implementation checks the digest before returning.
  - There is no list and no delete.
- **`RefStore`**: named pointers with compare-and-set.
  - `read(name) -> RefValue | None` returns the value with its version.
  - `swap(name, *, expected_version, value) -> RefValue`.
  - Each library's head is named `libraries/<library_id>/head`.
- **Namespaces** are fixed when an adapter is built (by the server token, or by a local directory). They are never passed per call.
- **Errors**
  - `StorageError(VoiceLabError)` carries `code` and `retryable`.
  - `StorageIntegrityError(SecurityError, StorageError)` covers digest, decryption, identity and authorization failures.
  - The codes are `STORAGE.UNAVAILABLE` (retryable), `STORAGE.TIMEOUT` (retryable), `STORAGE.UNAUTHORIZED`, `STORAGE.NOT_FOUND`, `STORAGE.CONFLICT`, `STORAGE.TOO_LARGE`, `STORAGE.QUOTA`, `STORAGE.INTEGRITY`, `STORAGE.IDENTITY_MISMATCH`, `STORAGE.NOT_APPROVED`, `STORAGE.NOT_BOUND` and `STORAGE.PROTOCOL`.

**Storage implementations.**
- **Local store** (`storage_local.py`): `LocalBlobStore` and `LocalRefStore`.
  - Blobs are write-once, published exclusively, with 0700 directories and 0600 files.
  - Ref swaps happen under `flock` with fsync.
  - It serves tests, a second local disk, and the vault server.
- **Tailnet client** (`storage_http_client.py`): `HttpBlobStore` and `HttpRefStore`.
- **Encryption** (`storage_sealing.py`): `SealedBlobStore` wraps any `BlobStore`.
  - Each blob is encrypted with AES-256-GCM, using a random 96-bit nonce and the library id as associated data.
  - The ref is the digest of the encrypted bytes. The same recording uploaded twice therefore gets two different refs~~, so the vault's contents cannot confirm that a known file is there~~. *(Amended 2026-09-17, Step 4: lengths are not hidden. A sealed blob is exactly 33 bytes longer than its plaintext, so the vault host learns each chunk's size and upload time, can link equal-size blobs, and can guess that a file of a distinctive size is stored. This is accepted and recorded in D-020; the format's version byte leaves room for padding later.)* The copying layer does its own de-duplication on the Mac.
  - The key is a 256-bit library key in the login Keychain, plus a recovery sheet Cole keeps offline (Decision 2).
  - The composition root always wraps the vault in `SealedBlobStore`, and no binding can turn encryption off.

**Catalog ports** (`catalog_ports.py`, `@canonical catalog_ports`).
- **`CatalogReader`** has `resolve_reference`, `materialize`, `list_catalogs`, `active_catalog`, `list_prompts` and `prompt_history`. The recording service, the fine-tune worker, `--catalog` resolution and coverage use it.
- **`CatalogEditor(CatalogReader)`** adds `add_prompt`, `import_prompt_suite`, `edit_prompt`, `retire_prompt`, `create_catalog`, `initialize_catalog`, `publish_version` and `activate`.
- The port module owns the records, `UNCHANGED`, the catalog error classes, and a complete map from error code to class.
- The SQLite store moves to `corpus_store_sqlite.py`, and `create` and `clock` become arguments of its constructor only.
- The shared catalog logic stays in `corpus_store.py`: fit, typo class, snapshot decoding and `read_catalog_snapshot`.
- `docs/CORPUS_STORE.md` records the rule for any future implementation: it returns snapshot bytes, and one Mac-side mirror checks them with `require_referenced_snapshot` and publishes them under `.corpus.lock`. That mirror is built in the plan that adds such an implementation, not in this one.

**Composition root** (`storage_binding.py`, `@canonical storage_binding`). Only this module and the two fixture seeders (`scripts/verify_native_bridge.py`, `scripts/verify_native_workspace.py`) construct adapters.
- **Entry points:** `open_catalog_reader(layout)`, `open_catalog_editor(layout, *, create)` and `open_library_vault(layout) -> LibraryVault`.
- **Binding file:** `provenance/vault/binding.json` (`StorageBinding`) names:
  - the backend: `local-directory` or `tailnet-vault`;
  - the endpoint name;
  - the namespace;
  - `library_id`;
  - the approval id;
  - the Keychain item names, which are fixed and derived from `library_id`;
  - *(amended 2026-09-17, Step 9)* the digest of the approval, so an approval edited after the binding was written is refused when the vault opens.

  It also names the file classes copied to that destination, so `training` is opt-in per binding (and, for the tailnet vault, must also be covered by the approval) *(amended 2026-09-17, Step 2)*.

  It holds no secret and no path to one.
- **When the binding is missing:**
  - With no binding, the status is "Vault · off".
  - If `provenance/vault/state.json` exists but the binding is missing, the library fails closed with `STORAGE.NOT_BOUND`.
  - There is never a silent fallback to another destination.
- **Switching backends:** `voice-lab vault bind …` plus a relaunch. There is no rebuild and no change to the app's settings.

**Library copies** (`library_replication.py`, `@canonical library_replication`).
- **Sources.** ~~Each owner module implements a `ReplicationSource`~~ Each owner has a `ReplicationSource`, kept in `library_replication.py` because sources inside the owner modules would create import cycles with the composition root *(amended 2026-09-17, Step 5)*. A source lists its items without blocking any writer:
  - write-once files are read directly;
  - replaced files are read, their identity re-checked, and the read retried if they changed;
  - the corpus database is copied with SQLite's online backup API into private scratch space.

  Every item's bytes must match the digest its seal or record names. If an owner changes during a pass, its previous entries are kept for that generation.
- **Classification.** Which paths are copied and which stay on the Mac is data, not code. A test fails if any path in a fixture library is unclassified, including every file this plan writes.
- **Manifests.** A `LibraryManifest` contains:
  - the generation number;
  - the digest of the previous manifest;
  - `library_id` and the creation time;
  - one entry per file: path, SHA-256, size, class, and the refs of its chunks (at most 16 MiB each).

  The manifest is itself encrypted and stored as blobs, and the head ref points to it.
- **Rollback protection.** The Mac records the last generation and its digest in `provenance/vault/state.json`, together with the destination it belongs to and the ledger entry that records it *(amended 2026-09-17, Step 9)*. It refuses a head that is older than that, forks from it, fails to decrypt, belongs to another destination, or whose ledger tail no longer matches.
- **Ledger.** Before the first new blob of a generation, the Mac appends an entry to `provenance/vault/ledger/` containing:
  - the approval digest;
  - the destination's identity;
  - ~~the plaintext manifest digest;~~ the payload digest: the digest of the generation's manifest *plan* (every file's path, class, digest, size, mode and pin, without chunk refs, which exist only after sealing) *(amended 2026-09-17, Step 2)*;
  - the classes and the byte count;
  - the previous entry's digest.

  It appends a completion entry after the head swap, naming the plaintext manifest digest (whose plan has that payload digest) and the head version. A local-directory destination needs no approval, so its entries carry no approval digest.
- **Verify.** `vault verify` downloads and decrypts every chunk (or a named subset) and compares digests. A file counts as present only after this check, never on the vault's word.
- **Restore.** `restore_library` restores into an empty asset root.
  - It requires either `--generation N --manifest-sha256 D`, or `--accept-latest`, which prints the generation's time and entry counts.
  - Before writing anything, it checks free space against the manifest's sizes plus a margin.
  - It writes 0700/0600 modes (0400/0500 under `raw/`) and never writes `provenance/vault/**`.
  - The recording index then rebuilds itself.
- **Background copying.** A supervisor owned by the service copies after durable commits, with a quiet period and backoff. It never runs on the Save path, never holds a lock a writer takes, and reports through `vault-status`.

**Fine-tune run archive.**
- **Archive:** `voice-lab finetune archive --run R` uploads the run, verifies it by downloading it again, and writes `archived.json`, which names the generation, the manifest digest and every file's digest.
- **Remove:** `--remove-local-copy` removes the run's large files only after that verification, and records the removal. It is an explicit flag, as AGENTS.md:43 requires.
- **Restore:** `voice-lab finetune restore-run --run R` brings the run back, after a free-space check.
- **Status:** `fine-tune-status` shows each run as `local` or `archived`. Speak and `finetune synthesize` refuse an archived run and name the restore command.

**Destination approval and vault identity.**
- **Approval.** A `VaultDestinationApproval`, kept in `provenance/vault/approvals/`, names:
  - the tailnet name;
  - the node's stable Tailscale ID;
  - the tag `tag:voice-lab-vault`;
  - the endpoint name;
  - the namespace;
  - `library_id`;
  - the classes of file it covers, with fine-tune checkpoints and datasets as a separate opt-in;
  - `approved_at`, a mandatory `expires_at`, `approved_by` and a statement.
- **Identity check.** Before each session, the client:
  1. resolves the endpoint;
  2. runs `tailscale whois --json` on the resulting address and requires the approved node ID, tag and tailnet;
  3. connects to that same address, with TLS verified for the endpoint name.

  Nothing is pinned to the certificate.
- **No approval, no upload.** Nothing is uploaded without a matching, unexpired approval.
- **Request log.** Every request the Mac sends is logged in `provenance/vault/requests/`.

**Vault server** (`storage_server.py`, `@canonical storage_server`, console script `cole-voice-lab-vault`).
- It serves `LocalBlobStore` and `LocalRefStore` for each namespace. It imports no recording, capture, catalog, consent or model module, and a test checks this.
- **Tokens.** Each namespace has a read token and a write token.
  - Only their hashes are stored in the server's configuration; the tokens arrive as Docker secrets.
  - A token works only in its own namespace.
  - Requests are authenticated before any body is read.
- **Limits.** `Content-Length` is required and capped at 16 MiB plus overhead. Worker threads are bounded, and requests have timeouts.
- **Writes.** Uploaded blobs are checked against their SHA-256 and are write-once. Ref updates are compare-and-set and fsynced.
- **Access log.** An append-only JSON log records the time, namespace, token scope, operation, ref, bytes and result. It never records the `Authorization` header. `voice-lab vault audit` compares its reads with the Mac's request log.
- **Health.** `/healthz` returns only "ok".
- **Network exposure.**
  - The server speaks plain HTTP; the Tailscale sidecar terminates TLS.
  - It binds to loopback unless given `--listen-internal`.
  - The client accepts plain HTTP only for a literal `127.0.0.1` or `::1`, and only through a constructor argument that no binding or environment variable can set.

**Container** (`deploy/vault/`).
- **Networks.**
  - The vault is on an `internal: true` Docker network, so it has no internet access.
  - The Tailscale sidecar joins that network and an egress network, and serves `https:443 → http://vault:<port>`.
  - The vault container holds no Tailscale state.
- **Hardening** (as in jobtrack):
  - a non-root UID, a read-only root filesystem, `cap_drop: ALL` and `no-new-privileges`;
  - CPU, memory and PID limits, a tmpfs `/tmp`, and a healthcheck;
  - no host ports, and log rotation;
  - the data directory bind-mounted from the large disk, with `create_host_path: false`.
- **Auth key.** Single-use, expiring within an hour, and deleted once the node has joined.
- **Tailnet policy.**
  - Only Cole may apply `tag:voice-lab-vault`.
  - Only Cole's Mac may reach it, and only on port 443.
  - The tag may reach nothing.
- **Mission Control.** The registry entry is bookkeeping only; the approval does not depend on it.

**Proof that implementations can be swapped.**
- **Storage conformance** (`tests/storage_conformance.py`) runs the `BlobStore` and `RefStore` checks against:
  - the local store;
  - the HTTP client talking to a loopback server;
  - the sealed store over the local store;
  - the sealed store over HTTP;
  - optionally, the deployed vault's `test` namespace, which has its own token. The suite refuses the production namespace.
- **Catalog conformance** (`tests/catalog_conformance.py`) runs checks written only against the port over the SQLite store. Timestamps are checked only for being timezone-aware and in order.
- **Import boundary** (`tests/test_storage_import_boundary.py`) fails if any module other than the composition root, the adapters and the two named seeders imports an adapter.

## Steps

- [x] **Step 1: Catalog ports; move the SQLite store behind them**
  - Add `catalog_ports.py` and move the store into `corpus_store_sqlite.py`. Behaviour does not change.
  - Every caller opens the store through `storage_binding.open_catalog_reader` or `open_catalog_editor`. At this step both are local-only and need no binding file.
  - Split `tests/test_corpus_store.py` into:
    - `tests/catalog_conformance.py`, with checks written only against the port;
    - `tests/test_corpus_store_sqlite.py`, which keeps the clock, timestamp and tampering tests;
    - `tests/test_corpus_store.py`, which keeps the tests of the shared catalog logic (fit, typo class, snapshot reading). *(Amended 2026-09-17: these tests had no destination.)*
  - Make pytest collect `*_conformance.py` files, so `scripts/verify` runs the conformance suites. *(Amended 2026-09-17: the default collection skipped them.)*
  - Add the import-boundary test.
  - Commit with a `refactor:` message.
  - Specialist: `makeover` (confirm the move preserves behaviour)
  - ~~Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/catalog_conformance.py tests/test_corpus_store_sqlite.py tests/test_corpus_store_cli.py tests/test_recording_service.py tests/test_recording_finetune_jobs.py tests/test_storage_import_boundary.py && .venv/bin/mypy src/cole_voice_lab && scripts/verify-native-recording --suite transport`~~
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/catalog_conformance.py tests/test_corpus_store_sqlite.py tests/test_corpus_store.py tests/test_corpus_store_cli.py tests/test_recording_service.py tests/test_recording_finetune_jobs.py tests/test_storage_import_boundary.py && .venv/bin/mypy src/cole_voice_lab && scripts/verify-native-recording --suite transport`

- [x] **Step 2: Storage ports and contracts**
  - Add `storage_ports.py` with `BlobStore`, `RefStore` and the errors.
  - Add `storage_contracts.py`, registering only the models Steps 2–6 use: `BlobRef`, `RefValue`, `LibraryManifest` and its entries, `StorageBinding`, `ReplicationState` and `LedgerEntry`.
  - Add contract tests, each with valid cases and cases that are invalid for exactly one reason.
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_storage_contracts.py tests/test_schema.py && .venv/bin/python -I -B -m cole_voice_lab.schema --check && .venv/bin/mypy src/cole_voice_lab`

- [x] **Step 3: Local blob and ref stores, and the storage conformance suite**
  - Add `storage_local.py` and `tests/storage_conformance.py`. The suite covers:
    - putting the same bytes twice, and bytes over the size limit;
    - write-once behaviour under concurrent writer processes;
    - `get` refusing a corrupted stored blob;
    - compare-and-set conflicts;
    - leftovers from a crash;
    - private file modes;
    - refusal of symlinks and hard links.
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/storage_conformance.py --cov=cole_voice_lab.storage_local --cov-branch`

- [x] **Step 4: Encrypted blob store and the library key**
  - Needs Cole's go-ahead to download `cryptography` (Decision 7).
  - Add the optional `vault` extra and `requirements/vault-macos.txt`, with an exact version and hashes.
  - Add `storage_sealing.py`. It creates the key in the login Keychain through `/usr/bin/security` and exports the recovery sheet.
  - Run the conformance suite over the sealed store wrapping the local store.
  - Add tests that refuse the wrong key, a changed library id, a truncated blob and a swapped nonce.
  - Confirm the release environment identity still verifies with the new package.
  - Specialist: `doddy` (the encryption construction and key handling)
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/storage_conformance.py tests/test_storage_sealing.py tests/test_native_recording_application.py`

- [x] **Step 5: Replication sources and path classification**
  - Add the `ReplicationSource` Protocol and one implementation per owner module.
  - Add the classification data and the test that every path is classified.
  - Tests on the pilot-v2 legacy and native fixture roots:
    - a take saved while a pass is reading is either complete in that pass or carried to the next;
    - no Save is refused because of a pass;
    - the corpus database is copied through the backup API while a writer is active.
  - Specialist: `testineer` (the concurrent-save tests)
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_replication_sources.py tests/test_recording_takes.py`

- [x] **Step 6: Manifest chain, verify and restore**
  - Build and encrypt manifests.
  - Add the head swap, `state.json`, the ledger, verification by download, and `restore_library`.
  - Tests refuse:
    - a rollback;
    - a fork;
    - a manifest that fails to decrypt;
    - a restore into a non-empty root;
    - a restore without enough free space;
    - a manifest that names `provenance/vault/**`.
  - A round trip (build, store in an encrypted local store, restore) must give the same recording index rebuild, `prompts coverage-report` and `finetune corpus` results.
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_library_replication.py tests/test_pilot_v2_coverage.py tests/test_prompt_coverage.py`

- [x] **Step 7: Vault server**
  - Add `storage_server.py` and the `cole-voice-lab-vault` console script, with:
    - the namespace configuration;
    - hashed read and write tokens, compared in constant time;
    - authentication before the body is read;
    - size limits, bounded threads and timeouts;
    - the access log and the health endpoint.
  - It binds to loopback unless given `--listen-internal`.
  - Tests use `http.client` on loopback. One test checks that the server imports no recording or catalog module.
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_storage_server.py`

- [x] **Step 8: HTTP client with the tailnet identity check**
  - Add `storage_http_client.py`, with:
    - the `tailscale whois` check behind an injectable resolver;
    - connecting to the address that was checked;
    - idempotent retries, only for retryable codes;
    - timeouts;
    - the test-only loopback mode.
  - Run the conformance suite over HTTP and over encrypted HTTP.
  - Fault-injection tests:
    - a connection dropped mid-upload;
    - a 5xx followed by a retry;
    - wrong bytes returned;
    - an expired token, or one from another namespace;
    - a slow server;
    - a node ID or tag mismatch;
    - a non-HTTPS URL outside test mode.
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/storage_conformance.py tests/test_storage_http_client.py`

- [x] **Step 9: Binding, destination approval and the `vault` commands**
  - Complete `storage_binding.py`, `VaultDestinationApproval` and the ledger checks.
  - Add `voice-lab vault approve|bind|show|push|status|verify|restore|audit`, and — *(amended 2026-09-17, Step 9)* — `init` (the library id in `provenance/vault/library.json`, which every other command reads), `key create|import|show` and `token create|import` (the key and the vault's tokens live in the login Keychain; `token create` prints only their SHA-256 values, which is all the server's configuration needs), plus `push --dry-run` (which also reports how many files are waiting) and `push --full` (re-upload everything, the fix when `verify` finds a blob the vault lost). The app's wire contracts do not change in this step.
  - Tests for each refusal:
    - no approval;
    - an expired approval;
    - the wrong node, tag or namespace;
    - a class the approval does not cover;
    - an unknown backend;
    - state without a binding;
    - a missing Keychain item.
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_storage_binding.py tests/test_vault_cli.py && .venv/bin/python -I -B -m cole_voice_lab.schema --check`

- [x] **Step 10: Background copying in the service**
  - Add the supervisor.
  - Add the `vault-status` command and event, with their Swift contract twins.
  - Report supervisor failures the same way the fine-tune supervisor does.
  - A test against the real service measures Save latency and refusals while an upload is stalled.
  - ~~Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_library_replication_service.py tests/test_recording_service.py tests/test_recording_contracts.py && scripts/verify-native-recording --suite contracts`~~
  - Verify *(amended 2026-09-17, Step 10: the native audit, the schema check and mypy belong in it)*: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_library_replication_service.py tests/test_recording_service.py tests/test_recording_contracts.py tests/test_recording_native_audit.py && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m cole_voice_lab.schema --check && PYTHONDONTWRITEBYTECODE=1 .venv/bin/mypy src/cole_voice_lab && scripts/verify-native-recording --suite contracts`

- [x] **Step 11: Fine-tune run archive**
  - Add `finetune archive` (with `--remove-local-copy`), `finetune restore-run` and `archived.json`.
  - Add the run's location to `fine-tune-status`, with Swift twins.
  - Make Speak and `finetune synthesize` refuse an archived run.
  - Tests use the stub runtime and a loopback vault.
  - Verify: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_finetune_archive.py tests/test_speech_synthesis.py tests/test_recording_finetune_jobs.py && scripts/verify-native-recording --suite contracts`

- [ ] **Step 12: Vault status in the app**
  - The Library footer shows one line: "Vault · up to date", "Vault · 12 files waiting", "Vault unreachable since 14:02", or "Vault · off".
  - Speak shows "Archived — restore with …" for an archived run.
  - Add a native `vault` suite. It uses a loopback vault, a fresh token and synthetic audio, and covers:
    - a recorded fixture take being copied;
    - an outage and recovery;
    - Speak on an archived run.
  - Verify: `scripts/verify-native-recording --suite vault && scripts/verify-native-recording --suite workspace && scripts/verify-native-recording --suite speak`

- [ ] **Step 13: Container image and deployment files**
  - `deploy/vault/Dockerfile`:
    - a digest-pinned `python:3.12-slim` base;
    - the wheel plus `requirements/vault-linux.txt` (pydantic only, hashed, for x86_64 and arm64);
    - a non-root UID;
    - the `cole-voice-lab-vault` entry point.
  - `deploy/vault/compose.yaml`: the networks, sidecar, secrets and hardening described above.
  - `deploy/vault/tailnet-policy.md`: the tag owner and access rules to apply.
  - `deploy/vault/README.md`, the runbook:
    - prerequisites;
    - the data path on the large disk;
    - creating and rotating tokens;
    - creating the auth key and deleting it after the node joins;
    - reserving the Mission Control endpoint;
    - backing up the data directory.
  - `scripts/verify-vault-container` builds the image on the Mac's Colima, publishes the port on `127.0.0.1` only, and runs the storage conformance suite against it.
  - Verify: `scripts/verify-vault-container && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I -B -m pytest -q tests/test_operator_scripts.py`

- [ ] **Step 14: Decision record, rules and docs**
  - `DECISIONS.md` D-020 records:
    - the vault as the encrypted durable copy;
    - what stays on the Mac, and why;
    - the approval, identity and ledger model;
    - the change to the transfer rule, with its reasons;
    - D-019's consent scope, decided again;
    - the `cryptography` exception.
  - Update:
    - AGENTS.md and SECURITY.md;
    - ARCHITECTURE.md;
    - `docs/CORPUS_STORE.md`: ports, backup and restore, and the rule for future implementations;
    - `docs/FINETUNE.md`: archiving;
    - `docs/NATIVE_RECORDING_UX.md`: vault status;
    - `docs/RUNBOOK.md`.
  - Add `docs/LIBRARY_VAULT.md`.
  - Specialist: `proofreader` for the whole change; `doddy` for a final review of the trust boundary
  - Verify: `scripts/verify`

- [ ] **Step 15: Deploy and make the first copy (with Cole)**
  - Cole approves each remote action.
  - Tailnet:
    1. Apply the tailnet policy.
    2. Create a single-use auth key.
  - On `cole-ubuntu-pc`:
    1. Build the image.
    2. Create the data directory and the secrets.
    3. Start the compose project.
    4. Delete the auth key.
    5. Reserve the endpoint.
  - On the Mac:
    1. Check `tailscale whois` and the health endpoint.
    2. Run the conformance suite against the `test` namespace.
    3. Run `voice-lab vault approve`, then `vault bind`.
    4. Create the key; Cole stores the recovery sheet.
    5. Run the first `vault push`, then `vault verify` on every chunk.
    6. Restore into a scratch root. Coverage and the fine-tune selection must match the live library.
    7. Relaunch the app.
  - Only if Cole asks: archive the smoke-test run with `--remove-local-copy`.
  - Verify:
    - `voice-lab vault verify` reports every entry present, with matching digests;
    - the restore drill matches;
    - the app shows "Vault · up to date";
    - Cole confirms.

## Done criteria

- `scripts/verify` passes, including every conformance suite, the import-boundary test and the native `vault` suite.
- The vault on `cole-ubuntu-pc` holds a verified, encrypted generation of the whole library, including the prompt database.
- A restore drill reproduces the library.
- Finished fine-tune runs can be archived, removed from the Mac on request, and restored.
- Every step is committed on `capture-console`.

## Decisions for Cole

0. **Should the vault hold a copy of the prompt database, or serve it live?**
   - Recommended: a copy. The Mac keeps the working database, and the vault holds encrypted, verified copies.
   - Serving it live would let whoever controls the vault change prompts and switch the active catalog. That would need a signed version chain and the rules re-checked on the Mac, so it belongs in a later plan built on the catalog port.
1. **Transfer rule.** The current rule needs a separate approval for each transfer. Should an encrypted library be storable on the approved `cole-ubuntu-pc` vault under a standing approval with an expiry instead, with a ledger entry written before each upload?
   - Recommended: yes, with a 12-month expiry.
   - Fine-tune checkpoints and datasets are enough to clone your voice, so they would be a separate opt-in.
   - Related: consent record `cole-self-v1` is not approved. Should vault storage require an approved consent record?
2. **Key custody.** The key lives in the Mac's Keychain, and you keep a recovery sheet offline (for example in your password manager). If both are lost, what's in the vault is lost. Is that acceptable?
3. **Tailnet changes.** Apply the tag and access rules above? Also consider turning on Tailnet Lock.
4. **Synthetic speech.** Keep Speak clips on the Mac only (recommended, per D-019), or copy them to the vault too?
5. **Second copy.** The vault is one copy on one disk. Add a second copy in a later plan, for example on the tailnet `archive` node or an external drive?
6. **Removing runs from the Mac.** Only with an explicit command per run, after a verified archive (recommended)? Or automatically, keeping the newest few?
7. **New dependency.** Allow downloading `cryptography` (exact version, hash-locked, used only for vault encryption)?
   - The alternative is a signed Swift helper using CryptoKit. It would keep the key out of Python but takes more work.

### Cole's answers (2026-09-17)

0. A copy. The Mac keeps the working database; the vault holds encrypted copies. No live remote catalog.
1. Yes: a standing `VaultDestinationApproval` with a 12-month expiry, plus a ledger entry before each upload. Fine-tune checkpoints and datasets are a separate opt-in class; it is built now, and Cole decides at deployment whether to opt in. Vault storage does not require an approved consent record; D-020 must say why.
2. Accepted: login Keychain plus an offline recovery sheet.
3. Apply the tag and access rules at deployment. No Tailnet Lock for now.
4. Speak clips stay on the Mac only.
5. No second copy in this plan.
6. Runs are removed from the Mac only by an explicit command per run, after verification.
7. Yes: download `cryptography`, exact version, hash-locked, as the optional `vault` extra.

## Review

The first draft was reviewed on 2026-09-17 by `rigorous-architect` (the storage boundary) and `doddy` (the trust boundary). The revision made these changes:
- **The vault's role.** It is now encrypted, content-addressed storage and applies no lab rules. Three items moved out of scope (Decision 0):
  - the live remote catalog;
  - starting with a degraded catalog;
  - moving the database.
- **Catalog port.** It is split into a reader and an editor, and the SQLite store now sits behind them.
- **Adapters.** They return bytes; the Mac checks and publishes the files.
- **Protection against a bad vault.**
  - Encryption, the generation chain and rollback refusal were added.
  - The approval now binds the node's identity and must expire.
  - A ledger entry is written before each upload.
  - Namespaces and scoped tokens were added.
  - Secrets are kept in the Keychain instead of token paths.
  - The container's network is internal-only.
- **Local safety.**
  - Local copies are removed only after verification by download.
  - Copying takes no writer locks.
  - Swift contract twins ship in the step that changes each wire contract.

**Amended 2026-09-17:** Cole answered Decisions 0–7 (recorded above) and chose to start before the pending combined build, which will be made from the finished vault commit.

**Amended 2026-09-17 (Step 1):** the old corpus-store test file keeps the shared-logic tests, pytest now collects `*_conformance.py`, and the Step 1 verify command runs `tests/test_corpus_store.py` too.

**Amended 2026-09-17 (Step 2):** the upload-started ledger entry names the payload digest of the generation's manifest plan (a manifest's chunk refs exist only after sealing); the completion entry names the manifest digest; the binding names its classes.

**Amended 2026-09-17 (Step 4):** sealed blobs do not hide plaintext sizes; the leak is accepted and goes into D-020.

**Amended 2026-09-17 (Step 5):** the replication sources live in `library_replication.py`, one per owner, and declare which owners their files refer to; an owner whose referent changed during a pass is held back with it.

**Amended 2026-09-17 (Step 9):** the `vault` group gained `init`, `key` and `token` commands and `push --dry-run|--full`; the binding names the approval's digest; the replication state names its destination and ledger tail.

**Amended 2026-09-17 (Step 10):** the step's verify line now also runs the native audit, the schema check and mypy.
