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
| 1 | Guard rules for voice-journey's private shapes; import voice-journey into `legacy/voice-journey/` | voice-lab | done, #17 |
| 2 | Import voice-journey-capture into `apps/desktop/` | voice-lab | done, #18 |
| 3 | Port, leaves: paths, lyrics, reclassify, goals, coach | voice-lab | done, #20 |
| 4 | Port, pipeline: index, filter, stt, features, journeys | voice-lab | done, #21 |
| 5 | Port: mirror sender + host access + launcher (`apps/mirror/`) | voice-lab | done, #22 |
| 6 | Port: watchd (`jobs/`), evidence report, the HTTP surface (`/api/v1`, old paths, `/view/`), the intake compatibility path | voice-lab | done, #24, #25, #27, #28; JS tree emptied #29 (but `capture.mjs`, gated) |
| 7 | Parity harness (fixture corpus), Node vs Python | voice-lab | fixture done (#21); real corpus: owner step |
| 8 | Serving from the PC (stack, host units, infra) | voice-lab, infra | code deployed; journey binds wait for infra#2 (owner hash approval); draft #23 |
| 9 | Runbooks: corpus move, mirror drop move, redirect, Air vault | voice-lab | done, `PHASE-3-RUNBOOKS.md`; Air vault code #26 |
| 10 | voice-journey PR #1 carried as a draft branch | voice-lab | draft #31, `phase3/chains-retire-capture` (gated on a real UMIK-1 session; it deletes `legacy/`) |

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
- The guard (with the phase-3 rules) over every commit of the merged history (voice-lab,
  both imports): 127 commits, 0 failing.
- #17 merged (`ceb9785`). A fresh verify environment resolved pydantic 2.14.0, under which
  strict mypy fails in untouched modules; #17 pins `pydantic==2.13.5` in the dev extra
  (every lock already pins it).
- #18 (capture import) and #19 (dev pins) merged together (`6839306`). The push run of #17
  (38008631241) had failed verify on a second drift of the same kind (a fresh environment
  resolved pycparser 3.11; `test_the_installed_sealing_closure_is_the_locked_one`), so
  nothing was deployed then; #19 pins `cffi==2.1.1`, `pycparser==3.0` in the dev extra. Run
  38010353315: verify, deploy, health green; `scshafe-deploy --verify voice-lab 6839306`
  as `run-voice-lab`: live, containers, witness, tailnet-only all ok.
- **The port, wave 1** (one agent per module group, each in its own worktree, every JS test
  translated case for case, the JS module and its tests deleted in the same commit):
  - `journey/paths.py` (paths.mjs; the chain registry ships in the package as
    `resources/journey-chains.json`, without `calFile`, §5.4), `journey/jsonio.py` (the
    Node pipeline's JSON and number rules: `JSON.stringify(p, null, 2)`, `toFixed`,
    `Math.round`, `String(n)`, checked against node) and `journey/jscompat.py` (every other
    JavaScript value rule: `Number()`, `String()`, truthiness, `localeCompare` as an ICU
    key for ASCII, `Date.parse` with V8's local-time rule, V8's `log2`/`log10`, Node's path
    helpers), consolidated from the ports in one pass;
  - leaves: `lyrics`, `reclassify`, `coach`, `goals` (24 JS cases);
  - pipeline: `index` + `corpus` (the server-side drop listing of `host-access.mjs`),
    `filter`, `stt` (+ the whisper-threads cases), `features` (the extractor moved in as
    `resources/journey_extract_features.py`, algorithm unchanged, its runtime pins in
    `requirements/runtimes/journey-analysis.txt`, not hash-locked yet), `journeys`
    (mulberry32 seeded bootstrap ported bit-exactly);
  - the mirror sender: `apps/mirror/` (stdlib-only package `voice_lab_mirror`, the C
    launcher with a fixed interpreter compiled in and only the `mirror` mode, a launchd
    template, `VOICE_LAB_MIRROR_*` configuration so the drop target (the laptop today, the
    PC later; the Mini today, the Air later) is a plist change, not a rebuild). Its tests
    are in pytest's `testpaths`; `scripts/verify` type-checks `apps/mirror/src`.
  - **Exactness, not tolerance:** `filter`'s frame analysis uses NumPy (the `journey`
    extra, `numpy==2.5.3`, hash-locked in `requirements/home-host-linux.txt`) with int64
    sums, which are exact, so every frame value equals the JS bit for bit (0.54 s against
    Node's 1.67 s for a 5-minute memo); `Math.log10`/`Math.log2` are V8's algorithms
    (the C library differs in the last bit for about 6% of values); means are summed in
    JS order (Python's `sum` is compensated). The design expected a float tolerance for
    the filter (§5.3); none is needed.
  - Python differs from the JS only in documented corners (each module's docstring):
    OS error texts, atomic manifest writes, non-ASCII sort order, legacy non-ISO date
    strings, and malformed inputs where the JS crashed.
- **Parity harness** (`scripts/journey-parity`, `tests/test_journey_parity.py`): the pinned
  Node tree (`1f236b5`) and the Python pipeline over one synthetic corpus (41 generated
  m4a memos, a mirror manifest, a measurement session; real ffmpeg; deterministic fake
  whisper-cli and analysis worker), every stage twice (fresh and resumed): **14/14 stage
  runs identical, 124 files compared, 0 differences at tolerance 0**; 20 differences
  excluded by named rule (wall-clock `updatedAt` stamps no flag pins, and the filter's
  tool-choice entry, which names the Python implementation). The test skips on the PC
  runner's shallow checkout (no `1f236b5`); it ran here. The real-corpus mode is the
  owner's step (runbook).
- Integration tip verified on the PC: 6166 passed, 137 skipped; 4 failures were the
  worktree's environment (a venv made before the pins, and without `pip`), all passing
  after fixing it; build, schema check, guard, `dev check .` clean. Coverage 93% overall,
  `cole_voice_lab.journey` 99.7%.
- infra branch `voice-lab-phase3` (draft PR scshafe/infra#2): the provisioning of
  `journey/` and `memos/`, the drop tool's `pc` layout, the host.conf binds, the Air
  vault's tailnet tag. **Host tree re-signed: `M0.sha256` =
  `83464eccd610362c48ffd2c9c65796bf85eba2e77d32c5ebd764698da3c700d7`, waiting for the
  owner's approval; not installed.**
- voice-journey: the cutover redirect is draft PR scshafe/voice-journey#4 (do not merge
  before the go-ahead; hosted CI green).
- #20–#22 merged (`8e65585`), deployed: run 38013628095 green (verify on the PC runner,
  deploy, health); `--verify` all ok; the sealed release (`61d70e25…`) carries numpy 2.5.3.
  The stacked PRs #21/#22 were merged locally; GitHub marks them merged after their base
  branches were fast-forwarded (lesson: open each PR against `main` and merge in order).
- **Wave 2** (each agent in its own worktree; all JS cases translated):
  - `jobs/watch.py` (watchd, 9 cases): the job runner. A one-shot tick for a systemd timer
    (the two-agreeing-scans rule persists across runs in the watch state), stages run as
    `python -m cole_voice_lab.journey.<stage>`, `voice-lab journey watch|<stage>`; the
    runner has no network, so notifications go to a file outbox
    (`state/journey/notify-outbox/`, `voice-lab.journey-notification.v1`) for a sender that
    does not exist yet (ntfy stays possible where a URL is configured). Units
    `deploy/host/voice-lab-jobs.{service,timer}` (no network, every 5 minutes, inert until
    `/data/voice-lab/journey/manifests` and the owner's `/etc/voice-lab/journey.env`
    exist); infra's validator (`validate_units` of the installed `scshafe-host-release`)
    accepts the set.
  - `api/journey_intake.py` (intake.mjs, 14 cases): voice-journey's `/api/intake/...` as a
    compatibility path. Staged under `incoming/`, re-verified on finalize, converted into a
    capture package the **unchanged** validator accepts (suite
    `voice-journey-measurement`, subject `cole-self`, `room-silence` as room tone, the rest
    `calibration`), published into `raw/` by the intake publisher with a v2 receipt; the
    imported client's own upload code was run against it (cut off, resumed, accepted,
    idempotent). The index lists measurement sessions from `raw/`.
    Open: the session's measurement metadata (input level, distance, segment labels) stays
    in `incoming/.journey-uploads/<id>/session.json`, not in `raw/` or the vault classes.
  - `journey/browser.py` + `api/journey_routes.py` (corpus-browser, 34 + 2 cases): the
    `/api/v1` resources of §3.4 and voice-journey's unversioned paths (same bytes as the JS
    on 18 paths × 4 fixtures), bearer-checked, schemas generated; SPA routes 308 to `/view/`.
  - `view/` (§6.5) and `view/report.py` (evidence-report, 4 cases, byte-identical to the
    JS): summary, recordings, journeys, trends, goals, report; no scripts, no audio,
    transcript or lyric; the forwarded identity must be the allowed subject (or a bearer).
  - The Air vault (D-036): the resume journal, `--if-older-than`/`--skip-unreachable`,
    `vault staleness`, the `memos` class, the per-destination tag, the Air's units and
    launchd server templates. Tested with synthetic interruptions only.
  - `legacy/voice-journey/` emptied (#29) but for `capture.mjs` and its test, which
    voice-journey's draft #1 retires; voice-journey's design docs moved to `docs/journey/`.
- Verified on the PC, the batch's combined tree (`da4613d`, before #28/#29): `scripts/verify`
  6427 passed, 137 skipped, exit 0.
- Prepared, not merged (each needs the owner):
  - scshafe/infra#2 (draft): the provisioning of `journey/` and `memos/`, the drop's `pc`
    layout, `host.conf` binds, `tag:voice-lab-vault-air`; **host tree `M0.sha256` =
    `83464eccd610362c48ffd2c9c65796bf85eba2e77d32c5ebd764698da3c700d7`**;
  - voice-lab #23 (draft): the stack serves journey and `/view/` (binds, `serve --journey`);
    infra's validator refuses it until infra#2's `host.conf` is on the PC;
  - scshafe/voice-journey#5 (draft): freeze the laptop's watcher (cutover step 1);
  - scshafe/voice-journey#4 (draft): the laptop stack becomes a 308 redirect to
    `voice-lab.<tailnet>` (the door stays with every route skipping auth, because the
    laptop's `host.conf` says `DOOR=pocket-id`; no infra change needed);
  - voice-lab draft: voice-journey #1 carried (`GET /api/v1/chains`, the capture module's
    removal, the end of `legacy/`), gated on a real UMIK-1 session.
- Runbooks: [PHASE-3-RUNBOOKS.md](PHASE-3-RUNBOOKS.md) (host preparation, the mirror drop
  fed in parallel from the Mini, the data move, real-corpus parity, serving, cutover,
  rollback, the Air as sender and vault, the 14- and 90-day removals). Facts used, read
  only: the laptop's `/srv/voice-journey` holds 19 MB of manifests, 90 MB of artifacts,
  no measurement sessions, the 1.2 GB phone drop and the 1.5 GB model; the Mini's sender
  is launchd job `ai.voice-journey.mirror` (FDA launcher, `VOICE_JOURNEY_MIRROR_SSH_KEY`;
  the Node mirror also reads `VOICE_JOURNEY_MIRROR_DEST` and `_STATE`).

## Open items and the owner's steps

1. **Approve infra's host tree** `83464ecc…00d7` (infra#2), then runbook §A (stage, provision,
   merge infra#2, pull `~/infra`, whisper-cpp, the model, the analysis runtime).
2. Runbook §B: the PC's drop with the Mini's key and a second Mini launchd job (dual feed).
3. The go-ahead for §C (freeze: voice-journey#5; the transfer approval; the copy), then §D
   (real-corpus parity, recorded here as digests and counts), §E (merge #23; the capture
   client reconfigured; **one real UMIK-1 session**), §F (cutover: `journey.env`, the Mini's
   laptop job off, voice-journey#4), §G is the rollback.
4. **The Air** (§H): the iCloud Voice Memos sync, the mirror sender, the vault (its install
   steps, the tailnet policy, then `HOST_EGRESS_TAILNET`), the sleep/resume and away-from-home
   drill.
5. Still open from phase 2: the real pilot session from the Air; the recovery sheet.
6. Follow-ups (not blocking): a sender for the notification outbox; the capture session's
   measurement metadata into `raw/`/provenance; `/data/voice-lab/journey` and
   `state/journey` (verdicts, goals: human judgements) are not in a snapshot or a vault
   class yet; hash-lock `requirements/runtimes/journey-analysis.txt`; Q-4's separate view
   stack.
