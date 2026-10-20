# The corpus client's protocol

How a corpus client (the house panel) signs in, packages a take, uploads it, and decides it.
The routes, their bodies and their answers are in [`openapi.json`](openapi.json); every record
named here has its schema in [`schemas/`](schemas) and an example in [`examples/`](examples).
The design is [docs/design/HOUSE-PANEL.md](../../docs/design/HOUSE-PANEL.md) (D-056).

## 1. Sign-in

The client signs in with Pocket ID's device-code grant (RFC 8628) through the public client
`voice-lab-panel`. It has no secret and no callback URL.

1. `POST https://id.<tailnet>/api/oidc/device/authorize` with `client_id=voice-lab-panel`.
   **Send no `resource`**: with one, the token's `aud` would name that resource, and the PC
   would refuse it.
2. Show `verification_uri` and `user_code`. Never show `verification_uri_complete` or a QR code
   of it: Pocket ID v2.13 approves a complete link on load once consent has been recorded.
3. Poll `POST /api/oidc/token` with
   `grant_type=urn:ietf:params:oauth:grant-type:device_code`, `device_code` and `client_id`,
   every `interval` seconds (5). The answer is `authorization_pending` until the owner approves.
   An expired code keeps answering that, so **stop at `expires_in` yourself** (15 minutes).
4. The answer holds an access token (a JWT, `typ: at+jwt`, `aud` an array naming
   `voice-lab-panel`, valid 15 minutes) and a refresh token.

Then:

- Send the access token as `Authorization: Bearer …` on every `/api/` request. Keep it in
  memory only.
- **Refresh single-flight.** The refresh token rotates on every use, and v2.13 has no grace
  period: reusing a rotated token revokes the whole family (`invalid_grant`), which means a new
  device sign-in. Write the new refresh token atomically (temporary file, fsync, rename) before
  you use the new access token.
- **Storage.** The refresh token is one file, mode 0600, owned by the uploader's user, in a 0700
  directory on persistent storage. It is never in the image, never in a log. Signing out deletes
  it.
- **401 `unauthorized`**: refresh once, then sign in again.

## 2. The approval and the ledger (D-021)

Before any upload, the client holds the owner's standing approval of the PC, as
`HomeHostApproval` ([example](examples/home-host-approval.json)). The owner makes it on the
client's own screen: the endpoint, the PC's tailnet node id and `tag:voice-lab`, the expiry (at
most 366 days) and the record's digest, then a confirmation.

- The binding (`HomeHostBinding`) names the approval and its SHA-256: the canonical bytes with
  their final `\n`.
- Before every upload session, the client checks that the PC's stable node id and tag (from its
  own `tailscaled`: status and whois) are still the approved ones.
- The ledger is a hash chain of `HomeHostLedgerEntry` records. `upload-started`, naming the
  session and the manifest's digest, is written **before the first byte**. Then the session ends
  with `upload-completed` (with the receipt's digest) or `upload-refused`.

## 3. The take package: `cole-voice-lab.corpus-take/v2`

A take is a directory of exactly five files ([the golden take](examples/take)):

| File | Record |
| --- | --- |
| `manifest.json` | `CorpusTakeManifestV2`: the take, its prompt and catalog version, the capture profile, the audio and a summary of the evidence |
| `qc.json` | `QcReport` of `original.wav` (informational: the PC recomputes it from the primary channel) |
| `READY` | `ReadyMarker`, written last: the digests of `manifest.json` and `qc.json` |
| `original.wav` | the frames ALSA delivered, every channel, unchanged |
| `continuity.json` | `AlsaContinuityEvidence` |

- **The take id** is `take-<SHA-256 of original.wav>`, and the subject is `cole-self`.
- **The prompt** is the exact prompt of the catalog version the take was recorded against, with
  its canonical digest. It is refused if that version on the PC does not count it.
- **The capture profile** is `xvf3800-linear-4`:
  - 16 kHz only, in the 2- or 6-channel image (48 kHz is refused);
  - `S16_LE`, `S24_3LE` or `S32_LE`;
  - the Flex Linear-4: USB vendor 0x2886 (Seeed), product 0x0022;
  - one `channels[]` entry per WAV channel, and a 1-based `primary_channel`.
- **The WAV** is the plain 44-byte PCM header, then the frames:
  `size = 44 + frames × channels × width`. A take lasts at most 660 s.
- **The evidence**:
  - After each read of one period, record `[position_frames, timestamp_ns]`: the hardware
    position (the frames read plus `avail`) and `snd_pcm_status`'s `htstamp`.
  - Every pair after the first must satisfy `|(t[i] - t[i-1]) - (p[i] - p[i-1]) × 62_500| ≤
    tolerance_ns` at 16 kHz. The tolerance is at most 4,000,000 ns.
  - `max_abs_deviation_ns` is the largest of those values, `null` for a single pair. The PC
    recomputes it and refuses a different figure.
  - Periods are 256 to 16,384 frames, the buffer holds at least two periods, and there is no
    xrun.
  - The frames are `len(periods) × period_frames`, and the last position is those frames plus at
    most one buffer.
  - The whole rule is in `cole_voice_lab/alsa_evidence.py`, and
    [an invalid example](examples/invalid/continuity-lost-period.json) shows a lost period.
- **The PC refuses**:
  - simulated audio (`take_simulated`);
  - a take on a diagnostic root (`take_diagnostic_root`);
  - a catalog version that contradicts its own (`take_catalog_invalid`);
  - a prompt that version does not count (`take_prompt_not_in_catalog`).

  It defers a take whose catalog version it does not hold yet (`take_catalog_missing`): the
  session stays `finalized`, and intake tries again on its next pass.

## 4. The intake

1. **Declare.** `POST /api/v1/intake/sessions` with an `IntakeSessionRequest`
   ([example](examples/intake-session-request.json)). Its `session_id` is the take id, its
   `files` are the five paths in sorted order with their SHA-256 and size, and its
   `manifest_sha256` is `manifest.json`'s. The answer is 201 for a new session, or 200 for one
   already open. Resume from its `files[].received_bytes`.
2. **Send.** For each incomplete file, `PUT /api/v1/intake/sessions/{take_id}/files/{path}`
   with `Content-Range: bytes S-E/T`, where `S` is the bytes the server holds. Use
   `bytes */0` for an empty file, and percent-encode the path as one segment.
   - Chunks are at most 64 MiB; 4–8 MiB is best.
   - 202 means the chunk is held: send the next range.
   - 200 means the file is complete and hashes to its declaration.
   - `range_not_contiguous` carries `received_bytes`: resume from there.
   - `checksum_mismatch` means the server discarded the file: send it again from 0.
   - `file_already_received` means the file is done.
3. **Finalize.** `POST …/{take_id}/finalize`. The server re-hashes the whole package, so allow
   minutes (the door's upstream timeout is 600 s).
   - 409 `incomplete`: a file is missing. Read the session and send it.
   - **422 `take_profile_not_allowed`**: the package is not a v2 take of your profile
     ([example](examples/refusal-take-profile-not-allowed.json)).
4. **Wait.** `GET …/{take_id}` until `state` is no longer `finalized`:
   - `accepted`: `receipt` is the intake receipt (also at `GET …/{take_id}/receipt`). Write the
     ledger's `upload-completed`.
   - `refused`: `refusal` says why. Write `upload-refused`. A refusal is final for this audio:
     the session is the audio's digest.

## 5. Reading the library

- `GET /api/v1/catalog`: the active catalog version (`CatalogView`).
- `GET /api/v1/corpus/library?profile=xvf3800-linear-4`: your profile's view (`CorpusLibrary`).
  - Each prompt has a status: `missing`, `draft`, `kept` or `needs-attention`, where the newest
    kept take of your profile stands for the prompt.
  - "Continue Missing" is the next `missing` row in catalog order.
- `GET /api/v1/corpus/prompts/{prompt_id}/takes?profile=xvf3800-linear-4`: a prompt's takes
  of your profile (`TakeHistory`), newest first.

Each answer has an `ETag`. Send it back as `If-None-Match` and a 304 means nothing changed.
The library and a history of one view share one tag. Without `?profile=` (the desktop's view),
or with anything else in the query, your token is refused 403 `client_scope`.

## 6. Decisions

`POST /api/v1/corpus/takes/{take_id}/decision` with a `TakeDecisionRequest`
([example](examples/take-decision-request.json)).

- `request_id` is `req-` and 32 lower-case hex digits, **the same on every retry** of this
  change.
- `expected_revision` is the take's `decision_revision` from the history.
- `reason` goes with a rejection, and only a rejection.

The answer is 202 `CorpusRequestStatus` (`queued`) with `Location`. Poll
`GET /api/v1/corpus/requests/{request_id}` until it is `applied` or `refused`. A refusal's
outcome code is one of these:
- `revision_conflict`, with the take's current revision: read the history and decide again;
- `take_not_found`;
- `invalid_request`.

You decide only published takes of your own profile:
- a take the PC has no record of yet answers 404 `take_not_found`, so ask again once intake has
  accepted it;
- another profile's take answers 403 `client_scope`.

## 7. Retries

| Answer | Do |
| --- | --- |
| network error, 408, 429, 5xx (`internal_error`, `insufficient_storage`, `request_queue_full`) | retry with backoff |
| 503 `catalog_unavailable`, `library_unavailable` | retry later: the PC is rebuilding its read model |
| 409 `session_busy` | retry shortly: another request holds the session |
| 401 `unauthorized` | refresh the token once, then sign in again |
| 403 `client_scope`, 422 `take_profile_not_allowed`, 400, 404, 405, 411, 413 | do not retry: fix the request |
| `refused` session | final for this audio |

Every refusal's body is `ApiRefusal`: `{"error": <code>, "detail": <message>}`, with
`received_bytes` on a `PUT`'s refusals ([example](examples/refusal-client-scope.json)).

## 8. The canonical form, byte for byte

Every record the client writes is canonical JSON and ends with one `\n`: the five-file
package's records, the approval, the binding and the ledger. The PC refuses a control file
whose bytes are not canonical. Request bodies are parsed strictly but need not be canonical;
writing them the same way is simplest.

- Object keys are sorted by Unicode code point (so `B` sorts before `a`, and `é` after `z`).
- The separators are `,` and `:`, with no space anywhere.
- Text is UTF-8 and is not escaped beyond JSON's own escapes:
  - `"` and `\` become `\"` and `\\`;
  - the control characters below U+0020 become `\b`, `\f`, `\n`, `\r` and `\t`, and the rest
    `\u00XX` in lower-case hex;
  - everything else, DEL (U+007F) included, is written as it is.
- Integers are written as integers. Floats use Python's `repr`: the shortest text that
  round-trips, in fixed notation for exponents from -4 to 15 (`31.6`, `0.15`, `100.0`), else
  in exponent notation (`1e-07`, `1e+22`, `1.2345678901234568e+17`). Every float keeps a `.` or
  an exponent. NaN and infinities are refused.
- Timestamps are UTC strings: `2026-10-19T12:00:00Z`, with a fraction only when there is one
  (`…:00.250000Z`, six digits).
- `null` is written for an absent optional field that the record names. A field the schema
  makes optional and excludes when absent is left out.

nlohmann/json's `dump()` agrees with this for the values these records hold. For floats, use
`std::to_chars` (shortest round-trip) with Python's rule for fixed or exponent notation.
[The vectors](examples/canonical-json-vectors.json) give each value and its exact text: a
client passes when it writes every one of them byte for byte.

## 9. The golden take

[`examples/take/`](examples/take) is a complete v2 take of 4,096 frames: the 6-channel image,
`S16_LE`, its primary channel microphone 1. Its `original.wav` is not in the bundle. Make it
from [`original.wav.recipe.json`](examples/take/original.wav.recipe.json) (`WavRecipe`):

- the 44-byte header;
- then, for each frame `f` from 0 and each channel `c` from 0, the sample
  `((f × (c + 3) + offset) mod modulus − center) × (c + 1) × scale` as a little-endian signed
  integer of the format's width.

The result must have the recipe's SHA-256 and size. With it, the five files are a package the
PC's validator accepts as they are, and a PC seeded with `voice-lab catalog init --subject-id
cole-self --from-builtin pilot-v2` admits it.
