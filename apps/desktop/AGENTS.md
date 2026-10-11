# AGENTS.md: voice-lab-desktop

voice-lab's desktop app (design [VOICE-PLATFORM.md](../../docs/design/VOICE-PLATFORM.md)
§6): PySide6 + QML on scshafe-qt, over the lab's `/api/v1`. The repository's own
[AGENTS.md](../../AGENTS.md) binds this directory too; what follows is specific to it.

- One distribution, `voice-lab-desktop`, Python 3.12 (D-031, D-037). It depends on the
  backend distribution (`cole-voice-lab`, from this repository) for the `/api/v1` contracts
  and, from 4b, the capture package and the D-021 approval and ledger. Read every server
  answer through those strict models (`api.py`); never parse the API's JSON by hand.
- Verify with the repository's `scripts/verify`: it runs Ruff and strict mypy over
  `apps/desktop`, this suite as its own pytest run (`-c apps/desktop/pyproject.toml`, with
  pytest-qt, offscreen; any Qt or QML warning fails a test) and builds the wheel. On its
  own: `.venv/bin/python -I -B -m pytest -c apps/desktop/pyproject.toml` from the
  repository root.
- Tests drive the screens against a real `/api/v1` started on synthetic manifests
  (`tests/synthetic.py`), with a simulated UMIK-1 and a stand-in audio device. Nothing here
  needs a microphone, a speaker, the network or the real server; never point a test at
  them. `docs/design/PHASE-4-CHECKLIST.md` names the tests that tick each item, and
  `tests/test_checklist.py` keeps the two in step.
- The measurement capture (`measurement/`) is voice-journey-capture's ritual, imported
  unchanged. The level guard never fails open: a level that cannot be read, that moved, or
  that is muted refuses or aborts. Only the UMIK-1 is supported; a new chain needs the owner
  and the chain registry first.
- `qml/VoiceLab/StandIn/` holds stand-ins for components scshafe-qt does not have yet
  (§6.4). Each is marked for removal and has the API asked of scshafe-qt
  (docs/design/PHASE-4-LOG.md, "scshafe-qt asks"). When scshafe-qt releases one, adopt it
  and delete the stand-in in the same change.
- Plain text only: every string from the server is shown with `Text.PlainText`. Logs and
  toasts carry ids, counts and error codes, never audio, a transcript, a token or a name.
- Audio is played from memory and never written to disk (D-038); recordings, cal files and
  tokens never enter Git (`*.wav` and the data dirs are ignored; tokens live only in the OS
  keychain). Never follow redirects or keep cookies toward the server.
