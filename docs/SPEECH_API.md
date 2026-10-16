# Speech API: Cole's voice for registered callers

Status: **built, in the stack** (designed 2026-10-11, D-044 to D-048). The code is in
`src/cole_voice_lab/speech/` (`voice-lab voice release …`, `voice-lab speech …`,
`serve --speech-config`), and `deploy/stack/compose.yaml` serves it; nothing renders on the
PC until the owner's steps in [deploy/speech/README.md](../deploy/speech/README.md) are done. It is part of the voice
platform design ([§3.6](design/VOICE-PLATFORM.md#36-speech-for-registered-callers)).

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
- **Cole's own voice, by standing decision** (D-050). No consent record is required or read:
  Cole's standing decision permits his own voice for assistant speech on his own hardware, and
  the subject is pinned in code (`subject_id`, `cole-self`). Consent records written before
  stay on disk as history. (D-047's consent record for `private-assistant-speech` on
  `home-host` is retired.)
- **A queued job in the one GPU slot** (D-046). No always-on synthesis endpoint, and no
  audio streamed while it is synthesized: finished sentences are published as they complete,
  and the job's event stream only announces them.
- **The network-facing process never renders; the renderer never touches the network.** The
  API container enqueues and serves finished files. The job runner and its resident model
  worker (no network) render. Webhooks go out from a third process that can reach the tailnet
  and nothing else.
- **Reproducible.** Sentences render sequentially with derived seeds; a rerun is
  byte-identical.
- **No text at rest beyond the job.** The ledger holds the text's keyed digest (HMAC-SHA256
  under a per-install key, §7) and its length, never the text. The job's input is deleted when
  the job ends.
- **No secret in configuration or Git.** Webhook secrets are root-only files referenced by
  path, which systemd hands to the delivery unit alone.
- **Model families** (D-048). The release, the backend port and the ledger are
  family-neutral; F5-TTS is the one family registered today. Whatever a family renders
  natively, the API's audio is 24 kHz mono 16-bit WAV.

## 2. The flow

```text
caller (agent server, a tailnet node)
  │ POST /api/v1/speech  {text, callback_url, idempotency_key}   bearer: Pocket ID machine token
  ▼
door (oauth2-proxy) ─► API container ── checks: token → caller, limits, release, GPU slot
  │                         │ writes state/speech/jobs/<id>/ + queue/<id>
  │ 202 {id, state: queued, links}
  ▼                         ▼
caller follows            voice-lab-speech.service (long-running host unit, PrivateNetwork=yes, GPU)
GET …/{id}/events           claims the job, re-checks the release, holds the GPU slot,
(or polls GET …/{id})       splits the text into sentences; for each, in order, the resident
  ◄── segment.ready n ──    model worker renders it (seeded) and the runner publishes
GET …/segments/n/audio      segments/000n.wav; then joins speech.wav, writes the ledger
  ─► plays sentence n        entry and an outbox event
                                   │
                                   ▼
                          voice-lab-speech-webhooks.service (host unit, tailnet only)
                            signs and POSTs {type, data: {id, state, …}} to callback_url
  ◄───────────────────────────────┘
caller GET /api/v1/speech/{id}/audio  (Range): the whole reply, if it wants it
```

## 3. The voice release

A `voice-release` record (`cole-voice-lab.voice-release/v1`) in
`provenance/voice-releases/<sequence>.json`, written once with mode 0400, never edited. Its
fields are common to every model family (D-048) except `model`, the family's own section:

| Field | Meaning |
| --- | --- |
| `release_id` | `vr-<sequence>`, for example `vr-0001` |
| `sequence`, `previous_release_sha256` | the append-only chain: the first release names none |
| `subject_id` | the run's subject: always the pinned subject, `cole-self` |
| `model_family` | the run's model family (`f5-tts`) |
| `run_id`, `run_record_sha256` | the finished fine-tune run and the digest of its `run.json` |
| `files` | every file the family needs, by role, with its path in the asset root and its SHA-256; for F5-TTS `checkpoint` (the run's recorded `model_last.pt`) and `reference` (the clip in the run's dataset) |
| `model` | the family's section, tagged by `family`; for F5-TTS the reference prompt id, the SHA-256 of its transcript, and the runtime the run trained with (F5-TTS and torch versions, pretrained weights digest) |
| `consent_id`, `consent_sha256` | only in a release designated before D-050: the consent record it named, never read; a new release has neither |
| `blind_review` | optional: a review id and the score sheet's digest |
| `created_at`, `designated_by` | when, and the name Cole gives |

`voice-lab voice release designate --asset-root … --run <id> --designated-by <name>
[--config <speech.json>] [--reference-prompt <id>] [--blind-review <id> --score-sheet-sha256
<sha>]` asks the run's family which files it binds (F5-TTS resolves them through the existing
Speak seam, `finetune.resolve_speech_model`), checks the run (finished, not archived, of the
pinned subject, `cole-self`: neither the command nor the configuration can name another),
hashes every file against the run's records, and prints the record it would write; with
`--i-designate-this-run` it writes it. No consent record is named or read (D-050).
`voice release show` prints the current one; `voice release history` lists them all. The
newest record is current.

A render refuses when the current release is of another subject than the configuration
pins, or its run or a file no longer hashes to what the release names
(`voice_release_changed`).

## 4. Callers and the configuration

One strict JSON file, `cole-voice-lab.speech-config/v1`, owned by root, readable by the
`voice-lab` user: `/data/voice-lab/config/speech.json` (root:voice-lab 0640, in a directory
root:voice-lab 0750). The API container sees it through a read-only bind of that directory, at
the same path; it is not under `/etc` because infra refuses any container bind there. It holds
no secret: only paths to secrets, which stay under `/etc/voice-lab/speech-webhook-secrets/`
(root only), outside every bind, read by the delivery unit alone (§8). The API, the job runner
and the delivery process each read the parts they need.

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
  "segment_deadline_seconds": 120,
  "idle_unload_seconds": 900,
  "gpu_busy_retry_seconds": 600,
  "runtime": {"model_family": "f5-tts",
              "runtime_root": "/data/voice-lab/lab/runtimes/f5-tts-rocm", "device": "cuda",
              "nfe_steps": 32, "visible_devices": "0"},
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

`subject_id` is the one subject whose release may speak, and it is pinned in code: it may be
left out, and any value but `cole-self` is refused (`config.SpeechSubject`, D-045), so a
configuration file cannot make another subject's release speak (D-050). A caller's
`allowed_purposes` (`private-assistant-speech`, the only value) is its scope in the registry,
not a consent purpose. `runtime` names the runtime of the release's model family
(`model_family`, D-048). Validation refuses: a duplicate caller id,
subject, secret path or secret file name; an empty audience list; an `http` webhook URL, one
with credentials, a query or a fragment, one whose host is not a single label under
`tailnet_suffix`, or one whose path has a dot segment, an empty segment, a path parameter
(`;`, as in `/hooks/..;/admin`, which some receivers resolve to `/admin`), or a
percent-encoded dot, slash, semicolon or backslash; a limit outside its bounds (`max_text_characters` 1–2,000,
`max_queued_jobs` 1–16, `daily_character_budget` up to 1,000,000; `queue_limit` 1–256;
`audio_retention_hours` 1–720, that is 1 hour to 30 days; `record_retention_days` 1–365); a
relative path. At most four callers (one is expected).

A caller's `webhook_urls` are prefixes: a request's `callback_url` must name the same host and
port (443 when none is given) and the same path segments, or extend them by whole segments
(compared after parsing, never as strings). The secret file is checked when the delivery
process reads it: a regular file, not a link, not group-writable or world-readable, holding
one `whsec_<base64>` secret of 24 to 64 bytes.

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
values match a registered caller. The check takes at most eight distinct audiences across
all callers (the first eight, sorted); a configuration that names more is served, but the API
logs at start how many were dropped and which callers lose one. The caller is the principal for everything after: limits,
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
`EXTRA_JWT_ISSUERS`, whose entries are `issuer=audience` pairs separated by commas. The stack
(`deploy/stack/compose.yaml`) has:

```yaml
OAUTH2_PROXY_EXTRA_JWT_ISSUERS: ${OIDC_ISSUER}=voice-lab-desktop,${OIDC_ISSUER}=${VOICE_LAB_SPEECH_AUDIENCE}
```

with `VOICE_LAB_SPEECH_AUDIENCE` a required, non-secret `.env` value: the caller's client id,
which Pocket ID puts in its client-credentials token's `aud` (`parley-relay` for Parley's
relay, whose `sub` is `client-parley-relay`).

A client-credentials token has no `email` claim, and that needs no change. For a token an
extra JWT issuer verifies, oauth2-proxy (v7.15.3, `pkg/apis/middleware/session.go`,
`CreateTokenToSessionFunc`) sets the session's email to the token's `sub` when it has no
`email`, and treats a missing `email_verified` as verified; the door's email rule (`*`)
accepts any non-empty email. Checked on 2026-10-16 against the pinned release's binary, with
a synthetic issuer and the door's settings: a token with `aud` `parley-relay`, `sub`
`client-parley-relay` and no `email` reaches the API, with `X-Forwarded-User` and
`X-Forwarded-Email` both `client-parley-relay`; another audience, another issuer, an expired
token or `email_verified: false` answer `401`; forwarded headers a client sends are replaced.
So `OAUTH2_PROXY_OIDC_EMAIL_CLAIM` stays unset (it would change every browser session's
email), and no route skips the door: a skip-auth route for `/api/v1/speech` would leave the
API's own check as the only one, which this design refuses.

The door admits such a token on every path, not only `/api/v1/speech`. The API refuses it
everywhere else: the global check takes only the desktop audience and Cole's subject, and
`/view/` takes the door's forwarded user only when it is Cole's subject. Creating the Pocket ID
client and setting `VOICE_LAB_SPEECH_AUDIENCE` are the owner's steps.

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
  body answers `409 idempotency_conflict`. Keys live as long as the job's record. Bodies are
  compared by their keyed digest, so a key whose job was made before the server's text key
  was replaced (§7) cannot be compared: it answers `409 idempotency_key_unverifiable`, and the
  caller retries with a new idempotency key.
- `client_ref`: optional, at most 200 characters, echoed in status and webhooks; never logged.

Answer `202` with the job's status, the same body as `GET …/{id}` below:

```json
{"id": "sp-3c0f…", "state": "queued", "created_at": "…", "client_ref": "conversation-12/turn-7",
 "links": {"self": "/api/v1/speech/sp-3c0f…", "audio": "/api/v1/speech/sp-3c0f…/audio",
           "events": "/api/v1/speech/sp-3c0f…/events",
           "cancel": "/api/v1/speech/sp-3c0f…/cancel"}, …}
```

Checks, in order, each with its refusal: the token (`401 unauthorized` without one, or for
one whose signature, lifetime, issuer, subject or audience is not a registered caller's) and
the caller (`403 caller_not_registered` when the claims match no single caller); the caller
may invoke `private-assistant-speech` (`403 purpose_not_allowed`); the body (`413`,
`422 invalid_request`, `422 unsupported_format`, `422 callback_not_allowed`); idempotency
(`200` replay, `409 idempotency_conflict` or `409 idempotency_key_unverifiable`); a current
voice release (`503 voice_release_unavailable`; its files
are re-hashed by the job runner before every render, not by the API; a release of another
subject than the configuration pins is unavailable too); a long job holding the GPU slot
(`503 gpu_busy`, with `Retry-After`); the caller's queued jobs (`429 too_many_jobs`), its daily characters (UTC day,
`429 daily_budget_exhausted`), and the queue in all (`503 queue_full`).

### `GET /api/v1/speech/{id}`

```json
{"id": "sp-…", "state": "running", "client_ref": "…", "format": "wav",
 "text_characters": 182, "created_at": "…", "started_at": "…", "finished_at": null,
 "queue_ms": 410, "render_ms": null, "segment_count": 3,
 "segments": [{"index": 1, "sha256": "…", "bytes": 140844, "duration_ms": 2933,
               "audio": "/api/v1/speech/sp-…/segments/1/audio"}],
 "audio": null, "error": null, "links": {"self": "…", "audio": "…", "events": "…", "cancel": "…"}}
```

Any other path or method under `/api/v1/speech` answers `404 not_found` or
`405 method_not_allowed`; the speech route owns the whole prefix, so no other route sees a
request that skipped the global token check.

States: `queued` → `running` → `succeeded` | `failed` | `cancelled`; a succeeded job becomes
`expired` once its audio is deleted. `segment_count` is known once the job runs; `segments`
lists the sentences already rendered, in order. `audio` (the joined file) is present once the
job has succeeded. `error` is `{code, detail}` for `failed`; the codes are
`runtime_unavailable` (no runtime for the PC, or it cannot start), `render_failed`,
`render_timeout`, `queue_timeout`, `voice_release_changed` and `internal_error`. `detail` is
one fixed sentence per code: what went wrong in particular (a path, a release's mismatch, a
worker's error) is in the job runner's log on the PC, never in a
status or a webhook. A job that fails or is cancelled part-way keeps the segments already
published until retention.

### `GET /api/v1/speech/{id}/segments/{n}/audio`

One finished sentence (`n` from 1), as soon as it is published: `200` with `audio/wav`
(24 kHz, mono, 16-bit), `Content-Length` and `ETag: "<sha256>"`, or `206` for a single
`Range` (ranges as for the joined audio, below). `409 audio_not_ready` while it is not yet
rendered, `410 audio_expired` after retention, `404` for an index beyond `segment_count`.
`HEAD` answers the headers.

### `GET /api/v1/speech/{id}/events`

A per-job server-sent event stream (`text/event-stream`), for a caller that wants each
sentence the moment it is ready rather than polling:

```text
id: 1
event: segment.ready
data: {"index": 1, "sha256": "…", "bytes": 140844, "duration_ms": 2933, "audio": "…/segments/1/audio"}

id: end
event: speech.succeeded
data: {…the job's status…}
```

Every published segment is sent once, in order; `Last-Event-ID: <n>` (1 to 4 ASCII digits;
anything else starts from the beginning) resumes after segment `n`. The stream ends with one terminal event (`speech.succeeded`, `speech.failed`,
`speech.cancelled` or `speech.expired`) and is closed. A comment line is sent every 15 seconds
while nothing happens, and the stream is closed after 10 minutes in any case (reconnect with
`Last-Event-ID`; oauth2-proxy's upstream timeout is 600 s). A caller holds at most four
streams at once; a fifth answers `429 too_many_streams` before the stream starts (each holds
one of the API's threads; close one, or poll). It carries no audio and no text.
This is a job's event stream, not a synthesis endpoint: it only reports files the job runner
has finished.

### `GET /api/v1/speech/{id}/audio`

The whole reply, the segments joined in order (their PCM concatenated, no cross-fade), only
when `succeeded`: `200` with `audio/wav`, `Content-Length` and `ETag: "<sha256>"`, or `206`
for a single `Range: bytes=…`; `416` with `Content-Range: bytes */<size>` when that one range
cannot be satisfied. Several ranges, another unit or a malformed header are ignored and the
whole file is answered `200` (RFC 9110 allows it). `409 audio_not_ready` before,
`410 audio_expired` after retention, `404 speech_not_found` for a job that is not the
caller's. `HEAD` answers the headers.

### `POST /api/v1/speech/{id}/cancel`

`202` with the job's state. A queued job is cancelled at once. A running job is cancelled by
the job runner between two sentences (at most one sentence's render later, without unloading
the model), and ends `cancelled` (its webhook names that state). A job that has ended answers
`409 already_finished` with its state. Cancel and the runner's claim race on one exclusive
claim file, so exactly one of them owns the job.

## 7. Execution

**State on disk** (`/data/voice-lab/state/speech/`, `voice-lab`, 0700 directories, 0600 files):

```text
jobs/<id>/request.json        the accepted request, without the text (digest and length only)
jobs/<id>/input.json          the text; deleted when the job ends
jobs/<id>/claim               created exclusively by whoever owns the job (runner, or a cancel)
jobs/<id>/cancel              a cancel request for a running job
jobs/<id>/status.json         the job's state and its published segments, replaced atomically
jobs/<id>/segments/0001.wav   each finished sentence, published by rename when complete
jobs/<id>/speech.wav          the joined audio, until retention removes it
queue/<id>                    empty; the job runner looks for these
idempotency/<caller>/<sha256 of the key>.json
text-digest.key               32 random bytes, made once (0600): the key of the text's and the
                              request's digests (HMAC-SHA256); never in the asset root, so
                              never in the vault. Made only on a new install (no job
                              record, no ledger entry); never made again or replaced
                              automatically after that (the log says why, once per
                              change; deploy/speech/README.md, "The text key")
.runner.lock                  held by the job runner for each pass
.runner.instance.lock         held by the one runner process (the unit, or a pass by hand)
outbox/<event id>.json        a webhook event waiting for the delivery process
webhooks/<event id>/          an event being delivered, with its delivery log
resident/                     the resident model's private scratch (a converted reference
                              copy); removed when it unloads, and at start
cache/                        the resident model's caches (MIOpen's kernel database);
                              no private data
```

The ledger lives in the asset root (§9); the audio does not, so the vault never sees it.

**Sentences and seeds.** The job runner splits the text into sentences (after `.`, `!`, `?`,
`…` and their CJK forms followed by a space or the end, and at line breaks); a fragment under
12 characters joins the next sentence, and a sentence too long for one pass of the release's
model is split at its last comma, semicolon or colon within the limit, else at a space. The
limit is the family's (D-048): for F5-TTS upstream's limit from the reference clip, its text's
bytes per second times 22 s less the clip's length. The job's seed is derived from the text's
keyed digest, the release's digest and the digest of the render settings (for F5-TTS the flow
steps); each sentence's seed from the job's seed and its index. Sentences render one after another, each
seeded just before it renders, never through F5-TTS's thread pool, so a rerun of the same
text with the same release and steps is byte-identical, sentence by sentence.

**The job runner.** `voice-lab-speech.service`, a long-running host unit as `voice-lab` with
`PrivateNetwork=yes` and only the GPU's devices (`/dev/kfd` and the DRM nodes, groups
`render` and `video`), writing only the speech state, the GPU slot's lock and the ledger's
directory. It looks for queued jobs every quarter second. For each, oldest first, it claims
the job; re-reads the release, refuses one of another subject and re-hashes what it names;
holds the GPU slot; splits the text; renders each sentence through the narrow `SpeechBackend`
port, checking before each one that the release is still the current one (so a new
designation stops a render at its next sentence); checks each WAV (24 kHz, mono, 16-bit, complete) and publishes it
(`status.json` lists it); joins them; writes the ledger entry, the final status and the outbox
event. The text is deleted on every ending, first. It sweeps retention every few minutes. A
cancel is honoured between sentences, and ends a wait for the GPU slot at once.

Every error ends the job with a code (`internal_error` when nothing more precise fits); the
particulars go to the unit's journal.

**The ledger and what is served.** Each sentence is served as soon as it is published, while
the job renders and before its ledger entry exists: that is what serving sentences during a
render means. Once a job has ended, nothing of it is served without its ledger entry: when the
entry cannot be written the job fails `internal_error` and its published segments are
unlisted and deleted; and a job that a killed runner left `running` without an entry has its
segments withdrawn the same way when the next pass recovers it (a job whose entry was written
before the kill keeps them). A runner that stops leaves nothing owned for ever: at its next
start (and every pass) a job it left `running` ends `cancelled` when a cancel asked for it and
fails `internal_error` otherwise, and a job it had claimed but not started is queued again, or
ends `cancelled` when a cancel asked for it, or `queue_timeout` once its queue time is up.

**Stopping.** A stop (SIGTERM, `systemctl stop`) is honoured between jobs, while a job waits
for the GPU slot (the job is queued again) and between sentences: the job in hand ends
`failed internal_error` with its ledger entry and the sentences it published. So a stop waits
for at most the sentence being rendered, which the unit's `TimeoutStopSec=150s` covers at the
default `segment_deadline_seconds`; only a kill leaves a job `running` for recovery. A cancel's claim the API never finished is
ended `cancelled` after a minute.

**Deploys.** The unit runs `/opt/voice-lab/current/bin/voice-lab … --release-link
/opt/voice-lab/current`. At the start of every pass and before it claims each job, with a
model loaded or not, the runner checks that its own code is still in the release that link
points at; when a deploy has moved it, the job being rendered (if any) has finished, and the
runner unloads the model, gives back the slot and any job it claimed but had not started, and
exits 75, and systemd starts the new code (deploy/speech/README.md, "Deploys and the runner").
A link that is missing, dangling or unreadable is not a deploy, and a restart cannot fix it:
the runner gives back what it holds the same way, logs "stopping, not restarting" and exits 78,
which the unit's `RestartPreventExitStatus=78` leaves stopped until the link is fixed and the
unit started. While a pass by hand runs, a starting unit logs once that it waits for it.

**One runner.** The unit holds `.runner.instance.lock` for its life. `voice-lab speech run
--config … --once` (one pass by hand, then unload) is refused at once (exit 2) while the unit
runs, rather than wait for the GPU slot the unit's resident model holds; a unit that starts
during a pass by hand waits for it.

**The resident model worker.** The real backend (F5-TTS's; each family has its own, D-048)
keeps one model process in the runtime's interpreter (`f5_resident.py`, D-004's isolation) with the F5-TTS model, the vocoder and the
current release's checkpoint and reference clip loaded. Requests and answers go over its
stdin and stdout as JSON lines, the text included, never through a file. It is started for the
first job, so the first job after an unload pays the cold start (about 20 s), and stopped:

- after `idle_unload_seconds` (15 minutes) without a job;
- when the current release (checkpoint or reference) changes;
- when a long job wants the GPU slot and no speech is queued (below);
- when a sentence exceeds `segment_deadline_seconds` (2 minutes), which fails the job
  `render_timeout`;
- when the job runner stops.

It needs about 1.3–3 GiB of the card's 16 GB; the PC's llama-swap shares the card, so the two
must fit together.

The existing `voice-lab-jobs` unit is the journey watcher (CPU, no GPU devices) and is
unchanged; speech gets its own unit so that its devices and residency do not widen the
watcher's.

**The GPU slot.** One slot, shared with every GPU job (the model comparison's benchmarks now,
D-049; fine-tuning, auditions and Speak when phase 5 brings them to the PC): an exclusive lock
on `state/jobs/.gpu-slot.lock` and a holder record `state/jobs/gpu-slot.json` naming the kind
(`speech`, `benchmark`, `finetune`, `audition` or `speak`), the job, when it started and its
expected end. The rules:

- Nothing preempts a holder.
- The resident speech worker holds the slot (kind `speech`) while its model is loaded.
- A long job (every kind but `speech`) that wants the slot writes `state/jobs/gpu-slot.wanted`
  (`GpuSlotWant`: its holder, its process id, the boot it was written in, and when its wait
  for the lock ends) and waits for the lock. The speech worker yields (unloads and releases
  the slot) as soon as no speech job is in hand and none is queued; queued speech goes first,
  so a long job waits for the speech queue to drain, never for the idle timeout. While the
  marker is there and the queue is empty, speech does not take the slot again. A marker whose
  process is gone, that was written before the host last booted, or that is past its wait
  plus a minute is stale: the runner removes it and logs why, so a killed job never keeps
  speech off the GPU (one that cannot be read counts for an hour from its last change).
- A benchmark (`voice-lab families record --benchmark`, kind `benchmark`) renders a run
  through the same `SpeechBackend` port as the runner: `prepare`, `render_segment`,
  `peak_memory` (the most accelerator memory the loaded model used and how the backend
  measured it; F5-TTS's worker reports torch's `max_memory_allocated`, `None` where it cannot
  tell), and `unload`. It runs in its own oneshot unit with the runner's fences
  (`voice-lab-speech-benchmark.service`), with its own resident scratch under
  `state/speech/benchmark/` (docs/MODEL_FAMILIES.md).
- While a long job holds the slot, `POST` answers `503 gpu_busy` with `Retry-After` from the
  holder's expected end (10 minutes when it gives none), so an assistant answers in text rather
  than waiting hours. Speech already queued waits, and fails `queue_timeout` if it has not
  started within `queue_timeout_seconds` (10 minutes): a stale reply is worth nothing.

**No ROCm runtime yet.** The real backend takes its runtime from the configuration. When the
runtime root, its interpreter or its pinned weights are missing, the runtime probe fails, or
the resident worker cannot load, the job fails `runtime_unavailable` with a plain message; the
runner does not crash and the next job is tried. The PC's hash-locked ROCm runtime lock is a
follow-up to spike S-2 (D-046).

## 8. Webhooks

**When.** Once, when a job reaches `succeeded`, `failed` or `cancelled`, if it has a
`callback_url`. Never per segment (the event stream is for that), and none for `expired`.

**The event** carries no text and no audio:

```json
{"type": "speech.succeeded", "timestamp": "2026-10-11T12:00:07Z",
 "data": {"id": "sp-…", "state": "succeeded", "client_ref": "…", "segment_count": 3,
          "audio": {"sha256": "…", "bytes": 581644, "duration_ms": 12116, "format": "wav"},
          "error": null, "links": {"self": "…", "audio": "…", "events": "…", "cancel": "…"}}}
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
30 seconds for retries). The callers' secrets are root-only files that systemd hands to this
unit alone (`LoadCredential=`); it reads a caller's as
`$CREDENTIALS_DIRECTORY/speech-webhook-secrets_<file name>`. Each attempt, before connecting:

1. re-reads the configuration; a caller no longer registered, or a URL that no longer matches
   the caller's allowlist or is no longer a valid tailnet webhook URL, ends the event
   `refused`, never sent and never retried (the registry is the owner's word);
2. reads the caller's secret: one missing, unreadable or malformed is retried like a failed
   attempt (a secret being provisioned or rotated comes back);
3. resolves the host and refuses unless every address is a tailnet address
   (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), then connects to the address it checked,
   verifying TLS for the host name, so a DNS change between check and connect cannot redirect
   it;
4. sends one `POST`, follows no redirect, and reads the answer's status line (at most 4 KiB),
   discarding the rest. An attempt has 5 seconds to connect and 10 seconds in all: each send
   and receive gets only the time that is left, so a peer that answers a byte at a time cannot
   hold it longer.

A `2xx` answer delivers; `410` stops; anything else, a timeout, an address off the tailnet or
a missing secret is retried.
Attempts are at 0, 30 s, 2 min, 10 min, 30 min and 60 min after the event: six attempts over an
hour, then the event is given up. Each attempt appends to the event's delivery log: number,
time, status code or error class, duration; never the secret, the signature or the body.

## 9. The render ledger and retention

**The ledger.** `provenance/speech-renders/<job id>.json` (`cole-voice-lab.speech-render/v1`),
written once by the job runner for every job it started rendering: job id, caller id, the
text's keyed digest (HMAC-SHA256 under the install's `text-digest.key`, so a short reply cannot
be found by hashing guesses; a text can be checked against it on the PC) and its length in
code points, release id and its record's digest, the model family (an entry written before
D-050 also names a consent record's id and digest; none does now), the model and runtime
fingerprint (the family, each loaded file's role and digest, the runtime
identity, the device, the family's native sample rate, whether it is seeded, the family's
render settings such as F5-TTS's flow steps, the voice-lab version), the job's seed, the number of sentences and each
published sentence's digest, the outcome, the joined output's digest, size and duration when
there is one, and the queue, render and audio durations. The vault copies it as a
record (a classification rule is added with it); nothing removes it.

**Retention.** The audio is deleted `audio_retention_hours` (7 days) after the job finished,
and the job reads `expired`. A job's directory (status, request, claim) and its idempotency
key are removed `record_retention_days` (30) after it finished. Sentences already published by a
job that later failed or was cancelled are kept until the same retention. The job runner
sweeps every few minutes.

## 10. Measured and unknown

Measured on 2026-10-11 by spike S-2 on the PC (RX 6900 XT, `gfx1030`, ROCm), with F5-TTS
1.1.22, the pinned base weights and F5-TTS's public English reference clip; the figures and
the evidence digests are in D-046:

- warm real-time factor about 0.70–0.78 at 32 flow steps and 0.35–0.39 at 16 (fp16, native);
- one 15-word sentence: 6.2 s at 32 steps, 3.1 s at 16. So with sentence-by-sentence
  delivery, first audio arrives about one sentence's render after the job starts
  (roughly 1.5–3 s for a short sentence at 16 steps, twice that at 32);
- 62 words: 23.6 s and 11.8 s; 206 words: 84 s and 42 s: the joined audio of a long reply
  takes minutes, which is why the API is asynchronous;
- a cold start is about 20 s, which the resident worker pays once per load, not per request;
- the CPU is about 19× slower than real time: there is no CPU fallback for this API;
- upstream's threaded chunking is not reproducible and peaks at 7.3 GiB; sequential,
  per-sentence seeds are byte-identical on rerun and peak near 1.6 GiB.

Unknown: the same figures for a release of Cole's own voice. They scale with the reference
clip's length and the speaking rate, so they are measured again once a release exists, and
Cole's 16-versus-32-step listening check decides the default steps (32 until then).

## 11. Deployment and the owner's steps

Written in the repository with the implementation; the stack and door changes are in
`deploy/stack/compose.yaml` and deploy with a merge, the rest is the owner's (the procedure,
with the commands: [deploy/speech/README.md](../deploy/speech/README.md); the host units:
`deploy/host-manual/voice-lab-speech*`). Steps 3 to 5 come before the merge that brings the
speech routes to the stack: the API does not start without a valid configuration.

1. **The runtime.** Provision the ROCm F5-TTS runtime on the PC (torch 2.14.0+rocm7.2,
   torchcodec 0.16.0+cpu, from spike S-2; its hash-locked lock is a follow-up). Until then
   every job fails `runtime_unavailable`.
2. **The voice release.** `voice-lab voice release designate --run <id> --config
   /data/voice-lab/config/speech.json` on the PC. Only Cole; no consent record is needed
   (D-050).
3. **Pocket ID.** Create the caller's confidential client with `client_credentials`; read one
   token's claims locally to fill `subject` and `audiences`.
4. **The configuration and secrets.** Write `/data/voice-lab/config/speech.json`; create each
   caller's webhook secret file (root:root 0600) and give the caller the same secret by a
   private channel.
5. **The door and the stack.** `VOICE_LAB_SPEECH_AUDIENCE` in the stack's `.env`, and
   infra's `host.conf` granting the read-only bind of `/data/voice-lab/config/` into the API
   container (`HOST_BINDS` … `speech-config:/data/voice-lab/config:ro`); the compose file
   already has the `EXTRA_JWT_ISSUERS` entry, `--speech-config` and the bind.
6. **Host units.** Create the ledger's directory, then install and enable
   `voice-lab-speech.service` (long-running, with the resident worker) and
   `voice-lab-speech-webhooks.{path,service,timer}` by hand; through the runner lane's
   host-unit install once infra's validator accepts GPU devices and `LoadCredential=` (which
   would also restart the runner on every deploy; until then it restarts itself).
7. **The tailnet policy.** Allow the PC to reach each caller's node on 443 for webhooks, and
   the caller's node to reach `tag:voice-lab` on 443.

## 12. Not in v1

- Audio streamed while it is being synthesized: only finished sentences are published.
- Ogg Opus (reserved; needs ffmpeg in the job runner's path).
- Any caller outside the tailnet, any audio leaving Cole's own devices, any voice but the
  release, and a watermark (D-046: revisited before audio could reach anyone else).
- Fine-tuning, auditions and Speak through `/api/v1/jobs` (phase 5); they will share the GPU
  slot defined here.
- A second model family. The release, the backend port, the ledger and the configuration's
  runtime are family-neutral (D-048, [MODEL_FAMILIES.md](MODEL_FAMILIES.md)); F5-TTS is the
  only family registered.
