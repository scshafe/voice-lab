# voice-journey-capture

Voice Journey's capture client: it records the measurement chain's session ritual
from a **miniDSP UMIK-1** as raw 48 kHz / 24-bit WAV, packages it with a checksum for
every file, and uploads it to the voice-journey server's intake, which accepts the
whole session or nothing. It signs in with Pocket ID and is built with
[scshafe-qt](https://github.com/scshafe/scshafe-qt) (PySide6 + the `Scshafe.Ui` QML
module). Design: voice-journey `docs/move-to-lubuntu.md`, "Capture client" (phase 5).

First time? See [docs/FIRST-RUN.md](docs/FIRST-RUN.md).

## What it does

- **The UMIK-1 only (chain `umik1`).** It lists the input devices and picks the
  UMIK-1 by name or USB id (`2752:0007`). Any other microphone, a UMIK-2 included, is
  refused with a message saying so. voice-journey's chain registry is not served over
  its API, so the chain id `umik1`, 48 kHz, 24-bit, mono and the 35 cm default
  distance are fixed in `chain.py`, matching `manifests/chains.json`.
- **The pinned input level (the Phase A guard).** The input level is pinned once and
  logged (`level-pins.jsonl`). It is then read when a session starts, before and after
  every segment, and every second while recording. If the level moved, is muted, or
  cannot be read, the session is refused or aborted, never silently recorded. A
  re-pin is logged as a chain era event. It is read from:
  - **macOS:** CoreAudio, the UMIK-1's input `VolumeScalar` (the slider in Audio MIDI
    Setup).
  - **Linux, ALSA path:** when the UMIK-1 is opened as an ALSA `hw:` device, the
    card's capture mixer control, read with `amixer`.
  - **Linux, PipeWire path:** when the UMIK-1 is reached through PipeWire's JACK
    device, the source node's `channelVolumes` and `mute`, read with `pw-dump`.
- **The ritual**, exactly as voice-journey's `capture.mjs`. Each segment is one file,
  `NN-<key>.wav`:

  | # | Segment | How it ends |
  | --- | --- | --- |
  | 1 | room silence: stand still, say nothing (the noise floor) | 10 s |
  | 2 | 94 dB calibrator tone (only with **94 dB calibrator** on) | 10 s |
  | 3 | sustained /a/, soft | you stop it |
  | 4 | sustained /a/, loud | you stop it |
  | 5 | a slow glide | you stop it |
  | 6 | the anchor song | you stop it |
  | 7 | free practice (unless **Skip free practice** is on) | you stop it |

  Timed segments hold exactly 480,000 frames. Live meters show peak and RMS in dBFS
  and flag clipping. Space records and stops, R redoes the last segment, Escape
  aborts. Before a session starts, the stream is opened at 48 kHz and checked for
  digital silence: macOS delivers zeros when microphone access is denied. Nothing is
  resampled or processed; the cal file is applied at analysis time, never at capture.
- **The package.** `session.json` has the fields `capture.mjs` writes: chain id, input
  level, distance, cal-file hash and the segment map. To these it adds the level
  details, the device, per-segment peak, RMS, clipping and overflow counts, and
  `files[]` (SHA-256 and size for every WAV), so the same document is the intake
  manifest. It is checked against the intake's rules before anything is sent.
- **The upload** follows voice-journey's README "Intake API":
  1. `POST /api/intake/sessions`;
  2. `PUT` each file in 8 MiB `Content-Range` chunks, resuming from the server's
     `receivedBytes`;
  3. `POST …/finalize`.

  A checksum the server rejects is sent once more, then the session is marked
  refused. Sessions are kept locally until the server accepts them, and every launch
  retries the pending ones. An aborted session is kept but never uploaded. Before
  any upload, the local files are re-hashed: a package that changed on disk is never
  sent.

## Install

It needs Python 3.14 and [uv](https://docs.astral.sh/uv/) 0.12.21 (the version
pinned in `pyproject.toml`). The repository is private, so clone it with your GitHub
access (`gh repo clone scshafe/voice-journey-capture`).

**macOS** (the owner's Macs; Apple Silicon or Intel):

```sh
brew install uv            # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.14
gh repo clone scshafe/voice-journey-capture ~/src/voice-journey-capture
cd ~/src/voice-journey-capture
uv sync --frozen           # the exact pins in uv.lock; the sounddevice wheel bundles PortAudio
uv run voice-journey-capture
```

The first recording asks for microphone access for the app that started it
(Terminal, iTerm…). Allow it, or the pre-session check refuses with "digital
silence". To install it as a command instead: `uv tool install --python 3.14 .`
(this resolves to the newest PySide6 6.11.x rather than the lock's exact pin).

**Linux** (Arch):

```sh
sudo pacman -S uv python portaudio alsa-utils pipewire   # amixer and pw-dump read the level
gh repo clone scshafe/voice-journey-capture ~/src/voice-journey-capture
cd ~/src/voice-journey-capture && uv sync --frozen && uv run voice-journey-capture
```

The sign-in is kept in the Secret Service (GNOME Keyring or KeePassXC), as for
mailroom-desktop: see [docs/FIRST-RUN.md](docs/FIRST-RUN.md).

Update with `git pull --ff-only && uv sync --frozen`.

## Command line

```sh
uv run voice-journey-capture                    # the window
uv run voice-journey-capture --devices          # inputs, the UMIK-1, its level against the pin
uv run voice-journey-capture --pin-level        # pin the current level (logged; a re-pin is an era event)
uv run voice-journey-capture --import-cal PATH  # miniDSP's per-serial cal file
uv run voice-journey-capture --sessions         # local sessions and their upload state
uv run voice-journey-capture --upload           # upload every pending session (stored sign-in)
uv run voice-journey-capture --retry-failed     # refused sessions back to pending (after a server fix)
uv run voice-journey-capture --prune-accepted   # delete local copies the server has accepted
uv run voice-journey-capture --check-online     # settings, keychain, Pocket ID and the server reachable?
uv run voice-journey-capture --sign-out
uv run voice-journey-capture --smoke            # offscreen self-test with a simulated UMIK-1
```

## Settings

The defaults are the live values. Precedence: `--server` (one run), then the
environment, then the settings file, then the defaults.

| Setting | Default | Environment |
| --- | --- | --- |
| `server_url` | `https://voice-journey.colobus-stargazer.ts.net` | `VOICE_JOURNEY_CAPTURE_SERVER` |
| `issuer` | `https://id.colobus-stargazer.ts.net` | `VOICE_JOURNEY_CAPTURE_ISSUER` |
| `client_id` | `voice-journey-capture` | `VOICE_JOURNEY_CAPTURE_CLIENT_ID` |
| `distance_cm` | `35` | |
| `bearer` | `access` (the access token when it is a JWT; `id` sends the ID token) | |
| the data dir | `~/Library/Application Support/voice-journey-capture` (macOS), `$XDG_DATA_HOME/voice-journey-capture` (Linux) | `VOICE_JOURNEY_CAPTURE_DATA` |

The settings file is `config.toml`:

- **macOS:** `~/Library/Application Support/voice-journey-capture/`;
- **Linux:** `$XDG_CONFIG_HOME/voice-journey-capture/`;
- anywhere else: `VOICE_JOURNEY_CAPTURE_CONFIG`.

The window's Settings panel writes `server_url` and `distance_cm` to it. During the
move, point it at the temporary server with:

```sh
VOICE_JOURNEY_CAPTURE_SERVER=https://voice-journey-next.colobus-stargazer.ts.net uv run voice-journey-capture
```

## Security and privacy

- **Sign-in:** Pocket ID, with the public OIDC client `voice-journey-capture` and
  PKCE (S256). The redirect goes to a one-shot loopback listener on `127.0.0.1` (a
  random port; `--fixed-port` uses 53683). The state, and the ID token's nonce,
  issuer and audience, are checked.
- **The bearer token** goes to the server's door (oauth2-proxy with
  `--skip-jwt-bearer-tokens`). A `401` refreshes the token once. Redirects are never
  followed: one means the door wants a browser and did not accept the token. No
  cookies are kept.
- **Tokens** live only in the OS keychain: the macOS Keychain, or the Secret Service
  on Linux. Without a keychain the app refuses to sign in.
- **Audio never leaves the machine except to the configured server.** Recordings are
  stored in the data dir, and Git ignores `*.wav`.
- **Logs:** the level log and `upload.json` hold levels, ids, counts and error codes.
  They never hold audio or names.

## Development

See [docs/DEVELOPING.md](docs/DEVELOPING.md). `uv sync --frozen`, then `uv run
pytest`. The tests run offscreen and need no microphone, network or server: they use
a simulated UMIK-1 and level, a fake intake behind a fake door, and a fake Pocket
ID. When a voice-journey checkout is found next to this repository, an end-to-end
test also uploads to the real voice-journey intake.

## License

MIT, see [LICENSE](LICENSE).
