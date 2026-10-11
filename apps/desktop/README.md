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
| **Uploads** | Each local session: waiting, uploading (with progress), accepted (when, where, the receipt's digests), refused (why, with Retry); an interrupted upload resumes where the server stopped. Sessions go through `/api/v1/intake` under this machine's standing home-host approval, each in its ledger (D-021) |
| **Measurement** | The UMIK-1 ritual (voice-journey-capture's, unchanged): the device, the pinned input level, the cal file, the segments, the live meter; Space records and stops, R redoes, Escape asks to abort. A session keeps running if another screen is shown (its sidebar entry shows a dot); a saved session goes to Uploads |
| **Corpus** | Every recording: search and filters, a sortable table that pages as you scroll, the row detail, playback, the local transcript on request, spot-check verdicts |
| **Journey** | The longitudinal analysis: the verdict, the range river, cause and effect, the vibrato story, what not to trust, song scorecards, repertoire threads, the take inspector |
| **Practice** | The coach (songs due for a take, the frontier) and pre-registered goals |
| **Referee** | Blind same-song A/B trials, loudness-matched and band-limited, and the results by year |

`voice-lab-desktop --measure` opens the window on the Measurement screen. Prompted capture
follows in 4c.
[docs/design/PHASE-4-CHECKLIST.md](../../docs/design/PHASE-4-CHECKLIST.md) maps every view
and field of voice-journey's React SPA to these screens.

What the server allows decides what you can do: playback (and so the referee's trials)
needs the operator's recorded playback approval on the server; transcript text and feature
detail need their local stores there. Status shows each gate.

## Install

**Linux**: the uv tool, plus a desktop entry. You need Python 3.12 and
[uv](https://docs.astral.sh/uv/). On Arch, `sudo pacman -S portaudio alsa-utils pipewire
gnome-keyring`:

- PortAudio, for playback and capture;
- `amixer` and `pw-dump`, which read the UMIK-1's level;
- the Secret Service, which keeps the sign-in.

From a checkout of this repository:

```sh
uv tool install --python 3.12 --with-executables-from cole-voice-lab ./apps/desktop
voice-lab-desktop --install-desktop-entry
```

The first command installs two commands: `voice-lab-desktop`, and the lab's own `voice-lab`,
which approves the home host once on this machine (docs/FIRST-RUN.md, "Approve the home
host").

The second adds **voice-lab** to the desktop's application menu, for you only. It writes:

- `~/.local/share/applications/voice-lab-desktop.desktop`, which runs the installed command by
  its absolute path;
- its icon, under `~/.local/share/icons/hicolor/256x256/apps/`.

Both go under `$XDG_DATA_HOME` when that is set. The command never overwrites a file it did not
write unless you pass `--replace`, and it prints how to remove both files.

To update, run `git pull --ff-only` and the same `uv tool install` with `--reinstall`. Then run
`voice-lab-desktop --install-desktop-entry` again; it changes nothing unless the command's path
moved. To remove the app, run `uv tool uninstall voice-lab-desktop`, then the `rm` the entry
printed.

**macOS**: the app bundle. Build it on the Air with `scripts/build-desktop-bundle`; the first
run of an ad-hoc-signed app is described in
[docs/INSTALL-MACOS.md](docs/INSTALL-MACOS.md). Microphone permission attaches to the bundle.

The uv tool above also works on macOS. The PortAudio and FFmpeg the app needs ship in the
`sounddevice` and `av` wheels, and the sign-in is kept in the Keychain. The microphone
permission then attaches to the app that starts it (Terminal, iTerm…).

**To work on the app**, run `cd apps/desktop && uv sync --frozen && uv run voice-lab-desktop`.
The uv tool and this both use the backend's contracts from the same checkout, installed
editable, so keep the checkout where it is. The macOS bundle carries its own copy.

## Settings

Nothing names a server in the code (the tailnet is configuration): set the server and the
Pocket ID issuer in **Status → Settings…**, or in the settings file. Precedence: `--server`
(one run), then the environment, then the settings file.

| Setting | Default | Environment |
| --- | --- | --- |
| `server_url` | none (`https://voice-lab.<tailnet>.ts.net`) | `VOICE_LAB_DESKTOP_SERVER` |
| `issuer` | none (`https://id.<tailnet>.ts.net`) | `VOICE_LAB_DESKTOP_ISSUER` |
| `client_id` | `voice-lab-desktop` | `VOICE_LAB_DESKTOP_CLIENT_ID` |
| `distance_cm` | `35` (the measurement chain's mouth distance; Status → Settings) | |
| `bearer` | `access` (the access token when it is a JWT; `id` sends the ID token) | |
| `catalog_file` | none: Record → Prompts offers the built-in pilot catalogs; a prompt-suite JSON file adds it (docs/FIRST-RUN.md §7) | |
| `prompted_uploads` | `false`: prompted sessions stay on this machine until spike S-1 passes (D-042); the owner sets it per machine after recording the pass | |
| the data dir | `~/Library/Application Support/voice-lab-desktop` (macOS), `$XDG_DATA_HOME/voice-lab-desktop` (Linux) | `VOICE_LAB_DESKTOP_DATA` |

The settings file is `config.toml` in `~/Library/Application Support/voice-lab-desktop/`
(macOS) or `$XDG_CONFIG_HOME/voice-lab-desktop/` (Linux), or `VOICE_LAB_DESKTOP_CONFIG`.

## Command line

```sh
voice-lab-desktop                       # the window
voice-lab-desktop --measure             # the window, on the Measurement screen
voice-lab-desktop --check-online        # settings, keychain, Pocket ID and the server
voice-lab-desktop --sign-out
voice-lab-desktop --smoke               # both windows offscreen (a simulated session)
voice-lab-desktop --devices             # inputs, the UMIK-1, its level against the pin
voice-lab-desktop --pin-level           # pin the level (a re-pin is a chain era event)
voice-lab-desktop --import-cal PATH     # miniDSP's per-serial cal file
voice-lab-desktop --sessions            # local measurement sessions and their upload state
voice-lab-desktop --upload              # upload every pending measurement session (/api/v1/intake); prompted ones go from Uploads
voice-lab-desktop --retry-failed        # refused sessions back to pending
voice-lab-desktop --prune-accepted      # delete local copies the server has accepted
voice-lab-desktop --import-capture-data [DIR]   # copy voice-journey-capture's pin, cal file, sessions
voice-lab-desktop --install-desktop-entry [--replace]   # Linux: the app in the desktop's menu
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
