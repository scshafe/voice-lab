# The read-only web view

`/view/` is the phone's window on the voice corpus: server-rendered HTML with inline
SVG charts, served by the API process (`python -m cole_voice_lab.api serve --journey`)
behind the Pocket ID door at `https://voice-lab.<tailnet>.ts.net/view/`. It has no
JavaScript and no client-side framework, fits a phone, and follows the system's light
or dark setting. Design: `docs/design/VOICE-PLATFORM.md` §6.5, §8.2, §3.5; decisions
D-025 and D-035. Code: `src/cole_voice_lab/view/`.

## Pages

| Page | Shows |
| --- | --- |
| `/view/` | Recording, review and review-queue totals, the years covered, the trends headline (takes measured, hours, working top), recordings per classification bucket (each links to the filtered table) and per year |
| `/view/recordings` | Every indexed recording, newest first, 50 a page: date, duration, bucket, labels (review queue, reviewed verdict, lyric match, wordless) and the generic song cluster. `?page=`, `?bucket=`, `?year=` |
| `/view/journeys` | Same-song journeys: the improvement index (slope per year, 95% interval, songs, verdict) and each recurring song's takes, span, tuning error and slopes |
| `/view/trends` | Voice trends by quarter: pitch range (envelope, typical range, median, on a note axis), longest sustained note, clarity (CPPS), vibrato rate, recordings per month, working top by year, eras, recurring songs over time, and a table of the quarters |
| `/view/goals` | Each goal's target, takes counted, running median and status. Creating and abandoning goals stays in the API (the desktop app) |
| `/view/report` | The evidence report ("did I get better"), rendered live from the manifests |

The data is the journey store's: the repo-safe manifests (recording index, filter
results, the transcript and lyric *indicator* manifests, voice features, song
journeys) and the goals and referee state files. Pages reflect a manifest within a
request of the pipeline replacing it.

The evidence report is rendered on request from the same manifests and the same code
as `python -m cole_voice_lab.view.report generate` (its sections, inside the view's
page), and re-rendered only when an input file changes. It is not read from a
generated file: a file can be stale or missing, and serving one verbatim would trust
whatever wrote it. The report holds numeric aggregates and generic labels only (no
audio, transcript or lyric text), so every section is shown.

## What it never shows (D-025)

- No audio: no page plays, serves or links a recording, and none reads anything under
  `raw/`, `derived/` or `memos/`, the phone corpus or the measurement sessions.
- No transcript or lyric text, and no feature detail: the view reads only the indicator
  manifests (counts, statuses, generic cluster labels such as
  `recurring_lyric_cluster_001`), never the local transcript or feature directories.
- No link to an `/api/` route, and no forms: the pages link only to other `/view/` pages.
- No writes: `GET` and `HEAD` only; any other method answers `405`.

Listening, review verdicts and the blind referee are the desktop app's.

## Access

The door (oauth2-proxy, Pocket ID, a 12-hour cookie) signs the phone in and forwards
the user as `X-Forwarded-User`. The view checks it again (defence in depth):

- With an `Authorization` header, the bearer token must pass the API's own checks
  (issuer, audience, lifetime, signature, the allowed subject) or the answer is `401`.
- Otherwise `X-Forwarded-User` must equal the server's allowed subject
  (`serve --allowed-subject`, the same one the bearer check uses): missing is `401`,
  anyone else `403`. Refusals are a short page with no data.

The forwarded header is trusted only because the API is reachable from the door alone
(its own container network). oauth2-proxy forwards the session's user, which for an
OIDC provider is the token's `sub` claim, so the allowed subject is the Pocket ID user
id the stack already passes as `--allowed-subject`.

Without `serve --journey` every page answers `503`. Every response carries
`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src data:;
base-uri 'none'; form-action 'none'; frame-ancestors 'none'` (nothing can run a
script), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`X-Frame-Options: DENY` and `Cache-Control: no-store`. Every value on a page is
HTML-escaped.
