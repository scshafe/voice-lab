# The house panel as a second capture client (D-056)

Issue scshafe/voice-lab#86 (the owner's plan, 2026-10-18). The decision is
[D-056](../../DECISIONS.md#d-056--the-house-panel-is-a-second-capture-client). The panel side
is scshafe/embedded-house-panel#22 (capture, `media_broker`'s purpose `voice_take`) and #23 (the
recording app). This document covers the server and the contract. The desktop app keeps
recording exactly as it does.

## 1. What this rests on

Read on 2026-10-18 from this repository, from the panel's public repository (main at
`e868003`) and from upstream sources.

**Intake today.**

- A take must be `cole-voice-lab.corpus-take/v1`: from a UMIK-1, with backend `portaudio` and
  `PortAudioContinuityEvidence`, canonical mono 48 kHz s24le (`corpus_takes`).
- An imported Swift take has a contract of its own (D-053).
- Schema dispatch happens in `intake.validate_package`, `intake._raw_session_id`,
  `corpus_take_store.record_takes`, `library_replication` (the vault's raw source) and
  `recording_library` (its raw checks).

**Auth today.**

- Every `/api/` route accepts any token whose `aud` meets the policy's audiences (`api/auth`).
  No route is scoped.
- `/view/` (`view/routes`) and the journey intake (`/api/intake/`) call the same
  `verify_header`.
- The door (oauth2-proxy) passes the client's `Authorization` through and forwards the token's
  user.
- A panel token carries **Cole's own subject**. Without a change, it would reach the web view,
  the journey routes and the journey intake.
- The door's `OAUTH2_PROXY_EXTRA_JWT_ISSUERS` is in this repository
  (`deploy/stack/compose.yaml`).

**Pocket ID v2.13.0**, the version the owner runs, read from its source at that tag (fosite
fork v1.2.0).

- **Who gets the device grant.** Every client gets `device_code`; only confidential clients get
  `client_credentials`. The client's PKCE setting applies only to the authorize path, and the
  device flow reads no callback URL. So a public client with PKCE on and no callback URLs
  completes RFC 8628 with `client_id` alone.
- **The endpoints.**
  - `POST /api/oidc/device/authorize` takes `client_id` and an optional `scope`.
  - `POST /api/oidc/token` takes `grant_type=urn:ietf:params:oauth:grant-type:device_code`,
    `device_code` and `client_id`.
  - `verification_uri` is `<APP_URL>/device`; `verification_uri_complete` adds
    `?user_code=E…` (an `E` and seven characters).
  - The polling interval is 5 s and a code lives 15 minutes.
- **What polling answers.** While a code is unapproved the answer is `authorization_pending`.
  `slow_down` is never sent, and the user has no reject path. An unapproved code that has
  expired keeps answering `authorization_pending`, so **the client enforces `expires_in`
  itself**.
- **Zero-click approval of the complete link.** At v2.13, opening `verification_uri_complete`
  while signed in submits the code on load. Once consent has been recorded, that approves the
  device without a click; v2.15 removed this. **Until the owner runs v2.15 or later, the panel
  shows `verification_uri` and the user code, not the complete link**, so an approval is always
  an act of typing the code the panel shows.
- **Access tokens** are JWTs (RFC 9068, `typ: at+jwt`), RS256 with the default key. Their
  claims:
  - `sub` is the user's id and `iss` is `APP_URL`;
  - `exp` is the client's lifetime (15 minutes, for every grant);
  - `client_id`, `scope`, `scp`, `jti` and `iat`; there is no `nbf`;
  - `aud` is **always an array**: `[client_id]`, plus the issuer when an identity scope
    (`openid`, `profile`, `email`, `groups`) is asked for.

  **The panel sends no `resource` parameter**: with one, `aud` names that resource instead of
  the client.
- **Refreshed tokens and `iat`.** A refreshed token probably keeps the first token's `iat`
  (inferred from the source, not run), so no check of a token's age from `iat` applies. Our
  verifier accepts an array `aud`, does not check `typ`, and refuses only an `iat` in the
  future.
- **Refresh tokens.**
  - One is issued without `offline_access`. It rotates on every use and its lifetime slides
    (43,200 minutes from each refresh).
  - Each refresh checks the user again: a user who is disabled or outside the group is refused.
  - **v2.13 has no grace period.** Reusing a rotated refresh token is treated as theft: the
    whole token family is revoked (`invalid_grant`), which forces a new device sign-in. A
    one-minute grace arrived in v2.17.
  - So **the panel refreshes single-flight**, and saves the new refresh token before it uses the
    new access token. A crash between receiving and saving still means a new sign-in.
- **Revocation.**
  - The user's `DELETE /api/oidc/users/me/authorized-clients/voice-lab-panel` (their account's
    authorized apps) deletes the consent, deactivates the client's refresh tokens and deletes
    its access-token rows.
  - The admin can delete the client (its sessions cascade), or disable the user or remove them
    from `cole_default` (the next refresh fails).
  - There is no RFC 7009 revocation endpoint, so an issued JWT stays valid until its `exp`.

**The XVF3800 Linear-4's USB firmware** (reSpeaker_Flex v1.0.4; XMOS user guide v3.2.1).

- There are three images: 16 kHz 2-channel, 16 kHz 6-channel and 48 kHz 2-channel.
- In the 6-channel image, channels 1–2 carry processed audio and channels 3–6 the four **raw**
  microphones (at a lower level). Since v1.0.2 those sources can be set with
  `AUDIO_MGR_OP_CH3`…`CH6`.
- The chip decimates its microphones to **16 kHz**. Every 48 kHz output is **upsampled from
  16 kHz**, so no image carries microphone content above 8 kHz.
- USB audio is 16-bit by default; `USB_BIT_DEPTH` sets 24 or 32.
- The firmware is read with `VERSION` (three bytes), `BLD_MSG` and `BLD_REPO_HASH`.
- The processing switches are `SHF_BYPASS` (AEC), `PP_AGCONOFF` with `PP_AGCGAIN`,
  `PP_MIN_NS`/`PP_MIN_NN`, `PP_ECHOONOFF`, `PP_NLATTENONOFF`, `PP_LIMITONOFF` and
  `AEC_HPFONOFF`.

**The panel** (C++17, nlohmann/json 3.12, libcurl).

- It pins lexinet's `study.v1` by commit SHA plus SHA-256s of the raw files, with no tags and no
  JSON canonicalization. That bundle holds `manifest.json`, `openapi.json` (3.1), `examples/` and
  `examples/invalid/`.
- The panel vendors the fixtures byte for byte, writes `contract-provenance.json`, and runs a
  live check (`make verify-lexinet-live`) outside `make verify`.
- Its HTTP client today is POST-only, with a 3 s budget and a 1 MiB cap on answers. It needs
  `https`, a host-root URL and no redirects. Custom CAs are allowed only for tests on
  `https://127.0.0.1:*`.
- It has no ALSA continuity code yet (#22).
- Its ADR 0010 refused to keep a secret on the panel (for lexinet), so a device-code refresh
  token is new to its threat model.

## 2. The take contract: `cole-voice-lab.corpus-take/v2`

**v2, not a separate import contract.** D-053 needed its own contract because a Swift take
carries another store's records verbatim, together with that store's decisions. A panel take is
a live client's take, with the same lifecycle as a desktop take: a draft on the PC, then
decisions. v2 is a take that **declares its capture profile and carries its backend's evidence**.

- **v1 is frozen.** The desktop app keeps writing it, and every v1 take and every imported take
  is profile `umik-1`.
- **v2 defines one profile, `xvf3800-linear-4`.** The profile is a tagged union, so `umik-1` is
  added to v2 when the desktop moves to it, and not before.

**The same five files**, so the upload, the staging and the publisher carry it unchanged:
`manifest.json`, `qc.json`, `READY`, `original.wav`, `continuity.json`.

**The capture profile** (`manifest.capture_profile`):

- `profile`: `"xvf3800-linear-4"`;
- `device`: the model, the USB vendor and product ids, the serial when it reports one, and the
  firmware (`VERSION`, `BLD_MSG`, `BLD_REPO_HASH`);
- `room`: a slug, required;
- `channels[]`: one entry per WAV channel, giving its source (`processed`, or raw `mic` 1–4),
  the output category it was routed from, and the processing that ran on it;
- `primary_channel`: the channel the take is judged by (QC) and later derived from;
- `settings`: the processing parameters read from the device when the take started.

**The audio is what the firmware gives.**

- `original.wav` holds exactly the frames ALSA delivered: every channel, at the device's rate
  and sample width. The panel does no resampling, no channel extraction and no gain (D-003).
- The profile takes **16 kHz only**, with 6 channels (preferred: the 6-channel image, its
  primary channel a raw microphone) or 2 channels.
- Samples are s16le (the default), s24le or s32le, as `USB_BIT_DEPTH` sets them.
- **48 kHz is refused.** That image's content is 16 kHz upsampled by the chip, so a take would
  claim a band it does not have.
- A take lasts at most 660 s, as a desktop take does. In 6-channel s32le that is about 254 MB.

**ALSA continuity evidence, `cole-voice-lab.alsa-continuity/v1`.** It holds integers only, so
the panel formats no floats in it:

- the PCM opened and its hardware parameters: access, format, rate, channels, period frames and
  buffer frames;
- the timestamp type (`monotonic_raw` preferred);
- the xrun count, which must be 0;
- **one `[position_frames, timestamp_ns]` pair per period**: ALSA's timestamp of the hardware
  pointer's update after each read (`snd_pcm_htimestamp` or the status). USB audio has no link
  timestamps;
- the tolerance and the largest deviation the device measured;
- the frames, and the WAV it describes (name, SHA-256, size);
- the start, the stop reason, success, the error, and whether the source was simulated.

The **PC recomputes** each period's deviation from the pairs. Its figure must equal the device's
and stay within the tolerance, which the PC bounds. The frames must add up to the WAV's. A
minimum period size keeps the file within a bound. The exact rule and bounds are fixed with the
contract (step 2 below) and published in the bundle, so #22 implements them as written.

**The rule and bounds, as built** (step 2, 2026-10-19; `alsa_evidence.py`):

- **Each pair** is `[position_frames, timestamp_ns]`, recorded after every read of one period:
  - the position is the stream's hardware position, the frames read so far plus `avail`;
  - the timestamp is `snd_pcm_status`'s `htstamp` of that position, on the `tstamp_type` clock
    (`monotonic_raw` or `monotonic`).
- **The rule**: for every pair after the first,
  `deviation = (timestamp[i] - timestamp[i-1]) - (position[i] - position[i-1]) * ns_per_frame`.
  - `ns_per_frame = 1_000_000_000 / rate` must be exact (62,500 ns at 16 kHz), so the panel and
    the PC compute the same integers.
  - Each pair is judged against the one before it, as D-039's blocks are, so jitter does not
    accumulate and the device clock's drift does not matter.
  - Every `abs(deviation)` is within `tolerance_ns`, and the largest equals the device's
    `max_abs_deviation_ns` (`null` for a single pair).
- **The bounds**:
  - `tolerance_ns` from 1 to 4,000,000 (4 ms, D-039's 64 frames at 16 kHz);
  - periods of 256 to 16,384 frames, so a lost period (at least 16 ms) is never within the
    tolerance;
  - a buffer of at least two periods and at most 2^20 frames;
  - at most 41,250 pairs (660 s at 16 kHz in periods of 256), and a file of at most 2 MiB.
- **A successful take**:
  - no error and no xrun;
  - its frames are its periods' frames (`len(periods) * period_frames`), and at most 660 s;
  - the last position is those frames plus at most one buffer;
  - its WAV (`original.wav`) is a plain 44-byte PCM header and its frames: `44 + frames *
    channels * width`, where `format` gives the width (`S16_LE` 2, `S24_3LE` 3, `S32_LE` 4) and
    the WAV's encoding byte for byte;
  - its start (UTC) and its stop reason are present.

  An unsuccessful take names its error.
- **The profile's fields**:
  - each channel gives:
    - `source` (`processed` or `mic`);
    - `mic` (1 to 4, for a raw microphone only);
    - `routing`, the `AUDIO_MGR_OP_*` pair (category, source) as read;
    - `processing`, the stages that ran, by the switches above (none on a raw microphone);
  - `settings` maps every one of the nine parameters above to the values read from the device;
  - the firmware's `version` is `VERSION`'s three bytes.

  These are published in the bundle (step 5); the panel can ask for a change before step 4,
  since nothing can upload a v2 take until then.

**Validation and admission on the PC.**

- Validation is as strict as v1's: the exact file set, no links, canonical JSON, every digest and
  byte count.
- The WAV's format equals the manifest's and is one the profile allows, and its channels equal
  `channels[]`.
- The evidence is bound to the WAV and successful, and the device is the Flex Linear-4: Seeed's
  USB vendor 0x2886 and product 0x0022, as the panel's array reports it (measured on the panel
  and relayed on 2026-10-19).
- Then the PC's rules: no simulated audio, no diagnostic root, and the catalog rule
  (`admit_catalog`, unchanged).
- **QC is recomputed on the server from the primary channel** (a channel-selecting inspection in
  `audio.py`), with the thresholds of a desktop take. The buffer-boundary and short-zero
  heuristics are tuned to the canonical format and stay limited to it; the ALSA evidence judges
  continuity instead.
- The raw microphones are quiet, so expect more `review` than `pass`. That is reported, not
  relaxed.
- The client's `qc.json` stays informational.

**Records on the PC.**

- The take record (`corpus-take-state/v1`) gains an optional capture-profile summary: profile,
  room, device model, firmware and primary channel. It is absent for a `umik-1` take, so every
  existing record keeps its bytes (`origin`'s precedent, D-053).
- Its audio may be any format the profile allows. A record without a profile must be canonical.
- The vault's raw source binds v2's files, and `library_classification` classifies every new
  path in the PR that first writes it (D-052).

**Training.**

- Dataset selection is **per profile, `umik-1` by default**.
- Nothing trains on the PC's corpus before phase 5 (D-051); this is the rule that phase follows.
- A run that includes panel takes names the profiles in its corpus record, so `families compare`
  flags it as another corpus and can test whether panel takes help.
- A family's training audio is derived from the primary channel and resampled to the family's
  rate. That is derived audio, written elsewhere.

## 3. Device sign-in

**The client**, created by the owner on 2026-10-18 on Pocket ID v2.13.0:

- a public client, custom id `voice-lab-panel`, no secret;
- PKCE on, no callback URLs, and the consent screen on;
- allowed for the group `cole_default` only;
- access tokens last **15 minutes**, refresh tokens 43,200 minutes (30 days).

Its discovery document lists RS256 and the grant `urn:ietf:params:oauth:grant-type:device_code`,
with `device_authorization_endpoint` `https://id.<tailnet>/api/oidc/device/authorize`. The
tokens' `sub` is Cole's user id, so takes stay under `cole-self` and the API's allowed subject
does not change.

**The door and the API.** The door gains `${OIDC_ISSUER}=voice-lab-panel` in
`EXTRA_JWT_ISSUERS`, in this repository, with no new `.env` value. The API gains
`--client-audience voice-lab-panel`.

**Scopes, fail-closed.**

- A verified principal carries a scope: `full` for the desktop audience, `corpus-client` for the
  panel's. A token that names any scoped audience is scoped.
- `verify_header` keeps its meaning, *full access only*. So every existing caller refuses a panel
  token without being changed: `/view/`, the journey routes, the journey intake. The speech
  routes have their own verifier and callers (D-046).
- Only the server's `/api/v1` check accepts `corpus-client`, and only on this allow-list. Every
  other route answers `403 client_scope`.

| Route | For the panel |
| --- | --- |
| `GET /api/v1/meta` | yes |
| `GET`/`HEAD /api/v1/catalog` | yes |
| `GET`/`HEAD /api/v1/corpus/library?profile=xvf3800-linear-4` | its profile only; no audio |
| `GET`/`HEAD /api/v1/corpus/prompts/{id}/takes?profile=xvf3800-linear-4` | its profile only; no audio |
| `/api/v1/intake/sessions…` (open, status, `PUT` a file, finalize, receipt) | finalize refuses `422 take_profile_not_allowed` unless the package's manifest is a v2 take of the panel's profile |
| `POST /api/v1/corpus/takes/{id}/decision` | only on a take of the panel's profile |
| `GET /api/v1/corpus/requests/{id}` | yes |
| take audio, trims, journey, `/view/`, speech | refused |

Finalize reads the package's manifest from the API's own `incoming/`, so a panel token cannot add
audio to the `umik-1` corpus that training uses by default.

**D-021 on a device without a browser.** The standing approval stays with the capturing host,
as on the desktop.

- The panel makes the approval on its own screen: endpoint, node, tag, expiry and digest, then a
  confirm.
- It reads the PC's stable node id and `tag:voice-lab` from its own `tailscaled` (status and
  whois) and keeps the approval at mode 0400.
- Before every upload session it checks the node again.
- It keeps the hash-chained ledger: `upload-started`, naming the manifest digest, before the
  first byte; then the outcome.
- The server does not carry the binding: it cannot vouch for its own identity to the client,
  which is what the approval checks.
- The bundle ships these records' schemas and examples.

**The refresh token on the panel.**

- One file, mode 0600, owned by the uploader's user, in a 0700 directory on persistent storage.
  It is never in the image and never logged.
- It is read with the panel's existing rules: `O_NOFOLLOW`, a regular file, owner-only, 1–4096
  bytes.
- Pocket ID rotates the token on every refresh, with no grace period at v2.13, and revokes the
  whole family when a rotated token is reused. So refreshes are single-flight, and the new
  token is written atomically (temporary file, fsync, rename) before the new access token is
  used.
- Access tokens stay in memory. Signing out deletes the file.
- The panel's sign-in screen shows `verification_uri` and the user code, never a QR code of the
  complete link (see §1). It enforces `expires_in` itself, and it sends no `resource`.

**Revocation.**

- Cole revokes `voice-lab-panel` under his Pocket ID account's authorized apps, or the admin
  deletes the client, disables the user, or removes the user from `cole_default`.
- An issued access token lives at most 15 minutes.
- Removing `--client-audience` (a deploy) or the tailnet grant cuts the panel off at once.
- A lost panel holds no other credential of the lab's.

**The tailnet.** Both grants are in place:

- `tag:house-panel` reaches `tag:voice-lab` on 443 (infra #19);
- `tag:house-panel` reaches the Pocket ID node on 443 through a `pocket-id` host alias (infra
  #21; the node carries `tag:container`). The device grant and every refresh go there.

## 4. The contract bundle: `contracts/corpus-client.v1/`

The bundle is shaped like lexinet's `study.v1`, so the panel's planned general pin tool takes it
as it is.

- **`manifest.json`**:
  - `manifest_version`, `contract_id: "corpus-client.v1"`, `schema_version` and
    `openapi_path`;
  - the ordered `required_semantics[]`;
  - `examples[{path, schema, expected}]`;
  - beyond lexinet's, `files[{path, sha256, size_bytes}]` for every other file of the bundle.
- **`openapi.json`** (3.1): the allowed routes, with their bodies, answers and refusal codes,
  and an `x-voice-lab-contract` block. Its components are the generated JSON Schemas.
- **`schemas/`**: the package's records (the v2 manifest, `QcReport`, `ReadyMarker`, the ALSA
  evidence) and the D-021 records (approval, binding, ledger entry).
- **`examples/` and `examples/invalid/`**, with their exact bytes:
  - Package records are canonical JSON followed by `\n`. Each invalid example names its refusal
    code.
  - A golden take package whose `original.wav` is **given by an integer recipe**
    (`original.wav.recipe.json`, with the resulting SHA-256 and size). The repository holds no
    audio file (`scripts/guard-private-assets`), and integer arithmetic gives the same bytes in
    Python and C++.
  - Canonical-JSON vectors, floats included.
- **`PROTOCOL.md`**:
  - sign-in, and the approval and ledger;
  - the intake: declare; send chunks with `PUT` and `Content-Range`, resuming from the session's
    status; finalize; poll for the receipt, a refusal or a deferral;
  - decisions with stable request ids, and the catalog's and library's `ETag`s;
  - which refusals to retry, the chunk sizes (the server takes at most 64 MiB; 4–8 MiB is
    recommended), and the timeouts (finalize re-hashes the package);
  - **the canonical form the panel writes byte for byte**: keys sorted by code point, compact
    separators, UTF-8 unescaped, Python's float repr, and a final `\n` in files. nlohmann's
    `dump()` agrees with it for the values these records hold. Floats are best formatted with
    `std::to_chars` and Python's rule for fixed or exponent notation; the vectors catch any
    difference.
- **The generator**: `python -m cole_voice_lab.contract_bundle write|check`. `scripts/verify`
  runs `check`, which fails on drift, as it does for `schemas/`.
- **Pinning**: as for lexinet, the panel pins the commit, `manifest.json`'s SHA-256 and the
  bundle digest (each sorted path hashed as `path NUL contents NUL`). No tags are needed.
- **The fixture server**, `python -m cole_voice_lab.contract_fixture serve --root <empty dir>
  --port 0 [--tls-cert … --tls-key …] --report <file>`:
  - It runs the real API server (staging, routes, the scoped verifier).
  - It runs the real validator, admission and publisher pass in-process, after each finalize and
    each decision.
  - It seeds a fixture catalog through the composition root, as `voice-lab catalog` does.
  - It includes a fake Pocket ID that behaves as v2.13.0 does (§1):
    - device authorization, and polling answered `authorization_pending` with no `slow_down`;
    - tokens with an array `aud` and `typ: at+jwt`;
    - refresh rotation with no grace, where reusing a rotated token revokes the family;
    - a JWKS. Its RS256 key is made at start with the standard library, so no key is in Git.
  - It admits simulated takes through a switch of its own, which no production entry point has.
  - It runs from a pinned checkout in a venv (the API image's hash-locked closure) or from the
    API image built at that commit. The panel's live check starts it, as its lexinet check
    starts lexinet.

## 5. Two clients

- **No prompt reservation.** Each take is unique by its audio's digest. With one speaker,
  duplicates are fine, and decisions resolve them.
- **One corpus, seen per profile.**
  - `GET /api/v1/corpus/library` and a prompt's take history answer the `umik-1` view by
    default, byte for byte today's answer. The desktop never sees panel takes, and the installed
    builds need no update.
  - `?profile=xvf3800-linear-4` answers the panel's view: its statuses, its coverage, its
    "Continue Missing".
  - In each view, the newest kept take of the profile stands for a prompt (D-016's rule, per
    profile).
- Decisions from two clients on one take are already resolved by revision (`corpus_requests`).

## 6. Steps

One pull request at a time. Each waits for CI, merges with `Merge #N: …`, and has its deploy
watched.

1. **D-056 and this document.** Docs only.
2. **The v2 take contract**:
   - the capture profile, the ALSA evidence and channel-aware QC;
   - the validator, admission, and the take record's optional profile;
   - the intake and publisher dispatch, the vault's raw source and classification, and the raw
     checks;
   - the schemas and tests.

   No token can upload a panel take yet, so this deploys inertly. Built in two PRs
   (2026-10-19): first the contracts, the ALSA rule and the channel-selecting inspection, which
   nothing dispatches to (a v2 package is still refused as an unknown capture package, so none
   reaches `raw/`); then the validator, admission, the dispatch at every point, the record's
   profile and the vault. Until step 3, the library's default view counts a published v2 take
   as it counts any take; no client writes one until the panel can sign in (step 4).
3. **Per-profile library views**: the read-model files, `?profile=` and their classification.
   The default answer stays byte-identical. As built (2026-10-19):
   - `derived/corpus/library-<subject>.json` stays the default view (`umik-1`, every take
     without a profile), and `library-<subject>-xvf3800-linear-4.json` is the panel's. Every
     refresh writes both (each only when it changed), so the panel's view exists before its
     first take. The read model's vault rule (`corpus-read-model`, Mac-only) covers both.
   - The coverage counts the view's takes only.
   - The panel's answers name their `profile`; the default view's omit it.
   - `?profile=umik-1` answers the default view; any other query is refused, 400
     `bad_request`, where before it was ignored.
4. **The panel's audience**:
   - the fail-closed scopes and the allow-list;
   - the finalize and decision profile checks;
   - `--client-audience`, and the door's `EXTRA_JWT_ISSUERS` entry;
   - SECURITY.md;
   - tests that refuse a panel token on `/view/`, `/api/intake/`, the journey routes, speech, take
     audio and trims.

   Deploying this before the Pocket ID client exists is harmless. As built (2026-10-19):
   - `TrustedIssuer.client_audiences` and `Principal.scope` (`api/auth.py`);
   - `verify_any_header` for the server's `/api/v1` check alone;
   - the allow-list in `api/client_scope.py`;
   - the finalize check in `api/staging.py`, after the files are re-hashed;
   - the decision check in `api/corpus_request_routes.py`. A take whose record is not written
     yet answers 404 `take_not_found`, and the panel asks again once intake has published it;
   - `deploy/stack/compose.yaml` holds the door's third entry and the API's flag.
5. **The contract bundle and the fixture server**: the generator, `check` in verify,
   PROTOCOL.md and the examples, and tests that drive the fixture through the golden package.

## 7. Outside this repository

- **Done by the owner and infra on 2026-10-18**:
  - the Pocket ID client (§3), with its version checked and RS256 in discovery;
  - the grants `tag:house-panel` → `tag:voice-lab` on 443 (infra #19) and → the Pocket ID node
    on 443 (infra #21).
- **Still to do**: enrolling the panel's node as `tag:house-panel`, which the owner gates on the
  panel's side.
- **Optional, the owner's call**: Pocket ID v2.15 or later ends the zero-click approval of
  complete links, and v2.17 or later adds the refresh grace period (§1). Neither is required:
  the panel's rules above hold on v2.13.
- **On the panel, once it runs**: its D-021 approval on its screen, and the first device-code
  approval from Cole's phone.
- **The panel's repository**, for its own agent (a comment on scshafe/embedded-house-panel#23):
  - its threat model for a stored refresh token (ADR 0010 refused secrets for lexinet);
  - `alsa-continuity/v1` in #22;
  - an HTTP client that sends `PUT` with `Content-Range` and allows longer timeouts.
- **No runner changes.** Deploys use the existing lane. The fixture runs on the panel's
  GitHub-hosted CI or on a workstation.
