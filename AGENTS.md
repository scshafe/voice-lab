# AGENTS.md: voice-journey-capture

Voice Journey's native capture client (PySide6 + QML on scshafe-qt), scshafe-dev kind
`native`. It records the UMIK-1 session ritual and uploads it to voice-journey's intake
API. Read README.md and docs/DEVELOPING.md first; the design is voice-journey
`docs/move-to-lubuntu.md` ("Capture client", phase 5).

- Python 3.14 and `uv` (the version pinned in pyproject). Verify (`dev.toml [verify]`):
  `uv run --frozen pytest -v && uv run --frozen voice-journey-capture --smoke`
  (offscreen; Qt warnings fail). The repository is private, so CI does not run: verify
  locally and say so in the PR.
- The intake contract, the `umik1` chain and the ritual belong to voice-journey
  (README "Intake API", `src/intake.mjs`, `manifests/chains.json`, `src/capture.mjs`).
  `chain.py`, `package.py`, `ritual.py` and `tests/fakes.py` mirror them. Change them
  together, and never change voice-journey from here.
- Only the UMIK-1 is supported at launch. A new chain needs the owner and a
  voice-journey registry change first.
- The level guard never fails open. A level that cannot be read, or that moved, or
  that is muted, refuses or aborts. Keep the platform readers small and tested with
  fakes.
- Never commit audio (`*.wav` is ignored), recordings, cal files, tokens or private
  text. Logs hold ids, counts, levels and error codes only. Tokens live only in the
  OS keychain. Never follow redirects or keep cookies toward the server.
- The scshafe-qt wheel is vendored; upgrade it per docs/DEVELOPING.md.
- Work on branches. Merge PRs as `Merge #N: <title>` merge commits, after local
  verification.
