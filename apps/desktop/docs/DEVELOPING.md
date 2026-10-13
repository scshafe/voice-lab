# Developing voice-lab-desktop

From the repository root, with its development environment (`.venv`, README "Linux"):

```sh
.venv/bin/python -I -B -m pytest -c apps/desktop/pyproject.toml   # offscreen; a Qt warning fails a test
.venv/bin/mypy apps/desktop/src
.venv/bin/ruff check apps/desktop && .venv/bin/ruff format --check apps/desktop
scripts/verify                                                    # all of it, and the backend
```

The repository is private, so GitHub Actions does not run: run `scripts/verify` (and
`dev check .`) before every merge and say so in the PR. The root `pyproject.toml`'s `dev`
extra carries the app's dependencies, so one environment checks both; `apps/desktop/uv.lock`
pins them exactly for `uv sync` and `uv tool install`.

## Layout

| Module | What |
| --- | --- |
| `app.py` | the composition root: the app model (account, settings, navigation), one model per screen, the context properties QML reads |
| `viewmodel.py` | the shared context (settings, session, HTTP, tasks, player) and the property plumbing of every screen's model |
| `api.py` | `/api/v1`, read through the backend's strict contract models; refusals as `ApiRefused`/`ApiUnavailable` |
| `auth.py`, `tokens.py` | Pocket ID sign-in (PKCE, loopback) and the keychain (from voice-journey-capture) |
| `config.py` | settings: `config.toml`, the environment, the Settings dialog's writes; no server in the code |
| `net.py`, `tasks.py` | the one HTTP client (no cookies, no redirects); the worker pool whose results come back on the GUI thread |
| `playback.py` | the one player: PyAV decode, NumPy shaping (gain, 8 kHz low-pass), PortAudio out (D-038) |
| `screens/` | `status.py`, `uploads.py`, `prompts.py` (Record → Prompts), `corpus.py`, `journey.py`, `practice.py`, `referee.py` |
| `corpus/` | the corpus client (D-052, [CORPUS-CLIENT.md](CORPUS-CLIENT.md)): the PC's catalog cached and pinned per session, the local spool of takes with their outboxes, and the sync engine that sends them in order, offline-first; no UI of its own (the prompt studio, step 6, is built on it) |
| `prompted/` | Record → Prompts' catalogs (built-in pilots or a file), its local take store (sessions, takes, decisions, trim recipes), the waveform bins, the capture package and its upload (D-042) |
| `formatting.py`, `journey_charts.py`, `education.py` | the SPA's display rules (`fmt.js`), its chart geometry as `SuiChart` specs (`chartUtils.js` and the chart components), the Journey tabs' static texts |
| `measurement/` | voice-journey-capture's UMIK-1 ritual: the chain, devices, level readers, the pin, the recorder, the session package, the local store and its upload; `controller.py` is the Measurement screen's model |
| `capture/` | prompted capture's PortAudio worker (`--capture-worker`, one process per take), its continuity rule and evidence (D-039), the parent's recorder, and the S-1 harness (`scripts/s1-portaudio`, docs/S-1.md, D-040) |
| `cli.py` | the command line and the windows (`--capture-worker` is routed before settings and Qt) |
| `desktop_entry.py`, `resources/` | the Linux desktop entry (`--install-desktop-entry`) and the icon it and the window use |
| `packaging/macos/build_bundle.py` | not in the wheel: the macOS app bundle's build (`scripts/build-desktop-bundle`; docs/INSTALL-MACOS.md) |
| `qml/Main.qml`, `qml/screens/` | the main window and its screens (`PlayerBar.qml`, `ScreenPage.qml`, `Card.qml` are shared) |
| `qml/VoiceLab/StandIn/` | stand-ins for scshafe-qt's gaps, each marked for removal |

## Tests

Nothing needs a microphone, a speaker, the network or the real server:

- **A real `/api/v1`.** `tests/synthetic.py` writes synthetic journey manifests (72 takes,
  2019 to 2024, two recurring songs) and short AAC tones made with PyAV, and runs the
  backend's own `ApiServer` with the journey routes over them on 127.0.0.1, behind the
  backend's test token verifier. `big_root` is the Corpus table's scale case (7,000 rows).
- **The screens offscreen.** The `window` fixture loads `Main.qml` over a signed-in app model;
  tests click through `objectName`s and read what the screens show. Any Qt or QML warning
  fails a test (`qt_log_level_fail`).
- **The audio device** is `RecordingOutput` (conftest): it keeps what would have played, so
  tests check the decoded samples, the referee's gain and its low-pass.
- **The measurement capture** keeps voice-journey-capture's fakes: a simulated UMIK-1 and
  level, a fake Pocket ID, and voice-journey's intake behind a fake door with injectable
  faults; its end-to-end tests upload to voice-lab's own API and its intake publisher.
- **Prompted capture.** The capture worker runs as a real process over a simulated UMIK-1
  whose ADC times can jump, stall or carry an overflow (`capture/backends.py`), so the tests
  exercise its protocol, its continuity rule and the parent's validation; the S-1 harness runs
  end to end on it (a simulated run never reports S-1 as passed). Record → Prompts' screen
  tests use a fake recorder (`tests/prompted/recorder_fakes.py`), and one test drives the
  screen over the real worker into a package.
- **The corpus client.** `tests/corpus_client/` runs the backend's `ApiServer` with the
  intake staging and the corpus routes over a synthetic lab with a seeded catalog
  (`corpus_rig.PC`), its intake publisher in-process between the client's polls, and takes
  made of generated tones with the worker's evidence. The PC goes down and comes back, the
  app "restarts" as new objects over the same data dir, and another client changes takes
  behind the client's back.
- **The checklist.** Each item of `docs/design/PHASE-4-CHECKLIST.md` names its tests, which
  carry `@pytest.mark.checklist(...)`; `tests/test_checklist.py` fails when the two drift.

## What only real hardware and a person prove

- Playback through a real device, and that it sounds right (the referee's matched playback
  included).
- The measurement capture's level readers and stream on a real UMIK-1 (macOS CoreAudio;
  Linux `amixer`, `pw-dump`, ALSA `hw:`).
- The sign-in with the real Pocket ID and the real door.
- Spike S-1: the worker's continuity on a real UMIK-1 through PortAudio, on macOS (Core Audio)
  and Linux (ALSA `hw:`), with `scripts/s1-portaudio` (docs/S-1.md).
- The macOS bundle: building it, its signature, its first run and the microphone prompt
  (docs/INSTALL-MACOS.md).

## scshafe-qt

scshafe-qt comes from PyPI (`scshafe-qt>=0.2.0,<0.3`; trusted publishing with PEP 740
attestations), pinned with its sha256 in `uv.lock`. When it releases a component that a
stand-in in `qml/VoiceLab/StandIn/` stands for, adopt it in one change: raise the floor,
`uv lock --upgrade-package scshafe-qt`, delete the stand-in and its qmldir line, and run the
suite. The lock's sha256 should match the GitHub Release's `SHA256SUMS`:

```sh
gh release download vX.Y.Z -R scshafe/scshafe-qt -p SHA256SUMS -O -
```
