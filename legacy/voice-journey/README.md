# Voice Journey — five years of singing

Longitudinal analysis groundwork for 2,433 Apple Voice Memos spanning 2019 to
present. The synced corpus is a mixed personal archive: some recordings are
singing, and some are speech or other non-singing memos. The kickoff arc builds
corpus access, rebuildable indexing, and a filtering pass that separates
non-singing recordings from singing recordings, then flags noise/music
contamination within the singing set.

The recordings themselves are NEVER committed — this repo holds code + manifests only.
See the project brief (Mission Control thought `project-brief`) for scope + constraints.

## Host-Access Seam

Docker-mode agents do not read `~/Library` directly. Corpus interaction goes
through the read-only host-side seam command:

```sh
npm run corpus -- describe
npm run corpus -- list --dry-run
npm run corpus -- metadata "20190301 090000-0A1B2C3D.m4a" --dry-run
npm run corpus -- read-handle "20190301 090000-0A1B2C3D.m4a" --approval "operator-approved sample" --dry-run
```

The seam exposes only `describe`, `list`, `metadata`, and `read-handle`. It
refuses mutation/upload commands such as `delete`, `remove`, `write`, and
`upload`. `read-handle` requires an approval note and returns a read-only handle;
it never prints or copies audio bytes.

## Recording Index

The recording index is rebuilt through the host-access seam. It records metadata
and first-class fields needed by the later singing/non-singing split, while
leaving audio bytes untouched:

```sh
npm run index -- --dry-run
npm run index -- --out manifests/recording-index.json
```

The real corpus rebuild must run in a host context where the approved Voice
Memos `Recordings/` path is visible. Docker-mode agents can inspect the dry-run
scope, but they must not read `~/Library` directly.

The index manifest contains recording identity, filename-derived capture time,
file stat metadata, source references back to the seam, placeholder
singing/non-singing classification fields, noise/music status fields, and
tool-choice records. The current indexer reads directory entries and file stat
metadata only; duration and content labels are intentionally left for later
approved analysis phases.

## Filtering Proof

The filtering proof is sample-first. Sample selection reads only the committed
index manifest:

```sh
npm run filter -- select-sample --index manifests/recording-index.json --out manifests/filtering-sample.json
npm run filter -- analyze --sample manifests/filtering-sample.json --dry-run
```

Non-dry-run analysis reads audio bytes and therefore runs only after release-gate
approval in a host context where the corpus is visible. It uses `ffmpeg` to
decode approved sample recordings to transient mono PCM, computes aggregate
features, and writes a repo-safe `manifests/filter-results.json` with
singing/non-singing and noise/music labels. It does not retain audio bytes,
waveforms, spectrograms, transcriptions, or other audible derivatives.

The same classifier can run over the full committed index after release-gate
approval:

```sh
npm run filter -- analyze-index --index manifests/recording-index.json --dry-run
npm run filter -- analyze-index --index manifests/recording-index.json --corpus-root "/Users/cole/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings" --approval "release-gate full-corpus classification approval" --out manifests/full-corpus-filter-results.json --ffmpeg-bin /opt/homebrew/bin/ffmpeg
```

`analyze-index` is resumable. It writes `manifests/full-corpus-filter-results.json`
after each recording and skips completed `recordingId`s when rerun with the same
command, so an interrupted host run does not re-decode completed files.

## Corpus Browser

The first corpus-browser slice is a read-only table over the generated manifests
(in `$VOICE_JOURNEY_DATA/manifests/` on the server; untracked, so a fresh
checkout has none until you run the pipeline or point at a data root that has
them). It joins `manifests/recording-index.json` and
`manifests/full-corpus-filter-results.json`, plus optional repo-safe STT and lyric
indicator manifests when present. It serves one sortable/filterable table and does
not read recordings, stream audio, upload data, or mutate the corpus. The browser
also includes the spot-check workbench: a
deterministic stratified review queue, local verdict capture, and a repo command
to merge verdict metadata back into the full-corpus results manifest.

The server itself runs on the laptop and deploys when a PR merges to `main`
(the runner lane: `.github/workflows/deploy.yml` builds the image there from
`deploy/stack/`; merge one change at a time and watch the run). To develop locally, use a temp data root so nothing touches real
data, and copy in only the manifests you need:

```sh
export VOICE_JOURNEY_DATA="$(mktemp -d)"
npm run browser -- --host 127.0.0.1 --port 8787
```

The launch shapes below (Tailscale IP, Mac corpus root, approvals) are the
historical Mini setup, kept for reference; the Mini no longer serves.

```sh
TAILSCALE_IP="$(tailscale ip -4)" npm run browser -- --host 127.0.0.1 --host "$TAILSCALE_IP" --port 8787
```

Playback stays disabled unless the host launch includes a release-gate approval
note and corpus root. After approval, the host launch shape is:

```sh
TAILSCALE_IP="$(tailscale ip -4)" npm run browser -- --host 127.0.0.1 --host "$TAILSCALE_IP" --port 8787 --corpus-root "/Users/cole/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings" --playback-approval "release-gate browser playback approval"
```

To show STT status, lyric-match marks, and host-local transcript text, relaunch
with the repo-safe manifests and the gitignored transcript store:

```sh
TAILSCALE_IP="$(tailscale ip -4)" npm run browser -- --host 127.0.0.1 --host "$TAILSCALE_IP" --port 8787 --corpus-root "/Users/cole/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings" --playback-approval "release-gate browser playback approval" --transcripts manifests/local-stt-transcripts.json --lyrics manifests/local-lyric-indicators.json --transcript-dir local-artifacts/stt-transcripts --features manifests/local-voice-features.json --feature-dir local-artifacts/voice-features
```

### The web app (React + @scshafe/ui)

The browser UI is a React + Redux Toolkit SPA in `web/`, built on the shared
`@scshafe/ui` component package (private GitHub Packages, exact version pinned in
`web/package.json`; `web/.npmrc` sets the registry and reads `NODE_AUTH_TOKEN`, so installing
`web/` needs GitHub Packages read access) with the brass theme layered over the `--sui-*` tokens (dark theme pinned)
in `web/src/theme.css`. The repo root stays zero-dependency; `web/` owns the
only `node_modules`. Build output (`web/dist/`, gitignored) is served
request-time by the corpus browser at `/assets/app.js|app.css`, so a rebuild
goes live on reload without relaunching:

```sh
cd web && npm install && npm run build
npm run typecheck --prefix web             # tsc, no emit
npm run e2e --prefix web                   # happy-dom boot check against the live server
```

`/`, `/journey` and `/referee` all serve the same shell (the bundle routes by
pathname; legacy deep links like `/?q=<clusterId>` still pre-filter the Corpus
table). Without a built bundle the pages serve a help notice and the JSON API
keeps working. State follows MC's FRONTEND-DOCTRINE: `createAsyncThunk` →
slices → memoized selectors (no RTK Query); paged rows accumulate through
`/api/rows?fields=list&limit&offset` behind @scshafe/ui's `InfiniteScrollSentinel`,
with exactly one on-demand `<audio>` element instantiated at a time. Charts
are plain React SVG under `web/src/components/charts/` (domain geometry ported
verbatim from the retired inline pages). The render smoke
(`test/web-spa.test.mjs`) server-renders all three pages from a preloaded
store via esbuild against `web/node_modules` and skips loudly when `web/` is
not installed.

### The Journey page

`/journey` renders longitudinal voice trends computed at boot from the
repo-safe voice-features manifest (`--features`, on by default when the
manifest exists), plus the lyric manifest's clusters. Sections: a stat band;
the Range River (quarterly pitch envelope/typical/median on a log-frequency
axis labeled in note names, with a register-share strip); Cause & Effect
(monthly practice cadence over quarterly sustain and CPPS on one shared time
axis, with high-practice era bands derived from the cadence and per-era
listen buttons that queue sample takes through the release-gated audio
route); the Vibrato Story (rate/extent/time-share triptych with IQR bands and
the settled 5–6 Hz reference zone, plus a per-take fingerprint scatter
colored by year); What Not To Trust (CPPS vs HNR paired panels and the
grayed tuning tile); and Repertoire Threads (top clusters as clickable
timelines that open the Corpus table pre-filtered).

Aggregates use the reliable subset (total f0 frame rejection <= 0.10); a
toggle re-renders everything including the flagged takes, drawn hollow in
scatters. `/api/trends` serves summary aggregates only — no feature arrays,
no audio, no transcript text — and reports `available: false` when the
features manifest is absent. Clicking a scatter dot opens a per-take
drill-down; its f0 contour is served only through
`/api/feature-detail/:recordingId` from the gitignored local feature store,
gated by `--feature-dir` exactly like transcript text.

Transcript text is not included in row JSON or committed fixtures. The browser
serves it only through `/api/transcript/:recordingId`, resolved as
`<transcript-dir>/<recordingId>.txt` from the local gitignored transcript store.

Spot-check verdicts are written to `local-artifacts/corpus-browser-state.json`
by default, which is gitignored. Merge only repo-safe review metadata with:

```sh
npm run browser -- merge-verdicts --results manifests/full-corpus-filter-results.json --state local-artifacts/corpus-browser-state.json --out manifests/full-corpus-filter-results.json
```

For fixture verification without leaving a server running:

```sh
npm run browser -- --index manifests/recording-index.json --results manifests/full-corpus-filter-results.json --transcripts manifests/local-stt-transcripts.json --lyrics manifests/local-lyric-indicators.json --transcript-dir local-artifacts/stt-transcripts --features manifests/local-voice-features.json --host 127.0.0.1 --port 0 --once
```

## Local STT

Local speech-to-text is host-executed and release-gated. Dry-run reads only the
committed index and reports the full-corpus scope, selected open-source tool,
model path, output manifest, local transcript store, and resume behavior:

```sh
npm run stt -- transcribe-index --index manifests/recording-index.json --dry-run
```

Non-dry-run reads audio, decodes each recording to a temporary 16 kHz mono wav,
runs `whisper.cpp`, deletes the temporary wav, writes transcript text/JSON under
`local-artifacts/stt-transcripts/`, and incrementally updates the repo-safe
`manifests/local-stt-transcripts.json`. The manifest records status, language,
duration processed, confidence aggregates, word/segment counts, derived aggregate
features, tool/model provenance, and local transcript path references only. It
must not contain transcript text.

Host provisioning candidate for release-gate review:

```sh
brew install whisper-cpp ffmpeg
/opt/homebrew/opt/whisper-cpp/share/whisper-cpp/models/download-ggml-model.sh medium
```

Full-corpus batch command candidate for release-gate review:

```sh
npm run stt -- transcribe-index --index manifests/recording-index.json --corpus-root "/Users/cole/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings" --approval "release-gate local STT approval" --out manifests/local-stt-transcripts.json --transcript-dir local-artifacts/stt-transcripts --whisper-bin /opt/homebrew/bin/whisper-cli --model /opt/homebrew/opt/whisper-cpp/share/whisper-cpp/models/ggml-medium.bin --ffmpeg-bin /opt/homebrew/bin/ffmpeg --language auto
```

`transcribe-index` is resumable by `recordingId`: rerunning the same command
skips completed recordings already present in the manifest and writes the
manifest after each new completion, so an interrupted host run can resume safely.

### Duration backfill

`backfill-manifest` re-derives `durationProcessedSeconds` (and the
duration-based `wordsPerMinute` / `segmentDensityPerMinute` rates) from the
millisecond `offsets` fields in the local transcript JSONs. whisper.cpp writes
SRT-style comma-decimal timestamps (`00:00:15,000`) that the original parser
rejected, so early manifests carried null durations; the parser now normalizes
comma decimals and prefers `offsets`. The backfill reads no audio, re-runs no
STT, and writes only numeric metadata plus a `durationBackfill` provenance
block. It is host-executed because it reads the gitignored local transcript
store; its dry-run reads only the committed manifest:

```sh
npm run stt -- backfill-manifest --dry-run
npm run stt -- backfill-manifest --manifest manifests/local-stt-transcripts.json --transcript-dir local-artifacts/stt-transcripts
```

## Local Lyric Indicators

Lyric indicators are local-first and host-executed because they read transcript
text from `local-artifacts/stt-transcripts/`, which is gitignored and not
available in sandboxed agent worktrees. The command reads the repo-safe STT
manifest plus local transcript text, then writes only non-identifying marks,
cluster ids, aggregate counts, confidence, and method metadata:

```sh
npm run lyric -- analyze --transcripts manifests/local-stt-transcripts.json --transcript-dir local-artifacts/stt-transcripts --out manifests/local-lyric-indicators.json
```

The analyzer uses cross-recording transcript similarity, repeated-phrase density,
and zero-word/non-English STT language detections as wordless/vocalise signals.
It does not call external lyric databases, upload transcript text, read audio,
or put raw lyric text in the output manifest. Use dry-run to inspect scope
without reading local transcript text:

```sh
npm run lyric -- analyze --transcripts manifests/local-stt-transcripts.json --dry-run
```

## Voice Feature Extraction (Arc 2)

Per-recording voice features (feature contract v1: voicing, pitch percentiles
and semitone range, self-referenced tuning error, vibrato rate/extent/share,
jitter/shimmer/HNR/CPPS, spectral shape and singer's-formant ratio, dynamics,
phrasing) over the classified singing subset. The stack and seam are specified
in `docs/arc-2-feature-stack-proposal.md` (design-board consult artifact); the
Node orchestrator follows the local-STT pattern and drives a long-lived Python
worker in a project-local venv.

Detailed per-recording feature JSONs (contours, vibrato tables) stay in the
gitignored `local-artifacts/voice-features/`; the committed manifest carries
summary aggregates, a per-recording `featuresDigest`, and a
`configFingerprint` binding results to the analysis configuration and library
versions. Failed recordings record `status: "failed"` rows and the run
continues; reruns retry failures and skip completions.

Host provisioning (release-gate consult alongside the batch command):

```sh
/opt/homebrew/bin/python3.12 -m venv analysis/.venv
analysis/.venv/bin/pip install -r analysis/requirements.txt
analysis/.venv/bin/python analysis/selftest.py
```

Dry-run reads only repo-safe manifests; the full-corpus batch candidate for
release-gate review is:

```sh
npm run features -- extract-index --dry-run
npm run features -- extract-index --index manifests/recording-index.json --results manifests/full-corpus-filter-results.json --corpus-root "/Users/cole/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings" --approval "release-gate voice feature extraction approval" --out manifests/local-voice-features.json --feature-dir local-artifacts/voice-features
```

## Same-Song Journeys (Arc 3)

The journeys analyzer reads the committed features + lyric manifests plus the
gitignored local feature detail store (stored 50 ms f0 contours — no audio
bytes). Each take's contour is segmented into note cores, tuning is scored on
note medians against the take's own inferred offset, and eligible clusters
(>= 6 reliable takes across >= 1.5 years) get per-year rollups and Theil–Sen
slopes per dimension. A same-song improvement index aggregates cluster slopes
with seeded-bootstrap 95% CIs — the strictest measurable answer to "did I get
better", controlled for repertoire by construction. The output manifest is
repo-safe: numeric aggregates and generic cluster labels only. Host-executed
because the feature store is gitignored; dry-run reads the generated manifests:

```sh
npm run journeys -- analyze --dry-run
npm run journeys -- analyze --analyzed-at 2026-07-31T00:00:00.000Z
```

The browser serves the manifest at `/api/journeys` (`--journeys`, on by
default when the manifest exists), and the Journey page renders it as The
Verdict (per-dimension verdict chips with CIs), Song Scorecards (per-song
slopes and yearly tuning sparklines, click-through to the Corpus table), and
the note-core tuning panel that retires the old every-frame tuning caveat.

### The Referee page

`/referee` runs the blind A/B perceptual experiment: pairs of takes from the
same recurring cluster captured in different years, sides shuffled, nothing
revealed until you judge. Pair selection is seeded and deterministic
(`--referee-seed`, capped per cluster and interleaved across songs; only
reliable takes). Trials require the release-gated playback launch — the page
explains itself otherwise. Verdicts (`A`/`B`/`too close`/`not the same
song`/`skip`) persist to the gitignored `--referee-state` file with a
post-verdict reveal, and `/api/referee/results` fits a Bradley–Terry
perceived-quality curve by year (ties as half-wins, +0.25 regularizing
pseudo-wins per compared pair, log2 strengths relative to the earliest
judged year). "Not the same song" verdicts double as human evidence for the
cluster repair pass.

## Evidence Report

The regenerable "did I get better" document. `npm run report` renders a
self-contained static HTML file (inline SVG, no scripts) from the committed
manifests plus the optional local referee judgments: executive verdict,
same-song improvement index, perceived-quality curve, yearly transformation
charts, note-core tuning, song scorecards, limitations, and a method
appendix. Default output is the gitignored
`local-artifacts/evidence-report.html` (repo = code + manifests; the report
is derivable). The browser serves the current file live at `/report`
(`--report` overrides the path) — regenerate and reload, no relaunch needed.

```sh
npm run report -- generate --dry-run
npm run report
```

## Server deployment (data root, container)

The server runs from a container on Lubuntu (design: `docs/move-to-lubuntu.md`).
Everything mutable lives under one data root, `VOICE_JOURNEY_DATA` (`/data` in
the image); with it unset, the historical local-dev layout applies unchanged.
The app never writes into its source tree.

| Under `$VOICE_JOURNEY_DATA` | Local dev (unset) | What |
| --- | --- | --- |
| `corpus/phone/` | the Voice Memos container (`VOICE_JOURNEY_CORPUS_ROOT`) | phone chain; the Mini mirror writes it, the app reads only |
| `corpus/measurement/<sessionId>/` | `local-artifacts/corpus/measurement/` | accepted capture sessions (WAVs + `session.json`) |
| `artifacts/` | `local-artifacts/` | transcripts, feature store, app state, report |
| `manifests/` | `manifests/` | **generated** manifests (outputs, below) |
| `staging/` | `local-artifacts/staging/` | intake uploads in flight; same volume as `corpus/` so finalize is one atomic rename; never scanned |
| `models/` | (system) | the whisper model, fetched once with `vj-fetch-model` |

**Input vs generated manifests.** `manifests/chains.json` (the chain registry)
is an *input*: authored, reviewed, versioned, always read from the source tree
(`VOICE_JOURNEY_CHAINS` overrides). Every other manifest is a *generated
output* of the pipeline and moves to `$VOICE_JOURNEY_DATA/manifests/`:
`recording-index`, `filtering-sample`, `filter-results`,
`full-corpus-filter-results`, `local-stt-transcripts`,
`local-lyric-indicators`, `local-voice-features`, `song-journeys`. They are
**untracked** (gitignored since phase 4; the cutover copied them to the data
root `/srv/voice-journey`), and the image does not contain them
(`.dockerignore`). Tests use fixtures or temp data roots. Goals, verdicts, referee and
watcher state are app state under `artifacts/`.

**Environment** (flags still win):

| Variable | Meaning |
| --- | --- |
| `VOICE_JOURNEY_DATA` | the data root above |
| `VOICE_JOURNEY_CORPUS_ROOT` | phone corpus root (default `$D/corpus/phone`, else the Mac container via the seam) |
| `FFMPEG_BIN`, `WHISPER_CPP_BIN`, `WHISPER_CPP_MODEL` | tools; default to PATH names `ffmpeg`, `whisper-cli` and `$D/models/ggml-medium.bin` (no Homebrew paths) |
| `WHISPER_CPP_THREADS` | positive integer (1-9999): every whisper.cpp run (the watcher's STT stage and `npm run stt`) gets `-t <n>`. Unset or empty keeps whisper.cpp's default; anything else is refused at startup. `--threads N` on `npm run stt` overrides it |
| `VOICE_JOURNEY_PYTHON` | the venv python for praat/librosa (default `analysis/.venv/bin/python`) |
| `VOICE_JOURNEY_HOST`, `VOICE_JOURNEY_PORT` | bind address (comma list) and port |
| `VOICE_JOURNEY_CONTAINER=1` | allows a non-loopback bind; set by the image only |
| `VOICE_JOURNEY_PLAYBACK_APPROVAL`, `VOICE_JOURNEY_WATCH`, `VOICE_JOURNEY_WATCH_APPROVAL`, `VOICE_JOURNEY_NTFY` | enable playback / the watcher / notifications |

Outside the container the server still refuses any non-loopback `--host`.
With a data root, `--corpus-root`, `--transcript-dir` and `--feature-dir`
default to their places under it, and a fresh volume starts with an empty
corpus (`GET /healthz` is 200) until the watcher's first run.

**Image** (`Dockerfile`; Node 24 on Debian bookworm, ffmpeg, whisper.cpp
v1.9.4 CPU build, the pinned venv, the SPA built at image time, non-root user
`voicejourney`). The private `@scshafe/ui` install needs GitHub Packages read;
pass the token as a BuildKit secret, never a build arg:

```sh
docker build --secret id=node_auth_token,env=NODE_AUTH_TOKEN -t voice-journey .
docker build --target test --secret id=node_auth_token,env=NODE_AUTH_TOKEN .   # npm test inside the image
docker run --rm -v voice-journey-data:/data voice-journey vj-fetch-model       # whisper model, sha256-pinned, once
```

The whisper model is not baked in (medium is 1.5 GB): it lives on the data
volume and is verified against a pinned sha256 when fetched.

## Intake API (native capture client)

For `voice-journey-capture` (UMIK-1 measurement chain only at launch). Every
route needs the door's identity headers (`X-Forwarded-User` or
`X-Forwarded-Email`, as set by oauth2-proxy) and answers `401
identity_required` without them; the app must only be reachable through the
door. The identity may come from the browser session or from a bearer token:
the door runs oauth2-proxy with `--skip-jwt-bearer-tokens` and an extra JWT
issuer for the native client `voice-journey-capture` (a Pocket ID public
client, authorization code with PKCE). For a valid `Authorization: Bearer`
token the door sets the same `X-Forwarded-User` / `X-Forwarded-Email` headers,
so the app reads only those and never inspects `Authorization` itself (a bare
`Authorization` header with no identity headers is `401`). JSON in, JSON out; errors are `{"error": "<code>", …}`.

1. `POST /api/intake/sessions` — the manifest (<= 256 KiB):
   `sessionId` (`[A-Za-z0-9][A-Za-z0-9_-]{5,63}`), `chainId` (must be a
   `measurement` chain in `manifests/chains.json` whose `deviceName` is a
   UMIK), `startedAt`/`endedAt` (ISO), `capture` (`inputVolume`, `distanceCm`,
   `calFileSha256` hex-or-null, optional `sampleRate` that must match the
   chain), `segments[]` (`key`, `file`, …; every `file` listed below) and
   `files[]` (`name` matching `[A-Za-z0-9][A-Za-z0-9_-]{0,63}.wav`, lowercase
   `sha256`, `sizeBytes`). `201` opens the session; the same manifest again
   is `200` with per-file progress (resume); a different manifest for the same
   id is `409 session_manifest_conflict`. Invalid manifests are `422`.
2. `GET /api/intake/sessions/<id>` — `{state: "open"|"accepted", files: [{name, sizeBytes, receivedBytes, complete}]}`.
3. `PUT /api/intake/sessions/<id>/files/<name>` — the raw bytes, with
   `Content-Length`. Whole upload: body is the entire file. Resumable: send
   `Content-Range: bytes <start>-<end>/<size>` starting at the server's
   `receivedBytes` (`409 range_not_contiguous` reports it). A complete file is
   checked against its size and sha256 at once (`422 checksum_mismatch`
   drops it). Re-uploading a received file is `200 already_received`.
4. `POST /api/intake/sessions/<id>/finalize` — re-verifies every size and
   sha256, then publishes the session with one atomic directory rename into
   `corpus/measurement/<id>/` (plus a `session.json` with the manifest and an
   `intake` record). Missing files: `409 incomplete`. Changed bytes: `422
   checksum_mismatch` (those files are dropped for re-send). Either way nothing
   under `corpus/measurement/` changes. Finalizing again is `200 accepted`.

Limits (defaults): 64 files, 1 GiB per file, 4 GiB per session, 8 open
sessions, staging older than 7 days purged. Logs carry session ids and counts
only: never audio, file contents or transcript text.

The index lists every accepted segment WAV as a `source.type: "measurement"`
row (`chainId`, `sessionId`, `segmentKey`) and the watcher scans
`corpus/measurement/` with the phone corpus. Filtering and STT skip those rows;
the rest of Phase B (calibrated analysis, pairing) is still to do.

## Tailnet exposure (HTTPS) — historical

> **Historical (pre-cutover).** The Mini no longer serves: since the phase 4
> cutover (2026-10-03) the app, door and node run on Lubuntu (infra stack
> `voice-journey`) and the Mini's browser, door and node jobs are disabled.
> Do not kickstart them or update the Mini's checkout to serve. Only the
> mirror runs on the Mini (see "Corpus mirror"). The sections "Tailnet
> exposure", "Auth door" and "Corpus access" describe the old Mini setup.

The authorized tailnet exposure is **https://voice-journey.colobus-stargazer.ts.net/**:
the app had its own tailnet node — a dedicated userspace tailscaled
(LaunchAgent `com.scshafe.tailscaled.voice-journey`, state in
`~/.local/share/tailscale/voice-journey/`, tagged `tag:service`) whose
`tailscale serve` terminates TLS with the node's `ts.net` certificate and
stays tailnet-only. Bind the app to loopback ONLY and let Tailscale proxy
it — never bind the tailscale IP directly (plain HTTP), and never use
`tailscale funnel` (public internet is forbidden by the project brief):

```sh
npm run browser -- --host 127.0.0.1 --port 8787 --corpus-root "/Users/cole/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings" --playback-approval "release-gate browser playback approval" --transcripts manifests/local-stt-transcripts.json --lyrics manifests/local-lyric-indicators.json --transcript-dir local-artifacts/stt-transcripts --features manifests/local-voice-features.json --feature-dir local-artifacts/voice-features
SOCK=$HOME/.local/share/tailscale/voice-journey/tailscaled.sock
tailscale --socket=$SOCK serve status   # verify: https://voice-journey.colobus-stargazer.ts.net, no Funnel
```

The serve config (443 → 127.0.0.1:8787) lives in the dedicated node's own
state, so it survives reboots with no config race against other host
services on the main node's 443 — which is why this app previously sat on
`elrics-mac-mini:8790`; that serve entry is now retired. One-time join:
`tailscale --socket=$SOCK up --advertise-tags=tag:service
--hostname=voice-journey --accept-dns=false`, with `tag:service` declared
in the tailnet policy's `tagOwners`. The launchd agent
`ai.voice-journey.browser` keeps the loopback server alive;
`com.scshafe.tailscaled.voice-journey` keeps the node alive (see that
plist's header comment for the full recipe).

## Auth door (Pocket ID)

*Historical: the door now runs in the Lubuntu stack; the commands below are
for the retired Mini setup.*

Tailnet access is gated by OIDC, fleet-style (the same doctrine as bellwether
and inbox-pipeline: the door is the boundary, not the binding). `oauth2-proxy`
— launchd agent `ai.voice-journey.auth-proxy`, wrapper
`~/.mission-control/bin/voice-journey-auth-proxy-launchd` — terminates Pocket
ID (`https://id.colobus-stargazer.ts.net`) and proxies to the loopback app:
tailnet 443 → 127.0.0.1:8786 (door) → 127.0.0.1:8787 (app). Local lanes
(watchd, e2e, operator curl against 8787) bypass the door by design; the door
gates the tailnet lane.

Credentials live in `~/.mission-control/secrets/voice-journey-oidc.env`
(client id + secret from a Pocket ID OIDC client with callback URL
`https://voice-journey.colobus-stargazer.ts.net/oauth2/callback`; the cookie
secret is pre-generated). The door exits cleanly and stays down until real
credentials exist. To activate: fill the env file, then

```sh
launchctl kickstart -k gui/$(id -u)/ai.voice-journey.auth-proxy
SOCK=$HOME/.local/share/tailscale/voice-journey/tailscaled.sock
tailscale --socket=$SOCK serve --https=443 off
tailscale --socket=$SOCK serve --bg --https=443 http://127.0.0.1:8786
```

## Corpus access (macOS TCC)

*Applies to the Mini's mirror (the launcher in `mirror` mode). The browser mode
below is retired with the Mini's browser job.*

The Voice Memos group container is TCC-protected: a launchd service gets no
access unless its **responsible process** holds a Full Disk Access grant. A
process launched from a terminal inherits the terminal's grant — which is why
manual launches play audio while the same command under launchd returns
`corpus_access_denied` (503) from `/api/audio/*`.

Granting FDA to `node` itself would work but is the wrong shape: it grants
every node script on the machine, and the grant binds to the exact Cellar
binary, so every `brew upgrade` silently breaks playback. Instead the
LaunchAgent runs `~/.mission-control/bin/voice-journey-launcher` — a tiny
compiled wrapper (source: `scripts/voice-journey-launcher.c`, ad-hoc signed as
`ai.voice-journey.launcher`) that spawns `voice-journey-browser-launchd` as a
child and stays the responsible process for the whole tree, node and seam
children included. FDA is granted to that one binary:

```sh
clang -O2 -o ~/.mission-control/bin/voice-journey-launcher scripts/voice-journey-launcher.c
codesign -s - -f -i ai.voice-journey.launcher ~/.mission-control/bin/voice-journey-launcher
# System Settings → Privacy & Security → Full Disk Access → "+"
#   → ⌘⇧G → /Users/cole/.mission-control/bin/voice-journey-launcher → Add → on
# (retired with the Mini browser job) launchctl kickstart -k gui/$(id -u)/ai.voice-journey.browser
```

node needs no grant and upgrades freely. The only event that invalidates the
grant is recompiling the launcher (the ad-hoc signature changes) — it has no
reason to change.

## Corpus mirror (Mini → Lubuntu)

`npm run mirror` (`scripts/voice-journey-mirror`, `src/mirror.mjs`) pushes the
Voice Memos corpus to the Lubuntu server (design: `docs/move-to-lubuntu.md`,
phase 3). It reads the corpus only through the host-access seam (directory
listing, stat, and read-only streams of `.m4a` files and the
`CloudRecordings.db` metadata database), never writes the container, and never
deletes anything on the server: a recording deleted on the phone stays in the
server's history.

```sh
npm run mirror -- --dry-run          # prints the plan and both rsync command lines; reads nothing
npm run mirror -- --approval "<release-gate decision>" [--dest TARGET] [--ssh-key PATH]
```

Each run: (1) lists the corpus and hashes (SHA-256) only files whose size or
mtime changed since the last run (cache in the state dir); (2) snapshots the
database files into a staging dir; (3) `rsync`s the recordings; (4) only if
that succeeded, `rsync`s the database snapshot plus
`_mirror/manifest-<runId>.json` and `_mirror/latest.json`. The manifest lists
every file with size, mtime and sha256, so the server can verify; its arrival
means the files it lists are complete. A copy is kept in
`<state-dir>/manifests/`. State dir default
`~/.mission-control/state/voice-journey-mirror` (lock, hash cache, staging).
Logs carry counts only, and rsync's own diagnostics (which can quote file
names) are suppressed to a line count.

Exit codes: `0` ok, `1` corpus unreadable/unexpected, `2` usage or refused
(local destination, state inside the corpus, no `--approval`), `3` another run
holds the lock, **or the server's rrsync drop is locked** ("Another instance
of rrsync is already accessing this directory": a concurrent run; skip, the
next period retries), `4` rsync failed (the next run retries). Diagnostics stay
counts only, never file names.

The rsync line (identical flags both passes; the sender is always the Mini):

```text
rsync -rt --no-perms --no-owner --no-group --omit-dir-times \
  --partial-dir=.rsync-partial --timeout=300 --no-motd \
  --chmod=D2750,F0640 \
  -e "ssh -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -i ~/.ssh/vj-mirror" \
  --from0 --files-from=LIST  SRC/  vj-mirror@cole-lubuntu-laptop:
```

The binary is `/opt/homebrew/bin/rsync` (3.x) by full path: macOS 26's
`/usr/bin/rsync` is openrsync (protocol 29), untested against rrsync. Override
with `VOICE_JOURNEY_RSYNC` or `--rsync-bin`. `--chmod` is there because the
Mini's files may be `0600` and the server reads them through the `vj-mirror`
group. The account is the dedicated drop user `vj-mirror` (forced command
`rrsync -wo -no-del -munge`, rooted at `/srv/voice-journey/corpus/phone/`), not
a person's account.

It works under `rrsync -wo` because it only pushes: no `--delete*`, no pull,
no remote `--files-from` (the list is local), and the destination is a path
**relative to the key's root** (`vj-mirror@host:`, the root itself; rrsync reads a leading `/` as the root
of the restricted tree, so the absolute `/srv/...` path must not be used).
The partial-dir keeps unfinished files in a hidden directory until they are
complete. Do not add `--delay-updates` (rrsync rewrites `--partial-dir` to an
absolute path, and rsync then silently discards the files with exit 0) and do
not run rrsync with `--no-overwrite` (changed recordings must overwrite).
Verified against a real `rrsync -wo` (rsync 3.5.1) in a temp tree.

### Install on the Mini (owner)

The mirror runs under the FDA-granted launcher in `mirror` mode, so there is
no second grant. The launcher was extended; **rebuilding and re-signing it
changes its code identity, so macOS may require re-granting Full Disk Access**
(System Settings → Privacy & Security → Full Disk Access: remove and re-add
`/Users/cole/.mission-control/bin/voice-journey-launcher`), then run the
mirror once to confirm. The Mini's browser job is disabled; only `mirror` mode runs.

```sh
cd ~/.mission-control/projects/voice-journey && git pull   # to build the launcher/mirror only; the Mini serves nothing
clang -O2 -o ~/.mission-control/bin/voice-journey-launcher scripts/voice-journey-launcher.c
codesign -s - -f -i ai.voice-journey.launcher ~/.mission-control/bin/voice-journey-launcher
# re-grant Full Disk Access if the mirror gets corpus_access_denied
install -m 755 scripts/voice-journey-mirror-launchd ~/.mission-control/bin/voice-journey-mirror-launchd
mkdir -p ~/.mission-control/logs
cp scripts/launchd/ai.voice-journey.mirror.plist ~/Library/LaunchAgents/
# the plist's key is /Users/cole/.ssh/vj-mirror; edit it / the log paths if yours differ
brew install rsync                      # 3.x at /opt/homebrew/bin/rsync
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.voice-journey.mirror.plist
launchctl kickstart gui/$(id -u)/ai.voice-journey.mirror          # first run now
```

Uninstall: `launchctl bootout gui/$(id -u)/ai.voice-journey.mirror && rm
~/Library/LaunchAgents/ai.voice-journey.mirror.plist`. The launcher accepts
only the fixed modes `browser` (the default) and `mirror`; any other argument
exits 64 and spawns nothing. The wrapper holds the flags (`--approval`, and
`--dest` from `VOICE_JOURNEY_MIRROR_DEST`; `--ssh-key` from
`VOICE_JOURNEY_MIRROR_SSH_KEY`, default `~/.ssh/vj-mirror`).

Server side (infra, `replication/voice-journey-mirror/README.md`): the drop
account `vj-mirror` and its forced command exist on Lubuntu. On the Mini
generate the key (`ssh-keygen -t ed25519 -f ~/.ssh/vj-mirror`) and give the
public key to the infra session to add to `vj-mirror`'s `authorized_keys`. No
tailnet grant is needed: the broad tailnet grant already allows Mini →
Lubuntu:22.

What the server does with the drop (`corpus/phone/`, read-only to the app): the
watcher and the index list only regular `*.m4a` files at the top level, so
rsync temp files (`.<name>.XXXXXX`), `.rsync-partial/`, `_mirror/` and
`CloudRecordings.db*` are never recordings, and nothing is written there.
`_mirror/latest.json` is used as a size check: a file whose size differs from
the manifest is held back until its manifest arrives. The index marks each
phone row `phoneState`: `present` (listed by the latest manifest), `deleted`
(listed once, then absent: deleted on the phone; the file and row stay),
`unverified` (not yet listed). `CloudRecordings.db` is mirrored for later use;
nothing reads it yet, so no server path depends on a Mini location.

## The breathing loop (watchd)

The browser process doubles as the pipeline's watcher (`--watch`; design:
`docs/breathing-studio-plan.md`). Every `--watch-interval` seconds it lists
the corpus through the host-access seam (metadata only); when two consecutive
scans agree — iCloud delivers files in bursts, so single sightings never
count — and the agreed state differs from `manifests/recording-index.json`,
it runs the incremental pipeline as child processes: index → filter → stt →
lyric → reclassify → features → journeys (every stage resumes by
recordingId). On success it hot-reloads the served manifests in place: no
restart, no stale rows. Because the loop lives inside the service process,
the whole tree inherits the launcher's Full Disk Access grant.

`--watch-approval` records the standing release-gate decision that authorizes
the audio-reading stages (filter/stt/features) to run unattended. `--ntfy URL`
sends run summaries, failures, and a weekly digest — counts only, never
transcript or lyric content — with `--digest-day`/`--digest-hour` controlling
the schedule. Run state and the lock live at `--watch-state` (default
`local-artifacts/watchd-state.json`); a failing stage notifies, backs off 30
minutes, and retries when the corpus changes. Live status:
`GET /api/watch/status`. The launchd wrapper
(`~/.mission-control/bin/voice-journey-browser-launchd`) carries the
production flag set.

## Session capture — the spine ritual (phase A)

The measurement chain records through `npm run capture` (design:
`docs/breathing-studio-plan.md`, phase A). Chains live in
`manifests/chains.json` — every chain carries its device, distance, cal-file
path, and pinned input volume; retag-style changes (new mic, re-pinned
slider) are era events, never silent swaps.

First-run checklist (once, when the mic arrives):

```sh
npm run capture -- devices                 # confirm the UMIK enumerates
# 1. Download the per-serial cal file from miniDSP →
#    local-artifacts/calibration/umik1-cal.txt
# 2. System Settings → Sound → Input → select UMIK-1; set the slider ONCE
npm run capture -- pin-volume --chain umik1
npm run capture -- session --chain umik1   # first spine session
```

`session` walks the ritual keypress by keypress — 10 s room silence (noise
floor), then sustained /a/ soft and loud, a range glide, the anchor song, and
free practice (cal tone joins via `--cal-tone` once a calibrator exists) —
writing raw WAV 48k/24 mono per segment plus `session.json` (chain id, input
volume, distance, cal-file hash, per-segment timing) under
`local-artifacts/capture/<sessionId>/`. Audio is captured RAW: the cal file
applies at analysis time, never at capture.

The guards are the point: capture refuses to run when the chain's input
volume was never pinned or has drifted (the macOS slider is a hidden gain
stage; a nudge invalidates the SPL reference), and warns when the cal file is
missing. macOS will ask for microphone permission for your terminal on the
first real recording — expected, approve it. Ingestion of these sessions
into the corpus index is phase B.

## Evidence-Join Reclassification

The reclassifier joins the committed filter results with the committed lyric
indicator manifest and resolves `uncertain_manual_review` rows that carry
singing evidence: recurring cross-recording clusters, wordless-vocalise STT
signals, or repeated-phrase structure (>= 3 repeated 4-grams), each gated on
the row's own voiced-ratio floor. Contamination for resolved rows reuses the
v1 noise/music score formulas — rows those detectors already flagged were
never in the uncertain bucket, so evidence-backed rows with no flag resolve
to clean. Rows without evidence stay in the review bucket. Decided rows are
never relabelled, but `non_singing` rows that sit in a recurring cluster get
a repo-safe `evidenceConflict` flag for the review queue.

Each resolved row keeps its v1 result under `previousClassification` and
records the rule, signals, and contamination scores under `evidence`. The
command reads committed repo-safe manifests only — no audio, no transcript
text, no host-only paths — so it runs in sandboxed worktrees:

```sh
npm run reclassify -- apply --dry-run
npm run reclassify -- apply --reclassified-at 2026-07-30T00:00:00.000Z
```

## Verification

```sh
npm test
npm run corpus -- describe
npm run corpus -- list --dry-run
npm run index -- --dry-run
npm run filter -- select-sample --index manifests/recording-index.json --out /tmp/filtering-sample.json --generated-at 2026-07-16T00:00:00.000Z
npm run filter -- analyze --sample /tmp/filtering-sample.json --dry-run
npm run filter -- analyze-index --index manifests/recording-index.json --dry-run
npm run browser -- --index manifests/recording-index.json --results manifests/full-corpus-filter-results.json --host 127.0.0.1 --port 0 --once
npm run browser -- --index manifests/recording-index.json --results manifests/full-corpus-filter-results.json --transcripts manifests/local-stt-transcripts.json --lyrics manifests/local-lyric-indicators.json --transcript-dir local-artifacts/stt-transcripts --features manifests/local-voice-features.json --host 127.0.0.1 --port 0 --once
npm run browser -- merge-verdicts --results manifests/full-corpus-filter-results.json --state /tmp/corpus-browser-state.json --out /tmp/full-corpus-filter-results.json
npm run stt -- transcribe-index --index manifests/recording-index.json --dry-run
npm run stt -- backfill-manifest --dry-run
npm run lyric -- analyze --transcripts manifests/local-stt-transcripts.json --dry-run
npm run reclassify -- apply --dry-run
npm run features -- extract-index --dry-run
npm run journeys -- analyze --dry-run
```
