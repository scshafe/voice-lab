# Migration: corpus-store

- **Purpose:** move the prompt catalog out of the build and into the library. Prompts and
  catalog versions live in `provenance/corpus/corpus.sqlite3` with immutable snapshots under
  `provenance/corpus/snapshots/`; new recording groups reference a published version instead
  of embedding the catalog; the app loads its catalog from the recording service at launch.
  A prompt change becomes an edit, a publish, an activate and a relaunch, never a rebuild.
- **Plan:** `.claude/plans/2026-09-15-catalog-in-database-corpus-store.md`
- **Design and operation:** `docs/CORPUS_STORE.md`; decision D-017.
- **Started:** 2026-09-15. **Target completion:** after plan Step 7 (seed, build, physical
  check, install).
- **Current state:** `migrating` (since `ebedd9d`, 2026-09-16).

## States

| State | What it means for this migration | Commits |
| --- | --- | --- |
| `planned` | the plan is written and confirmed by Cole | `8f8f308` (2026-09-15) |
| `expanding` | Steps 1–3: the shared SQLite migration runner; the corpus store (schema version 1 of `corpus.sqlite3`), `CatalogReference` and `CatalogSnapshot`; `recording-group/v2` and alias-aware readers beside the embedded `recording-group/v1` form. The app still embedded its catalog and minted v1 groups | `33d7e8d` (`refactor:`, the runner); `c901c9d` (`migration:`, schema file `20260916100000_create_corpus_prompt_catalog_store.sql`, together with the whole `corpus_store.py` module, the new contracts and a `recording_index.py` refactor); `790f224` (`feat:`, schema file `20260916110000_create_recording_prompt_alias_projection.sql`, index schema version 4, together with the readers) |
| `migrating` | Step 4: the app loads its catalog from the service (`CatalogCommand` / `CatalogEvent`), settings and configuration are v2 without a catalog, `--expect-catalog-sha256` is removed, every new context mints v2 groups. Step 5: `voice-lab catalog …` and the standard-set importers. Step 7: the real library is seeded and the new build installed | `ebedd9d`, `6b5c874`; Step 7 pending |
| `contracting` | the built-in resources are seeds and historical pins only; no settings generator or runbook writes a catalog into settings or passes `--expect-catalog-sha256`; this manifest is deleted | pending |

The commit prefixes do not separate schema changes from feature work: the index schema file
arrived in a `feat:` commit, and the `migration:` commit also carries the store module. The
history is not rewritten, so find this migration's schema files through this table, not
through `git log --grep '^migration:'`.

Phase dependencies:

- `migrating` required `expanding`: every reader could resolve a v2 group before any process
  minted one.
- Step 7 requires the Step 4 and Step 5 code built and verified (`scripts/verify`), and a
  read-only baseline coverage report taken before the store is seeded.
- `contracting` requires Step 7's coverage parity (34 of 1,886 covered, `changed` = the two
  room tones, exit 0; `finetune corpus --catalog cole-corpus --allow-missing` selects the same
  34 takes as `--catalog pilot-v2`) and Cole's confirmation of the Library and a recorded take.

## `_migration` companion files

None. The app and the service always ship together in one frozen build, so no build ever needs
to route between an old and a new command form, and no dual-write window exists. The
`*_migration*` glob also matches `src/cole_voice_lab/sqlite_migrations.py` and
`tests/test_sqlite_migrations.py`; those are the permanent migration runner and its tests (no
`@migration` tag), not companion files.

The schema files are permanent and stay after completion:

- `migrations/20260916100000_create_corpus_prompt_catalog_store.sql` (the store)
- `migrations/20260916110000_create_recording_prompt_alias_projection.sql` (index schema
  version 4, the prompt-alias projection)

The permanent reader rule (groups that embed their catalog stay readable forever) is recorded in
D-017 and `docs/CORPUS_STORE.md`; it is not part of this migration's cleanup.

## Rollback

The first launch of the new build is a one-way door for the older native build.

- The new recording service opens the recording index when it starts, and that open applies
  index schema version 4 before anything is recorded. Pre-corpus-store code refuses any
  index version other than 0 to 3, so the older native build cannot open the library after
  the new build's first launch.
- No command rebuilds or downgrades the index. `RecordingIndex.quarantine_cache` has no
  production caller.
- The running app (PID 35990, the installed pilot-v2 native build) may keep running until
  the new build is verified. Once the new build has launched, do not relaunch the old
  build. After the new build records a take, its `recording-group/v2` group fails the old
  `RecordingGroup` contract, so the still-running old app refuses it at its next reconcile.
- The fallback after the first launch is the Qt application, which does not open the
  recording index.
- No evidence is lost in any of these cases; the new build reads everything.

## Ordered cleanup steps

1. **Docs and plans (Step 6).** Docs describe settings without a catalog, the service-loaded
   catalog and the removed `--expect-catalog-sha256`; `docs/PROMPT_CATALOG_V2.md` revision 3
   marks every older mention as superseded; the corpus-first pipeline plan's `pilot-v3` step
   becomes a catalog publish. Check: every line that
   `grep -rn -- "--expect-catalog-sha256" docs README.md` finds either says the flag no longer
   exists or is revision-2 history that `docs/PROMPT_CATALOG_V2.md` §A.5 names.
2. **Baseline (Step 7, read-only).** `voice-lab prompts coverage-report --asset-root <library>
   --subject-id cole-self --catalog pilot-v2 --json`.
3. **Seed (Step 7).** `voice-lab catalog init --subject-id cole-self --from-builtin pilot-v2
   --drop-room-tones`, `import-harvard` and `import-arctic` (each with `--from-file`,
   `--fetched-at` and the copy's real `--source-url`), `publish`, then
   `catalog show --version 2 --json` (its `reference.catalog_sha256` must be
   `1ebd48cbc6f6a2b2ea0fe70d0cff8f1df1e91b3bbf2a5be52959ffba141ff90f`; activate nothing on a
   mismatch), then `activate --version 2`. Check: `coverage-report --catalog cole-corpus
   --baseline pilot-v2` and the fine-tune parity above.
4. **Build and install (Step 7).** Build from `cole-voice-lab.native-application-settings/v2`
   settings (`purpose: capture-pilot`, no catalog), run `verify_native_recording_application`
   and `codesign --verify --strict --deep`, install by copytree. The first launch upgrades the
   recording index (see "Rollback"). Check: the Library lists 1,886 prompts with 34 recorded,
   and a new take's `head.json` references the snapshot.
5. **Retire the old settings.** The settings file that embeds a catalog stays only beside the
   copy of the previous build; no new build reads it (a v1 settings file exits 2). That build
   can no longer open this library (see "Rollback").
6. **Confirm no companion files.** `grep -rn "@migration" src tests scripts apps` finds nothing.
7. **Complete.** Delete this manifest in a commit titled
   `migration: complete corpus store -- all migration files removed`.
