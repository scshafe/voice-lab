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
| `POST /api/v1/corpus/takes/{take_id}/decision` | `TakeDecisionRequest` (`request_id`, `decision`, `reason`, `expected_revision`): queues the take's new decision for the publisher (below); 202 `CorpusRequestStatus` with `Location` |
| `PUT /api/v1/corpus/takes/{take_id}/trim` | `TakeTrimRequest` (`request_id`, `start_frame`, `end_frame`, both `null` to reset, `expected_revision`): queues the take's new trim; 202 `CorpusRequestStatus` with `Location` |
| `GET /api/v1/corpus/requests/{request_id}` | `CorpusRequestStatus`: `queued`, or `applied`/`refused` with the publisher's outcome; `request_not_found` (404) |

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
an undecided draft; its summary's `takes_recorded`), applies the queued decisions and trims
(below; `requests_applied`, `requests_refused`, `requests_waiting`), and refreshes the corpus
read model (`derived/corpus/`: the active catalog and its library) the corpus routes serve
(D-052); its summary's `read_model` says `current`, `no catalog` or the error.

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
| `take_catalog_invalid` | the store published that catalog id and version with another digest or snapshot (a published version never changes) |
| `take_prompt_not_in_catalog` | that version does not count the take's exact prompt (not the prompt, nor an earlier revision its alias accepts) |

The validator itself refuses a take whose evidence names no UMIK-1 (voice-lab-desktop's rule,
`umik[ _-]?1`, on the worker's own device name), as well as the manifest's label.

A take the PC cannot judge yet is **deferred**, not refused: the package stays in `incoming/`,
the pass lists it under `deferred`, and the next pass (the path unit, or the intake timer within
15 minutes) judges it again. A refusal would be final, since the upload session is the take's
audio digest, and these are the server's state, not the take's:

| Reason | When |
| --- | --- |
| `take_catalog_missing` | the store holds no such catalog or version yet (seed it first, RECORDING-V1.md §7) |
| `take_catalog_unreadable` | the store, or that version's snapshot, cannot be read or fails its checks |

An admitted take is published like any package, into `raw/<manifest_sha256>/`, read-only,
with `intake-qc.json` and the receipt, and recorded in `provenance/intake/sessions/`. The take
store's record names it; the vault copies the raw package (`raw-packages`) and the record
(`corpus-take-states`), and never the read model.

## Imported takes (D-053)

The Swift app's takes (Cole Voice Recorder, on the Mac) reach the PC once, through the same
upload, as **imported takes** (`corpus_take_imports`, `cole-voice-lab.corpus-take-import/v1`),
packaged and sent by `voice-lab corpus-import` on the Mac (docs/design/RECORDING-V1.md §7,
owner step 3). A Swift take has no PortAudio evidence, so it is not a take package: its Core
Audio provenance travels byte for byte. An imported take is a directory of exactly eight files:

| File | What it is |
| --- | --- |
| `manifest.json` | `CorpusTakeImportManifest`: the PC's take id (`take-<SHA-256 of original.wav>`), the Swift take's id (`source_take_id`), `cole-self`, the exact prompt and its digest, `recorded_against` (the group's published `CatalogReference`, or its embedded suite's id and digest), `catalog` (the PC's version that counts the prompt), the seal's request and first-sample times, the host, the Core Audio device, the client (Cole Voice Recorder, the seal's recorder build, macOS), the audio, the digests of the four records below, and the exporter's version and time |
| `qc.json`, `READY` | as a take's |
| `original.wav` | the take, mono 48 kHz s24le: the bytes the seal names |
| `seal.json`, `reservation.json`, `group.json` | the Swift take's `CaptureSealReceipt` (with its `capture-continuity/v1` evidence), `CaptureReservation` and `RecordingGroup`, verbatim |
| `mac-state.json` | `MacTakeState`: the take's decision, revision, reason and restore target, the decision's time when the Mac's records give one, and its frame recipe and edit revision, at export |

The publisher tells it apart by its manifest's `schema_version` and validates it
(`validate_take_import_package`): the exact file set, no links, hard links or directories,
canonical JSON, every digest and byte count, each record parsed by its own strict model, the
WAV canonical and equal to the seal's digest, size and format, the seal bound to the
reservation (its digest) with a checked, successful continuity, `group.json` equal to the
reservation's group and its device equal to the seal's and named as a UMIK-1, the manifest's
prompt, host, device, times and client equal to the records', the Mac's decision between the
seal and the export, and its trim within the take. It admits it (`admit_take_import`) by the
take's rules above except the simulated one (the Swift app has no simulated device):
`take_diagnostic_root`, `take_catalog_invalid`, `take_prompt_not_in_catalog`, and deferred
with `take_catalog_missing` or `take_catalog_unreadable`. The same audio as a take the PC
already holds has its take id, so it is refused as a session id collision.

An admitted imported take is published into `raw/<manifest_sha256>/` like any package, and its
record in `provenance/corpus-takes/` carries the Mac's state rather than an undecided draft:

- the decision at revision 1 (`kept`, `rejected`, `removed`), or the undecided `draft` at 0;
- a rejection's reason; a removal's stays in the raw package's `mac-state.json`;
- decided at the Mac's time for an explicit reject, remove or restore, else at the PC's
  acceptance (a Save records no time on a Swift take);
- the trim at revision 1 when the Mac kept less than the whole take, else untrimmed at 0;
- `origin: {"kind": "imported", "source_take_id": …}`, which the library's takes and
  `GET /api/v1/corpus/prompts/{prompt_id}/takes` carry too (absent for an uploaded take).

From then on the PC holds the take: decisions and trims (below) apply to it as to any other,
and a later change on the Mac does not follow it. The vault binds its raw files to the
manifest (`raw-packages`) and copies its record (`corpus-take-states`).

## Decisions and trims (D-052)

A take's decision (`draft`, `kept`, `rejected` with a reason, `removed`) and its trim (the
frames `[start_frame, end_frame)` it keeps, or all of them) each carry a revision, 0 until
first changed. The API cannot write them: its lab mount is read-only except `incoming/`. So
a change travels as a **corpus request** (`corpus_requests`), which the publisher applies:

1. The client sends the change with its own `request_id` (`req-` and 32 lower-case hex
   digits, the same on every retry) and the revision it last saw (`expected_revision`:
   `decision_revision`, or `trim.revision`, from the library). A client sends a take's
   changes one at a time, in order. A restore is the decision the take had before (`kept`,
   or `draft`); `removed` is a state, never a deletion.
2. The API checks only what it can know read-only: the take id, that the PC has the take's
   record (or its package is uploading or waiting in `incoming/`), a trim's end within the
   take's frames, and the body. It never checks the revision: requests already queued may
   change it first. It writes `cole-voice-lab.corpus-request/v1` (with `received_at`) to
   `incoming/.corpus-request-tmp-<hex>` (0600, fsynced) and renames it to
   `incoming/.corpus-request-<UTC time to the nanosecond>-<request_id>.json`, which starts the
   publisher. Names sort in arrival order (a later request is always named after every
   request still queued). It answers 202 `queued`.
3. The publisher applies the queued requests in name order. It reads each one without
   following a link or blocking on a FIFO, bounded, and requires it canonical and named for
   its request. A change applies only when `expected_revision` is the take's current
   revision of that kind; the take's record is rewritten with the next revision (a decision
   with the publisher's time). The outcome
   (`cole-voice-lab.corpus-request-outcome/v1`, `provenance/corpus-requests/<request_id>.json`)
   is written first, then the record, then the request file is removed: an interrupted pass
   is finished from the outcome by the next one, so a change applies exactly once. The
   library follows in the same pass. The path unit does not start a pass for what changes
   while one runs, so the pass lists `incoming/` again after its requests and after the
   library, and applies what arrived meanwhile (up to three rounds; the timer catches the
   rest). One pass runs at a time: `publish_pending` holds `provenance/intake/.publisher.lock`
   (`flock`), and a second pass (the unit's while one runs by hand) waits for it, up to an
   hour, rather than interleave an outcome and its record.

A retry with the same `request_id` and body answers as before (202, with the outcome once
there is one); the same id with another take or change is `request_conflict` (409). The
outcome's `revision` is the take's revision after the request: the new one when applied, the
current one when refused for a conflict, so the client can rebase.

| Outcome code | When |
| --- | --- |
| `revision_conflict` | `expected_revision` is not the take's current decision (or trim) revision |
| `take_not_found` | the PC has no record of the take, and it is not on its way (below): intake refused it, say |
| `trim_out_of_bounds` | the trim ends after the take's last frame (the API checks this when it can) |
| `invalid_request` | the request file could not be read safely (a link, a FIFO, a hard link, too large, not canonical, not named for its request): its outcome is written, then it is set aside in `incoming/.refused/`, and the outcome names no take. A file that could not be opened for now (EACCES, EMFILE) waits instead |

A request whose take has no record yet **waits** in `incoming/` while the take is on its way:
its package is uploading (`incoming/.uploads/<take_id>` without `FINALIZED`), waits in
`incoming/` itself (finalized, or deferred), or was finalized and intake has not refused it
(`provenance/intake/refusals/<take_id>.json`): a pass has claimed it in `tmp/` or accepted it
and records it at the end of its packages. The API queues such a request rather than answering
404. Every later request of that take waits with it; the pass lists them under `requests_waiting`. So
does a request the pass could not finish (an `OSError`, a damaged record). Nothing waits
forever unnoticed: each pass lists it again.

API refusals besides the body's (`invalid_json` 400, `invalid_request` 422,
`content_length_required` 411, `body_too_large` 413 past 8 KiB): `take_not_found` (404),
`trim_out_of_bounds` (422), `request_conflict` (409), `request_queue_full` (503, with
`Retry-After: 60`, while 10,000 requests wait: the publisher is not running), `take_invalid`
(500, the take's record fails its checks). No audio is involved, so no transfer approval or
ledger entry; the bearer token is checked as on every `/api/` route.

The vault copies the outcomes (`corpus-request-outcomes`, records) with the take records;
`incoming/` (the queue and `.refused/`) never leaves the PC.

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
