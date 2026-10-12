# Speech API: Cole's voice for registered callers

Status: **designed 2026-10-11** (D-044 to D-047). This document specifies the asynchronous
speech API that the next increment implements; it describes nothing that runs yet. It is
part of the voice platform design ([§3.6](design/VOICE-PLATFORM.md#36-speech-for-registered-callers)).

The caller Cole has in mind is his own agent server, built later and outside this
repository: it sends the text of a reply to voice-lab, gets back audio in Cole's cloned
voice, and plays it on Cole's phone through his own app. voice-lab knows nothing of the
agent or the app. It knows a registered caller, a text, and a job.

Contents:

1. [Rules this design keeps](#1-rules-this-design-keeps)
2. [The flow](#2-the-flow)
3. [The voice release](#3-the-voice-release)
4. [Callers and the configuration](#4-callers-and-the-configuration)
5. [Authentication](#5-authentication)
6. [Endpoints](#6-endpoints)
7. [Execution](#7-execution)
8. [Webhooks](#8-webhooks)
9. [The render ledger and retention](#9-the-render-ledger-and-retention)
10. [Measured and unknown](#10-measured-and-unknown)
11. [Deployment and the owner's steps](#11-deployment-and-the-owners-steps)
12. [Not in v1](#12-not-in-v1)

## 1. Rules this design keeps

- **Tailnet only.** The API is reached through `voice-lab.<tailnet>.ts.net` behind the
  Pocket ID door; Funnel stays off; there is no public endpoint. Free and on Cole's hardware
  (D-028).
- **Cole's voice release only** (D-045). Never the base weights, a zero-shot reference,
  another subject or an undesignated run.
- **Consent** (D-047). An approved, checksum-bound record whose purposes include
  `private-assistant-speech` and whose boundaries include `home-host`, checked before a job is
  queued and again before the worker starts.
- **A queued job in the one GPU slot** (D-046). No always-on or streaming endpoint.
- **The network-facing process never renders; the renderer never touches the network.** The
  API container enqueues and serves files. The job runner (no network) renders. Webhooks go
  out from a third process that can reach the tailnet and nothing else.
- **No text at rest beyond the job.** The ledger holds the text's SHA-256 and length, never
  the text. The job's input is deleted when the job ends.
- **No secret in configuration or Git.** Webhook secrets are files referenced by path, read
  only by the delivery process.

## 2. The flow

```text
caller (agent server, a tailnet node)
  │ POST /api/v1/speech  {text, callback_url, idempotency_key}   bearer: Pocket ID machine token
  ▼
door (oauth2-proxy) ─► API container ── checks: token → caller, limits, release, consent, GPU slot
  │                         │ writes state/speech/jobs/<id>/ + queue/<id>
  │ 202 {id, state: queued, links}
  ▼                         ▼
caller polls GET …/{id}   voice-lab-speech.service (host unit, PrivateNetwork=yes, GPU)
                            claims the job, re-checks release + consent, takes the GPU slot,
                            renders with the release's checkpoint and reference clip,
                            writes speech.wav, the ledger entry, and an outbox event
                                   │
                                   ▼
                          voice-lab-speech-webhooks.service (host unit, tailnet only)
                            signs and POSTs {type, data: {id, state, …}} to callback_url
  ◄───────────────────────────────┘
caller GET /api/v1/speech/{id}/audio  (Range)  ─► plays it on Cole's phone through its own app
```

## 3. The voice release

A `voice-release` record (`cole-voice-lab.voice-release/v1`) in
`provenance/voice-releases/<sequence>.json`, written once with mode 0600, never edited:

| Field | Meaning |
| --- | --- |
| `release_id` | `vr-<sequence>`, for example `vr-0001` |
| `sequence`, `previous_release_sha256` | the append-only chain: the first release names none |
| `subject_id` | the run's and the consent record's subject (`cole-self`) |
| `run_id`, `run_record_sha256` | the finished fine-tune run and the digest of its `run.json` |
| `checkpoint_path`, `checkpoint_sha256` | the run's recorded `model_last.pt` |
| `reference_prompt_id`, `reference_path`, `reference_sha256`, `reference_text_sha256` | the reference clip in the run's dataset, as the dataset recorded it |
| `consent_id`, `consent_sha256` | the consent record at designation |
| `runtime` | the run's runtime identity (F5-TTS and torch versions, pretrained weights digest) |
| `blind_review` | optional: a review id and the score sheet's digest |
| `created_at`, `designated_by` | when, and the name Cole gives |

`voice-lab voice release designate --asset-root … --run <id> [--reference-prompt <id>]
--designated-by <name>` checks the run (finished, not archived, the subject of an approved
consent record for `private-assistant-speech` on `home-host`), hashes the checkpoint and the
reference clip against the run's records through the existing Speak seam
(`finetune.resolve_speech_model`), and writes the next record. `voice release show` prints
the current one; `voice release history` lists them all. The newest record is current.

A render refuses when the current release's checkpoint, reference clip or consent record no
longer hashes to what the release names (`voice_release_changed`, `consent_changed`): rotating
the consent record, even to renew it, means designating again.

## 4. Callers and the configuration

One strict JSON file, `cole-voice-lab.speech-config/v1`, owned by root, readable by the
`voice-lab` user, mounted read-only into the API container (`/etc/voice-lab/speech.json`). It
holds no secret: only paths to secrets. The API, the job runner and the delivery process each
read the parts they need.

```json
{
  "schema_version": "cole-voice-lab.speech-config/v1",
  "asset_root": "/data/voice-lab/lab",
  "state_root": "/data/voice-lab/state/speech",
  "gpu_slot_root": "/data/voice-lab/state/jobs",
  "subject_id": "cole-self",
  "tailnet_suffix": "<tailnet>.ts.net",
  "audio_retention_hours": 168,
  "record_retention_days": 30,
  "queue_limit": 32,
  "queue_timeout_seconds": 600,
  "render_deadline_seconds": 300,
  "runtime": {"runtime_root": "/data/voice-lab/lab/runtimes/f5-tts-rocm", "device": "cuda", "nfe_steps": 32},
  "callers": [
    {
      "caller_id": "agent-server",
      "issuer": "https://id.<tailnet>.ts.net",
      "subject": "<the client's token subject>",
      "audiences": ["<the client's token audience>"],
      "allowed_purposes": ["private-assistant-speech"],
      "webhook_urls": ["https://agent.<tailnet>.ts.net/hooks/voice-lab"],
      "webhook_secret_file": "/etc/voice-lab/speech-webhook-secrets/agent-server",
      "max_queued_jobs": 4,
      "max_text_characters": 2000,
      "daily_character_budget": 50000
    }
  ]
}
```

Validation refuses: a duplicate caller id, subject or secret path; an empty audience list; an
`http` webhook URL, one with credentials, a query or a fragment, or one whose host is not a
single label under `tailnet_suffix`; a limit outside its bounds (`max_text_characters` 1–2,000,
`max_queued_jobs` 1–16, `daily_character_budget` up to 1,000,000; `queue_limit` 1–256;
retention 1 hour to 30 days); a relative path. At most four callers (one is expected).

A caller's `webhook_urls` are prefixes: a request's `callback_url` must equal one, or extend
one with further path segments. The secret file is checked when the delivery process reads it:
a regular file, not a link, not group-writable or world-readable, holding one
`whsec_<base64>` secret of 24 to 64 bytes.

## 5. Authentication

**Callers are machine clients.** Pocket ID's discovery document advertises
`client_credentials`, the device-code grant, `refresh_token` and `authorization_code`. A
caller is a confidential Pocket ID client, one per caller, using `client_credentials`. Its
tokens are RS256 JWTs signed with the issuer's keys, which the API already reads from the JWKS
file a host unit refreshes (`state/auth/jwks.json`, design §3.5).

**The API's check.** The speech routes check the bearer themselves, as the intake
compatibility route does, and do not take the global `/api/` check (which admits only Cole's
desktop audience and subject). A token is accepted when its signature verifies, its issuer is
trusted, it is within its lifetime (60 s leeway), and its (`iss`, `sub`) and one of its `aud`
values match a registered caller. The caller is the principal for everything after: limits,
idempotency, job visibility (a caller sees only its own jobs; another's id answers 404) and
allowed webhook URLs. A speech caller's token never passes the global check, so it reaches no
other route; Cole's desktop tokens are unchanged and keep reaching everything they reach
today. Cole's own desktop may be registered as a caller too (its subject, audience
`voice-lab-desktop`, no webhook URL) if he wants to use the speech API directly.

**What Pocket ID puts in the claims is to be confirmed.** The registry names `subject` and
`audiences` explicitly because a client-credentials token's `sub` and `aud` are expected to be
the client's id, but that is to be read from one real token (decode the claims locally,
never print or log the token) before the caller is registered.

**The door.** oauth2-proxy admits bearer JWTs through `SKIP_JWT_BEARER_TOKENS` and
`EXTRA_JWT_ISSUERS`, whose entries are `issuer=audience` pairs separated by commas. The change:

```yaml
OAUTH2_PROXY_EXTRA_JWT_ISSUERS: ${OIDC_ISSUER}=voice-lab-desktop,${OIDC_ISSUER}=${VOICE_LAB_SPEECH_AUDIENCE}
```

with `VOICE_LAB_SPEECH_AUDIENCE` a new non-secret `.env` value. To confirm with the first real
token: oauth2-proxy builds a session from a bearer token's claims and applies its email-domain
rule (`*` here); a client-credentials token has no `email` claim. If the door refuses such a
token, the narrow fix is `OAUTH2_PROXY_OIDC_EMAIL_CLAIM=sub` only if that leaves the browser
sign-in unchanged, or a per-client email claim in Pocket ID; skipping the door for
`/api/v1/speech` is not acceptable. Creating the Pocket ID client and changing the door are
the owner's steps.

## 6. Endpoints

All bodies are strict JSON (`schemas/api-v1-speech-*.schema.json`), refusals are `ApiRefusal`
with a stable code, and every answer is `Cache-Control: no-store`.

### `POST /api/v1/speech`

```json
{"text": "…", "format": "wav", "callback_url": "https://agent.<tailnet>.ts.net/hooks/voice-lab",
 "idempotency_key": "reply-8f2c…", "client_ref": "conversation-12/turn-7"}
```

- `text`: 1 to 2,000 code points (the caller's limit may be lower), not only whitespace, no
  format or control character except a newline: Speak's rule.
- `format`: `wav` (default): 24 kHz, mono, 16-bit PCM, the synthesizer's own output. The API
  image has no ffmpeg, so v1 offers nothing else; `ogg-opus` is reserved and answers
  `422 unsupported_format` until the job runner encodes it with the host's ffmpeg.
- `callback_url`: optional; must match the caller's allowlist (§4), else
  `422 callback_not_allowed`.
- `idempotency_key`: optional, 1 to 128 characters of `[A-Za-z0-9._:-]`. A replay with the
  same key and the same body answers `200` with the same job; the same key with a different
  body answers `409 idempotency_conflict`. Keys live as long as the job's record.
- `client_ref`: optional, at most 200 characters, echoed in status and webhooks; never logged.

Answer `202`:

```json
{"id": "sp-3c0f…", "state": "queued", "created_at": "…",
 "links": {"self": "/api/v1/speech/sp-3c0f…", "audio": "/api/v1/speech/sp-3c0f…/audio",
           "cancel": "/api/v1/speech/sp-3c0f…/cancel"}}
```

Checks, in order, each with its refusal: the token and caller (`401 unauthorized`,
`403 caller_not_registered`); the caller may invoke `private-assistant-speech`
(`403 purpose_not_allowed`); the body (`413`, `422 invalid_request`, `422 unsupported_format`,
`422 callback_not_allowed`); idempotency (`200` replay, `409`); a current voice release whose
files still hash (`503 voice_release_unavailable`); a valid consent record
(`503 consent_unavailable`); a long job holding the GPU slot (`503 gpu_busy`, with
`Retry-After`); the caller's queued jobs (`429 too_many_jobs`), its daily characters (UTC day,
`429 daily_budget_exhausted`), and the queue in all (`503 queue_full`).

### `GET /api/v1/speech/{id}`

```json
{"id": "sp-…", "state": "succeeded", "client_ref": "…", "format": "wav",
 "text_characters": 182, "created_at": "…", "started_at": "…", "finished_at": "…",
 "queue_ms": 410, "render_ms": 6120,
 "audio": {"sha256": "…", "bytes": 581644, "duration_ms": 12116, "expires_at": "…"},
 "error": null, "links": {…}}
```

States: `queued` → `running` → `succeeded` | `failed` | `cancelled`; a succeeded job becomes
`expired` once its audio is deleted. `error` is `{code, detail}` for `failed`; the codes are
`runtime_unavailable` (no runtime for the PC, or it cannot start), `render_failed`,
`render_timeout`, `queue_timeout`, `voice_release_changed`, `consent_changed` and
`internal_error`.

### `GET /api/v1/speech/{id}/audio`

Only when `succeeded`: `200` with `audio/wav`, `Content-Length` and
`ETag: "<sha256>"`, or `206` for a single `Range: bytes=…` (`416` when unsatisfiable).
`409 audio_not_ready` before, `410 audio_expired` after retention, `404 speech_not_found` for
a job that is not the caller's. `HEAD` answers the headers.

### `POST /api/v1/speech/{id}/cancel`

`202` with the job's state. A queued job is cancelled at once. A running job is cancelled by
the job runner within a second: it kills the worker's process group, and the job ends
`cancelled` (its webhook names that state). A job that has ended answers
`409 already_finished` with its state. Cancel and the runner's claim race on one exclusive
claim file, so exactly one of them owns the job.

**Events.** There is no server-sent event stream in v1: no jobs event stream exists to reuse.
Polling always works; the webhook is the push.

## 7. Execution

**State on disk** (`/data/voice-lab/state/speech/`, `voice-lab`, 0700 directories, 0600 files):

```text
jobs/<id>/request.json   the accepted request, without the text (digest and length only)
jobs/<id>/input.json     the text; deleted when the job ends
jobs/<id>/claim          created exclusively by whoever owns the job (runner, or a cancel)
jobs/<id>/cancel         a cancel request for a running job
jobs/<id>/status.json    the job's state, replaced atomically
jobs/<id>/speech.wav     the audio, until retention removes it
queue/<id>               empty; its appearance starts the job runner (a path unit)
idempotency/<caller>/<sha256 of the key>.json
outbox/<event id>.json   a webhook event waiting for the delivery process
webhooks/<event id>/     an event being delivered, with its delivery log
```

The ledger lives in the asset root (§9); the audio does not, so the vault never sees it.

**The job runner.** `voice-lab-speech.service`, a host unit as `voice-lab`, with
`PrivateNetwork=yes`, the GPU's devices (`/dev/kfd`, `/dev/dri`, groups `render` and
`video`), started by `voice-lab-speech.path` when `queue/` changes and by a timer as a
backstop. One pass claims each queued job, oldest first, and for each: re-reads the release
and the consent record; takes the GPU slot (below); renders through a narrow
`SpeechBackend` port (the real one calls `finetune.synthesize_speech`, the existing Speak
seam, with the release's run and reference clip, in a process group of its own); checks the
WAV; moves it to the job; writes the ledger entry, the status and the outbox event; deletes
the input. Then it sweeps retention.

The existing `voice-lab-jobs` unit is the journey watcher (CPU, no GPU devices) and is
unchanged; speech gets its own unit so that its devices and deadlines do not widen the
watcher's.

**The GPU slot.** One slot, shared with every GPU job (fine-tuning, auditions and Speak, when
phase 5 brings them to the PC): an exclusive lock on `state/jobs/.gpu-slot.lock` and a holder
record `state/jobs/gpu-slot.json` naming the kind, the job, when it started and its expected
end. Speech never preempts a holder. It is never queued behind a long job either: while a
fine-tune or audition holds the slot, `POST` answers `503 gpu_busy` with `Retry-After` from the
holder's expected end (10 minutes when it gives none), so an assistant can answer in text
rather than wait hours. When the slot frees, queued speech goes before any queued long job.
A speech job not started within `queue_timeout_seconds` (10 minutes) fails `queue_timeout`:
a stale reply is worth nothing. A render past `render_deadline_seconds` (5 minutes) is killed
and fails `render_timeout`.

**No ROCm runtime yet.** The real backend takes its runtime from the configuration. When the
runtime root, its interpreter or its pinned weights are missing, or the runtime probe fails,
the job fails `runtime_unavailable` with a plain message; the runner does not crash and the
next job is tried. The ROCm runtime and its lock are phase 5's (spike S-2).

## 8. Webhooks

**When.** Once, when a job reaches `succeeded`, `failed` or `cancelled`, if it has a
`callback_url`. No webhook for `expired`.

**The event** carries no text and no audio:

```json
{"type": "speech.succeeded", "timestamp": "2026-10-11T12:00:07Z",
 "data": {"id": "sp-…", "state": "succeeded", "client_ref": "…",
          "audio": {"sha256": "…", "bytes": 581644, "duration_ms": 12116, "format": "wav"},
          "error": null, "links": {"self": "…", "audio": "…"}}}
```

**Signing** follows Standard Webhooks:

- `webhook-id`: the event id, the same for every attempt (`msg_<job id>`);
- `webhook-timestamp`: the attempt's time in whole seconds since the epoch;
- `webhook-signature`: `v1,<base64 of HMAC-SHA256(key, "<id>.<timestamp>.<body>")>`, where the
  key is the base64-decoded part of the caller's `whsec_` secret.

The receiver recomputes it, compares in constant time, and rejects a timestamp more than five
minutes from its clock. The body is the same bytes on every attempt.

**Delivery** (`voice-lab-speech-webhooks.service`, a host unit as `voice-lab`, network limited
to the tailnet ranges as the JWKS unit is; started by a path unit on `outbox/` and a timer every
30 seconds for retries). Each attempt, before connecting:

1. re-reads the configuration and refuses a URL that no longer matches the caller's allowlist;
2. requires `https`, a host under `tailnet_suffix`, and no credentials;
3. resolves the host and refuses unless every address is a tailnet address
   (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), then connects to the address it checked,
   verifying TLS for the host name, so a DNS change between check and connect cannot redirect
   it;
4. sends one `POST` with a 5-second connect and 10-second total timeout, follows no redirect,
   and reads at most 4 KiB of the answer, which it discards.

A `2xx` answer delivers; `410` stops; anything else, a timeout, or a refusal above is retried.
Attempts are at 0, 30 s, 2 min, 10 min, 30 min and 60 min after the event: six attempts over an
hour, then the event is given up. Each attempt appends to the event's delivery log: number,
time, status code or error class, duration; never the secret, the signature or the body.

## 9. The render ledger and retention

**The ledger.** `provenance/speech-renders/<job id>.json` (`cole-voice-lab.speech-render/v1`),
written once by the job runner for every job it started rendering: job id, caller id, text
SHA-256 and length in code points, release id and its record's digest, consent id and digest,
the model and runtime fingerprint (checkpoint digest, reference digest, runtime identity,
device, flow steps, voice-lab version), the outcome, the output's digest, size and duration
when there is one, and the queue, render and audio durations. The vault copies it as a
record (a classification rule is added with it); nothing removes it.

**Retention.** The audio is deleted `audio_retention_hours` (7 days) after the job finished,
and the job reads `expired`. A job's directory (status, request, claim) and its idempotency
key are removed `record_retention_days` (30) after it finished. Failed and cancelled jobs keep
no audio. The sweep runs after each job runner pass.

## 10. Measured and unknown

- **Unknown: synthesis speed on the PC.** No F5-TTS run on the RX 6900 XT (`gfx1030`, ROCm)
  is recorded yet; a separate spike (S-2) is measuring it. The API is asynchronous so that the
  answer does not depend on it. On the Air, Speak's deadline of 120 seconds per request has
  sufficed for local listening (D-019); that is not a figure for the PC.
- **Known.** The text limit, format and sample rate are Speak's. One job renders at a time.

## 11. Deployment and the owner's steps

Written in the repository with the implementation, applied only by the owner:

1. **Consent.** Approve a consent record naming `private-assistant-speech` and `home-host`
   (`consent preview`, then `consent approve-self`, RUNBOOK §1). Only Cole.
2. **The runtime.** Provision the ROCm F5-TTS runtime on the PC (phase 5, S-2). Until then
   every job fails `runtime_unavailable`.
3. **The voice release.** `voice-lab voice release designate --run <id>` on the PC.
4. **Pocket ID.** Create the caller's confidential client with `client_credentials`; read one
   token's claims locally to fill `subject` and `audiences`.
5. **The configuration and secrets.** Write `/etc/voice-lab/speech.json`; create each
   caller's webhook secret file and give the caller the same secret by a private channel.
6. **The door and the stack.** The `EXTRA_JWT_ISSUERS` change and the read-only bind of
   `/etc/voice-lab/speech.json` into the API container (infra's `host.conf` grants binds).
7. **Host units.** Install and enable `voice-lab-speech.{path,service,timer}` and
   `voice-lab-speech-webhooks.{path,service,timer}` through the runner lane's host-unit
   install, which infra's validator must accept (GPU devices; tailnet-only network).
8. **The tailnet policy.** Allow the PC to reach each caller's node on 443 for webhooks, and
   the caller's node to reach `tag:voice-lab` on 443.

## 12. Not in v1

- Streaming or partial audio, and a server-sent event stream (no jobs stream exists yet).
- Ogg Opus (reserved; needs ffmpeg in the job runner's path).
- Any caller outside the tailnet, any audio leaving Cole's own devices, any voice but the
  release, and a watermark (D-046: revisited before audio could reach anyone else).
- Fine-tuning, auditions and Speak through `/api/v1/jobs` (phase 5); they will share the GPU
  slot defined here.
