# Home-host intake

Phase 2 of the [voice platform design](design/VOICE-PLATFORM.md) moves the lab's intake
from the Mac mini to the PC, the home host (D-021). A capturing host uploads a finalized
capture package over HTTPS with a resumable protocol (D-024); a network-less publisher on
the PC validates it with the unchanged strict validator (`intake.py`) and publishes it into
the content-addressed, read-only `raw/` store.

```text
capturing host (Air, PC)                    PC
voice-lab intake upload <session>
  validate locally (symlinks, undeclared,   ts-voice-lab → oauth2-proxy (Pocket ID door)
  tampered files refused)                     → voice-lab API (/api/v1, container)
  standing approval + node identity check       writes incoming/.uploads/<id>/ only
  ledger entry before the first byte            finalize: incoming/<id>/
  PUT chunks with Content-Range       ──►   voice-lab-intake.path → .service (no network)
  finalize, wait for the receipt                accept_capture_session → raw/<manifest>/
  receipt digest = local manifest digest        provenance/intake/{sessions,refusals}/
```

## The protocol (`/api/v1`)

Every `/api/` request carries `Authorization: Bearer <token>`, a Pocket ID token for the
public client `voice-lab-desktop` (its JWT access token, else its ID token). The door checks
it, and the API checks it again (`api/auth.py`: RS256 signature against the provider's
JWKS, issuer, audience `voice-lab-desktop`, lifetime, and a one-subject allow-list).

| Route | Does |
| --- | --- |
| `GET /healthz` | `{"status":"ok"}`, no token (the stack's health check) |
| `GET /api/v1/meta` | server build, API versions, minimum client version |
| `POST /api/v1/intake/sessions` | opens a session from `IntakeSessionRequest`: every file of the package (path, SHA-256, size), the three control files included. Same declaration again: the existing session (200); another declaration for the id: `session_conflict` |
| `GET /api/v1/intake/sessions/{id}` | `IntakeSessionStatus`: `open` with each file's `received_bytes`, `finalized`, `accepted` with the receipt, or `refused` with the publisher's reason |
| `PUT /api/v1/intake/sessions/{id}/files/{path}` | the path percent-encoded as one segment; `Content-Range: bytes S-E/T` (or `bytes */0` for an empty file). `S` must equal the bytes held (`range_not_contiguous` names them). A complete file is hashed: a mismatch discards it (`checksum_mismatch`) |
| `POST /api/v1/intake/sessions/{id}/finalize` | re-hashes every file and renames the package to `incoming/{id}` in one step; idempotent |
| `GET /api/v1/intake/sessions/{id}/receipt` | the intake receipt once accepted, `not_accepted` before |
| `GET /api/v1/catalog` | `CatalogView`: the catalog version `cole-self` records against (D-052), from the corpus read model and the digest-checked snapshot; `ETag` is the snapshot's SHA-256 (`If-None-Match` → 304); `catalog_unavailable` (503) until the PC's catalog is seeded (docs/design/RECORDING-V1.md §7) |
| `GET /api/v1/corpus/library` | `CorpusLibrary`: a row per prompt of the active version (status `missing`, `draft`, `kept` or `needs-attention`, take count, the take that stands for it) and coverage totals; `ETag` is the library read model's SHA-256 (`If-None-Match` → 304); `library_unavailable` (503) while the active version's library is not built yet |
| `GET /api/v1/corpus/prompts/{prompt_id}/takes` | `TakeHistory`: the prompt's row and every take that counts for it, newest first (request time, duration, host, device, decision and revision, trim, QC); same `ETag`; `prompt_not_found` (404) for a prompt the active version does not hold |
| `GET /api/v1/corpus/takes/{take_id}/audio` | the take's `original.wav` from `raw/`, `Range` answered 206 (`range_not_satisfiable`, 416, outside the file), `ETag` the audio's SHA-256; only with `serve --playback-approval` (`playback_not_approved`, 403, without it); `take_not_found` (404), `take_invalid` (500) when the file disagrees with its record |

Limits: 8 open sessions; 4,096 files and 64 GiB per session; 8 GiB per file; 64 MiB per
`PUT`; the intake disk floor (10 GiB free) plus the declared bytes, or
`insufficient_storage`. A session untouched for 7 days is removed when another opens.
Refusals are `ApiRefusal` (`error`, `detail`, sometimes `received_bytes`); the codes are
`api.contracts.ApiErrorCode`. JSON Schemas: `schemas/api-v1-*.schema.json`.

The API never writes `raw/` and never decides acceptance. The publisher
(`voice-lab intake publish`, one pass, run by the PC's path unit) accepts each finalized
package with `accept_capture_session`; a package it refuses is moved untouched to
`incoming/.refused/<id>.<time>` with a record in `provenance/intake/refusals/<id>.json`.
Each pass then writes the record of each new take (`provenance/corpus-takes/<take_id>.json`,
an undecided draft; its summary's `takes_recorded`) and refreshes the corpus read model
(`derived/corpus/`: the active catalog and its library) the corpus routes serve (D-052); its
summary's `read_model` says `current`, `no catalog` or the error.

## Take packages (D-052)

The prompt studio's takes reach the PC one at a time, through the same upload and the same
standing approval (class `capture`), as **take packages** (`corpus_takes`,
`cole-voice-lab.corpus-take/v1`). A take package is a directory of exactly five files:
`manifest.json` (`CorpusTakeManifest`: the take id `take-<SHA-256 of original.wav>`, subject
`cole-self`, the exact prompt and its digest, the catalog version, the request time, the
client, host and PortAudio device, the audio and a summary of the continuity evidence),
`qc.json`, `READY`, `original.wav` (mono 48 kHz s24le) and `continuity.json` (the PortAudio
worker's evidence, bound to the WAV). The client builds one with `build_take_package`; the
upload's session id is the take id.

The publisher tells a take package from a capture package by its manifest's
`schema_version` (`intake.validate_package`), validates it as strictly as a capture package
(`validate_take_package`: the exact file set, no links or directories, canonical JSON, every
digest, the WAV's format, successful evidence describing exactly that WAV), and then admits it
(`admit_take`). It quarantines a valid take with one of these reasons, the first word of the
refusal the client sees:

| Reason | When |
| --- | --- |
| `take_simulated` | the evidence says the simulated capture device made it: no generated audio enters the corpus |
| `take_diagnostic_root` | the asset root is a physical-test diagnostic root |
| `take_catalog_missing` | the catalog version the take names has no snapshot on this server (seed it first, RECORDING-V1.md §7) |
| `take_catalog_invalid` | that snapshot fails its digest checks |
| `take_prompt_not_in_catalog` | that version does not count the take's exact prompt (not the prompt, nor an earlier revision its alias accepts) |

An admitted take is published like any package, into `raw/<manifest_sha256>/`, read-only,
with `intake-qc.json` and the receipt, and recorded in `provenance/intake/sessions/`. The take
store's record names it; the vault copies the raw package (`raw-packages`) and the record
(`corpus-take-states`), and never the read model.

## The capturing host

1. **Approve the home host once** (D-021). Preview, review the printed record and digest,
   then approve with that digest. The node's stable ID comes from
   `tailscale status --json` (the peer whose `DNSName` is the endpoint), never from a
   document:

   ```sh
   voice-lab home-host approve --asset-root "$ASSET_ROOT" \
     --approval-id pc-home-host-2026 \
     --endpoint voice-lab.<tailnet>.ts.net --tailnet-suffix <tailnet>.ts.net \
     --node-stable-id <StableID> --classes capture \
     --approved-at <now, ISO-8601 with offset> --expires-at <at most 366 days later> \
     --approved-by Cole --statement "My captures, to my own PC." --preview
   # then the same arguments, without --preview, plus:
   #   --reviewed-approval-sha256 <the printed approval_sha256> --i-approve-this-destination
   ```

2. **Sign in once** in the browser (Pocket ID, PKCE): the refresh token goes to the OS
   keychain (the login Keychain on macOS, the Secret Service on Linux), never a file.

   ```sh
   voice-lab home-host sign-in --asset-root "$ASSET_ROOT" \
     --issuer https://id.<tailnet>.ts.net --client-id voice-lab-desktop
   ```

3. **Upload** a finalized package. It resumes where it stopped when run again, and prints
   the `accepted` status whose receipt names the package's manifest digest. The package
   stays in the capturing host's library.

   ```sh
   voice-lab intake upload "$ASSET_ROOT/tmp/capture-$SESSION_ID" --asset-root "$ASSET_ROOT"
   voice-lab home-host show --asset-root "$ASSET_ROOT"    # approval and ledger tail
   ```

`voice-lab intake synthetic <new-dir> --session-id <id>` writes a synthetic package
(generated tones; subject `synthetic-test`) for testing the path without any recording.

The rsync lane to the Mac mini (`voice-lab sync plan`, [CAPTURE_AND_SYNC.md](CAPTURE_AND_SYNC.md))
stays usable as the rollback path until phase 2 is accepted.
