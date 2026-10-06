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

Every `/api/` request carries `Authorization: Bearer <Pocket ID ID token>`. The door checks
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

Limits: 8 open sessions; 4,096 files and 64 GiB per session; 8 GiB per file; 64 MiB per
`PUT`; the intake disk floor (10 GiB free) plus the declared bytes, or
`insufficient_storage`. A session untouched for 7 days is removed when another opens.
Refusals are `ApiRefusal` (`error`, `detail`, sometimes `received_bytes`); the codes are
`api.contracts.ApiErrorCode`. JSON Schemas: `schemas/api-v1-*.schema.json`.

The API never writes `raw/` and never decides acceptance. The publisher
(`voice-lab intake publish`, one pass, run by the PC's path unit) accepts each finalized
package with `accept_capture_session`; a package it refuses is moved untouched to
`incoming/.refused/<id>.<time>` with a record in `provenance/intake/refusals/<id>.json`.

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
