# Prompt catalog v2: labels, instructions, pilot-v2 and the physical-test suite

- **Status:** draft, revision 3, 2026-09-16. Revision 2 (2026-09-14) was revised after adversarial review rounds 1 and 2 and after the operator decision in `operator-choices-05.json`, which removes supersession; Round 3 has not reviewed it. Appendix A maps every review finding to its resolution. Plan `.claude/plans/2026-09-14-prompt-labels-instructions-test-suite.md`, Step 1. Revision 3 records what the corpus store (`.claude/plans/2026-09-15-catalog-in-database-corpus-store.md`, D-017, `docs/CORPUS_STORE.md`) changes here: catalogs are published from a database, the app receives its catalog from the recording service, settings carry no catalog, and `--expect-catalog-sha256` no longer exists. §7.7 is new; §A.5 lists every revision-3 change. Revision 3 has not been adversarially reviewed.
- **Roots named in this document:**
  - **R** is the private readiness root `…/20260912-defect-b-vch9e1h7/readiness-20260914-ihozooyb/`. It holds `operator-choices-04.json`, `operator-choices-05.json`, the code maps, and the reviews `R/catalog-v2-design/review-{integrity,trust,operator}.md` and `R/catalog-v2-design/round2-{sequencing,supersession,legacy-mapping-qt}.md`.
  - **B** is the defect root above R.
  - **A** is the asset root a command operates on. Tools never write their reports under A.
- **Code map:** `R/feature-maps-01/catalog-identity.md` §4; every binding has a disposition in §8.1.
- **Binding rules:**
  - `operator-choices-05.json`: "Just forget/remove/delete old recordings that can't be migrated", recorded with the interpretation that forgetting means not counting; nothing is deleted or rewritten.
  - `docs/NATIVE_RECORDING_UX.md` §§4–6 and 9, plus `:340-341` ("Preserve historical format readers indefinitely").
  - `DECISIONS.md` D-010 (`:82`, prompt suites are never migrated) and D-013 (`:141-157`, silent tasks are instructions; category decides silence).
  - `docs/DESKTOP.md:314-316` (prompt content is recording identity).
  - `.claude/plans/2026-09-11-native-recording-ux.md:34-51`: one canonical implementation; no evidence rewrite; raw and consent never transformed; schema migrations separate from backfills; the `*_migration` convention.

Line citations come from the 2026-09-14 working tree. `recording_discovery.py`, `recording_takes.py` (navigate) and `RecordingWorkspaceModel.swift` are under concurrent edit, so they are cited by symbol where lines have drifted.

Notation:
- `H(x)` is `canonical_sha256(x)` (`src/cole_voice_lab/canonical.py:27-40`).
- **Built-in** means a prompt-suite resource pinned in `BUILTIN_PROMPT_SUITE_SHA256` (§2.5).
- MUST and MUST NOT are normative.
- Bare file names are under `src/cole_voice_lab/`; Swift files are under `apps/macos/Sources/`.

## 1. Goals and non-goals

| ID | Goal | Source |
| --- | --- | --- |
| G1 | Several labels per prompt; the category is the primary label; the sidebar filters labels with OR | `operator-choices-04.json` `design_choices.labels` |
| G2 | Prompt (spoken text) is separate from Instructions; silent room tone has no Prompt, and its instruction appears in a different section | `operator-choices-04.json` `operator_request_verbatim` |
| G3 | A new pilot catalog version, pilot-v2, with explicit instructions. Its 34 spoken prompts keep every existing take through their unchanged prompt hashes. Its two room-tone prompts change form, so no existing take counts for them | `design_choices.catalog`, narrowed by `operator-choices-05.json` |
| G4 | A built-in physical-test suite, one prompt per procedure step, only for diagnostic previews with a private root and subject | `design_choices.test_suite` |
| G5 | v1 bytes and hashes are unchanged; pilot-v1, evaluation-v1 and the known private catalogs read stored evidence exactly as today (§3.4 names the one deliberate difference); the live Qt launcher never loads a v2 resource | plan Context; `.claude/handoff.local.md` |
| G6 | No recording, raw package, sidecar, manifest, provenance record, consent or `resources/pilot-v1.json` is deleted, moved or rewritten | `operator-choices-05.json` `agent_interpretation`; `.claude/plans/2026-09-11-native-recording-ux.md:34-51` |

Non-goals:
- Supersession, or any other rule that lets a take count toward a prompt with a different hash. Coverage stays exact (§3.1). Revision 3: database catalogs add one bounded exception, the keep-recordings alias (§3.1, §7.7); built-in catalogs have none.
- Physical deletion of old room-tone takes. Any future deletion is a separate, irreversible operation that needs its own explicit operator confirmation. Nothing in this design performs or prepares it.
- Theme (plan Step 8), except the scene-level constraint in §8.2.
- Continue Missing placement (plan Step 7), except the filter interplay in §6.4.
- Step 17 cutover, corpus or gate changes, new corpus prompts, numeric targets.
- Transfers of recordings between roots. §3.4 states the fail-closed result if a pilot-v2 package is transferred anyway.
- Mapping the one-off catalogs `cole-physical-diagnostic-20260913` and `cole-physical-diagnostic-20260914` (`B/physical-20260914T013126Z-i649qel_/settings.json`, `B/physical-20260914T064801Z-c65af236/settings.json`). Their takes stay readable through their own catalogs.
- Carrying purpose inside `CaptureManifest` v1.
- Temporary old/new routing (§3.8).

## 2. Model: `cole-voice-lab.prompt-suite/v2`

### 2.1 Decision: extend `Prompt` and `PromptSuite`, do not fork them

Many contracts embed `Prompt` and `PromptSuite` (`models.py:55-95`): groups, reservations, takes, history entries, navigation results, package maps, settings, sidecars, dataset entries and audition plans. Forking would need `Prompt | PromptV2` unions, and those do not work here: `Prompt` has no discriminator, so a v1-shaped object matches both branches, and exact containment checks such as `prompt in group.catalog.prompts` (`recording_contracts.py:199`) would compare values of different types.

Old readers already fail closed on v2 data: the `schema_version` literal rejects v2 (`models.py:82`), and `extra="forbid"` rejects the new keys (`models.py:32-33`). Container schema versions (`recording-group/v1`, `capture-reservation/v1`, …) therefore stay unchanged.

### 2.2 Fields

All fields land in Step 2.

| Field | Declaration | v1 suite | v2 suite |
| --- | --- | --- | --- |
| `PromptSuite.schema_version` | `Literal[".../v1", ".../v2"]`, default v1, always serialized | v1 | v2 |
| `PromptSuite.purpose` | adds `"physical-test"` | forbidden | §5 |
| `Prompt.text` | `str \| None = Field(default=None, min_length=1, max_length=2000, exclude_if=lambda v: v is None)` | required | S2/S3 |
| `Prompt.category` | adds `"procedure"` | forbidden | physical-test only |
| `Prompt.labels` | `tuple[PromptLabel, ...] = Field(default=(), max_length=8, exclude_if=lambda v: not v)`; `PromptLabel` matches `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`, 2–32 chars | forbidden | optional |
| `Prompt.instructions` | `str \| None = Field(default=None, min_length=1, max_length=2000, exclude_if=lambda v: v is None)` | forbidden | S2 |

`prompt_id`, `style` (1–160 characters, required in both versions), `reference_candidate`, `holdout` and `expected_seconds` do not change.

An explicit `"labels": []`, `"text": null` or `"instructions": null` parses as absent, and canonical output omits it. Rounds 1 and 2 checked this on the installed pydantic 2.13.5: `exclude_if` applies to Python and JSON dumps, re-validating a dump does not mark excluded fields as set, and model equality ignores `model_fields_set`.

### 2.3 Validation rules

**Prompt-level rules** need no context and apply to every embedded `Prompt`:
- **P1** `text`, `style` and (if present) `instructions` contain a non-whitespace character. This extends `models.py:73-78`.
- **P2** `text is None` ⇒ `category ∈ {room-tone, procedure}`.
- **P3** `category == procedure` ⇒ `text is None ∧ instructions is not None`.
- **P4** Labels are unique, and no label is a reserved token. Reserved tokens are:
  - the nine category tokens;
  - the derived tokens `reference-candidate` and `holdout`;
  - the status and filter vocabulary `all`, `attention`, `missing`, `recorded`, `verified`, `draft`, `needs-review`, `saved-checking`, `pending`, `running`, `invalid`, `failed`, `unknown`, `removed`, `rejected`, `kept` (`RecordingWorkspace.swift:137-148`; `recording_index.py:1504`);
  - the section words `prompt`, `instructions`, `guidance`, `read-aloud`, `silent`, `do-not-speak`.

**Suite-level rules** live in the `PromptSuite` validator next to `models.py:87-95`:
- **S1 (v1 exactness)** A v1 suite requires `text` on every prompt and forbids `labels`, `instructions`, category `procedure` and purpose `physical-test`. Every v1 file that validates today still validates.
- **S2** In a v2 suite, category `room-tone` ⇒ `text is None ∧ instructions is not None`.
- **S3** In a v2 suite, any other category except `procedure` ⇒ `text is not None`.
- **S4** In a v2 suite:
  - `purpose == physical-test` ⇔ `suite_id` starts with `cole-physical-test-`.
  - In physical-test suites, every prompt's labels contain `test` and every `reference_candidate` is false.
  - In other suites, no label is `test` and no category is `procedure`.
- **S5** Unique IDs and the holdout rule (`models.py:87-95`) are unchanged, so physical-test prompts have `holdout: false`.
- **S7 (v2 text hygiene)** In a v2 suite, `text`, `instructions` and `style` contain no Unicode `Cf` character and no `Cc` character other than `\n`. The P1 `.strip()` check keeps zero-width and bidi controls. S7 is suite-level, so v1 validity is unchanged. pilot-v1's text and style contain no such character (checked 2026-09-14), so its 34 spoken prompts pass S7 inside pilot-v2.

P5 and S6 were supersession rules and are retired. The other numbers stay, so review citations remain valid.

### 2.4 Category, labels and derived helpers

- **Category** is the primary label: implied, never repeated in `labels` (P4).
- **Effective labels** are used only for presentation and filtering (§6.1).
- **Behavior keys on category only**, never on labels:
  - capture kind and path (`capture.py:694-705`);
  - package room-tone rules (`recording_packages.py:462-470`);
  - dataset refusal (`datasets.py:160-161`);
  - D-013 (`DECISIONS.md:153-154`): "Category, not prompt ID or text matching, determines silence".
- **New read-only helpers** on `Prompt` (not fields):
  - `spoken` is `category ∉ {room-tone, procedure}`. A v1 room tone has `text` but is not spoken.
  - `allows_quiet` is `not spoken`.
  - `content` is `text` if `spoken`, else `instructions ?? text`. P2, P3, S2 and S3 make it non-null.
  - `require_text()` returns `text` or raises `ContractError`. Every `str`-typed consumer of `text` that strict mypy flags uses it, for example `experiments.py:151` and the Qt script view (`desktop_window.py:1199`). v1 prompts never raise.
- **Replacements.** These inline `category == "room-tone"` quiet checks become `prompt.allows_quiet`, so every site derives the analysis-job digest input the same way (`recording_takes.py:176-197`, `:359-367`). v1 values do not change.
  - `recording_takes.py:366`
  - `recording_analysis.py:500`
  - `recording_packages.py:388`
  - `recording_library.py:517`
  - `recording_index_backfill.py:173`

### 2.5 Hash stability, Pydantic floor and built-in pins

Golden values, recomputed 2026-09-14 with `.venv/bin/python` and pinned by tests TM1 and TR1 (§8.3):

| Artifact | File SHA-256 | Canonical `H` |
| --- | --- | --- |
| `pilot-v1.json` | `ed188aad76a9f0741acbb93b654bca24574b37b90bfa5b4836f4158cdf155b93` | `eac118a995a1e184d5ed20b9ee23c3e0924c272b28d87308df2abeee1f8772d0` |
| `evaluation-v1.json` | `559898769afd88ba749f78c880f9914440b643783bded09b3ebbb1dd7e267a1b` | `2854decad263e9e82a131f130782ff7c846d4ffd14c21eab87cab8cca2e6caf2` |
| pilot-v1 `room-tone-start` | — | `b59836ab0b8c1e2ede111471c9f69571d38fc1270d88faa7138e32ea7c5c5664` |
| pilot-v1 `room-tone-end` | — | `6bb832ddefda164d24fe2414e8e7e1e0ece9e8413e51e0cca1539351e6401798` |
| `pilot-v2.json` (derived from the §4 contract) | not pinned | `0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a028` |
| pilot-v2 `room-tone-start` | — | `0377c02d0dd8bfbee08367edc73e016b0d156a9fc112aa7dc099a4f116eb90fb` |
| pilot-v2 `room-tone-end` | — | `105b37fa88783ade0bb9fb995ae528ca814a54e935759b8978008507fb1f60d1` |
| `physical-test-v1.json` (Step 5 resource, 16 prompts) | not pinned | `b17cc176ee36ac5aa053c949e7ff44ce6e1b0c13b95b4cf7184693c09e48750b` |

The pilot-v2 values come from pilot-v1's prompt dumps, with each room tone's `text` moved to `instructions`, under a v2 header with `suite_id` `cole-pilot-v2`. They assume exactly the §2.2 serialization, so a Step 2 model that serializes differently fails TR1 when the resource lands. The file SHA-256 depends on formatting and is not pinned. pilot-v2's 34 spoken prompt hashes equal pilot-v1's, and pilot-v1 shares no prompt hash with evaluation-v1.

**Pydantic floor.** `pyproject.toml:15` declares `pydantic>=2.11,<3`, but a local pydantic 2.11.5 install has no `exclude_if`. The repository already depends on it (`recording_contracts.py:447`, `recording_takes.py:265`). Raise the floor to `pydantic>=2.13.4,<3`: the lowest version verified locally, matching `requirements/capture-kit-runtime.artifacts.txt:4`.

**Built-in pins. Decision: keep them, with no trust role.**
- Released built-in resources are immutable and never removed.
- `prompt_catalogs.py` pins each resource's canonical hash in `BUILTIN_PROMPT_SUITE_SHA256`, in release order with `pilot-v1.json` first.
- `load_builtin_prompt_suite(resource)` refuses any name that is not a key of the table, reads only that package resource, and raises `SecurityError` when its `H` differs from the pin.
- Why they stay:
  - **Closed names.** Settings generation, `prompts show`, the coverage report (§7.2) and the sidecar-less rule (§3.4) name built-ins through this loader. A working-directory file named like a suite never wins, unlike the path-first `load_prompt_suite` (`resources.py:17-19`).
  - **Integrity.** The sidecar-less rule attributes legacy files through pilot-v1's transcripts, so pilot-v1 must be the genuine resource. Resource JSON is not part of the build identity: `recording_service_build_identity` fingerprints `*.py` files and migrations only (`recording_service.py:1230-1239`), while the release copies `.json` resources (`native_recording_application.py:189-230`). The load-time pin is what detects an edited resource in a frozen runtime.
- Each pin lands in the same commit as its resource. Step 3's table holds `pilot-v1.json` and `evaluation-v1.json`; Step 5 adds `pilot-v2.json` and `physical-test-v1.json`.
- **Revision 3.** Built-ins are also the seeds of the corpus store (`voice-lab catalog init --from-builtin`, §7.7), which copies their prompts with unchanged digests. Settings generation no longer names a built-in, because settings carry no catalog. `prompts show`, the coverage report and `finetune` still resolve an exact built-in name before any published catalog name. The recording service build identity now also fingerprints the corpus store and prompt-alias migrations (`recording_service_build_identity`); resource JSON is still not part of it.

### 2.6 JSON schema generation

- **Generator.** Unchanged (`schema.py` `SCHEMA_MODELS`). The shared `$defs/Prompt` and `$defs/PromptSuite` change in every embedding schema. Regenerate with `scripts/generate-schemas` in Step 2; `scripts/verify` runs `--check`.
- **Tuples only, never `set` or `frozenset`.** The Swift validator fails closed on any keyword outside `RecordingContracts/RecordingSchemaValidator.swift:92`, and that list has no `uniqueItems`. The new fields emit only `anyOf`, `default`, `items`, `maxItems`, `pattern`, `minLength`, `maxLength` and `type` (checked in rounds 1 and 2).
- **New schema in Step 4.** `schemas/diagnostic-root.schema.json` (§5.2).

### 2.7 Swift decoding and semantic validation

- **`RecordingUI/RecordingPromptPresentation.swift:5-28`**
  - Decodes `text: String?` (absent and `null` mean the same), `instructions: String?`, `labels: [String]`, `reference_candidate` and `holdout`, and takes the catalog `purpose` from its caller.
  - Replaces the public `text` property with the §6.1 derived fields. The compiler then flags every consumer: `RecordingWorkspace.swift:122`, `RecordingWorkspaceModel` (`filteredPrompts`, and `promptText` in the transition diagnostic), `RecordingHarness/RecordingHarness.swift:171-200` and `Tests/RecordingWorkspaceTests/RecordingWorkspaceTests.swift:63`. Round 2 confirmed that list is complete. An optional `text` alone would still compile at `RecordingWorkspace.swift:122` (`prompt?.text ?? placeholder`) and silently show the placeholder.
  - Missing `prompt_id`, `category`, `style` or `expected_seconds` still throws.
  - Once `purpose:` is added, the catalog mapping in `RecordingWorkspaceModel` (`.map(RecordingPromptPresentation.init)`) needs a closure.
- **Readers the compiler cannot flag.** `RecordingPackagePreparation.swift:36` already falls back to `promptID`. The fixture in `RecordingWorkspaceModel.init(fixture:)` stays v1-shaped and valid.
- **Step 2 card.** Until Step 6 replaces the layout (§6.3), `RecordingWorkspace.script` shows `displayPrompt ?? displayInstructions` under a banner chosen by kind: `READ ALOUD` for spoken prompts, `SILENT ROOM TONE · DO NOT SPEAK` for room tone and `TEST STEP · FOLLOW INSTRUCTIONS` for procedure steps. A room tone or procedure step never shows the placeholder or `READ ALOUD` (TU0).
- **Schema defaults.** Normalization inserts declared defaults (`labels: []`, `text: null`, `instructions: null`) into validated copies (`RecordingSchemaValidator.swift:63-88`). Every Swift consumer MUST treat `null` and empty arrays as absent. The S1 check MUST NOT use `present()`, which treats `[]` as present (`RecordingSemanticValidator.swift:11`).
- **`RecordingContracts/RecordingSemanticValidator.swift`**
  - The `Prompt` case (`:32-33`) replaces "text and style nonblank" with P1–P4.
  - The `PromptSuite` case (`:34-38`) adds S1, S2, S4 and S7.
  - Exact containment checks are unchanged. Swift computes no digests (`docs/NATIVE_RECORDING_APPLICATION.md:5-6`).
- **Conformance.** `tests/test_recording_contracts.py` gains v2 cases in `synthetic_contracts` and `invalid_contracts` (owner `shared`) for P2–P4, S1, S2, S4 and S7, so the Python and Swift validators cannot diverge silently.
- **`RecordingPackageCandidate`** (`RecordingUI/RecordingPackagePreparation.swift:29-44`) uses the prompt title when `text` is absent. Filtering is in §3.6.

## 3. Coverage and identity

### 3.1 Coverage stays exact

For catalog `C` registered for subject `S`, prompt `p ∈ C.prompts`, and an indexed take `t` (native row or legacy row):

```text
covers(t, p) = t.subject_id = S ∧ H(t.prompt) = H(p)
retained(t)  = t.decision = kept ∧ t.analysis ≠ invalid                (recording_index.py:66)
covered(p)   = ∃ t : covers(t, p) ∧ retained(t)
```

- This is today's rule: `next_missing` (`recording_index.py:1157-1169`), history takes and counts (`:1313-1328`) and `_prompt_summary` (`:1486-1536`). This design adds no table, view, migration, query or audit rule.
- `t.prompt` is the exact prompt the audio was recorded against: the reservation prompt for native takes (`_entry`, `recording_index.py:333-350`), and for legacy rows the catalog prompt that the exact filters make equal to the source prompt (§3.4).
- A take counts toward every catalog that contains its exact prompt, across groups. Removed, rejected and invalid takes never count (`NATIVE_RECORDING_UX.md:269-273`).
- These tests pin the rule and stay green: `test_exact_prompt_style_change_does_not_reuse_coverage` (`tests/test_recording_index.py:198`) and `test_cross_group_exact_prompt_coverage_without_acoustic_reuse` (`tests/test_recording_navigation.py:210`).

**Revision 3: keep-recordings aliases.** A catalog version published from the corpus store (§7.7) may accept earlier prompt digests: a revision made with `voice-lab catalog edit-prompt --keep-recordings` accepts the previous revision's digest and everything that revision accepted. The rule becomes:

```text
counts(t, p) = H(t.prompt) = H(p)
             ∨ (H(t.prompt) ∈ aliases(C, p) ∧ identity(t.prompt) = identity(p))
covers(t, p) = t.subject_id = S ∧ counts(t, p)
identity(q)  = (q.prompt_id, q.holdout, spoken or the silent category)     (recording_contracts.keep_recordings_identity)
```

- `aliases(C, p)` comes from the version's `CatalogSnapshot.prompt_aliases`. The index stores it in the projection tables `recording_catalog_alias_sets` and `recording_prompt_aliases` (index schema version 4, `migrations/20260916110000_create_recording_prompt_alias_projection.sql`), and `next_missing`, history counts, prompt summaries, `latest_retained_take` and `review_aliases` count through it. The coverage report and the fine-tune corpus apply the same rule through `CatalogSnapshot.current_prompt_for`.
- Built-in catalogs and every `recording-group/v1` catalog have no aliases, so for them coverage is exactly the revision-2 rule above.
- An alias never counts across prompt ids, held-out flags, or spoken and silent behaviour, so held-out audio never reaches training and silence never covers speech.

### 3.2 What pilot-v2 means for existing evidence

pilot-v2 keeps pilot-v1's 34 spoken prompt hashes and replaces its two room-tone hashes (§2.5). Under §3.1:

| Evidence already in a root | Under pilot-v2 | Under pilot-v1 | Decided by |
| --- | --- | --- | --- |
| Native take whose prompt is one of the 34 spoken prompts, in any group | counts | counts | exact `H` (§3.1) |
| Native take on a pilot-v1 room tone | not listed, not reviewable, does not count | counts | exact `H`; `recording_review.py:122-129` |
| Legacy sidecar take on a spoken prompt (finalized, READY, in progress, rejected, interrupted) | listed; kept takes count | counts | exact filters (§3.4) |
| Legacy sidecar take on a room tone | not listed | counts | exact filters |
| Sidecar-less `cole-pilot-v1` manifest file, spoken prompt | counts | counts | `legacy_pilot_v1` (§3.3, §3.4) |
| Sidecar-less `cole-pilot-v1` manifest file, room tone | not listed | counts | exact filter after mapping |
| Foreign native package without a local map | `(catalog unknown)` | same | §3.4 |

Consequences, stated exactly:
- **Spoken prompts.** Every retained take of the 34 spoken prompts counts under pilot-v2, whether native, legacy with a sidecar, or legacy without one. It is listed in that prompt's history and is playable and reviewable there.
- **Room tones.** `room-tone-start` and `room-tone-end` show Missing under pilot-v2 until a take of the v2 prompt is saved. No pilot-v1 room-tone take ever counts for them.
- **Old room-tone takes** stay on disk unchanged. They remain listed, playable and reviewable under every catalog that contains their exact prompt: pilot-v1, in the live Qt app or a pilot-v1 native build. Under pilot-v2 they are neither listed nor reviewable (`recording_review.py:122-129`; `recording_index.py:1073-1088`).
- **Nothing is deleted or rewritten (G6).** pilot-v2's legacy publication replaces index rows only on its own 36 hashes (`recording_index.py:1706-1713`). pilot-v1's room-tone rows are outside that scope, and spoken rows are byte-identical in both catalogs (L1, §3.4). The index is a disposable projection; filesystem evidence stays authoritative (`migrations/20260911070000_create_recording_library_projection.sql:1-5`). TC3 checks the file tree.
- **Later takes.** pilot-v2 spoken takes also count under pilot-v1, because the hashes are identical. pilot-v2 room-tone takes do not.

### 3.3 Built-in catalogs: `prompt_catalogs.py`

A new durable module (`@canonical prompt_catalogs`), Step 3:

```python
BUILTIN_PROMPT_SUITE_SHA256: Final[Mapping[str, str]]  # resource file name -> pinned H; release order, pilot-v1.json first
def load_builtin_prompt_suite(resource: str) -> PromptSuite  # table keys only; never aliases or paths; SecurityError on pin mismatch
def legacy_pilot_v1(suite: PromptSuite) -> bool  # the suite shares at least one prompt hash with pilot-v1
```

Normative behavior:
- **Definition.** `legacy_pilot_v1(C)` holds when `{H(p) : p ∈ C.prompts} ∩ {H(q) : q ∈ PILOT_V1.prompts} ≠ ∅`. When `H(C)` equals the `pilot-v1.json` pin, the answer is true without opening a file.
  - It depends only on the bytes of `C` and the immutable pilot-v1 resource. It never depends on which other built-ins the running build ships, so every build that has this module computes the same value.
  - Values for the built-ins, pinned by TR1: pilot-v1 true, evaluation-v1 false, pilot-v2 true, physical-test-v1 false (S4's `test` label changes every hash).
- **Loading.** Nothing loads at import. `legacy_pilot_v1` opens no file when `C` is pilot-v1; otherwise it opens only `pilot-v1.json`, once per process. No code path opens `pilot-v2.json` or `physical-test-v1.json` unless a caller names it. The live Qt launcher therefore opens no resource beyond the `pilot-v1.json` it already loads (TQ1).
- **Failure.** Nothing here raises for catalog content. A built-in that fails its pin raises `SecurityError`, because that is a corrupt build.

Step 3 also makes two index helpers public, so the coverage report (§7.2) shares the index's predicates: `is_retained(decision, analysis)` beside `_RETAINED` (`recording_index.py:66`), and `history_entry_for_take` (today's `_entry`, `:333-350`).

### 3.4 Legacy desktop and manifest mapping

**Exact filters stay.** Native package entries (`recording_library.py:547-551`), finalized manifests (`:627`), rejections (`:671-677`), in-progress sessions (`:749-755`) and interrupted sidecar takes (`recording_index_backfill.py:347-349`) keep admitting only prompts equal to `C`'s. Backfill keeps `prompt=prompts[old.prompt_id]` (`recording_index_backfill.py:190`), which these filters make equal to the source prompt.

**Sidecar-less rule** (`recording_library.py:582-596`). It no longer calls `load_prompt_suite("pilot")`:

```text
if manifest.prompt_suite_id == "cole-pilot-v1" and legacy_pilot_v1(C):
    suite = PILOT_V1        # C itself when H(C) is the pilot-v1 pin; otherwise load_builtin_prompt_suite("pilot-v1.json")
elif manifest.session_id starts with "native-":
    return ()               # foreign native session (no local map, recording_packages.py:783-786)
elif manifest.prompt_suite_id == C.suite_id:
    raise ContractError(<unchanged message>)
else:
    return ()
```

- `suite` is the source suite. The path and transcript checks (`recording_library.py:614-625`) run against it, and the exact filter (`:627`) then admits only prompts that `C` holds exactly.
- Backfill sets `catalog_sha256 = sidecar.catalog_sha256 if sidecar else H(PILOT_V1)` (`recording_index_backfill.py:189`). Today sidecar-less rows arise only when `C` is pilot-v1, so existing rows stay byte-identical.

| Library catalog `C` | Manifest without sidecar | Today | This design |
| --- | --- | --- | --- |
| pilot-v1 | `cole-pilot-v1`, native or not | maps with `suite = C` | identical |
| shares no prompt hash with pilot-v1: evaluation-v1, physical-test-v1, both one-off diagnostic catalogs | any | skipped, or raises for a non-native manifest naming `C.suite_id` | identical |
| private catalog that shares prompt hashes with pilot-v1 | `cole-pilot-v1` | skipped (a native one is listed as catalog unknown), or raises for a non-native one when `C.suite_id` is `cole-pilot-v1` | maps through pilot-v1; only the shared prompts count |
| pilot-v2 | `cole-pilot-v1` | — | maps through pilot-v1; the 34 spoken prompts count and the room tones are skipped |
| pilot-v2 | foreign native `cole-pilot-v2` | — | skipped; impact below |
| pilot-v2 | non-native `cole-pilot-v2` | — | raises; no shipped writer produces one (guard #10) |

**The one deliberate v1 difference** is the third row.
- Today such a catalog skips sidecar-less pilot manifests, while its legacy publication deletes pilot-v1's rows on the shared hashes (`recording_index.py:1706-1713`). That was the former F1 hazard.
- No known catalog is affected. evaluation-v1 and both one-off diagnostic catalogs share 0 prompt hashes with pilot-v1 (their `settings.json`, checked 2026-09-14), and no test pins the old branch.

**L1: legacy rows do not depend on the catalog.** For every prompt hash `h`, every catalog that contains `h` emits byte-identical legacy rows on `h`, in every build from Step 3 onward:
- take IDs exclude the catalog (`recording_index_backfill.py:47-58`);
- admission is exact prompt equality, which is the same for every catalog holding `h`;
- `catalog_sha256` is the source catalog; `prompt`, `allow_quiet`, decision, analysis, export, dates and edit projections derive from the source evidence and `h` (`recording_index_backfill.py:163-199`; `recording_edits.py:1125-1140`).

A publication that deletes and reinserts the legacy rows of its own hashes therefore never strands another catalog's rows. That matters because discovery skips a catalog whose evidence fingerprints are unchanged (`recording_discovery.py`, `produce_discovery`), so stranded rows would stay lost.

**Fail-closed divergence.** Today's pilot check compares by value with the path-first `load_prompt_suite("pilot")` (F8). The new rule attributes files only through the pinned, genuine pilot-v1. The two differ only when `pilot` is shadowed by a working-directory file or `pilot-v1.json` has been edited, and the new rule then maps less or refuses. The launcher's working directory is `/`, and the configured root has no sidecar-less manifests (`B/configured-library-count.json`).

**Foreign native packages.** A skipped same-subject native session is listed as `(catalog unknown)` (`recording_library.py:708-726`), and backfill records a `legacy:<session>` Unknown record (`recording_index_backfill.py:88-100`). Every other legacy row still publishes (`recording_index.py:1779-1793`). This is today's tested policy (`tests/test_recording_packages.py:448-469`). For a transferred full pilot-v2 package, the operator sees the following under pilot-v2 and pilot-v1 alike:
- Completeness stays `unknown`, so every prompt without a kept take shows Unknown rather than Missing (`recording_index.py:1526`).
- Continue Missing and Save & Next are refused (`recording_index.py:1141-1142`). Plain Save still works.
- This lasts as long as the immutable raw package exists, and there is no in-app remedy (F5).
- It is reachable only through a cross-root transfer, which is out of scope, or by losing both the origin file and the registry (`recording_packages.py:776-786`). The configured root has 0 raw and 0 incoming sessions.

### 3.5 Discovery, completeness and checkpoints

- **First open.** A new catalog starts `building` (`recording_index.py:727`) with the checkpoint key `H([subject, catalog])` (`recording_discovery.py`, `discover_recording_library`).
  - That triggers a full native reconcile and a legacy scan that re-inspects every legacy WAV (`recording_index_backfill.py:159-162`).
  - Complete comes only from a publication with no Unknown records (`recording_index.py:1782-1793`) or from `set_completeness` (`:798-820`).
- **Checkpoints.** Unchanged. Coverage and legacy rows are pure functions of root evidence, catalog bytes and pilot-v1 (§3.1, L1), so every build from Step 3 onward computes the same result. A rebuild changes `index_instance`, which already invalidates checkpoints (`discover_recording_library` validity block).
- **Worker.** The worker checks the group catalog and builds its library from it (`produce_discovery`).
- **Unknown records.** This design adds no record type.

### 3.6 Packages

- **Unchanged bindings.** One group per package (`recording_packages.py:452-455`); exact catalog prompts in entries (`recording_contracts.py:534-541`); manifest transcripts equal the recorded prompt (`recording_packages.py:288-294`); at least one utterance plus room tone from the same group (`recording_packages.py:462-470`).
- **Old takes** belong to old groups, so they never enter a pilot-v2 package. Preparing a package under pilot-v2 needs a room-tone take recorded in the current pilot-v2 group, as for every new group today.
- **`_selected_suite`** (`recording_packages.py:233-247`):
  - Step 2: MUST copy `schema_version` from `group.catalog`; otherwise every v2 subset with a text-less room tone fails S1 with a raw `ValidationError`.
  - Step 4: for purpose `physical-test`, MUST name the subset `cole-physical-test-subset-<sha24>` (§5.4). Until then a physical-test subset fails S4, which is fail-closed, and Step 2 already refuses physical-test contexts (§5.3).
- **`RecordingPackageStore.plan`** (`:421-496`) refuses `procedure` prompts with `RECORDING.PACKAGE_INCOMPLETE`: "Test steps without spoken text or room tone cannot be packaged." (Step 4)
- **Swift candidates** (`RecordingPackagePreparation.swift:133-139`) must satisfy all of the following (Step 4): `group_id == state.groupID`; the prompt is exactly in `configuration.catalog.prompts`; the category is not `procedure`. Today legacy rows with no group are offered and then refused by the service.

### 3.7 Context, groups, cursor, navigation and review

- **First open of a catalog.** Creates a new group (`recording_takes.py:835-872`) and resets the cursor to `C.prompts[0]` in `selected-prompt` mode with no take (`:1362-1388`). Selection does not carry across catalogs.
- **Unchanged commands.** Reserve, Save, Save & Next, navigate, retake and review keep binding the group catalog, current prompt IDs and exact containment (`:943-947`, `:1173-1177`, `recording_takes.navigate`, `select_review` at `:1627-1633`, `:1700-1704`; `recording_review.py:122-129`).
- **Filters.** Continue Missing and Save & Next ignore them (`NATIVE_RECORDING_UX.md:269-270`).
- **Continue-legacy is a new limitation under pilot-v2.** `ContinueLegacyRecordingCommand` opens a group on the sidecar's frozen pilot-v1 catalog (`cli.py:525-531`, `recording_recovery.py:70-83`).
  - A pilot-v2 app refuses a cursor on a foreign catalog (`RecordingWorkspaceModel.applyCursor`), and its next open-context resets the cursor.
  - The configured root has up to five unfinished Qt sessions (6 sidecar sessions, 1 READY; `B/configured-library-count.json`). They can be continued only in Qt or in a pilot-v1 build. Their spoken takes still count under pilot-v2.

### 3.8 Migration classification

- No index migration, data backfill, `*_migration` module or `migrations/active/*.md` entry. Coverage semantics do not change, and nothing dual-writes or routes between old and new.
- The sidecar-less rule and the source catalog of sidecar-less rows are permanent reader semantics.
- The Step 2 refusal of physical-test catalogs (§5.3 #2, #3) is a guard, not routing. Step 4 adds its marked-root branch; no stored format is routed.
- ~~The operator's switch to pilot-v2 is a settings change.~~ Revision 3: settings carry no catalog. Switching catalogs is `voice-lab catalog activate` plus a relaunch of the app (§7.7).
- Revision 3: the corpus store is a separate migration with its own manifest, `migrations/active/corpus-store.md`. It does not change this classification. Groups that embed their catalog (`recording-group/v1`) are permanent reader semantics in the same way as the sidecar-less rule (§3.4).

## 4. Suite identities and aliases

| Name | Resource | `suite_id` | Purpose | Status |
| --- | --- | --- | --- | --- |
| `pilot` | `pilot-v1.json` | `cole-pilot-v1` | capture-pilot | unchanged; MUST NOT move |
| `pilot-v1` | `pilot-v1.json` | `cole-pilot-v1` | capture-pilot | unchanged |
| `pilot-v2` | `pilot-v2.json` | `cole-pilot-v2` | capture-pilot | new (Step 5) |
| `physical-test-v1` | `physical-test-v1.json` | `cole-physical-test-v1` | physical-test | new (Step 5) |
| `evaluation`, `evaluation-v1` | `evaluation-v1.json` | `cole-held-out-v1` | held-out-evaluation | unchanged |

**Why `pilot` stays v1.** The live launcher `/Applications/Cole Voice Lab.app/Contents/MacOS/ColeVoiceLab` executes `.venv/bin/python -I -B -m cole_voice_lab.cli desktop … --suite pilot` against this checkout's editable install (operator review, read-only check, 2026-09-14). Its builder defaults to `pilot` (`desktop_application.py:20`, `:40-64`; `scripts/build-desktop-app:25`; `desktop.py:16`). Moving the alias would make that app load v2, which it then refuses (guard #9). Even with that refusal bypassed, it could not resume its six v1 sidecar sessions (`recording_library.py:372-374`).

New suites get versioned names only; there is no unversioned `physical-test` alias.

| Entry point | v2 behavior |
| --- | --- |
| `load_prompt_suite` (`resources.py:16-29`) | adds the two new aliases in Step 5; the existing path-first rule is unchanged (pre-existing, F8) |
| settings generation, `prompts coverage-report`, `prompts show`, the sidecar-less rule | `load_builtin_prompt_suite`, restricted to the pin table. Revision 3: settings name no catalog; `prompts coverage-report`, `prompts show` and `finetune` try an exact built-in name first and otherwise resolve `<catalog_id>[@<version>]` in the library's corpus store (§7.7) |
| `capture run` (`cli.py:413-423` → `capture_prompt_suite`, `capture.py:853`) and `capture console` (`cli.py:452-468` → `TakeEngine`, `capture.py:1178-1218`) | choices stay v1 (`cli.py:1479`); both engines refuse v2 suites (guard #9, Step 2), which also covers Qt capture (`recording_library.py:300-317`) |
| `prompts show` (`cli.py:1527`, `:383-393`) | Step 5 adds `pilot-v2` and `physical-test-v1`; v1 human and JSON output stays byte-identical; v2 output adds `instructions`, effective labels and `(no spoken prompt)` |
| `desktop --suite` (`cli.py:1503-1513`), `run_desktop` (`desktop.py:31`), `create_desktop_application` (`desktop_application.py:40`) | refuse v2 catalogs ("prompt-suite/v2 catalogs require the native recording application", Step 2) and physical-test roots (Step 4) |
| `audition plan --suite` (`cli.py:1762`) | unchanged |
| native settings (`native_recording_application.py:49-75`) and builder `main` (`:728-755`) | ~~the catalog stays inline and is generated from `load_builtin_prompt_suite`; builder and verify apply §5.3 #2; the builder gains `--expect-catalog-sha256` (Step 4) and refuses a mismatch~~ Revision 3: settings and the app configuration carry no catalog (`native-application-settings/v2`, `recording-application/v2`) and declare a required `purpose`; builder and verify check the root against that purpose (§5.3 #2); `--expect-catalog-sha256` is removed and refused as an unknown argument; the app receives its catalog from the service (§7.7) |

**`pilot-v2.json` authoring contract (Step 5)**
- **Header.** v2, `suite_id` `cole-pilot-v2`, purpose `capture-pilot`. The same 36 IDs in the same order; 312 seconds in total.
- **Spoken prompts.** The 34 non-room-tone prompt objects are copied verbatim from pilot-v1, with no labels and no instructions, so each keeps its `H(p)`.
- **`room-tone-start` and `room-tone-end`:**
  - omit `text`;
  - set `instructions` to the v1 text verbatim;
  - keep `style` ("Silence; keep the recording position unchanged."), `expected_seconds` (15) and both flags.
  - Their `H(p)` changes (§2.5). That is the accepted cost recorded in `operator-choices-05.json`.
- **Goldens.** `H(pilot-v2)` and both room-tone hashes equal §2.5. The Boundary-6 build passes `--expect-catalog-sha256` with `H(pilot-v2)`. Revision 3: that flag no longer exists; `voice-lab catalog init --from-builtin pilot-v2 --suite-id cole-pilot-v2` publishes a version whose `catalog_sha256` is `H(pilot-v2)`.
- **Style.** Spoken `style` stays as delivery guidance and is shown in the Instructions section (§6.3). Moving it into `instructions` would change 34 hashes and drop their coverage.
- **Labels.** None beyond the category. A label on a spoken prompt would change its hash and drop its coverage (§9 FYI).

**`physical-test-v1.json` authoring contract (Step 5)**
- **Header.** v2, `cole-physical-test-v1`, purpose `physical-test`. All prompts have `holdout: false` and `reference_candidate: false`.
- **Prompts.** One per step of `B/physical-gate-procedure.md`, in this order. The design fixes the `prompt_id`, category and duration; Step 5 writes the instructions under the rules below.

  | Step | `prompt_id` | Category | `expected_seconds` |
  | --- | --- | --- | --- |
  | P01 | `p01-setup-identity` | procedure | 120 |
  | P02 | `p02-microphone-denial` | procedure | 120 |
  | P02a | `p02a-permission-reset` | procedure | 120 |
  | P03 | `p03-grant-and-immediate-stop` | procedure | 2 |
  | P04 | `p04-room-tone` | room-tone | 10 |
  | P05 | `p05-spoken-sentence` | neutral | 10 |
  | P06 | `p06-native-playback` | procedure | 10 |
  | P07 | `p07-independent-playback` | procedure | 10 |
  | P08 | `p08-continuous-capture` | conversational | 60 |
  | P09 | `p09-rapid-cycles` | procedure | 3 |
  | P10 | `p10-unplug-while-capturing` | procedure | 10 |
  | P11 | `p11-reconnect-and-recover` | procedure | 3 |
  | P12 | `p12-sleep-wake` | procedure | 3 |
  | P13 | `p13-draft-reopen` | procedure | 3 |
  | P13a | `p13a-interrupted-control` | procedure | 3 |
  | P14 | `p14-package-and-intake` | procedure | 120 |

- **Durations.** A step with a take uses the procedure's per-take bound; a step without a take uses 120.
- **Labels.** `test` plus at most two of `setup`, `permission`, `transport`, `waveform`, `playback`, `continuity`, `device-loss`, `recovery`, `package`.
- **Spoken and silent content.** P04 instructions and style are taken verbatim from `diagnostic-room-tone` in `cole-physical-diagnostic-20260914`; P05 and P08 `text` and `style` from its `diagnostic-spoken` and `diagnostic-continuous` prompts.
- **`style`** for a procedure step is a short operator cue, for example "Follow the steps; speak only if a step says so."
- **`instructions`** say only what the operator does and observes. They contain no outcome fields and no agent-only actions: `tccutil`, `afplay`, `intake validate` or `intake accept` commands, hash, PID or identity checks, or "retain exact output". They contain none of these substrings: `/Users/`, `~/`, `<GATE>`, a backtick, `sudo`, `rm `, `private.cole.` (TR1). Built-in text ships in the wheel and the app bundle (`native_recording_application.py:189-230`), and the script view is selectable (`RecordingWorkspace.swift:124`).
- **Per-step notes in `instructions`:**
  - P02a and P13a: "The agent performs this step outside the app; selecting it is optional."
  - P09 and P12: "After each Save, select this step again before the next take." Save advances to the next missing prompt once history is complete (`RecordingWorkspaceModel.saveAdvances`).
  - P14: "Record fresh P04 then P05 first, in this app session, then prepare the package."

## 5. The `physical-test` purpose

### 5.1 Policy

**Accident model.** The root marker, the suite-ID prefix and the subject prefix are self-asserted. They stop mistakes by an operator or agent with write access, not tampering.

| Concern | Rule |
| --- | --- |
| Schema | v2 only (S1); `cole-physical-test-` suite-ID prefix and a `test` label on every prompt (S4) |
| Holdout | always `false`; the held-out flag belongs to held-out evaluation (`models.py:92-94`) |
| Approval and gate | gate `not-required` (`recording_contracts.py:176-177`); pilot approval refused as for any non-corpus catalog (`:630-631`) |
| Capture and analysis | native capture only, in a diagnostic root (§5.2); `procedure` and `room-tone` takes allow quiet audio (§2.4) |
| Coverage | a `procedure` step is Recorded only with a saved take; steps without recordings stay Missing by design, and explicit selection drives the procedure |
| Packaging | only in a diagnostic root (P14 needs it); `procedure` refused; existing UMIK-1 and room-tone prerequisites apply (`recording_packages.py:437-470`) |
| Intake | accepted only into a diagnostic root with the marker subject; refused by standard roots |
| Pilot gate, datasets, references, headphone reviews, consent approval | always refused |
| Audition | already refused (held-out required, `audition.py:180`, `:712`) |
| Legacy Qt, console and terminal capture | refused (v2 catalog, physical-test root) |

### 5.2 Diagnostic root marker (Step 4)

- **File.** `provenance/diagnostic-root.json`, model `DiagnosticRootMarker`, schema `cole-voice-lab.diagnostic-root/v1`.
- **Fields.** `purpose: Literal["physical-test"]`, `subject_id: StableId` starting with `cole-physical-diagnostic-`, `created_at: UtcTimestamp`.
- **Storage.** Immutable private canonical evidence, read with the exact-canonical reader `read_recording_evidence` (`recording_takes.py:95-109`).
- **Reading, `root_purpose(layout)`.** It `lstat`s the marker path. `ENOENT` means `"standard"`. Any other state raises `SecurityError`, and every guarded operation refuses: a symlink (dangling or not), a non-regular file, a wrong owner or mode, `st_nlink != 1`, non-canonical bytes, or a parse failure.
- **Creation** happens only through `voice-lab assets init --asset-root A --diagnostic physical-test --subject-id S`, which extends `_assets_init` (`cli.py:352-363`).
  - `S` MUST start with `cole-physical-diagnostic-`.
  - The command refuses unless `A` does not exist, or every `ASSET_DIRECTORIES` member (`paths.py:12-23`) is absent or empty.
  - It writes the marker and installs no consent record; the standard path's `install_pending_consent` (`cli.py:354`) is skipped.
  - A marker is never added to or removed from an existing root.
- **Subject prefix.** Readers and writers on the corpus side refuse `cole-physical-diagnostic-` subjects in every root (§5.3 #6, #7, #7a, #11). Open-context in a standard root does not refuse them: the one-off roots `…i649qel_` and `…c65af236` are unmarked, use `cole-physical-diagnostic-2026091{3,4}`, and must stay openable.
- **Module** `diagnostic_roots.py`: `root_purpose(layout)`, `require_root_purpose(layout, purpose, *, subject_id=None)`, `is_physical_test_suite_id(suite_id)` (Step 2), `is_diagnostic_subject(subject_id)`, and `require_corpus_eligible_package(layout, package)`, which refuses a physical-test root, a physical-test suite ID and a diagnostic subject.

### 5.3 Guards

| # | Boundary | Check | Error | Step |
| --- | --- | --- | --- | --- |
| 1 | `PromptSuite` validator | S1, S4, S7 | `ValidationError` | 2 |
| 2 | native builder and verify (`native_recording_application.py:324-346`, `:641-725`) | Step 2: refuse every physical-test catalog. Step 4: root purpose is `physical-test` iff catalog purpose is; in a marked root the marker subject equals the settings subject; `--expect-catalog-sha256` when given. Revision 3: the settings' declared `purpose` replaces the catalog purpose (`_require_release_root`); there is no catalog and no `--expect-catalog-sha256` | `ContractError` | 2, 4 |
| 3 | service `OpenContextCommand` and `ContinueLegacyRecordingCommand`: first statement of the block (`recording_service.py:544`), before `context_replay` (`:550-552`) and `prepare_corpus_gate` (`:553-563`); repeated in `RecordingTakeStore.open_group` (`recording_takes.py:817`), where every group is minted. Revision 3: also `CatalogCommand` and the pre-context `SnapshotCommand` without a launch digest, on the pinned catalog, before its content or digest is served; `voice-lab catalog init` and `activate` apply the same check and raise `ContractError` | same as #2 | `RECORDING.GATE_NOT_READY`, with a message naming the cause | 2, 4 |
| 4 | `RecordingPackageStore.plan` (`recording_packages.py:433`) | group purpose matches root purpose; `procedure` refused | `RECORDING.PACKAGE_INCOMPLETE` | 4 |
| 5 | `accept_capture_session` (`intake.py:577`, right after validation, before any raw write) | marked root: physical-test suite ID and marker subject; standard root: no physical-test suite ID | `ContractError` | 4 |
| 6 | `load_accepted_utterance` (`references.py:270-337`), shared by datasets, headphone reviews (`cli.py:672`) and references (`references.py:477`, `:544`) | `require_corpus_eligible_package` | `ContractError` | 4 |
| 7 | `load_pilot_gate` (`datasets.py:448-469`), also via `prepare_corpus_gate` (`recording_takes.py:744`) | `require_corpus_eligible_package` for every named manifest | `ContractError` | 4 |
| 7a | `dataset pilot-gate` writer (`cli.py:748-795`): inside the loop at `:765-773`, before `write_canonical_json` at `:783` | `require_corpus_eligible_package` for every named manifest | `VoiceLabError` | 4 |
| 8 | `build_dataset_manifest` (`datasets.py:498-537`) | refuse physical-test suites before `DatasetEntry(...)` (`datasets.py:554`); `DatasetEntry.prompt_suite_purpose` (`:121`) stays without `physical-test`, which keeps `mypy --strict` green | `ContractError` | 2 |
| 9 | legacy capture: `TakeEngine` (`capture.py:1178-1218`), `capture_prompt_suite` (`capture.py:853`), `run_desktop` (`desktop.py:31`), `create_desktop_application` (`desktop_application.py:40`), `_capture_inputs` (`cli.py:435`) | refuse v2 suites (Step 2) and physical-test roots (Step 4) | `ContractError` | 2, 4 |
| 10 | `finalize_capture_session` (`capture.py:1761`), where every `CaptureManifest` is minted | `is_physical_test_suite_id(suite.suite_id) == (suite.purpose == "physical-test")`; a v2 suite requires a `native-` session ID; no `procedure` prompt | `ContractError` | 2 |
| 11 | `approve_self_consent` (`consent.py:106`) | refuse diagnostic subjects and physical-test roots | `SecurityError` | 4 |
| — | `sync.py` transport | not guarded (payload-agnostic); intake at the destination is the boundary | — | — |

**Sequencing. A refusal lands with the capability it guards.**
- **Step 2** is the first commit where a v2 suite and purpose `physical-test` parse. It lands #1, #8, #10, the v2 half of #9, the Step 2 form of #2 and #3, and `diagnostic_roots.is_physical_test_suite_id`. No physical-test catalog can therefore mint a group, take or manifest before its corpus guards exist.
- **Step 4** lands before any marked root or physical-test resource can exist: the marker and the rest of `diagnostic_roots.py`, #2 and #3 in their final form, #4–#7a, the root half of #9, #11, the physical-test subset prefix (§3.6), `--expect-catalog-sha256`, the Swift candidate narrowing and the sheet title (§5.5).
- **Step 5** ships `physical-test-v1.json` only after Step 4.

**Refusal precedes write.** Every command that writes evidence derived from a raw package evaluates `require_corpus_eligible_package` for that package before its first write: pilot gate (#7a); headphone review (loader `cli.py:672` before write `:696`); reference publication (loader `references.py:544` before staging `:594`); dataset build (through `build_dataset_manifest`); prepared corpus gate (`recording_takes.py:744` before `:776`).

**Messages.** Root mismatches name the cause:
- "physical-test catalogs require an asset root created with `voice-lab assets init --diagnostic physical-test`";
- "this asset root is marked for physical tests; open it only with a physical-test catalog".

No new error codes. The existing vocabulary (`recording_contracts.py:62-87`) covers every refusal, so Swift error handling does not change.

### 5.4 How manifests identify physical-test evidence

- **Manifest shape.** `CaptureManifest` v1 is unchanged (`models.py:132-168`). It identifies its suite only by `prompt_suite_id`, and it carries `subject_id`.
- **Reserved prefix.** Every physical-test manifest carries `cole-physical-test-`: full suites satisfy S4; native subsets are named `cole-physical-test-subset-<sha24>` instead of `native-subset-<sha24>` (`recording_packages.py:244`), which would lose the purpose; guard #10 makes the prefix and the purpose agree at the single minting point.
- **v1 suites.** A v1 suite could carry the prefix, because S4 applies to v2 only. Guard #10 then refuses to finalize it, and readers over-refuse, which fails closed. No stored suite uses the prefix (repository grep, 2026-09-14).
- **Three independent layers.** The suite prefix and the subject prefix travel with the package; the root marker binds where evidence may live.

### 5.5 UI

- **Prepare for lab** stays available in physical-test builds, which can only run on a marked root. The sheet title reads "Prepare diagnostic package", and candidates exclude `procedure` rows (§3.6).
- **Purpose chip.** In a physical-test catalog the `Test` chip comes from `catalog.purpose`, sits right after the category chip, and never collapses into `+N` (§6.2). There is no other purpose banner.

## 6. Presentation contract

### 6.1 Derived fields (identical for v1 and v2)

```text
displayPrompt       = spoken ? text : nil
displayInstructions = instructions ?? (spoken ? nil : text)
guidance            = style
kind                = room-tone | procedure | spoken      (from category)
effectiveLabels     = [category]
                      + (catalog.purpose == physical-test ? ["test"] : [])
                      + (labels without "test")
                      + (reference_candidate ? ["reference-candidate"] : [])
                      + (holdout ? ["holdout"] : [])
labelTitle(token)   = token with "-" replaced by " ", then .capitalized    (as RecordingWorkspace.swift:98 today)
```

**Room tone.** A v1 room tone shows its text as Instructions and has no Prompt. This matches D-013, the Qt desktop's "Instructions only" card (`desktop_window.py:1189`) and the operator's request, without touching v1 bytes. The operator's example "Remain quiet for ten seconds…" is the v1 room-tone text of the one-off diagnostic catalog, so this rule covers that screen. A v2 room tone shows `instructions` and has no text.

**Verbatim rendering.** Prompt, Instructions, Guidance and chip titles render catalog text with `Text(verbatim:)` or a `String`-typed initializer, never a `LocalizedStringKey` built from catalog text.

### 6.2 Header

- **Region.** `header` stays 48 pt (`RecordingUI/RecordingLayout.swift:27-31`).
- **Line 1** is the title (`RecordingWorkspace.swift:97`).
- **Line 2** replaces the category subtitle (`:98`) with a single row of chips in `effectiveLabels` order. It never wraps. The category chip and, in physical-test catalogs, the `Test` chip are always shown; later chips overflow into a trailing `+N` chip. Its accessibility label reads "Labels: Procedure, Test, Recovery".

### 6.3 Script region: one layout for every kind

The `script` region frame is unchanged; only its internal layout changes, which the geometry harness does not measure (`R/feature-maps-01/sidebar-ui.md` §4). From top to bottom:
1. **Kind banner.** Room tone shows `SILENT ROOM TONE · DO NOT SPEAK` (`:119`). A procedure step shows `TEST STEP · FOLLOW INSTRUCTIONS`. A spoken prompt has no separate banner; its Prompt heading carries `READ ALOUD`.
2. **Prompt section.** Rendered iff `displayPrompt != nil`.
   - Heading `PROMPT · READ ALOUD`; `displayPrompt` at `scriptSize`, scrolling, with scroll reset per prompt (`:121-126`).
   - Identifier `recording.script`; accessibility label "Prompt, read aloud".
   - For room tone and procedure steps, a muted one-line "No spoken prompt" takes its place.
3. **Instructions section.** Always rendered, because `style` is required in both versions.
   - Heading `INSTRUCTIONS · NOT READ ALOUD`; `displayInstructions` at 17 pt when present; then `Guidance: <style>` and `About N sec` in today's fixed 32-point row (`:127-131`).
   - Identifier `recording.instructions`; accessibility label "Instructions, do not read aloud".
   - With a Prompt section it takes at most 40% of the card and scrolls. Without one it fills the card.

**Library row icons** (`:80`): `waveform` for room tone, `checklist` for procedure, `text.alignleft` otherwise.

### 6.4 Library search and label filter

- **Search** stays case-insensitive (`RecordingWorkspaceModel.filteredPrompts`). It matches title, `prompt_id`, `displayPrompt`, `displayInstructions`, `style`, and the `labelTitle` of each effective label.
- **Selection.** `selectedLabels: Set<String>` holds label tokens in memory only. Options are the union of effective labels, in first-appearance catalog order. Tokens absent from the loaded catalog are pruned.
- **Predicate:** `searchMatches ∧ statusMatches ∧ (selectedLabels.isEmpty ∨ ¬selectedLabels.isDisjoint(effectiveLabels))`.
  - An empty selection shows all prompts. Labels combine with OR; labels, status and search combine with AND.
  - `statusMatches` is unchanged, and `currentPromptFilteredOut` uses the same predicate.
- **Control.** A menu of toggles with identifier `library.labels`, not an editable field; the transport harness treats the first editable field as search (`RecordingHarness/RecordingHarness.swift:139-145`). The menu title reads "Labels: Any", "Labels: Room Tone, Test" (up to two titles) or "Labels: 3 selected". A "Show all labels" item empties the selection.
- **Scope.** Filters never affect Continue Missing, Save & Next or the covered count. Continue Missing's help text reads "Selects the next missing prompt in catalog order; ignores search and filters."

## 7. Operator consequences and runbook

### 7.1 First open of pilot-v2 on the configured root

**Scope.**
- The only real root that switches catalogs is the configured root. Its pilot-v2 build is the Boundary-6 preview, built only after §7.3 passes with operator approval.
- The one-off diagnostic roots `…i649qel_` and `…c65af236` never change catalog.
- Plan Step 9 and the acceptance fixtures stay on their synthetic v1 catalogs.

**Sequence.**
1. No index migration runs. A new group opens (reason `app-start` or `configuration-change`), and the cursor moves to `room-tone-start` with no take selected.
2. Until discovery finishes:
   - the Library shows `—/36` and Unknown statuses;
   - plain Save works; Save & Next and Continue Missing do not (`NATIVE_RECORDING_UX.md:241-247`);
   - discovery costs about as much as a first pilot-v1 open: a full native reconcile plus a legacy WAV inspection, with no checkpoint reuse.
3. At Complete, each spoken prompt is covered exactly when it is covered under pilot-v1, and both room tones show Missing. The §7.2 report states the exact numbers before the switch.
4. Spoken takes appear in take history and are playable and reviewable. Old room-tone takes do not appear under pilot-v2; they remain in the Qt app and in pilot-v1 builds (§3.2). No file changes.
5. Continue Missing selects the room tones, and any spoken prompt without a kept take, in catalog order. Each room tone needs a new take of about 15 seconds.
6. Prepare for lab needs a room-tone take recorded in the current pilot-v2 group (§3.6).
7. Later pilot-v2 spoken takes also count for pilot-v1. pilot-v2 room-tone takes do not count for pilot-v1, and the Qt app does not show them.
8. Unfinished Qt sessions can no longer be continued from this build (§3.7).

### 7.2 Read-only coverage report

```sh
voice-lab prompts coverage-report --asset-root A --subject-id S --catalog pilot-v2 --baseline pilot-v1 --json > R/<run>/coverage-report.json
```

- **Catalogs.** `--catalog` and the optional `--baseline` accept built-in names only and resolve through `load_builtin_prompt_suite`. The output includes `catalog_sha256` and `baseline_catalog_sha256`. Revision 3: an exact built-in name still resolves this way; any other name is a published `<catalog_id>[@<version>]` resolved in A's corpus store (§7.7), and the output adds `catalog_id`, `catalog_version`, `baseline_catalog_id` and `baseline_catalog_version` (`null` for built-ins).
- **Implementation** (`prompt_coverage.py`, Step 3). The CLI resolves names and calls one function that takes suites, which tests call with synthetic suites. It reuses the canonical readers and predicates:
  - legacy rows from `legacy_projection_records(RecordingLibrary(A, C, S), collect)` (`recording_index_backfill.py:61`), with a collecting callback;
  - native takes from `RecordingTakeStore(A, read_only=True).iter_takes_for_projection` (`recording_takes.py:1968`), projected with `history_entry_for_take`;
  - coverage by §3.1: `H(entry.prompt) == H(p)`, `entry.subject_id == S` and `is_retained(entry.decision, entry.analysis)`. TD2 proves parity with `RecordingIndex.history`.
  - It never constructs `RecordingIndex` and never writes or creates anything under A (TD1). Revision 3: this holds for built-in names. A published name opens A's corpus store (`create=False`) only to resolve the version. That open takes the store's lease and lock, opens its database, and may change the store's own files under `A/provenance/corpus/` (`docs/CORPUS_STORE.md`, "Where it lives"). The store is closed before any evidence is read, and no evidence is written.
  - Revision 3: a native take in a `recording-group/v2` group is resolved through its sealed snapshot (`resolve_group_catalog`, files only). A missing or altered snapshot fails the report with `CorpusSnapshotSecurityError` (exit 2), never as a gap.
- **Output.**
  - Per prompt of `--catalog`: `prompt_id`, `prompt_sha256`, `covered`, `retained_take_count` (distinct take IDs); with a baseline, also `changed` and `covered_in_baseline`.
  - Summary: `covered_prompt_count`, `total_prompt_count`, `missing_prompt_ids`, `changed_prompt_ids` (baseline prompt IDs that are absent or have another hash), `lost_prompt_ids` (covered in the baseline, not covered now), `unknown_record_ids` and `baseline_unknown_record_ids`.
- **Exit status.** 1 when a projection fails, when `lost_prompt_ids ⊄ changed_prompt_ids`, or when `unknown_record_ids ⊄ baseline_unknown_record_ids`; otherwise 0.
- **Audio access.** It opens legacy WAVs read-only, through `RecordingLibrary.verified_history_snapshot` (`recording_library.py:785-798`) and backfill inspection (`recording_index_backfill.py:159-162`). Unlike `B/count_configured_library.py`, it is not audio-free, so running it against the configured root needs operator approval.

### 7.3 Deployment order

1. Ask the operator to leave the live Qt app closed from the first Step 2 commit until Step 9 passes. Its launcher imports the working tree, and an old Qt process can mix old and new modules at first use (it imports `recording_packages` lazily, `recording_library.py:543`).
2. Land Steps 2–8 in order. Each step runs its Verify (§8.2).
3. Pass Step 9.
4. Build no v2 or physical-test preview before step 3 passes. The first build is the physical-test preview (§7.4).
5. Boundary 6 only, for the configured root, each item with operator approval:
   1. Take a fresh audio-free count with `B/count_configured_library.py` into R. Require 0 native sessions in `raw/` or `incoming/` without a local map.
   2. Run the §7.2 report into R. Require exit 0, `changed_prompt_ids` equal to the set {`room-tone-start`, `room-tone-end`}, and empty `unknown_record_ids` and `baseline_unknown_record_ids`.
   3. Show the operator the §7.6 notice filled in from the report, and get approval to switch.
   4. Generate pilot-v2 settings with `load_builtin_prompt_suite("pilot-v2.json")`.
   5. Build with `--expect-catalog-sha256 0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a028`, which must equal the report's `catalog_sha256`.

Any future transfer of diagnostic packages to another machine needs its own deployment-order line, because the destination build must include guard #5.

Revision 3: items 5.4 and 5.5 describe the revision-2 build. A build from revision 3 takes settings without a catalog and has no `--expect-catalog-sha256`. The equivalent check is §7.7 seeding item 5: before `voice-lab catalog activate`, the published version's `reference.catalog_sha256` (`voice-lab catalog show --version N --json`) must equal the expected digest. The §7.2 baseline report cannot supply that digest, because it names pilot-v2's `catalog_sha256`, which a `cole-corpus` version never equals.

### 7.4 Physical testing (P10–P14)

An ordered checklist. Ask before each presence step, as usual.

1. **Old preview.** Ask, then quit the freeze-09 diagnostic preview normally (PID 14805 at review time) and confirm it has exited. Every preview shares the bundle ID `private.cole.voice-lab.recording.preview` (`native_recording_application.py:504`), so launch new builds only by exact path. Both apps would also contend for the UMIK-1.
2. **New root.** `mkdir -m 700 B/physical-<UTC>`, then `voice-lab assets init --asset-root B/physical-<UTC>/assets --diagnostic physical-test --subject-id cole-physical-diagnostic-<date>`.
3. **Device.** Re-enumerate the UMIK-1 Core Audio UID with `list_coreaudio_input_devices`.
4. **Settings.** Generate them in Python with `catalog=load_builtin_prompt_suite("physical-test-v1.json")` and `subject_id` equal to the marker subject; never hand-copy catalog JSON. Record `settings-provenance.json` with the catalog hash.
5. **Build.** `scripts/build-native-recording-app --output 'B/physical-<UTC>/release/Cole Voice Recorder Physical PREVIEW.app' --settings … --source-root … --expect-catalog-sha256 b17cc176ee36ac5aa053c949e7ff44ce6e1b0c13b95b4cf7184693c09e48750b` (the §2.5 `H` of `physical-test-v1.json`), then run `verify_native_recording_application` on its `release.json` with the same expected hash.
6. **Permission.** Tell the operator: "If the new build asks for microphone access, choose Allow. This is not the P02 denial test." Ad-hoc signatures do not promise permission persistence across builds (`B/physical-gate-procedure.md:27-28`).
7. **Expectations.** Only P10–P14 plus fresh P04 and P05 are used here. Every step without a saved take shows Missing, and an unsaved P13 draft shows Draft, by design. Once history is complete, Save always advances; re-select the step before each additional take on it.
8. **Order.**
   1. P10, P11, P12, P13 and P13a, as the procedure describes. P11, P12, P13 and P13a each create a new context (`B/physical-gate-procedure.md:396`).
   2. Then fresh P04 and P05, in one app session with no sleep, relaunch or device change.
   3. Then P14 immediately. A package needs takes from one group (`recording_packages.py:452-455`), and Swift offers only current-group candidates (`RecordingPackagePreparation.swift:133-139`).
9. **Evidence.** P01–P09 evidence stays readable in `…c65af236` with its own build and catalog; it is not mapped. P14 intake stays inside the marked root, and every standard root refuses that package (guard #5).

Revision 3 replaces items 4 and 5 for any build from revision 3 on:
- **Catalog.** `voice-lab catalog init --asset-root B/physical-<UTC>/assets --subject-id cole-physical-diagnostic-<date> --from-builtin physical-test-v1`. It publishes and activates a version whose `catalog_sha256` is `b17cc176ee36ac5aa053c949e7ff44ce6e1b0c13b95b4cf7184693c09e48750b`, the §2.5 `H` of `physical-test-v1.json`; confirm it with `voice-lab catalog show --asset-root … --json`.
- **Settings.** Generate `native-application-settings/v2` settings with `purpose: "physical-test"` and `subject_id` equal to the marker subject. They carry no catalog.
- **Build.** `scripts/build-native-recording-app --output 'B/physical-<UTC>/release/Cole Voice Recorder Physical PREVIEW.app' --settings … --source-root …`, then run `verify_native_recording_application` on its `release.json`. Both check the root against the declared purpose. The app's verification report names the `catalog_sha256` the service delivered.

### 7.5 Rollback

- **Supported: rebuild the preview (same build) with pilot-v1 settings.** This opens a new group and resets the cursor. pilot-v1 coverage is exactly what it was before the switch, including the old room-tone takes, which were never deleted. Room tone recorded only under pilot-v2 does not count under pilot-v1.
- **Not supported: running a build older than Step 2 on a root that holds v2 evidence.**
  - That build's index audit fails on the first v2 catalog row (`recording_index.py:565`), so the service cannot open the index. Even after a manual `quarantine_cache` it cannot parse v2 groups, takes or package maps, so it fails closed.
  - No production quarantine command exists; the only callers of `RecordingIndex.quarantine_cache` (`recording_index.py:1851`) are tests. Nothing is lost; the forward build reads everything.
- **Source rollback.** Once the configured root holds any prompt-suite/v2 package map, group or take, this checkout MUST NOT be rolled back below Step 2 while the Qt launcher points at it. The Qt library parses package maps during its scan (`recording_library.py:545` → `recording_packages.py:787`).
- **Mixed builds.** Every build from Step 3 onward computes the same coverage and legacy rows for any catalog (§3.3, L1), so no build can strand another build's rows.
- **Revision 3.** A build from revision 3 records only against published store versions, and the store publishes `prompt-suite/v2` suites only, so it can no longer record against pilot-v1 itself. Rollback between catalog versions is `voice-lab catalog activate --version N` plus a relaunch. pilot-v1 coverage, including the old room-tone takes, stays readable with `voice-lab prompts coverage-report --catalog pilot-v1` and in the Qt app. A native build older than the corpus store cannot open a version-4 recording index or read `recording-group/v2` groups. The new build's service upgrades the index to version 4 when it starts, before anything is recorded, so an older native build MUST NOT run on a root once the new build has launched there. No command rebuilds or downgrades the index (`RecordingIndex.quarantine_cache` has only test callers). The fallback after that launch is the Qt app (`migrations/active/corpus-store.md`, "Rollback").

### 7.6 Operator notice for the pilot-v2 switch

Send this before Boundary 6, with the numbers from the §7.2 report:

> **Pilot catalog v2: what changes for your recordings**
> - Your 34 spoken prompts are unchanged, so every take you kept for them still counts (<covered spoken>/34 covered today).
> - Room Tone Start and Room Tone End now show their "Remain still and silent…" line under Instructions, with no Prompt. That change makes them new prompts, so your earlier room-tone takes do not count for v2. Both will show Missing until you record about 15 seconds of silence for each.
> - Nothing is deleted or changed on disk. Your earlier room-tone takes stay playable and reviewable in the Cole Voice Lab app you use today and in any pilot-v1 build. Deleting them would be a separate step, and I would ask you first.
> - The first time v2 opens, the library count shows —/36 while it checks your history. Save works during the check; Save & Next and Continue Missing wait until it finishes.
> - Unfinished sessions from the current Cole Voice Lab app can still be continued there, but not in the v2 build.
> - If we switch back to pilot-v1, your earlier room-tone takes count there again; room tone recorded in v2 does not.

### 7.7 Database catalogs

Added in revision 3. From the corpus store onward (D-017), the catalog the native app records against is a version published from the library's corpus store, `A/provenance/corpus/corpus.sqlite3`, backed by an immutable snapshot under `A/provenance/corpus/snapshots/`. `docs/CORPUS_STORE.md` is the full reference; this section states what changes for this design.

**Rules.**
- **Built-ins are seeds and pins.** `voice-lab catalog init --from-builtin <name>` copies a built-in's prompts into the store with unchanged prompt digests. The resources stay immutable and pinned (§2.5), and the `pilot` alias still MUST NOT move (§4). The store publishes `prompt-suite/v2` suites only, so a seed must form a valid v2 suite of the catalog's purpose; pilot-v1, whose room tones carry text (S2), seeds only with `--drop-room-tones`.
- **The app receives its catalog.** Settings and the app configuration carry no catalog. After its handshake the app sends `CatalogCommand`; the service answers with `CatalogEvent`, the subject's active version as an exact `PromptSuite` plus its `CatalogReference`, pinned for the life of the service process. The app opens its context by that reference.
- **Switching catalogs** is `voice-lab catalog activate --subject-id S --version N` plus a relaunch. Adding or editing a prompt is `add-prompt` or `edit-prompt`, then `publish`, `activate` and a relaunch. None of these needs a rebuild.
- **Groups.** New contexts mint `recording-group/v2` groups that reference the version instead of embedding the catalog. A new version is a new catalog, so its first open creates a new group and resets the cursor, as in §3.7. `recording-group/v1` groups stay readable forever (§3.8). `continue-legacy-recording` still opens a v1 group on the session's frozen catalog (§3.7).
- **Coverage** is §3.1 with keep-recordings aliases. A default edit gives the prompt a new digest, so its earlier takes stop counting in the new version and it shows Missing, as the room tones did in §3.2. An edit made with `--keep-recordings` keeps them counting, and the corrected text becomes their training transcript.
- **The coverage report and `finetune`** accept `<catalog_id>[@<version>]` beside the built-in names (§7.2).

**Seeding the configured root** (corpus-store plan Step 7), each item with operator approval:
1. Baseline, read-only: `voice-lab prompts coverage-report --asset-root A --subject-id cole-self --catalog pilot-v2 --json > R/<run>/baseline.json`.
2. `voice-lab catalog init --asset-root A --subject-id cole-self --from-builtin pilot-v2 --drop-room-tones`. This publishes and activates version 1 of `cole-corpus` (suite `cole-corpus`, purpose `capture-pilot`, 34 prompts, `catalog_sha256` `e234266eccb7b8a1c55e7454b32a2a61a99e72a3939bc4850a6951acc25fd7b6`).
3. `voice-lab catalog import-harvard` and `import-arctic`, each with `--from-file`, `--fetched-at` and the saved copy's real `--source-url`.
4. `voice-lab catalog publish --asset-root A`. With the 2026-09-16 copies this is version 2: 1,886 prompts, `catalog_sha256` `1ebd48cbc6f6a2b2ea0fe70d0cff8f1df1e91b3bbf2a5be52959ffba141ff90f`.
5. Before activating, `voice-lab catalog show --asset-root A --version 2 --json`. Require `reference.catalog_version` equal to 2 and `reference.catalog_sha256` equal to `1ebd48cbc6f6a2b2ea0fe70d0cff8f1df1e91b3bbf2a5be52959ffba141ff90f`. On a mismatch, stop and activate nothing: the seed or the saved copies differ from the 2026-09-16 inputs.
6. `voice-lab catalog activate --asset-root A --subject-id cole-self --version 2`.
7. `voice-lab prompts coverage-report --asset-root A --subject-id cole-self --catalog cole-corpus --baseline pilot-v2 --json`. The plan requires 34 of 1,886 covered, `changed_prompt_ids` equal to {`room-tone-start`, `room-tone-end`}, and exit 0.
8. `voice-lab finetune corpus --asset-root A --subject-id cole-self --catalog cole-corpus --allow-missing` selects the same takes as `--catalog pilot-v2`. Without `--allow-missing` it refuses, because the standard-set prompts have no take yet.
9. Build from `native-application-settings/v2` settings (no catalog), verify, install, and launch by exact path. That first launch upgrades the recording index to version 4, after which the older native build cannot open the root (§7.5).

**What the operator sees.** The first launch on the new version behaves like §7.1 items 1–2: a new group, then discovery at the cost of a first open. The Library lists the active version's prompts. The old room-tone takes stay on disk and count under pilot-v1 and pilot-v2 in the coverage report, but no prompt of `cole-corpus` lists them. Prepare for lab needs a room-tone take from the current group (§3.6), which a catalog without room-tone prompts cannot record.

**Refusals the operator can meet.** No store, no active catalog, and a missing or altered snapshot each leave the app without a Library, showing the service's message (`docs/NATIVE_RECORDING_UX.md` §10). The fix is the command the message names (`voice-lab catalog init` or `voice-lab catalog activate`), or restoring `provenance/corpus/` from backup, followed by a relaunch.

## 8. Implementation sequencing and test plan

### 8.1 Disposition of every `catalog-identity.md` §4 binding

Rows group bindings that share a disposition; every binding number appears once.

| # | Binding | Disposition |
| --- | --- | --- |
| 1 | group embeds catalog and hash; gate follows purpose (`recording_contracts.py:150-180`) | unchanged; physical-test uses the `not-required` branch |
| 2, 4, 6–9 | reservation prompt ∈ group catalog, exact (`:183-209`); group file path (`recording_takes.py:90`, `:715-720`); reserve by `prompt_id` with reread match (`:943-960`); selection and cursor shape, one cursor per subject (`recording_contracts.py:313-331`; `recording_takes.py:1250-1261`); `_check_selection` (`:1268-1284`); Save binds catalog and prompt (`:1173-1177`, `:309-312`; `recording_contracts.py:364-367`, `:972-977`) | unchanged |
| 3 | seal binds `H(reservation)` (`recording_contracts.py:461`) | unchanged; `exclude_if` keeps v1 reservation bytes (TM1) |
| 5, 10 | group reuse and file conflict compare catalog (`recording_takes.py:835-842`, `:876-889`); `open_context` keep-or-reset (`:1331-1400`) | unchanged; the first pilot-v2 open yields a new group and resets the cursor; guard #3 in `open_group` |
| 11–16, 18, 21 | navigate and missing resolver (`recording_takes.navigate`; `recording_library.py:823-837`); `next_missing` (`recording_index.py:1132-1193`); projection tables (`migrations/20260911070000_…sql`); `_register_catalog` (`recording_index.py:710-741`); registration call sites (`recording_service.py:565`, `:599`, `:1308`; `discover_recording_library`; `recording_index.py:997`, `:1618-1622`; `recording_index_backfill.py:296`); `history` (`recording_index.py:1271-1536`); `review_aliases` (`:1073-1088`); completeness transitions (`:798-820`, `:1782-1793`) | unchanged: exact hash is the coverage rule (§3.1); no migration; old room-tone takes are absent from pilot-v2 history and review |
| 17, 28, 41 | `_project` immutability (`recording_index.py:905-946`); legacy edit intents bind catalog (`recording_edits.py:734-745`); `replace_legacy_catalog` scope (`recording_index.py:1706-1713`) | unchanged; L1 keeps shared legacy rows identical, and sidecar-less rows bind pilot-v1's hash in every catalog (§3.4) |
| 19 | `_invalidate_take` (`:867-893`) | unchanged; a spoken take marks both pilot catalogs Unknown |
| 20 | `_audit` (`:553-584`) | unchanged; v1 prompt JSON re-serializes identically (TM1) |
| 22 | discovery checkpoint key; worker catalog check (`discover_recording_library`, `produce_discovery`) | unchanged (§3.5) |
| 23, 44 | `OpenContextCommand` (`recording_contracts.py:608-632`); corpus gate prepared on open (`recording_service.py:553-563`) | models unchanged; the service applies guard #3 first |
| 24 | settings and configuration embed catalog; reserved `--catalog-sha256` (`native_recording_application.py:39-75`, `:100-141`, `:565-577`) | model unchanged; builder and verify apply #2 and `--expect-catalog-sha256` |
| 25, 32, 33, 36, 47 | service catalog args and history catalog match (`recording_service.py:1268-1333`, `:663-668`); package map bindings (`recording_contracts.py:498-558`); package publication (`recording_takes.py:381-398`); sidecar canonical bytes and exact resume (`recording_library.py:198-212`, `:372-374`); `AuditionPlan` (`audition.py:160-190`) | unchanged |
| 26 | Swift catalog guards; current-group candidates (`RecordingWorkspaceModel` guards including `applyCursor`; `RecordingPackagePreparation.swift:133-139`) | guards unchanged; candidates narrowed (§3.6) |
| 27 | review resolver (`recording_review.py:113-133`) and `select_review` (`recording_takes.py:1610-1633`) | unchanged; exact containment |
| 29, 30, 34 | package plan embeds group and suite (`recording_packages.py:99-126`); one group per package (`:452-455`); map vs manifest and alias resolution (`:271-294`, `:875-883`) | unchanged; guard #4; old-group takes never join a pilot-v2 package; `procedure` is refused before manifests exist |
| 31 | subset suite ID (`recording_packages.py:233-247`) | extended: copy `schema_version` (Step 2); physical-test prefix (Step 4) |
| 35 | sidecar catalog, subset and room tone (`recording_library.py:101-156`) | unchanged; Qt refuses v2, so sidecars stay v1 |
| 37 | continue-legacy frozen catalog (`cli.py:525-531`; `recording_recovery.py:70-83`) | unchanged binding; operator limitation under pilot-v2 (§3.7) |
| 38 | legacy filters (`recording_library.py:547-551`, `:618-628`, `:667-677`, `:748-755`) | unchanged; exact |
| 39 | sidecar-less pilot rule (`:582-596`) | extended: `legacy_pilot_v1` by content; reordered branches (§3.4) |
| 40 | backfill IDs, header, Unknown, catalog, publish, errors (`recording_index_backfill.py:47-58`, `:68-69`, `:88-100`, `:189`, `:266-267`, `:314-327`) | extended: source catalog for sidecar-less rows; `allows_quiet`; the rest unchanged |
| 42, 43 | `PilotGateApproval` (`datasets.py:87-110`, `:448-469`); prepared gate evidence (`recording_takes.py:224-241`, `:738-815`) | models unchanged; guards #7 and #7a run before any write |
| 45 | `CaptureManifest` suite ID and transcripts (`models.py:98-168`; `capture.py:1807-1826`) | model unchanged; guard #10 and §5.4 |
| 46, 48 | `DatasetEntry` bindings and suite mapping (`datasets.py:117-162`, `:498-560`; `cli.py:250-265`); reference source binding (`references.py:115-146`, `:270-337`) | models unchanged; guards #6 and #8; v2 capture suites allowed |
| 49 | consent and intake bind no suite (`models.py:218-271`; `intake.py:539-600`) | guard #11 for consent approval; guard #5 for intake |

Revision 3: the dispositions above are revision 2's and were executed as written. Where a row above says "unchanged" or "exact", read it together with this list. The corpus store later changed these bindings, among others (§7.7, `docs/CORPUS_STORE.md`):
- **1, 5, 10:** new groups are `recording-group/v2` and carry a `CatalogReference`; reuse compares the reference.
- **2, 4:** the contract's exact-membership check (`CaptureReservation`) applies to `recording-group/v1` groups only. A v2 group carries no prompts, so `RecordingTakeStore.reserve` looks the prompt up by id in the group's snapshot.
- **11–16, 18, 21:** index schema version 4 adds the prompt-alias projection; coverage counts through keep-recordings aliases (§3.1).
- **19:** `_invalidate_take` marks Unknown every catalog in which the take's digest is current or a counted alias.
- **20:** `_audit` also audits the prompt-alias projection (`_audit_prompt_aliases`).
- **22:** discovery resolves each group's catalog with `resolve_group_catalog`, so a v2 group is read through its sealed snapshot.
- **23, 44:** `OpenContextCommand` names `catalog_reference`; its embedded form survives only inside `ContinueLegacyRecordingCommand` (row 37, unchanged).
- **24:** settings and configuration carry no catalog and declare a purpose; `--expect-catalog-sha256` is removed.
- **25:** the workspace service pins its catalog from the store; `--catalog-sha256` is optional in workspace mode.
- **27:** exact containment, or containment through a keep-recordings alias (§3.1). Review resolves an alias-covered take to its current prompt (`CatalogSnapshot.require_current_prompt_for`). It refuses a take whose prompt no longer counts with `RecordingPromptNotCountedError`, which the service sends as `RECORDING.STALE_REVISION`.
- **29, 30, 34:** the package plan resolves the group's catalog through the take store (`group_catalog`) instead of reading an embedded suite.
- **38:** exact, or through a keep-recordings alias (§3.1): `RecordingLibrary.counts` also accepts a recorded prompt that the catalog's aliases accept.
- **39:** `legacy_pilot_v1` also takes `aliases=`; an accepted digest equal to a pilot-v1 prompt digest counts as shared.
- **40:** backfill registers the library's catalog snapshot, aliases included (`register_catalog(library.catalog)`).

### 8.2 Steps, deliverables and verification

Every command runs from the repository root, is self-contained, and must exit 0. Three abbreviations are used below:
- **Static:** `.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy src/cole_voice_lab && .venv/bin/python -B -m cole_voice_lab.schema --check`
- **Pytest `<files>`:** `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -B -m pytest -q <files>` (environment as in `scripts/verify:6-8`)
- **Native `<suite>`:** `scripts/verify-native-recording --suite <suite> --output "$(mktemp -d /private/tmp/cvl-native-XXXXXX)/<suite>"`, which gives every run a new private directory outside the repository (`scripts/verify_native_recording.py` refuses anything else). "Unlocked" marks suites that need the screen unlocked.

Each step records every mutation (file, edit, failing test name) as evidence in R.

**Step 2: v2 models, schemas, Swift decoding and the dataset refusal (v1 bytes unchanged).**
- Deliver:
  - `models.py`: §2.2 fields, P1–P4, S1–S5, S7; the §2.4 helpers, the five `allows_quiet` replacements and `require_text()` at the strict-mypy `text` consumers.
  - `pyproject.toml`: `pydantic>=2.13.4,<3`.
  - `_selected_suite` copies `schema_version` (§3.6).
  - Guards #1, #8, #10, the v2 half of #9, the Step 2 form of #2 and #3, and `diagnostic_roots.is_physical_test_suite_id` (§5.3).
  - Schemas regenerated with `scripts/generate-schemas`.
  - Swift: `RecordingPromptPresentation` without `text`, `RecordingSemanticValidator` rules, the Step 2 card (§2.7), and the v2 conformance cases in `tests/test_recording_contracts.py`.
  - Tests TM1–TM4, TS1, TU0, TG2 and TQ1 (§8.3).
- Verify:
  - Static.
  - Pytest `tests/test_models.py tests/test_resources.py tests/test_schema.py tests/test_recording_contracts.py tests/test_capture.py tests/test_capture_console.py tests/test_desktop.py tests/test_desktop_window.py tests/test_recording_library.py tests/test_recording_packages.py tests/test_recording_takes.py tests/test_recording_analysis.py tests/test_recording_index.py tests/test_recording_service.py tests/test_native_recording_application.py tests/test_datasets.py tests/test_experiments.py tests/test_cli.py tests/test_qt_launcher_isolation.py`
  - Native `contracts`, `workspace`, `transport`, `package-preparation`; unlocked: `shell`, `accessibility`.

**Step 3: Prove the pilot-v2 coverage consequence and add the read-only coverage report.**
- Deliver:
  - `prompt_catalogs.py` with pins for `pilot-v1.json` and `evaluation-v1.json` (§3.3).
  - The sidecar-less rule by content in `recording_library.py`, and the source catalog for sidecar-less rows in `recording_index_backfill.py` (§3.4).
  - Public `is_retained` and `history_entry_for_take` in `recording_index.py`.
  - `prompt_coverage.py` and `voice-lab prompts coverage-report` (§7.2).
  - Tests TC1–TC3 on a synthetic in-memory pilot-v2 built by the §4 contract, TD1, TD2, and the TQ1 fixture extension (§8.3).
- Verify:
  - Static.
  - Pytest `tests/test_prompt_catalogs.py tests/test_pilot_v2_coverage.py tests/test_prompt_coverage.py tests/test_recording_library.py tests/test_recording_index.py tests/test_recording_index_lock_lifetime.py tests/test_recording_navigation.py tests/test_recording_discovery*.py tests/test_recording_legacy_*.py tests/test_recording_takes*.py tests/test_recording_packages.py tests/test_recording_contracts.py tests/test_recording_service.py tests/test_recording_edits.py tests/test_desktop.py tests/test_cli.py tests/test_qt_launcher_isolation.py`
  - Native `transport`.

**Step 4: Refuse physical-test evidence outside diagnostic roots.** Specialist: `doddy`.
- Deliver:
  - The rest of `diagnostic_roots.py`, `DiagnosticRootMarker`, `schemas/diagnostic-root.schema.json` and `voice-lab assets init --diagnostic physical-test` (§5.2).
  - Guards #2 and #3 in final form, #4, #5, #6, #7, #7a, the root half of #9, and #11.
  - The physical-test subset prefix in `_selected_suite` (§3.6), builder `--expect-catalog-sha256`, Swift candidate narrowing and the "Prepare diagnostic package" title (§5.5).
  - TP1 with synthetic physical-test suites only.
- Verify:
  - Static.
  - Pytest `tests/test_diagnostic_roots.py tests/test_datasets.py tests/test_recording_packages.py tests/test_references.py tests/test_intake.py tests/test_consent.py tests/test_native_recording_application.py tests/test_recording_service.py tests/test_recording_takes.py tests/test_capture.py tests/test_capture_console.py tests/test_desktop.py tests/test_schema.py tests/test_cli.py tests/test_qt_launcher_isolation.py`
  - Native `package-preparation`.

**Step 5: Author `pilot-v2.json` and `physical-test-v1.json`.**
- Deliver:
  - Both resources by their §4 contracts, each with its pin in the same commit.
  - Loader aliases `pilot-v2` and `physical-test-v1` (`pilot` stays v1), CLI choices, and v2 `prompts show` output.
  - TR1, TS2, TC1 and TC2 re-run against the real pilot-v2, and the TQ1 fixture on the real resource.
- Verify:
  - Static.
  - Pytest `tests/test_resources.py tests/test_prompt_catalogs.py tests/test_pilot_v2_coverage.py tests/test_prompt_coverage.py tests/test_recording_contracts.py tests/test_recording_library.py tests/test_recording_index.py tests/test_recording_navigation.py tests/test_native_recording_application.py tests/test_desktop.py tests/test_cli.py tests/test_qt_launcher_isolation.py`
  - Native `contracts`.

**Step 6: Present Labels, Prompt and Instructions.**
- Deliver: the §6.1–§6.3 chips and script layout, row icons, and the spec amendment in `docs/NATIVE_RECORDING_UX.md` §§4–5. Test TU1 (presentation half).
- Verify: Native `workspace`; unlocked: `shell`, `accessibility`. Both unlocked harnesses already run light and dark (`RecordingHarness.swift:312-322`).

**Step 7: Add the label OR filter and a prominent Continue Missing.**
- Deliver: §6.4, Continue Missing above search with a prominent style and an accessibility identifier, and the transport harness focus assertion. Test TU1 (filter half).
- Verify: Native `workspace`, `transport`; unlocked: `shell`.

**Step 8: Support System, Light and Dark appearance.**
- Deliver:
  - A View ▸ Appearance menu with System, Light and Dark, stored as a user preference.
  - The override is applied at the `Window` scene in `RecordingApp/RecordingApp.swift:9-14`, never inside `RecordingWorkspace`. The harnesses host `RecordingWorkspace` with forced `NSAppearance` and `colorScheme` (`RecordingHarness.swift:312-322`, `RecordingAccessibilityHarness.swift:26-27`), so a stored "Light" preference must not turn their dark pass light.
  - Returning to System restores the system appearance without a relaunch.
  - A hard-coded colour audit that replaces non-adaptive colours with semantic ones.
  - Test TA1: the preference type lives in `RecordingUI`, and its persistence test uses `UserDefaults(suiteName:)`, never `.standard`.
- Verify: Native `workspace`, `waveform`; unlocked: `shell`, `accessibility`.

**Step 9: Full verification of this plan.**
- Verify, unlocked: `scripts/verify`, then Native `all`.

### 8.3 Tests and mutations

Each "Mutation" is a deliberate source change that MUST make the preceding test fail.

**Step 2**

- **TM1 hash stability.**
  - Pins the pilot-v1 and evaluation-v1 goldens of §2.5.
  - Pins canonical-bytes digests of deterministic synthetic group, reservation, take, package map, sidecar, dataset entry and audition plan instances. The digests are captured on the pre-Step-2 tree and committed as constants before the model edit, so the test is not tautological. Equal bytes also mean `RecordingIndex._audit` keeps accepting existing prompt rows (`recording_index.py:579-583`).
  - Mutation: remove `exclude_if` from `labels`.
- **TM2 v1 strictness.** A v1 suite with labels, instructions, absent text, `procedure` or `physical-test` is refused. Mutation: delete S1.
- **TM3 v2 matrix.** P1–P4 (every reserved token), S2–S5, S7 (U+200B, U+202E and `\t` refused; `\n` allowed) and every bound. Mutation: allow text on a v2 room tone.
- **TM4 schemas.** `schema --check` passes, and generated schemas use only the keywords at `RecordingSchemaValidator.swift:92`. Mutation: make `labels` a `frozenset` (it emits `uniqueItems`).
- **TS1 Swift contracts.** v1 and v2 suites validate; absent and `null` text are equivalent; the S2, S4 and S7 refusals fire; the Python↔Swift conformance cases agree. Mutation: restore the old `:33` rule.
- **TU0 Swift card.** v2 room-tone and procedure fixtures never render the placeholder or `READ ALOUD`, and v1 spoken rendering is unchanged. Mutation: bind the card to an optional `text` again.
- **TG2 Step 2 guards.**
  - #9: `capture_prompt_suite`, `TakeEngine` and Qt refuse a v2 suite.
  - #10: finalize refuses a prefix/purpose disagreement, a v2 suite with a non-`native-` session, and a `procedure` prompt.
  - #8: `build_dataset_manifest` refuses a physical-test suite before building a `DatasetEntry`.
  - #2 and #3: builder, verify, open-context, continue-legacy and `open_group` refuse a physical-test catalog; open-context refuses before `context_replay` and `prepare_corpus_gate` run.
  - Mutations: drop the finalize prefix check; move the open-context refusal after `context_replay`.
- **TQ1 Qt launcher isolation** (Steps 2–5, in a subprocess).
  - The driver runs as `sys.executable -I -B driver.py <root>`, like the launcher. Its first statement installs `sys.addaudithook`, before `import cole_voice_lab`. The hook raises on any `open` of a `.json` file under the package `resources/` directory other than `pilot-v1.json`, and it logs every allowed open.
  - The driver stubs `PySide6.QtGui`, `PySide6.QtWidgets` and `cole_voice_lab.desktop_window` in `sys.modules`, then runs the `desktop --suite pilot` CLI entry, so `run_desktop` constructs the real `RecordingLibrary`. The stub window calls `verified_history_snapshot()`.
  - The root holds a sidecar session and a sidecar-less `TakeEngine` session. From Step 3 it also holds a local package prepared from a synthetic pilot-v2 group (map, native takes, `raw/` alias); from Step 5 that group uses the real resource.
  - Assertions: `canonical_sha256` of the library's suite is `eac118a9…72d0`; the snapshot succeeds; the pilot-v2 package's spoken entries are listed and no session is `(catalog unknown)`; every logged resource open is `pilot-v1.json` (at least one). At Step 2 the Qt path still opens it twice, from `run_desktop` and the sidecar-less rule, because `load_prompt_suite` has no cache. From Step 3, once `legacy_pilot_v1(C)` recognizes pilot-v1 without opening a file, assert exactly one open. The existing launcher test still shows `create_desktop_application` writing `--suite pilot`.
  - Mutations: alias `pilot` to `pilot-v2.json` (Step 5; the hook and the hash assertion fail); load every built-in at import of `prompt_catalogs` (Step 3; the hook refuses `evaluation-v1.json`).

**Step 3**

- **TC1 native coverage.**
  - Fixture: a synthetic pilot-v2 built in memory from pilot-v1 by the §4 contract, and pilot-v1-group native takes on one spoken prompt and on `room-tone-start`.
  - Under pilot-v2: the spoken prompt is Recorded; both room tones are Missing; Continue Missing reaches `room-tone-start`; room-tone history lists no take; the resolver refuses the v1 room-tone take. Under pilot-v1 that take is listed, reviewable and counted.
  - Covered counts: 2 under pilot-v1, 1 under pilot-v2.
  - Mutations: match `next_missing` coverage by `prompt_id` instead of hash; join history takes by `prompt_id`.
- **TC2 legacy coverage.**
  - Inputs: sidecar sessions (one finalized with READY, one in progress with an interrupted draft) holding kept, rejected and removed takes on a spoken prompt and a room tone; a sidecar-less `cole-pilot-v1` `TakeEngine` manifest; a foreign native `cole-pilot-v2` package without a map.
  - Publishing pilot-v1 → pilot-v2 → pilot-v1 leaves `recording_takes` payload bytes and pilot-v1 coverage identical. Under pilot-v2, spoken rows from both sessions count and no room-tone row appears.
  - The foreign package yields exactly one `legacy:<session>` Unknown record under both catalogs, no `legacy-backfill` record, and every other row published.
  - Variant: between publications, apply a legacy edit through the pilot-v2 context (review, then reject) and create a READY file. There is one row per event, its revision is kept, bytes are identical, and `_project` raises nothing.
  - L1: a synthetic private catalog that shares three spoken prompts with pilot-v1 publishes rows identical to pilot-v1's on those hashes.
  - Mutations: keep `catalog_sha256 = catalog` for sidecar-less rows; require built-in membership in `legacy_pilot_v1`; restore the order that checks the suite ID before the `native-` early return.
- **TC3 evidence preservation.** Switching the TC2 root without its foreign package pilot-v1 → pilot-v2 → pilot-v1, with open-context and discovery to Complete each time, removes no file. Every regular file that existed before keeps its bytes, except the subject cursor, `provenance/recording-takes/.store-head.json` (every open-context commit rewrites both), and files under `provenance/recording-library/` and `provenance/recording-library-discovery/`.
- **TD1 read-only report.**
  - On the TC2 root without its foreign package: `changed_prompt_ids` is both room tones, `lost_prompt_ids` is the room tones covered under pilot-v1, both Unknown lists are empty, and the exit status is 0.
  - With the foreign package, both Unknown lists hold its `legacy:<session>` record and the exit status stays 0; §7.3 still blocks Boundary 6.
  - The exit status is 1 for a projection failure, and for a local package whose group catalog shares no prompt with the report catalog, because its Unknown record then appears only under that catalog.
  - A subprocess audit hook proves no write, no directory creation and no SQLite open under A.
  - Mutation: construct `RecordingTakeStore` without `read_only=True` (the hook sees a directory creation or write).
- **TD2 parity.**
  - `is_retained` equals `_RETAINED` for every decision × analysis pair, evaluated in an in-memory SQLite table.
  - On the TC1 and TC2 fixtures, each prompt's `covered` equals `kept_count > 0` in `RecordingIndex.history`.
  - Mutation: make `is_retained` accept `draft`.

**Step 4**

- **TP1.** Covers every §5.3 row for marked and unmarked roots, modeled on `tests/test_recording_packages.py:606`, `tests/test_datasets.py:690-706` and `tests/test_recording_library.py:699`, including:
  - a refused `dataset pilot-gate` command leaves `provenance/pilot-gates/` without a new file;
  - a dangling-symlink marker raises `SecurityError`;
  - `assets init --diagnostic` refuses a non-empty asset directory and a subject without the prefix, and installs no consent;
  - open-context and continue-legacy refuse before `context_replay` and `prepare_corpus_gate` run;
  - a physical-test subset is named `cole-physical-test-subset-*`; `plan` refuses `procedure`; the builder refuses an `--expect-catalog-sha256` mismatch;
  - the unmarked one-off pattern (standard root, `cole-physical-diagnostic-*` subject, private capture-pilot catalog) still opens.
  - Mutations: remove the intake prefix check; move #7a after the write.

**Step 5**

- **TR1 resources.**
  - Both resources follow their §4 contracts (IDs, categories, durations, labels, forbidden substrings).
  - `H(pilot-v2)` and its room-tone hashes equal §2.5, and its 34 spoken prompts equal pilot-v1's.
  - Every pin equals `H` of its loaded resource.
  - `legacy_pilot_v1` is true for pilot-v1 and pilot-v2 and false for evaluation-v1 and physical-test-v1.
  - `pilot` loads v1, and no `physical-test` alias exists.
  - `load_builtin_prompt_suite` refuses `"../x"` and any name outside the table; a working-directory file named `pilot-v2.json` does not change its result.
  - Mutations: edit one byte of a resource's content (the pin refuses); define `legacy_pilot_v1` as table membership (the evaluation-v1 and physical-test-v1 cases fail). The alias mutation belongs to TQ1, because `tests/test_resources.py:38-45` stays green when `pilot` is aliased to v2.
- **TS2 Swift resources.** Both resources are added to `synthetic_contracts` and validate as `PromptSuite` in Swift through the contracts suite.

**Steps 6–8**

- **TU1 Swift `RecordingWorkspaceTests`.**
  - Derived fields for v1 and v2, including v1 room-tone text shown as Instructions.
  - For spoken, room tone and procedure, in v1 and v2: `recording.script` exists iff the prompt is spoken, and `recording.instructions` always exists and contains the guidance.
  - Chip order, with the category and `Test` chips pinned; a label or instruction containing `**x**` or `[a](b)` renders literally.
  - The search haystack and the OR truth table: empty selection shows all; A ∨ B; combined with status and search; `currentPromptFilteredOut`.
  - No `GEOMETRY_*` failures. The accessibility harness (`RecordingHarness/RecordingAccessibilityHarness.swift:30`) additionally requires `recording.instructions`, and `recording.script` for its spoken prompt `reading-one`.
- **TA1 appearance.** The preference round-trips through `UserDefaults(suiteName:)`. With "Light" stored in the harness process's defaults, the shell and accessibility dark passes still render dark. Mutation: apply the override inside `RecordingWorkspace`.

**Existing suites** stay green: `tests/test_recording_index.py`, `test_recording_navigation.py`, `test_recording_library.py`, `test_recording_discovery*.py`, `test_recording_legacy_*.py`, `test_recording_takes*.py`, `test_recording_packages.py`, `test_recording_contracts.py`. No existing test pins the replaced sidecar-less branch (repository grep, 2026-09-14).

## 9. Open questions and follow-ups

**Open questions for the operator:** none. `operator-choices-05.json` decided the room-tone consequence; §7.6 is the notice before the switch.

**Operator FYI** (for the resume notification): pilot-v2 shows the category chip and, where set, the Reference candidate chip. A label added later to an already-recorded spoken prompt would change that prompt's identity, so its earlier takes would stop counting in that catalog version, as room tone does now.

**Follow-ups that need no operator decision:**
- **F2.** Amend the plan's Done criterion "existing diagnostic takes still show as Recorded where mapped". TC1–TC3 prove the pilot-v2 consequence with synthetic fixtures; the one-off diagnostic catalogs never switch.
- **F3.** Update `B/physical-gate-procedure.md` so future diagnostic builds use the `physical-test` purpose, a marked root and the §7.4 order.
- **F4.** Add `DECISIONS.md` D-016, "Prompt suites are versioned by new immutable resources". It says: built-in suites are immutable and hash-pinned; coverage is exact per prompt hash, so a changed prompt starts Missing; stored evidence is never rewritten or deleted; D-010's "never migrated" still holds. Also update `docs/NATIVE_RECORDING_APPLICATION.md` (settings from built-ins), `docs/DESKTOP.md` (Qt refuses v2) and `docs/RUNBOOK.md` (its `--suite pilot` examples at `:164`, `:210` and `:230` stay valid). Revision 3: D-016 was used for the fine-tune corpus decision. D-017 records that catalogs are data in the corpus store, with built-ins as immutable seeds and pins, and reconciles D-010. The `NATIVE_RECORDING_APPLICATION.md` item is moot, because settings carry no catalog. The `DESKTOP.md` (Qt refuses v2) and `RUNBOOK.md` items remain open.
- **F5.** Parity for transferred full pilot-v2 packages (§3.4), together with any transfer work.
- **F7.** Structure signal from the integrity review, recorded without acting on it: label assignments held outside `H(p)`, as a suite-level overlay. Without supersession it tips over at the first label added to a recorded spoken prompt.
- **F8.** The pre-existing path-first rule in `load_prompt_suite` also affects the live launcher's `desktop --suite pilot`.

F1 and F6 are retired (Appendix A.4).

## Appendix A. Review resolution

### A.1 Round 2

| Review | Finding | Resolution | Where |
| --- | --- | --- | --- |
| sequencing S1 (blocking) | physical-test evidence could be packaged before the corpus guards existed | The subset prefix lands with guards #4–#8 in Step 4, which precedes Step 5's resource. Step 2 already refuses physical-test catalogs in the builder, verify and open-context. Qt stays closed and no preview is built before Step 9 (plan Context). TI7's physical-test cases moved to TP1 | §3.6, §5.3, §7.3, §8.2 |
| sequencing S2 (blocking) | Step 3 tests relied on Step 5 guards | The trust half is moot. The rule stays: a refusal lands with the capability it guards. Step 2 lands the physical-test refusal with the purpose; Step 4 adds the marked-root branch; each step tests only its own guards | §5.3, §8.2 |
| sequencing S3 (blocking) | Step 2 broke `mypy --strict` at `datasets.py:554` | guard #8 lands in Step 2; ruff and mypy run in every Python step's Verify | §5.3, §8.2 |
| sequencing 1–2 | Step 2 deliver list and verify commands | adopted | §8.2 Step 2 |
| sequencing 3 | TM1 was tautological | goldens plus pre-Step-2 digests | TM1 |
| sequencing 4 | Swift `.map` closure; S1 must not use `present()` | adopted | §2.7 |
| sequencing 5, 6 | migration commit test updates; coverage cache key | moot: no migration, and `legacy_pilot_v1` has no registry input | §3.3, §3.8 |
| sequencing 7 | audit hooks cannot be removed in-process | TQ1 and TD1 run in subprocesses | §8.3 |
| sequencing 8, 10–13 | per-step deliverables and test lists | adopted for the new step order | §8.2 |
| sequencing 9 | plan wording on room-tone category and style | only P04 is room tone; style kept | §4 |
| sequencing 14, 15 | name the harness suites; Step 8 scene-level override and `UserDefaults(suiteName:)` | adopted | §8.2, TA1 |
| sequencing 16 | Done criterion | F2 | §9 |
| supersession M1 (blocking) | builds with different built-in sets used two coverage definitions | Moot: there are no edges, no trust and no build-dependent coverage. Its item 8, `legacy_pilot_v1` differing between builds, is resolved by the content-only definition and L1 | §3.3, §3.4 |
| supersession 1–7 | TI3b cycle fixture, resolution termination, anchored but untrusted built-ins, frozen `view`, promotion, `test` in `view`, D-016 authoring note | moot | — |
| legacy-mapping-qt 1 | TQ1 mutation ineffective in-process | subprocess driver; hook installed before import; every other resource JSON refused | TQ1 |
| legacy-mapping-qt 2 | post-Boundary-6 fixture; table order | local pilot-v2 package in TQ1 from Step 3; pins in release order with pilot-v1 first | §2.5, TQ1 |
| legacy-mapping-qt 3 | foreign package impact understated | adopted | §3.4 |
| legacy-mapping-qt 4 | the proof could not see Unknown records; fresh count | adopted | §7.2, §7.3 |
| legacy-mapping-qt 5 | pin `legacy_pilot_v1` per built-in; record the path-first divergence | adopted | §3.3, §3.4, TR1 |

### A.2 Round 1 blocking issues

| Review | Issue | Resolution | Where |
| --- | --- | --- | --- |
| integrity B1 | T3 let non-spoken content change | moot: no supersession | — |
| integrity B2 | T5 made trust non-monotone | moot: no supersession | — |
| trust B1 | `dataset pilot-gate` wrote a permanent approval before the refusal | #7a runs in the writer's loop before `write_canonical_json`; TP1 checks `provenance/pilot-gates/` | §5.3, TP1 |
| trust B2 | the sidecar-less pseudocode raised on a transferred pilot-v2 package | reordered rule; the foreign package becomes a named Unknown record; TC2 | §3.4 |
| operator B1 | v2 previews could be built before the Swift presentation | builds wait for Step 9; the Step 2 card never shows the placeholder or `READ ALOUD` for silent kinds (TU0) | §2.7, §7.3 |
| operator B2 | the Qt launcher could depend on new resources; wrong TR1 mutation | nothing loads at import; pins land with their files; TQ1; TR1 mutation text corrected | §3.3, TQ1, TR1 |
| operator B3 | no distinct Instructions section | one layout for every kind, both identifiers tested | §6.3, TU1 |

### A.3 Round 1 non-blocking items

| Review item | Disposition | Where |
| --- | --- | --- |
| integrity N1 (anchors outside build identity) | adopted as loader pins with no trust role | §2.5 |
| integrity N2, N4, N5, N10; trust E3, E10, E11, E13 (trust invariants, interim window, duplicate coverage, Down migration) | moot: no supersession or migration | — |
| integrity N3 (subject key on `cov` joins) | moot: queries unchanged; each site binds the subject today | §3.1 |
| integrity N6, operator minor (map-less pilot-v2 package) | as trust B2 | §3.4 |
| integrity N7 (legacy ownership link table) | resolved structurally by L1 | §3.4 |
| integrity N8, operator N8 (second coverage implementation) | shared `legacy_projection_records`, `history_entry_for_take` and `is_retained`; TD2 parity; WAV access stated | §7.2 |
| integrity N9 (R meant two roots) | A is the asset root; reports go to R | header |
| integrity N11 (TI2 filesystem changes) | kept in TC2 | TC2 |
| integrity N12 (discovery citations) | cited by symbol | §3.5 |
| integrity graph-shape notes | label overlay recorded as F7; lineage moot | §9 |
| trust E4 (name shadowing) | closed-name loader; `--expect-catalog-sha256` | §2.5, §4, §7.3 |
| trust E5 (label spoofing, hidden purpose) | reserved tokens, purpose-derived pinned `Test` chip, verbatim rendering | §2.3, §6 |
| trust E6, E7, E9 (guard #3 placement, marker fail-open, marking a used root) | first statement of the block and `open_group`; `lstat` semantics; every `ASSET_DIRECTORIES` member absent or empty | §5.2, §5.3 |
| trust E8 (diagnostic subject) | marker prefix, no consent, reader and consent refusals; standard roots still open one-off subjects | §5.2, §5.3 |
| trust E12, E14, E15 (private text, invisible text, manifest minting) | authoring rule and TR1 lint; S7 (v2 only); guard #10 | §4, §2.3, §5.3 |
| trust "can't tell" items | frozen resources: pins; "rebuild required" behavior: this design adds none; report read-only: TD1; lab-side intake: §7.3; Swift: TU1 | as listed |
| operator N1–N7, N9, N10, minor (checklist, P14 order, Save advances, Qt restart and source rollback, which roots switch, continue-legacy, rollback losses, authoring details, filter copy, `prompts show`, error message) | adopted | §3.7, §4, §5.3, §6.4, §7.1, §7.3–§7.5 |
| operator N11, N12 (decision record, open questions) | F4; no open questions, labels FYI | §9 |

### A.4 Changes and corrections in revision 2

- **Supersession removed** per `operator-choices-05.json`: the `supersedes` field, P5, S6, the T1–T5 trust rule, migration 4 with `recording_prompt_supersessions` and `recording_catalog_coverage`, audit rules A1–A3, `CatalogCoverage`, the coverage-driven query and review changes, and the edge logic of `compare-coverage`, which became the §7.2 report.
- **`legacy_pilot_v1` is defined by content**, not by membership in the running build's pin table. This makes legacy rows build-independent (L1), retires F1, and adds the one v1 difference in §3.4.
- **F6 retired.** This design adds no "rebuild required" state.
- **Step order.** Guards (Step 4) now precede resources (Step 5); Step 3 proves the coverage consequence.
- **Citations.** `_capture_inputs` is at `cli.py:435`. `RecordingWorkspaceModel.swift` has drifted (catalog mapping at `:126`, `applyCursor` at `:602`), so it is cited by symbol. Both one-off diagnostic catalogs share no prompt hash with pilot-v1.

### A.5 Changes in revision 3

Revision 3 (2026-09-16) follows the corpus store (`.claude/plans/2026-09-15-catalog-in-database-corpus-store.md`, D-017, `docs/CORPUS_STORE.md`). It changes no revision-2 decision about labels, instructions, pilot-v2 or the physical-test purpose.

- **Status line** names revision 3 and this appendix.
- **§1 non-goals and §3.1:** keep-recordings aliases are the one bounded exception to exact coverage, for database catalogs only; the alias identity rule is stated.
- **§2.5:** built-ins are also corpus-store seeds; settings no longer name a built-in; the service build identity also fingerprints the corpus store and prompt-alias migrations.
- **§3.8:** the switch-by-settings sentence is struck; switching is `voice-lab catalog activate` plus a relaunch. The corpus store's own manifest is named, and `recording-group/v1` groups are permanent reader semantics like the sidecar-less rule, which §3.4 defines.
- **§4:** the entry-point rows for settings, `prompts coverage-report` and `prompts show` describe settings without a catalog, the removed `--expect-catalog-sha256` and published catalog names. The pilot-v2 goldens note how the store reproduces `H(pilot-v2)`.
- **§5.3 #2, #3:** the builder and verify check the declared purpose; the service guard also covers `CatalogCommand` and the pre-context snapshot; `catalog init` and `activate` apply it.
- **§7.2:** published catalog names, the added report fields, the store opening for a published name, and the fail-closed snapshot rule.
- **§7.3, §7.4, §7.5:** revision-3 notes replace the settings-with-catalog and `--expect-catalog-sha256` steps with the §7.7 digest check, state rollback between store versions, and state that the new build's first launch upgrades the recording index so the older native build can no longer open the root.
- **§7.7** is new: database catalogs and the seeding sequence for the configured root, including the digest check before `catalog activate`.
- **§8.1:** a note lists the bindings the corpus store changed, including the reservation, invalidation, audit, discovery, review, legacy-filter, sidecar-less and backfill rows.
- **§9 F4:** its decision-record and settings items are resolved or moot; the `DESKTOP.md` and `RUNBOOK.md` items remain open.
- **History left as written.** These lines still name `--expect-catalog-sha256` or an inline settings catalog because they record revision 2's plan steps as executed: §5.3 "Sequencing" (Step 4), §8.1 row 24, §8.2 Step 4, TP1 in §8.3, and Appendix A.3 (trust E4). They do not describe the current build.

## References

- R/`operator-choices-04.json`; R/`operator-choices-05.json`; R/`feature-maps-01/{catalog-identity,sidebar-ui,theme-and-ux-findings,diagnostic-suite-plumbing}.md`; R/`catalog-v2-design/review-{integrity,trust,operator}.md`; R/`catalog-v2-design/round2-{sequencing,supersession,legacy-mapping-qt}.md`; B/`physical-gate-procedure.md`; B/`configured-library-count.json`; B/`count_configured_library.py`; B/`physical-20260914T013126Z-i649qel_/settings.json`; B/`physical-20260914T064801Z-c65af236/settings.json`.
- `.claude/plans/2026-09-14-prompt-labels-instructions-test-suite.md`; `.claude/plans/2026-09-11-native-recording-ux.md:34-51`; `.claude/handoff.local.md`.
- `docs/NATIVE_RECORDING_UX.md` §§4–6, 9–11; `docs/DESKTOP.md:314-316`; `docs/NATIVE_RECORDING_APPLICATION.md:1-6`; `DECISIONS.md` D-010, D-012, D-013; `AGENTS.md`.
- Python sources: `models.py`, `resources.py`, `canonical.py`, `schema.py`, `paths.py`, `consent.py`, `cli.py`, `desktop.py`, `desktop_application.py`, `desktop_window.py`, `experiments.py`, `recording_index.py`, `recording_index_backfill.py`, `recording_library.py`, `recording_takes.py`, `recording_contracts.py`, `recording_review.py`, `recording_packages.py`, `recording_discovery.py`, `recording_service.py`, `recording_recovery.py`, `recording_analysis.py`, `recording_edits.py`, `native_recording_application.py`, `datasets.py`, `references.py`, `intake.py`, `capture.py`, `audition.py`.
- Tests: `tests/test_resources.py`, `tests/test_recording_index.py`, `tests/test_recording_navigation.py`, `tests/test_recording_packages.py`, `tests/test_datasets.py`, `tests/test_recording_library.py`, `tests/test_recording_contracts.py`.
- Build and verification: `migrations/20260911070000_create_recording_library_projection.sql`; `pyproject.toml`; `scripts/verify`; `scripts/verify-native-recording`; `scripts/verify_native_recording.py`; `scripts/build-native-recording-app`; `scripts/generate-schemas`.
- Swift, under `apps/macos/Sources/`: `RecordingApp/RecordingApp.swift`; `RecordingContracts/{RecordingSchemaValidator,RecordingSemanticValidator}.swift`; `RecordingUI/{RecordingPromptPresentation,RecordingWorkspace,RecordingWorkspaceModel,RecordingPackagePreparation,RecordingLayout}.swift`; `RecordingHarness/{RecordingHarness,RecordingAccessibilityHarness}.swift`.
