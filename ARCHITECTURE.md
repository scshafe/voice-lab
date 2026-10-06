# Architecture

For a visual walkthrough of the native app, recording-to-voice workflow, and
vault archive/restore process, see
[Application architecture diagrams](docs/APPLICATION_ARCHITECTURE_DIAGRAMS.md).

## Boundaries

```text
code repository (Git-safe)
  schemas + CLI + workers + prompts + policy + tests

private asset root (mode 0700)
  incoming/       resumable Tailnet landing zones
  raw/            verified, write-protected capture sessions
  derived/        trims, resamples, references, datasets
  provenance/     consent and transfer approvals; corpus/ prompt and catalog
                  store (corpus.sqlite3 + immutable snapshots/); vault/ the
                  library identity, binding, destination approvals, generation
                  state, ledger, manifest copies and request log (never copied
                  to a vault, never restored from one)
  experiments/    plans, raw renders, normalized renders, run evidence
  evaluations/    blinded indices, listening scores, reveals, verdicts
  models/         shared model cache and immutable lock evidence
  runtimes/       isolated Python environments/source checkouts
  tmp/            same-filesystem atomic staging
```

## Data flow

```text
PromptSuite
    │
    ▼
CaptureSession ── finalize/QC ──> CaptureManifest + manifest digest
    │                                  │
    └──── rsync staging over Tailnet ──┘
                                       ▼
                                  Intake validator
                                       │
                                       ▼
                              content-addressed raw session
                                       │
                         reference/dataset derivations
                                       │
                                       ▼
ExperimentPlan ──> isolated WorkerRequest/WorkerResult ──> normalization
                                       │
                                       ├── automatic diagnostics
                                       └── blinded listening package
                                                  │
                                                  ▼
                                           Verdict + promotion
```

## Storage and vault

```text
catalog_ports                CatalogReader and CatalogEditor: what the lab may
                             ask of the prompt and catalog store and what it may
                             change, with the records and the catalog errors
corpus_store_sqlite          the SQLite store behind those ports
storage_ports                BlobStore and RefStore: content-addressed bytes and
                             compare-and-set named pointers, with the STORAGE.*
                             errors every implementation raises
storage_contracts            blob refs, ref values, manifests, the binding, the
                             replication state, the ledger, the destination
                             approval and the server's records
storage_local                blobs and refs in one private local directory
storage_sealing              AES-256-GCM sealing, the library key and the
                             Keychain seam that holds it
storage_http_client          the Mac's client for a tailnet vault, including the
                             node identity check
storage_server               the vault host's HTTP face over one local blob and
                             ref store per namespace
storage_binding              composition root: the only module that builds an
                             adapter for the lab, and the reader of the binding,
                             the approval and the Keychain items
library_classification       the table that decides, per path, what a copy
                             carries and what stays on the Mac
library_replication          one pass: every owner's files, read without taking
                             a lock a writer takes
library_generations          sealed manifests chained under a head ref, the
                             ledger, push, verify and restore
library_replication_service  the recording service's copy supervisor and the
                             worker subprocess each pass runs in
```

A vault is dumb storage: encrypted, content-addressed bytes and named pointers,
reached only through the two ports. It applies no lab rule and sees no file path
and no plaintext. The Mac keeps the complete working library and stays the only
place where lab rules are applied — classification, the destination approval,
digests, the generation chain and every refusal. `storage_binding` is the one
module that builds an adapter for the lab — the vault server builds the local
store it serves, and the two native fixture seeders their own fixture libraries;
`tests/test_storage_import_boundary.py` parses every module under `src/` and
every script and fails if anything else does — so no caller can pick a
destination, and every vault is wrapped in `SealedBlobStore` there: no binding
can turn encryption off. The whole design is in
[docs/LIBRARY_VAULT.md](docs/LIBRARY_VAULT.md).

The app shows copying status in the sidebar and blocks archived fine-tuned
models in Speak. The native `vault` suite checks that boundary with a synthetic
loopback server. The hardened container and dedicated HTTPS sidecar templates
are in [deploy/vault/](deploy/vault/README.md); the server's entry point is
`python -I -B -m cole_voice_lab.storage_server`. D-020 records the approved
storage policy. Deployment (Step 15) is still outstanding: no real library is
bound and no vault host has been started.

## Trust model

- Capture hosts are not trusted to declare correctness; the mini re-hashes and
  re-inspects every accepted file.
- Transport encryption does not replace the content manifest.
- Worker processes are dependency isolation boundaries, not security sandboxes.
- The audition orchestrator persists a prompt-suite-bound immutable matrix,
  runs one isolated installed worker subprocess at a time, and resumes only
  after rehashing the full result/evidence chain.
- A model cache is untrusted until its resolved revision and relevant artifact
  hashes have been captured.
- Seed-VC source performances are path-, byte-count-, and SHA-256-bound in the
  worker request and rechecked before and after inference.
- Model eviction is an explicit, digest-confirmed deletion of one verified
  source-ID directory and is serialized against active workers.
- Static review pages receive blinded audio indices, never the reveal mapping.
- A vault host is not trusted with plaintext: every blob is sealed on the Mac
  under a key never sent to the vault. Sealing hides content, not sizes or times.
- A vault is not trusted about what it holds: a file counts as present only
  after its chunks are downloaded, decrypted and re-hashed, never on `has()`.
- A tailnet vault's identity is the node's stable Tailscale ID and its tag,
  rechecked before each session; DNS names and certificates are not identity.
- Nothing reaches a tailnet vault without an unexpired approval of that
  destination for that library, namespace and file class. Every upload, local
  or tailnet, is preceded by a ledger entry naming what it is about to send.
- The head a vault returns is checked against the Mac's own record of the last
  generation: an older head, a fork, an undecryptable manifest or a broken
  ledger chain refuses the push and uploads nothing.

## Reusable-library lens

The canonical JSON, secure manifest intake, content-addressed evidence, isolated
worker protocol, and blind-evaluation planner are intentionally provider-neutral.
So are the storage and catalog ports: an implementation that passes their
conformance suites (`tests/storage_conformance.py`, `tests/catalog_conformance.py`)
can replace the current one without a change to any dependent module.
After the pilot, extract them only if a second project demonstrates the same
boundary; avoid premature packaging around voice-specific policy.
