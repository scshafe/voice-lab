# Developing voice-journey-capture

```sh
uv sync --frozen
uv run pytest -v                   # offscreen; any Qt/QML warning fails a test
uv run voice-journey-capture --smoke
```

`dev.toml [verify]` runs the last two. The repository is private, so GitHub Actions
does not run: verify locally before every merge and say so in the PR.

## Layout

| Module | What |
| --- | --- |
| `chain.py` | the UMIK-1 chain (`umik1`, 48 kHz, 24-bit, mono, 35 cm, USB id): must match voice-journey `manifests/chains.json` |
| `config.py` | settings: defaults, `config.toml`, the environment; the Settings panel's writes |
| `devices.py` | input devices, choosing the UMIK-1, the PortAudio input stream (int32 frames) |
| `levels.py` | the level readers: CoreAudio (ctypes HAL), ALSA (`amixer`), PipeWire (`pw-dump`) |
| `pin.py` | the pin, its log, the guard; the cal file |
| `wav.py` | 24-bit WAV writing, meter arithmetic |
| `recorder.py` | one segment: audio thread → queue → writer thread; the pre-session probe |
| `ritual.py` | the plan (capture.mjs's segments), the guard around every segment, `session.json` |
| `package.py` | checksums, the intake's manifest rules client side |
| `store.py` | local sessions and their upload state |
| `upload.py` | the intake client (resumable PUT, finalize, retries) |
| `tokens.py`, `auth.py` | Pocket ID sign-in (PKCE, loopback) and the keychain (from mailroom-desktop) |
| `controller.py`, `qml/Main.qml` | the window |
| `simulated.py` | the simulated UMIK-1 and level for tests and `--smoke` (never offered in the window) |
| `cli.py` | the command line |

## Tests

Nothing needs a microphone, the network or the server:

- **`simulated.py`** is the audio backend and level reader: a 240 Hz tone at -24 dBFS,
  faster than real time, with switches for digital zeros, input overflows and a device
  that will not open.
- **The level readers** are tested against recorded `amixer` and `pw-dump` output and a
  fake CoreAudio HAL.
- **`tests/fakes.py`** runs a fake Pocket ID and a fake intake behind a fake door as
  real HTTP servers on 127.0.0.1. The intake follows voice-journey's `src/intake.mjs`
  and can inject faults: a dropped connection mid-chunk, a corrupted chunk, a 503, a
  door that redirects.
- **`tests/test_e2e_intake.py`** uploads a recorded session to the **real**
  voice-journey intake: `node src/corpus-browser.mjs` from a checkout, with a temporary
  `VOICE_JOURNEY_DATA`, behind a small door that sets the identity headers. It runs
  when `$VOICE_JOURNEY_CHECKOUT` or `../voice-journey` holds a checkout and node >= 22
  is installed, and skips otherwise. It only reads the checkout, and fails if the
  checkout's git status changes.

When voice-journey changes its intake contract (README "Intake API", `src/intake.mjs`)
or the `umik1` chain, update `package.py`, `chain.py`, `tests/fakes.py` and the tests in
the same change.

## What only real hardware proves

- **macOS:** `CtypesCoreAudio`, the HAL calls: device list, name, input channels,
  `VolumeScalar`, mute.
- **Linux:** the PortAudio stream on a real UMIK-1, and `amixer` / `pw-dump` against
  its real card and node.

The parsing and the decisions around them are tested; the calls themselves are kept
small.

## scshafe-qt

scshafe-qt comes from PyPI (`scshafe-qt>=0.1.2,<0.2`; published by trusted publishing
with PEP 740 attestations), and `uv.lock` pins its sha256. 0.1.1 is the security floor:
from it on, every `Text` in its components is `Text.PlainText`. To upgrade within 0.1:

```sh
uv lock --upgrade-package scshafe-qt && uv run pytest
# the lock's sha256 should match the GitHub Release's SHA256SUMS:
gh release download vX.Y.Z -R scshafe/scshafe-qt -p SHA256SUMS -O -
```

A 0.2 upgrade moves the bound in `pyproject.toml` too. Commit `pyproject.toml` and
`uv.lock` together.
