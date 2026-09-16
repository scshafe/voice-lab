# Plan: Prompt labels, instructions, filtering, theme, and physical-test suite

**Goal:**
- Prompts carry multiple labels and separate Prompt text from Instructions; room tone has no Prompt.
- The sidebar filters by label with OR, and Continue Missing is a prominent primary action above search.
- The app offers System/Light/Dark.
- A built-in physical-test suite drives diagnostic previews.
- A new pilot-v2 catalog is available. Its 34 spoken prompts keep every existing recording by exact prompt hash. The two room-tone prompts change form and start Missing. Nothing is deleted or rewritten (`operator-choices-05.json`).

**Out of scope:** supersession or prompt-mapping machinery; physical deletion of any recording; moving the `pilot` alias; Step 17 cutover; corpus approval/gate changes; recording transfers; changes to raw/consent assets; numerical target changes.

## Context

- **Normative design:** `docs/PROMPT_CATALOG_V2.md` (sha256 `649bc587…` plus the TQ1 wording correction). Its §8.2 step blocks override this file if they ever disagree.
- **Review trail:** the private readiness root `…/20260912-defect-b-vch9e1h7/readiness-20260914-ihozooyb/catalog-v2-design/`, rounds 1–3.
- **Operator decisions:** `operator-choices-04.json` and `operator-choices-05.json` in the readiness root. Code maps are in `readiness-…/feature-maps-01/`.
- **Identity:**
  - Coverage is decided per subject by the per-prompt hash.
  - New fields use `exclude_if`, so v1 bytes and hashes never change.
  - Changed room-tone content starts new coverage; old room-tone takes stay on disk and count only under pilot-v1.
  - `pilot` stays v1 permanently; the live Qt launcher uses `--suite pilot`.
- **Freeze rules:**
  - Keep the live Qt app (`/Applications/Cole Voice Lab.app`) closed from the first Step 2 edit until Step 9 passes.
  - Build no v2 or physical-test preview before Step 9.
  - Physical-test guards (Step 4) land before the physical-test resource (Step 5).
- **Rules from `.claude/plans/2026-09-11-native-recording-ux.md`:**
  - Historical readers are kept indefinitely.
  - Stored evidence is never rewritten.
  - Fixtures are synthetic and private.
  - Swift builds use strict concurrency with warnings as errors.
  - Use an external Swift scratch root, and record every mutation (file, edit, failing test) in the readiness root.
- **Coordination:** this plan runs after, or file-disjoint from, the Step 16 readiness fixes (`RecordingWorkspaceModel.swift`, `recording_takes.py` navigate, discovery, capture controller, harness, waveform). Never edit the same files concurrently.

## Steps

- [x] **Step 1: Write the prompt-suite v2 design.**
  - Deliver: `docs/PROMPT_CATALOG_V2.md`. It covers the model fields, the pilot-v2 coverage consequence (exact hashes, room tones Missing, no deletion), suite identities with `pilot` staying v1, the physical-test purpose and guards, the presentation contract and operator consequences.
  - Verify: three adversarial review rounds with no unresolved design blocker. Round 3 found only plan-sync issues, which are resolved by this rewrite.
  - Specialist: `rigorous-architect`

- [x] **Step 2: Add v2 models, schemas, Swift decoding and the dataset refusal without changing v1 bytes.** *(Verified 2026-09-14: all Verify commands passed, mutations killed, adversarial review approved; evidence `readiness-…/feature-step-2-01` and `review-feature-step-2-01`.)*
  - Deliver (design §8.2 Step 2):
    - `models.py` v2 fields with `exclude_if`: P1–P4, S1–S5, S7, `spoken`, `allows_quiet`, `content`, `require_text()`, and the five `allows_quiet` replacements
    - `pydantic>=2.13.4,<3`
    - `_selected_suite` copies `schema_version`
    - guards #1, #8 (inside the selection loop after `suite` is bound), #10, the v2 half of #9, and the Step 2 form of #2/#3 (physical-test catalogs refused in builder, verify, the first statement of open-context, and `open_group`)
    - `diagnostic_roots.is_physical_test_suite_id`
    - regenerated schemas
    - Swift `RecordingPromptPresentation` without `text`, and `RecordingSemanticValidator` P1–P4/S1/S2/S4/S7
    - the Step 2 card
    - v2 conformance cases in `tests/test_recording_contracts.py`
    - tests TM1–TM4, TS1, TU0, TG2, TQ1
  - Verify:
    - `.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy src/cole_voice_lab && .venv/bin/python -B -m cole_voice_lab.schema --check`
    - `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -B -m pytest -q tests/test_models.py tests/test_resources.py tests/test_schema.py tests/test_recording_contracts.py tests/test_capture.py tests/test_capture_console.py tests/test_desktop.py tests/test_desktop_window.py tests/test_recording_library.py tests/test_recording_packages.py tests/test_recording_takes.py tests/test_recording_analysis.py tests/test_recording_index.py tests/test_recording_service.py tests/test_native_recording_application.py tests/test_datasets.py tests/test_experiments.py tests/test_cli.py tests/test_qt_launcher_isolation.py`
    - `scripts/verify-native-recording --suite contracts|workspace|transport|package-preparation --output "$(mktemp -d /private/tmp/cvl-native-XXXXXX)/<suite>"` (one run per suite)
    - With the screen unlocked, `--suite shell` and `--suite accessibility`, each with its own fresh output directory.

- [x] **Step 3: Prove the pilot-v2 coverage consequence and add the read-only coverage report.** *(Verified 2026-09-14: all Verify commands passed, mutations killed, review approved; evidence `feature-step-3-01`, `review-feature-step-3-01`. Follow-up noted: design §8.3 TC3 should also list `provenance/recording-takes/.store-head.json` as rewritten by open-context.)*
  - Deliver (design §3.3, §3.4, §7.2):
    - `prompt_catalogs.py`, with pins for `pilot-v1.json` and `evaluation-v1.json`, `load_builtin_prompt_suite`, and `legacy_pilot_v1` determined by content
    - the reordered sidecar-less rule; sidecar-less rows carry `H(pilot-v1)` as their source catalog
    - public `is_retained` and `history_entry_for_take`
    - `prompt_coverage.py` and `voice-lab prompts coverage-report`
    - tests TC1–TC3 on a synthetic in-memory pilot-v2, plus TD1, TD2 and the TQ1 fixture extension
  - Verify:
    - static checks as in Step 2
    - `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -B -m pytest -q tests/test_prompt_catalogs.py tests/test_pilot_v2_coverage.py tests/test_prompt_coverage.py tests/test_recording_library.py tests/test_recording_index.py tests/test_recording_index_lock_lifetime.py tests/test_recording_navigation.py tests/test_recording_discovery*.py tests/test_recording_legacy_*.py tests/test_recording_takes*.py tests/test_recording_packages.py tests/test_recording_contracts.py tests/test_recording_service.py tests/test_recording_edits.py tests/test_desktop.py tests/test_cli.py tests/test_qt_launcher_isolation.py`
    - `scripts/verify-native-recording --suite transport`
  - Specialist: `codd`

- [x] **Step 4: Refuse physical-test evidence outside diagnostic roots.** *(Verified 2026-09-14: all Verify commands passed, mutations killed, `doddy` review approved; evidence `feature-step-4-01`, `review-feature-step-4-01`.)*
  - Deliver (design §5.2, §5.3):
    - the rest of `diagnostic_roots.py`, `DiagnosticRootMarker`, `schemas/diagnostic-root.schema.json`, and `voice-lab assets init --diagnostic physical-test`
    - final guards #2/#3, plus #4, #5, #6, #7, #7a, the root half of #9, and #11
    - the physical-test subset prefix
    - builder `--expect-catalog-sha256`
    - Swift candidate narrowing and the "Prepare diagnostic package" title
    - TP1, with synthetic suites only
  - Verify:
    - static checks
    - `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -B -m pytest -q tests/test_diagnostic_roots.py tests/test_datasets.py tests/test_recording_packages.py tests/test_references.py tests/test_intake.py tests/test_consent.py tests/test_native_recording_application.py tests/test_recording_service.py tests/test_recording_takes.py tests/test_capture.py tests/test_capture_console.py tests/test_desktop.py tests/test_schema.py tests/test_cli.py tests/test_qt_launcher_isolation.py`
    - `scripts/verify-native-recording --suite package-preparation`
  - Specialist: `doddy`

- [x] **Step 5: Author `pilot-v2.json` and `physical-test-v1.json`.** *(Verified 2026-09-14: all Verify commands passed, mutations killed, review approved; evidence `feature-step-5-01`, `review-feature-step-5-01`. Disclosed: the implementer read `physical-…/settings.json` once, read-only, to copy the P04/P05/P08 wording; the operator should acknowledge. TS2 cases live in `prompt_suite_v2_cases()`, not `synthetic_contracts()`.)*
  - Deliver (design §4):
    - both resources by contract, each with its pin in the same commit; `H(pilot-v2)` must equal `0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a028`
    - aliases `pilot-v2` and `physical-test-v1`; `pilot` stays v1
    - CLI choices and v2 `prompts show`
    - TR1 and TS2; TC1/TC2 and TQ1 re-run on the real resource
  - Verify:
    - static checks
    - `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -B -m pytest -q tests/test_resources.py tests/test_prompt_catalogs.py tests/test_pilot_v2_coverage.py tests/test_prompt_coverage.py tests/test_recording_contracts.py tests/test_recording_library.py tests/test_recording_index.py tests/test_recording_navigation.py tests/test_native_recording_application.py tests/test_desktop.py tests/test_cli.py tests/test_qt_launcher_isolation.py`
    - `scripts/verify-native-recording --suite contracts`

- [x] **Step 6: Present Labels, Prompt and Instructions in the workspace.** *(Verified 2026-09-14: workspace, shell and accessibility suites passed with the screen unlocked, strict build warning-free, 7 TU1 mutations killed, review approved with non-blocking notes only; evidence `feature-step-6-01` (first pass, cut off), `feature-step-6-02` (completion), `review-feature-step-6-01`. Deviation recorded: the §6.3 40% rule is "natural height up to 40% of the card's content area" via a custom Layout; chips carry `recording.labels`.)*
  - Deliver: design §6.1–§6.3 chips and script layout; row icons; spec amendment in `docs/NATIVE_RECORDING_UX.md` §§4–5; TU1 presentation cases.
  - Verify: `scripts/verify-native-recording --suite workspace`. With the screen unlocked, `--suite shell` and `--suite accessibility`.

- [x] **Step 7: Add a label OR filter and a prominent Continue Missing.** *(Verified 2026-09-14: workspace, transport, shell and accessibility suites passed with the screen unlocked, 10 mutations killed, review approved with non-blocking notes; evidence `feature-step-7-01`, `review-feature-step-7-01`. Carried into Step 8: harden the transport "above search" frame guard and assert `library.labels` is not a text field in the accessibility harness. Disclosed: the transport harness now calls `NSApplication.finishLaunching()` with no window shown.)*
  - Deliver: design §6.4; Continue Missing above search with a prominent style and an accessibility identifier; the transport harness focus assertion; TU1 filter cases.
  - Verify: `scripts/verify-native-recording --suite workspace` and `--suite transport`. With the screen unlocked, `--suite shell`.

- [x] **Step 8: Support System/Light/Dark appearance.** *(Verified 2026-09-15: workspace, waveform, transport, shell and accessibility suites passed with the screen unlocked, strict build warning-free, 8 of 9 mutants killed (survivor: orange take-history error text, cosmetic, no hosted test), review approved with non-blocking notes; evidence `feature-step-8-01`, `review-feature-step-8-01`. The Step 7 hardenings (a) and (b) landed here. Disclosed: the appearance preference lives in the bundle-id defaults domain shared by every preview copy; the harness "Light" registration uses the volatile registration domain of `.standard`, the persistence test uses a path-named suite; the menu placement is proven by a scratch app until the Step 9 build.)*
  - Deliver:
    - a View ▸ Appearance menu, with the stored preference applied at the `Window` scene in `RecordingApp.swift`, never inside `RecordingWorkspace`
    - switching back to System takes effect without relaunch
    - a colour audit
    - TA1, using `UserDefaults(suiteName:)`
  - Verify: `scripts/verify-native-recording --suite workspace` and `--suite waveform`. With the screen unlocked, `--suite shell` and `--suite accessibility`.

- [x] **Step 9: Full focused and repository verification of this plan.** *(Verified 2026-09-15: `scripts/verify` exit 0 (ruff, mypy, native `all`, pytest 2638 passed / 1 pre-existing skip, build, schema check, private-asset guard) followed by `verify-native-recording --suite all` into a fresh private output with an external scratch root; run under the stage driver with 41 screen observations, all unlocked, and no source drift; evidence `readiness-…/feature-step-9-verify` and `feature-step-9-logs`.)*
  - Verify (screen unlocked): `scripts/verify`, then `scripts/verify-native-recording --suite all` with a fresh output directory and an external scratch root.

## Done criteria

- Every step is ticked, with evidence in the readiness root.
- `scripts/verify` passes.
- v1 bytes and hashes are proven unchanged.
- Synthetic fixtures prove that under pilot-v2 every spoken take (native, sidecar, sidecar-less) counts, both room tones show Missing, and pilot-v1 coverage and every evidence file are unchanged (TC1–TC3).
- The one-off diagnostic catalogs never switch.
- A diagnostic preview built from `physical-test-v1` in a marked diagnostic root shows Labels, Prompt and Instructions correctly.
- The operator is notified to resume physical testing.

**Amended 2026-09-14:** Rewritten to match the final design. The operator decided to forget unmigratable room-tone recordings rather than build supersession, and rounds 2 and 3 corrected the order to guards before resources, with concrete verify commands.
