# Moving Voice Journey to Lubuntu, with a native capture client

Design, 2026-10-03. Owner decisions are marked **(owner)**. This keeps the
[Breathing Studio](breathing-studio-plan.md): the loop, its stages, both capture
chains and the privacy rules are unchanged. What changes is where each part runs
and how takes reach the server.

## Why

Today everything runs on the Mac mini. That includes the corpus browser and its
watcher, the whole pipeline, and live capture (`capture.mjs`: ffmpeg over
AVFoundation plus `osascript`). It all lives in the checkout
`~/.mission-control/projects/voice-journey`, under three launchd jobs. Updates are
a manual pull plus a `launchctl kickstart`, and only the Mini can serve it.

The owner's decisions (2026-10-03):

- **Server:** move it to cole-lubuntu-laptop as a normal infra stack, deployed
  automatically on merge (lane `autodeploy`).
- **Corpus:** the Mini mirrors the Voice Memos corpus to the server. It is the
  only machine that can read it (a TCC-protected, iCloud-synced group container).
- **Capture:** replaced by a native client app, built on `@scshafe/qt`
  (PySide6 + the `Scshafe.Ui` QML module), not Swift. It runs on whichever
  machine has the microphone and talks to the server.

## Target architecture

```text
 iPhone ─Voice Memos─▶ iCloud ─▶ Mini (TCC group container)
                                   │  vj-mirror (FDA launcher, launchd, every 15 min)
                                   │  rsync over the tailnet, write-only drop
                                   ▼
 any client ── capture app (Qt) ──HTTPS + OIDC──▶  Lubuntu: infra stack `voice-journey`
   UMIK-1 / chain eras              │                 ├─ door (oauth2-proxy, Pocket ID)
   session WAVs + session.json      │                 ├─ app: corpus browser + watcher + intake API
   checksummed package, upload      │                 │      ffmpeg, whisper.cpp, praat/librosa
                                    ▼                 ├─ ts-voice-journey (tailnet node voice-journey)
                           https://voice-journey.<tailnet>   └─ data volume: corpus/, artifacts/, manifests/
```

### Server: the `voice-journey` infra stack on Lubuntu

- **Image (voice-journey repo, `Dockerfile`):** Node 24, the SPA built at image
  time, ffmpeg, whisper.cpp (CPU build), the pinned Python venv
  (praat-parselmouth, librosa) and the whisper model.
  - Tool paths come from the existing environment overrides (`FFMPEG_BIN`,
    `WHISPER_CPP_BIN`, `WHISPER_CPP_MODEL`), not Homebrew defaults.
- **Process:** one service runs the corpus browser and the watcher, as today
  (Phase D decision 1). Since the watcher no longer needs TCC, the in-process
  design stays for simplicity, not necessity.
- **Data root** (`VOICE_JOURNEY_DATA`, a volume under the stack):
  - `corpus/phone/`: the mirror, read-only to the app.
  - `corpus/measurement/`: accepted capture sessions.
  - `artifacts/`: today's `local-artifacts/`.
  - `manifests/`: generated manifests. Which of today's committed manifests are
    inputs (chains, goals) and which are outputs is decided in phase 1.
  - The app never writes into its source tree.
- **Stack** (infra `stacks/voice-journey/`, modelled on wedding-planner and
  mail-otp):
  - `compose.yaml`: app, door and tailnet sidecar;
  - `serve.json`;
  - `backup.conf`: the data root's files lane; the mirror is re-creatable but
    sessions are not;
  - `deploy.conf`;
  - `check.conf`.
- **Identity:** the existing Pocket ID client and the `voice-journey` hostname
  move with it. The tailnet node's tag becomes `tag:container`, like every app
  sidecar. Binding stays loopback plus tailnet only, never `funnel`.
- **Deploys:** lane `autodeploy`. It needs an mc-autodeploy enrollment, plus
  `dev.toml [deploy]` set to lane `autodeploy`, stack `voice-journey`, host
  `lubuntu`. A merge redeploys after its tests pass; the watcher re-arms on
  restart, because every stage is resumable.

### Corpus mirror: Mini to Lubuntu

- **On the Mini:** `vj-mirror`, a small script run by launchd every 15 minutes
  through the FDA-granted launcher. The launcher gains a `mirror` mode, so no
  second Full Disk Access grant is needed.
  - It copies new and changed recordings, plus the Voice Memos metadata
    database the index reads, through the existing read-only seam.
  - It never writes to the container.
- **Transport:** `rsync` over SSH to Lubuntu.
  - The Mini's key is restricted on Lubuntu with `rrsync -wo`, rooted at the
    stack's `corpus/phone/`. This is the backup chain's pattern turned around:
    write-only, so the Mini cannot read the server.
  - The drop account is a dedicated `vj-mirror` (forced command
    `rrsync -wo -no-del -munge`, rooted at `/srv/voice-journey/corpus/phone/`),
    not the owner's account. No tailnet grant is needed: the broad tailnet grant
    already allows Mini to Lubuntu on 22.
- **Deletions:** a recording deleted on the phone is not deleted on the server.
  It is kept and marked by the index. History is the point of the corpus.
- **Integrity:** per-file size plus mtime, plus a checksum manifest per run. The
  watcher's existing debounce (stable file set and sizes across two scans)
  absorbs partial transfers.

### Capture client: native, `@scshafe/qt`

A new repository (name **(owner)**, proposed `voice-journey-capture`), a
scshafe-dev `native` project. It is a PySide6 app with the `Scshafe.Ui` QML
module, and packaging follows scshafe-qt (wheels or a `uv` tool install).

- **What it does:**
  - **Devices and levels:** it lists and chooses the device (UMIK-1 and chain
    eras from the server's chain registry). It pins and logs the input level, and
    refuses to record if the level moves (the Phase A guard). The level pin uses
    CoreAudio on macOS and PipeWire on Linux.
  - **Recording:** it runs the segmented ritual (silence, cal tone, /a/ soft and
    loud, glide, anchor song, free) and records 48 kHz/24-bit WAV, with live meters.
  - **Packaging:** it writes `session.json` (chain id, level, distance, cal-file
    hash, segment map), plus SHA-256 for every file.
- **Upload** to the server's intake API (new, in voice-journey):
  1. `POST /api/intake/sessions` sends the manifest;
  2. `PUT` sends each file, resumable;
  3. `POST …/finalize`.

  The server verifies every checksum and accepts the session into
  `corpus/measurement/`, or nothing (fail-closed, voice-lab's intake rule). The
  watcher then ingests it like any take.
- **Auth:** the app signs in through Pocket ID: OIDC authorization code with
  PKCE, on a loopback redirect. It sends the access token as a bearer, which the
  door accepts (oauth2-proxy `--skip-jwt-bearer-tokens` with the Pocket ID
  issuer). The app reads only the door's identity headers.
- **Offline:** sessions are kept locally until accepted and retried on the next
  launch. Raw audio never leaves the machine except to the server.
- **Server address:** the client asks the server where to send takes (`arch` or
  `lubuntu` in general; `https://voice-journey.<tailnet>` after this move).

`capture.mjs` stays until the client has recorded a real session end to end.
After that, its AVFoundation path is retired.

## Phases

| # | What | Where | Done when |
| --- | --- | --- | --- |
| 1 | Make the server portable: data root, tool paths through the environment, `Dockerfile`, the intake API (manifest, upload, finalize) with tests, generated manifests moved out of the repo | voice-journey | Image builds; `npm test` passes in the image; the intake rejects a bad checksum and accepts a good session |
| 2 | Infra stack `voice-journey` on Lubuntu: compose, door, sidecar, backup, check, deploy; tailnet node and Pocket ID callback; mc-autodeploy enrollment; `dev.toml [deploy]` to `autodeploy` | infra, voice-journey | Stack deploys from `main`; `/healthz` 200 behind the door; a merge redeploys |
| 3 | Corpus mirror: `vj-mirror` and the launcher's `mirror` mode on the Mini; the dedicated `vj-mirror` drop account with its restricted `rrsync -wo` key on Lubuntu (no tailnet grant needed) | voice-journey, infra, Mini | A new Voice Memo appears in the server's index within one mirror period plus one watcher scan |
| 4 | Cutover: copy `local-artifacts/` and the generated manifests from the Mini to the data root; move the `voice-journey` node name to the sidecar; stop the Mini's browser and door jobs (the mirror stays) | Mini, Lubuntu | The browser on Lubuntu shows the full corpus and journeys; the Mini serves nothing |
| 5 | Capture client: new repo (native kind), devices and levels, ritual, packaging, OIDC sign-in, upload | new repo | A real UMIK-1 session recorded on a client is accepted by the server and processed |
| 6 | Retire `capture.mjs`'s AVFoundation path; update AGENTS.md and README; infra PLACEMENT and ONBOARDING (no longer a TCC exception) | voice-journey, infra | Docs match; `dev check` is clean everywhere |

**Phase 3 status (2026-10-03): voice-journey side done** (PR on
`phase3/corpus-mirror`): `npm run mirror`, the launcher's `mirror` mode, the
launchd plist template, tests and docs (README "Corpus mirror"). Remaining
owner steps: (1) build and re-sign the launcher on the Mini; (2) re-grant Full
Disk Access if macOS asks (rebuilding changes the binary's identity); (3)
install the wrapper and `ai.voice-journey.mirror.plist`; (4) create the Mini's
key `~/.ssh/vj-mirror` and give the public key to infra for the `vj-mirror`
account. The mirror pushes into the rrsync root
(`./`): recordings and `CloudRecordings.db` at the top, manifests in
`_mirror/`.

**Phase 3 alignment (2026-10-03, branch `phase2/server-on-lubuntu`):** the
mirror matches infra's drop: default dest `vj-mirror@cole-lubuntu-laptop:`,
`/opt/homebrew/bin/rsync` by full path, `--chmod=D2750,F0640`, ssh
`IdentitiesOnly=yes IdentityAgent=none BatchMode=yes` with `~/.ssh/vj-mirror`,
and rrsync's drop lock is exit 3 (try the next period). Server side: the watcher
and index ignore rsync temp files, `.rsync-partial/` and `_mirror/`, never write
the drop, hold back files whose size differs from `_mirror/latest.json`, and the
index marks a recording deleted on the phone (`phoneState: deleted`) without
removing it.

**Phase 2, repo side (same branch):** `dev.toml [deploy]` is lane `autodeploy`,
stack `voice-journey`, host `lubuntu`. The intake accepts an identity that came
from a bearer token (the door's `--skip-jwt-bearer-tokens` plus a JWT issuer for
the public client `voice-journey-capture`): the app still reads only
`X-Forwarded-User` / `X-Forwarded-Email`. HOST and PORT stay env-driven
(`VOICE_JOURNEY_HOST`, `VOICE_JOURNEY_PORT`; image default `0.0.0.0:8787`);
`/healthz` is unauthenticated. No `[deploy]` lane is live until the infra stack
lands.

## Status (2026-10-03)

- **Phase 1:** done (#8).
- **Phase 2: done.** The infra stack `voice-journey` is deployed through
  mc-autodeploy (lane `autodeploy`, host `lubuntu`); a manual conductor run
  succeeded. A merge to `main` now redeploys: the deploy rebuilds the image on
  Lubuntu.
- **Phase 3: done.** The first mirror run pushed 2,433 recordings to the drop;
  write-only access was verified (the Mini's key can write the drop and nothing
  else).
- **Phase 4: done (cutover).** The server runs on Lubuntu: infra stack
  `voice-journey`, node `https://voice-journey.colobus-stargazer.ts.net`, data
  root `/srv/voice-journey`. `local-artifacts/` and the eight generated
  manifests were copied to the data root (checksums match); the server shows all
  2,433 recordings and 46 journeys with the watcher idle. The Mini's browser,
  door and node jobs are stopped and disabled (their plists are in
  `~/Library/LaunchAgents/disabled/`); only the Mini's corpus mirror runs. The
  generated manifests are now untracked in Git (`.gitignore`). Rollback: infra
  `stacks/voice-journey/README.md`, "Phase 4".
- **Phase 5: built, awaiting a real session.** The client lives in the repo
  `voice-journey-capture`; it waits for a real UMIK-1 session.
- **Phase 6: pending** on phase 5.

Phases 1 and 3 can run in parallel. Phase 5 can start against the phase-1 API
before cutover.

## Phase 1 status (2026-10-03)

**Done:** implemented on `phase1/server-portable` (#8), built and verified in
rootless Docker.

- **Data root:** `src/paths.mjs`, `VOICE_JOURNEY_DATA`. Input manifest:
  `chains.json` (stays in the repo). Generated: index, filtering-sample,
  filter-results, full-corpus-filter-results, local-stt-transcripts,
  local-lyric-indicators, local-voice-features, song-journeys (data root).
  They are still tracked in Git as a snapshot: untracking them waits for the
  Phase 4 copy, so a `git pull` on the Mini cannot delete its working copies.
- **Tools:** all through the environment; no Homebrew defaults remain.
- **Image:** `Dockerfile` (Node 24 bookworm-slim pinned by digest, ffmpeg from
  Debian, whisper.cpp v1.9.4 pinned by commit, the pinned venv on Debian's
  Python 3.11, SPA built with the token as a BuildKit secret). The whisper
  model is **not** in the image: `vj-fetch-model` fetches it onto the data
  volume against a pinned sha256 (decision: image size, and the model size is
  still an open owner decision).
- **Intake API:** `src/intake.mjs`, contract in the README. Accepted sessions
  are indexed as `measurement` rows and watched. **Remaining Phase B:**
  filtering/STT/features skip measurement rows; calibrated analysis of the
  segments, chain pairing (`npm run pair-chains`) and the bias report are not
  built.
- **Left for later phases:** the infra stack, door `--skip-jwt-bearer-tokens`
  for the client's bearer token (Phase 2); untracking the generated manifests
  (Phase 4).

## Risks and open points

- **STT and feature compute on the laptop's CPU.** The history is already
  processed; new takes are a few per day. If whisper.cpp medium is too slow on
  Lubuntu, measure and choose a smaller model or GPU offload. The Breathing Loop
  tolerates latency.
  - *Measured on Lubuntu* (i7-6600U, 2 cores / 4 threads shared with other
    services): `medium` at 4 threads runs at 4.1-4.5x real time; at 2 threads
    it is about 12% slower. The stack sets `WHISPER_CPP_THREADS=2` so STT leaves
    room for the other services. **Owner decision: keep `medium`**, so new
    transcripts stay comparable with the 2,433 existing medium transcripts.
  - `WHISPER_CPP_THREADS` (positive integer; unset or empty = whisper.cpp's
    default) is passed as `-t <n>` to every whisper.cpp run; a non-integer
    value is refused at startup. `npm run stt --threads N` overrides it.
- **The level pin on Linux and macOS.** The Phase A guard needs reliable OS
  level reads. The client refuses to record when it cannot read the level, never
  silently.
- **Privacy.** Recordings, transcripts and lyrics stay on the server and the
  Mini. They are never in Git, logs or notifications (Phase D rule 4). The
  mirror and the uploads are tailnet-only.
- **The Mini still matters:** it is the only Voice Memos reader. If it is off,
  phone takes queue in iCloud and arrive later. Nothing is lost.
- **Owner decisions (2026-10-03):**
  - the whisper model is fetched onto the data volume with `vj-fetch-model`
    (not baked into the image); measure `medium` on Lubuntu first, and pick a
    smaller model or GPU offload only if it is too slow;
  - the generated manifests are untracked in Git in phase 4 (after the copy to
    the data root);
  - the client repository is `voice-journey-capture`;
  - the client supports the UMIK-1 chain only at launch.
