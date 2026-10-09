# Phase 3 progress log

Phase 3 of [VOICE-PLATFORM.md](VOICE-PLATFORM.md) (§9): voice-journey merged into
voice-lab, its server ported to Python, its features served from the PC. Approved by
the owner on 2026-10-09. Written as the work goes, so that a successor can resume it.
Newest entries last.

Rules that bind this work: recordings, consent evidence and weights never enter Git
(`scripts/guard-private-assets` before every push); synthetic audio only; never read
`/data/backups/voice-lab` or the Air's asset root, and nothing is copied from
`/srv/voice-journey`; voice-journey on the laptop stays untouched as the rollback until
the owner says to cut over; infra `host/` changes stop at the owner's hash approval.

## Plan

| Step | What | Where | State |
| --- | --- | --- | --- |
| 1 | Guard rules for voice-journey's private shapes; import voice-journey into `legacy/voice-journey/` | voice-lab | in progress |
| 2 | Import voice-journey-capture into `apps/desktop/` | voice-lab | in progress |
| 3 | Port, leaves: paths, lyrics, reclassify, goals, coach | voice-lab | open |
| 4 | Port, pipeline: index, filter, stt, features, journeys | voice-lab | open |
| 5 | Port: mirror sender + host access + launcher (`apps/mirror/`) | voice-lab | open |
| 6 | Port: watchd (`jobs/`), evidence report, the HTTP surface (`/api/v1`, old paths, `/view/`) | voice-lab | open |
| 7 | Parity harness (fixture corpus), Node vs Python | voice-lab | open |
| 8 | Serving from the PC (stack, host units, infra) | voice-lab, infra | open |
| 9 | Runbooks: corpus move, mirror drop move, redirect, Air vault | voice-lab | open |
| 10 | voice-journey PR #1 carried as a draft branch | voice-lab | open |

## Entries

### 2026-10-09

- Read the design (§3–§5, §8, §9), D-019..D-034, the phase-2 log, voice-journey,
  voice-journey-capture and infra's agent-deploy (phase 6 voice-journey, the PC).
- **Deviation (recorded as D-035):** the owner's phase-3 brief imports *both*
  histories now; D-030 put voice-journey-capture's in phase 4. It goes to
  `apps/desktop/` as §5.2 says, unchanged and outside voice-lab's tooling until
  phase 4 makes it the desktop app; the separate repository stays the live client.
- **Screens** (method of `docs/PUBLICATION-SCREEN.md`, recorded there as §5): nothing
  sensitive in either history. voice-journey: one gitleaks false positive; the
  personal data it had when it was published. voice-journey-capture (private): no
  credential, no private asset; the tailnet name in its config defaults and tests; a
  vendored wheel of the owner's public `scshafe-qt` 0.1.1 in two commits; the
  university address as author. The guard (with the new rules) passes every
  historical tree of both (10/10, 17/17).
- Guard: refuses voice-journey's generated manifests by name and by schema id (all
  `voice-journey*` ids but `voice-journey.chains.v1`), the mirror's `latest.json`, and
  `CloudRecordings.db*` (§4.4).
- voice-journey imported at `7f62275` with `git subtree add` (history kept) into
  `legacy/voice-journey/`. Removed from the imported tree in the same PR, as not
  ported: its repository scaffolding (`.github/`, `deploy/`, `Dockerfile`, `dev.toml`,
  `CLAUDE.md`) and the React SPA (`web/`, `test/web-spa.test.mjs`).
  **Deviation (D-035):** §9 phase 3 serves the SPA unchanged from the PC. It needs the
  private `@scshafe/ui` package at build time, which the public repository and the PC's
  runner cannot read (§6.5); the owner's brief serves the read-only `/view/` instead.
  So the PC serves `/view/` (summaries, no audio, §6.5) in phase 3, and the SPA keeps
  running on the laptop until the cutover; the acceptance item "the four SPA views
  work from the PC" becomes "`/view/` works from the PC".
  The legacy tree passes its own `node --test` (160/160) here; Ruff excludes `legacy/`.
- voice-journey-capture imported at `f281a70` with `git subtree add` (history kept) into
  `apps/desktop/` (D-035). Its repository scaffolding (`.github/`, `dev.toml`,
  `CLAUDE.md`) is not carried; Ruff excludes `apps/desktop/` until phase 4. Its own suite
  passes there (`uv run --frozen pytest`: 139 passed, 2 skipped: the end-to-end intake
  tests that need a voice-journey checkout).
