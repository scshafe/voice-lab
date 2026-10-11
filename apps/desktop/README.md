# voice-lab-desktop

voice-lab's desktop app for macOS and Linux (design
[VOICE-PLATFORM.md](../../docs/design/VOICE-PLATFORM.md) §6): the corpus, the analysis and
the blind referee over the lab's `/api/v1`, the uploads of this machine's sessions, and the
UMIK-1 measurement capture. It signs in with Pocket ID and is built with
[scshafe-qt](https://github.com/scshafe/scshafe-qt) (PySide6 + the `Scshafe.Ui` QML
module). It began as `voice-journey-capture`, whose history it keeps (D-030, D-037).

First time? See [docs/FIRST-RUN.md](docs/FIRST-RUN.md).

## Screens

| Screen | What |
| --- | --- |
| **Status** | Sign-in, the settings, the server (build, API versions, the minimum client version), its gates (playback, transcript text, feature detail), the job runner, the local spool, the evidence report (opens the web view's in the browser) |
| **Uploads** | Each local session: waiting, uploading (with progress), accepted (when and where), refused (why, with Retry); an interrupted upload resumes where the server stopped |
| **Corpus** | Every recording: search and filters, a sortable table that pages as you scroll, the row detail, playback, the local transcript on request, spot-check verdicts |
| **Journey** | The longitudinal analysis: the verdict, the range river, cause and effect, the vibrato story, what not to trust, song scorecards, repertoire threads, the take inspector |
| **Practice** | The coach (songs due for a take, the frontier) and pre-registered goals |
| **Referee** | Blind same-song A/B trials, loudness-matched and band-limited, and the results by year |

The measurement capture (the UMIK-1 ritual) is its own window in 4a, `voice-lab-desktop
--measure`; phase 4b brings it into the main window. Prompted capture follows in 4c.
[docs/design/PHASE-4-CHECKLIST.md](../../docs/design/PHASE-4-CHECKLIST.md) maps every view
and field of voice-journey's React SPA to these screens.

What the server allows decides what you can do: playback (and so the referee's trials)
needs the operator's recorded playback approval on the server; transcript text and feature
detail need their local stores there. Status shows each gate.

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/). From a checkout of this repository:

```sh
uv tool install --python 3.12 ./apps/desktop   # the command voice-lab-desktop
voice-lab-desktop
```

or, to work on it, `cd apps/desktop && uv sync --frozen && uv run voice-lab-desktop`.

- **Linux** (Arch): `sudo pacman -S portaudio alsa-utils pipewire gnome-keyring` (PortAudio
  for playback and capture; `amixer` and `pw-dump` read the UMIK-1's level; the sign-in is
  kept in the Secret Service).
- **macOS**: the PortAudio and FFmpeg the app needs ship in the `sounddevice` and `av`
  wheels. The sign-in is kept in the Keychain. Microphone access attaches to the app that
  starts it (Terminal, iTerm…) until the app bundle of phase 4c.

Update with `git pull --ff-only` and the same `uv tool install --reinstall`.

## Settings

Nothing names a server in the code (the tailnet is configuration): set the server and the
Pocket ID issuer in **Status → Settings…**, or in the settings file. Precedence: `--server`
(one run), then the environment, then the settings file.

| Setting | Default | Environment |
| --- | --- | --- |
| `server_url` | none (`https://voice-lab.<tailnet>.ts.net`) | `VOICE_LAB_DESKTOP_SERVER` |
| `issuer` | none (`https://id.<tailnet>.ts.net`) | `VOICE_LAB_DESKTOP_ISSUER` |
| `client_id` | `voice-lab-desktop` | `VOICE_LAB_DESKTOP_CLIENT_ID` |
| `distance_cm` | `35` (the measurement chain's mouth distance) | |
| `bearer` | `access` (the access token when it is a JWT; `id` sends the ID token) | |
| the data dir | `~/Library/Application Support/voice-lab-desktop` (macOS), `$XDG_DATA_HOME/voice-lab-desktop` (Linux) | `VOICE_LAB_DESKTOP_DATA` |

The settings file is `config.toml` in `~/Library/Application Support/voice-lab-desktop/`
(macOS) or `$XDG_CONFIG_HOME/voice-lab-desktop/` (Linux), or `VOICE_LAB_DESKTOP_CONFIG`.

## Command line

```sh
voice-lab-desktop                       # the window
voice-lab-desktop --measure             # the UMIK-1 measurement window
voice-lab-desktop --check-online        # settings, keychain, Pocket ID and the server
voice-lab-desktop --sign-out
voice-lab-desktop --smoke               # both windows offscreen (a simulated session)
voice-lab-desktop --devices             # inputs, the UMIK-1, its level against the pin
voice-lab-desktop --pin-level           # pin the level (a re-pin is a chain era event)
voice-lab-desktop --import-cal PATH     # miniDSP's per-serial cal file
voice-lab-desktop --sessions            # local measurement sessions and their upload state
voice-lab-desktop --upload              # upload every pending session
voice-lab-desktop --retry-failed        # refused sessions back to pending
voice-lab-desktop --prune-accepted      # delete local copies the server has accepted
voice-lab-desktop --import-capture-data [DIR]   # copy voice-journey-capture's pin, cal file, sessions
```

## Security and privacy

- **Sign-in:** Pocket ID, the public client `voice-lab-desktop`, PKCE (S256), a one-shot
  loopback redirect on `127.0.0.1` (a random port; `--fixed-port` uses 53683); the state,
  nonce, issuer and audience are checked. Tokens live only in the OS keychain.
- **The bearer** goes to the server's door and to the API, which verifies it again (design
  §3.5). A `401` refreshes once; redirects are never followed; no cookies are kept.
- **Audio** is played from memory and never written to disk (D-038). Measurement sessions
  stay in the data dir until the server accepts them; they go only to the configured server.
- **Logs and toasts** carry ids, counts and error codes, never audio, a transcript or a
  token.

## Development

See [docs/DEVELOPING.md](docs/DEVELOPING.md).

## License

MIT, see [LICENSE](LICENSE).
