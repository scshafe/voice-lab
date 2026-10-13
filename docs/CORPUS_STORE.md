# The corpus store: prompts and catalogs as data

The corpus store holds the prompts Cole records and the catalog versions the app records
against. It is a SQLite database inside the library plus one immutable snapshot file per
published version. `voice-lab catalog …` edits it, and the app loads the active version from
the recording service at launch, so a prompt change is an edit, a publish, an activate and a
relaunch, never a rebuild. New recording groups reference a published version instead of
embedding the whole catalog. Ports: `src/cole_voice_lab/catalog_ports.py`
(`@canonical catalog_ports`); the one implementation: `src/cole_voice_lab/corpus_store_sqlite.py`
(`@canonical corpus_store_sqlite`); the catalog logic every implementation shares:
`src/cole_voice_lab/corpus_store.py` (`@canonical corpus_store`); the one opener:
`src/cole_voice_lab/storage_binding.py` (`@canonical storage_binding`); standard sentence sets:
`src/cole_voice_lab/standard_prompt_sets.py` (`@canonical standard_prompt_sets`);
schema: `migrations/20260916100000_create_corpus_prompt_catalog_store.sql`.
Decision: D-017. Rollout state: `migrations/active/corpus-store.md` (deleted when the rollout
completes).

## Where it lives

Everything is under `<library>/provenance/corpus/`:

| Path | Mode | What it is |
| --- | --- | --- |
| `provenance/corpus/` | 0700 | the store directory; created only by `voice-lab catalog init` (or an editor opened with `create=True`, as test fixtures do) |
| `corpus.sqlite3` | 0600 | the database (WAL journal, schema version 1 in `PRAGMA user_version`) |
| `corpus.sqlite3-wal`, `-shm`, `-journal` | 0600 | SQLite's own sidecars, present only while needed |
| `.corpus.lease` | 0600 | held with a shared `flock` for as long as a process has the store open |
| `.corpus.lock` | 0600 | held with an exclusive `flock` while the store opens and during every transaction |
| `snapshots/` | 0700 | one file per published version |
| `snapshots/<catalog_sha256>.json` | 0600 | a `CatalogSnapshot`, written once and never rewritten |
| `snapshots/.<catalog_sha256>.json.tmp-<hex>` | 0600 | scratch of a publish in progress; removed at the next open if a publish crashed |

Any other entry in `provenance/corpus/` or `snapshots/` makes the store refuse to open
(`CORPUS.STORE_UNSAFE`), so keep backups and notes elsewhere.

No caller names an implementation. The store is opened through the composition root, as a
context manager: `storage_binding.open_catalog_reader(layout)` for a reader and
`storage_binding.open_catalog_editor(layout, create=…)` for an editor. What they return are the
two ports, `CatalogReader` and `CatalogEditor(CatalogReader)` (`catalog_ports`), which own the
records the store returns and every `CORPUS.*` error class. Behind them today is the one
implementation, `corpus_store_sqlite.CorpusStore(layout, create=…, clock=…)`; `create` and
`clock` are its constructor arguments, and only `storage_binding` (and the two native fixture
seeders) constructs it.

| Opener | How | When |
| --- | --- | --- |
| `voice-lab catalog init` | `open_catalog_editor(create=True)` | once, to create and seed the store |
| `catalog list`, `show`, `export` | `open_catalog_reader` | for the length of the command |
| every other `voice-lab catalog` command | `open_catalog_editor` | for the length of the command; `import-harvard` and `import-arctic` check with a reader, close it while they read or fetch the source, and open an editor to import |
| `prompts show`, `prompts coverage-report`, `finetune …` with a published `--catalog` | `open_catalog_reader` | only to resolve the name; closed before any evidence is read |
| the recording service | `open_catalog_reader` | once per process, to pin the active catalog; closed at once |
| the fine-tune worker of a group-scoped recording service (`recording_finetune_jobs`) | `open_catalog_reader` | once per job whose request pins no version, to read the subject's active version; closed before the snapshot file or any evidence is read. The native app's workspace service pins its version in every job request, so its worker never opens the store; a Speak request's worker never opens it either |

Only an editor opened with `create=True` creates `provenance/corpus/` or the database file; a
missing store is otherwise `CORPUS.STORE_MISSING`. Opening a reader is not side-effect free
either: on SQLite every open, with either port, still changes the store directory:

- it takes the lease and the lock, creating `.corpus.lease` and `.corpus.lock` when absent;
- it creates an empty `snapshots/` when absent;
- it deletes a crashed publish's scratch files
  (`snapshots/.<catalog_sha256>.json.tmp-<hex>`);
- it applies schema version 1 to a database that exists but is still empty at version 0 (for
  example one left by a crashed `init`);
- it opens the database in WAL mode, so the WAL files can appear or change.

No opener writes recording evidence.

The store is authoritative. Unlike the recording index (`provenance/recording-library/`), it
is never quarantined, rebuilt or derived from anything else: its rows are the editing history
and its snapshots are evidence. Every structural problem is a hard error.

## Data model

Seven tables; the migration file comments every table and every non-obvious column.

| Table | One row per | Key | Notes |
| --- | --- | --- | --- |
| `corpus_prompts` | prompt | `prompt_id` | insertion ordinal (the default publish order), source (`pilot-v1`, `pilot-v2`, `harvard`, `arctic`, `manual`, `import`), source reference, current revision, `prompt_retired_at` |
| `corpus_prompt_revisions` | revision of a prompt | `prompt_id`, `prompt_revision` | append-only content: text, category, style, instructions, labels, expected seconds, reference-candidate and held-out flags, `prompt_sha256`, `prompt_revision_keeps_recordings` |
| `corpus_prompt_aliases` | earlier digest a revision accepts | `prompt_id`, `prompt_revision`, `accepted_prompt_sha256` | written with a keep-recordings revision; a default revision has none |
| `corpus_catalogs` | catalog | `catalog_id` | suite id, purpose, description; `catalog_retired_at` is reserved (no retire command exists) |
| `corpus_catalog_versions` | published version | `catalog_id`, `catalog_version` | `catalog_sha256` (unique across all catalogs), snapshot path and `catalog_snapshot_sha256` |
| `corpus_catalog_version_prompts` | prompt in a version | `catalog_id`, `catalog_version`, `prompt_id` | the exact revision and its 0-based position |
| `corpus_subjects` | subject | `subject_id` | the active catalog id and version |

Rules the store enforces:

- `prompt_sha256` is `canonical_sha256` of the exact `Prompt` contract, the digest coverage and
  every take already use. Seeding pilot-v2's prompts gives each the built-in's digest.
- Editing appends revision N+1. Only a prompt's current-revision pointer and retired time,
  and a subject's active version, are ever updated in place. No row is ever deleted, and
  every foreign key is `ON DELETE RESTRICT`.
- A live prompt's id is unique, and no two live prompts read the same: their texts may not
  be equal, nor equal once case, punctuation and spacing are ignored
  (`corpus_store.spoken_words`), because one recording would count for both
  (`CORPUS.PROMPT_DUPLICATE`, naming both prompts). Retiring a prompt frees its text for
  another prompt, but its id stays used.
- Every live prompt fits every catalog of the store, because `publish` takes every live
  prompt (see "Every live prompt fits every catalog" below).
- A keep-recordings edit accepts only a typo-class text change unless its caller allows a
  rewording (see "Editing" below).
- Publishing materializes a `prompt-suite/v2` `PromptSuite` (the catalog's suite id and
  purpose; every live prompt in insertion order), validates it with the unchanged contract,
  writes the snapshot, and takes version `max + 1` under the exclusive lock. Two concurrent
  publishers get consecutive versions.
- A version's `catalog_sha256` is `canonical_sha256` of that suite, so it compares directly
  with every built-in digest and every `catalog_sha256` a take already records.
- Publishing content that is already published (in any catalog) is refused
  (`CORPUS.VERSION_UNCHANGED`); the message names that version.
- A published suite must fit one app message: at most `MAX_CATALOG_SUITE_BYTES`
  (1,044,480 canonical bytes, about 3,600 short prompts) and at most 10,000 prompts.

## Contracts

`CatalogReference`, `CatalogSnapshot` and `RecordingGroup` are in
`src/cole_voice_lab/recording_contracts.py`.

**`CatalogReference`** (`schemas/catalog-reference.schema.json`) names one published version:

| Field | Value |
| --- | --- |
| `catalog_id` | the catalog |
| `catalog_version` | the version, 1 or more |
| `catalog_sha256` | canonical digest of the version's `PromptSuite` |
| `snapshot_sha256` | SHA-256 of the exact snapshot file bytes; it pins the aliases too |
| `suite_id`, `purpose` | what the suite declares, so contract checks (purpose against the capture gate) need no file read |
| `snapshot_path` | always `provenance/corpus/snapshots/<catalog_sha256>.json` |

**`CatalogSnapshot`** (`schemas/catalog-snapshot.schema.json`, schema version
`cole-voice-lab.catalog-snapshot/v1`) is the file behind a version. The file is its canonical
JSON followed by one newline.

| Field | Rule |
| --- | --- |
| `suite` | the exact `PromptSuite`; `canonical_sha256(suite)` must equal `catalog_sha256` and the file name |
| `catalog_sha256` | as above |
| `prompt_aliases` | `PromptAlias(prompt_sha256, accepted_prompt_sha256)` pairs, sorted and unique; each `prompt_sha256` is a prompt of the suite; an accepted digest belongs to one prompt only and is never a current digest; at most 100,000 |

`CatalogSnapshot.current_prompt_for(recorded)` is the one in-memory statement of what counts:
the recorded prompt's digest is current or accepted, and the current prompt keeps the
recorded prompt's `keep_recordings_identity` (see "Editing" below).
`CatalogSnapshot.embedded(suite)` is the snapshot a built-in or an embedded catalog implies:
the suite and no aliases.

**Recording groups** exist in two forms. Container contracts (`capture-reservation/v1`,
`recording-take/v1`, `recording-package-map/v1`, responses) keep their versions and nest
either form.

| Form | Carries | Written by | Read by |
| --- | --- | --- | --- |
| `cole-voice-lab.recording-group/v1` | `catalog` (the whole suite) and `catalog_sha256` | every group before the corpus store; now only `continue-legacy-recording` | `CatalogSnapshot.embedded(group.catalog)`, forever; its canonical bytes are unchanged |
| `cole-voice-lab.recording-group/v2` | `catalog_reference` and `catalog_sha256` (equal to the reference's); no catalog | every new app context | `read_catalog_snapshot`, which checks the file against the sealed reference |

`corpus_store.resolve_group_catalog(layout, group)` is the one reader for both forms. It reads
files only and never opens the database, so discovery and the coverage report stay
SQLite-free for group catalogs. A v2 group whose snapshot is missing or altered fails with
`CorpusSnapshotSecurityError`; nothing falls back to another catalog. Each reader instance
(a `RecordingTakeStore`, a `RecordingIndex`) caches the catalogs it resolved, keyed by the
whole sealed reference (`GroupCatalogCache`). It verifies each reference once and then keeps
its verified copy.

The fine-tune contracts gained two fields: `FineTuneCorpus.catalog_reference` (the published
version the corpus was selected from, `null` for a built-in) and
`FineTuneCorpusEntry.recorded_prompt_sha256` (see "Editing").

## The catalog commands

Every command takes `--asset-root` (or `COLE_VOICE_LAB_ASSET_ROOT`). `--catalog-id` defaults to
`cole-corpus`. `list`, `show` and `edit-prompt` print text unless `--json` is given; every other
command prints one JSON document. Every refusal exits 2; a refusal with a code prints
`voice-lab: <CODE>: <message>` on stderr.

| Command | Does | Writes |
| --- | --- | --- |
| `init --subject-id S [--from-builtin B] [--drop-room-tones] [--catalog-id] [--suite-id] [--purpose] [--description]` | creates the store; with a seed, imports its prompts, creates the catalog, publishes version 1 and activates it for S, in one transaction | the store |
| `create <catalog_id> --purpose P [--suite-id] [--description]` | an empty catalog | a catalog row |
| `list [--subject-id S] [--json]` | catalogs, latest versions, prompt counts, S's active version | nothing |
| `show [--catalog-id] [--version N] [--json]` | one version (latest by default): reference, alias count, prompts | nothing |
| `export [--catalog-id] [--version N] --json` | the version's `PromptSuite`, in the format `import-json` reads | nothing |
| `add-prompt <id> [--text T] [--instructions I] [--category] [--style] [--expected-seconds] [--label …] [--reference-candidate] [--holdout]` | revision 1 of a new prompt (source `manual`); a silent prompt has instructions and no text | a prompt |
| `edit-prompt <id> <fields> [--keep-recordings [--allow-rewording]] [--subject-id S] [--expected-revision N] [--dry-run] [--json]` | the next revision, after a preview | a revision |
| `retire-prompt <id>` | leaves the prompt out of every later version | the retired time |
| `import-harvard`, `import-arctic` (`--from-file F --fetched-at T --source-url U` \| `--fetch`) `[--accept-unpinned-source]` | a whole standard set | 720 or 1,132 prompts |
| `import-json <prompt_suite.json>` | every prompt of a suite (source `import`) | prompts |
| `publish [--catalog-id]` | a new version from every live prompt | a version and its snapshot |
| `activate --subject-id S --version N [--catalog-id]` | S records against that version from the next app launch | the subject row |

Defaults: `add-prompt` uses category `neutral`, style "Plain reading, natural pace." and
`expected_seconds = round(0.42 × words + 0.8, 1)`; a prompt without `--text` needs
`--expected-seconds`. `edit-prompt` counts recordings for `--subject-id`, default `cole-self`.
`init` takes the seed's purpose (pilot-v2 gives `capture-pilot`; a `capture-corpus` catalog
would need a pilot approval) and requires `--purpose` without a seed; the suite id defaults to
the catalog id. `init` refuses a catalog id that names a built-in, and refuses a store that
already holds anything; its message names the command that continues from the store's state.

On the PC (D-052, docs/design/RECORDING-V1.md), the same store is the corpus for every
recording client: the owner runs these commands there as the `voice-lab` user with
`--asset-root /data/voice-lab/lab`, the API serves the active version over `/api/v1`, and a
client pins the version it records against (the recording service's relaunch pin is the
Swift app's). `scripts/seed-pc-catalog` runs the sequence below there, with the downloads, the
pin checks and the digest check before activating (RECORDING-V1.md §7 step 1). `init`,
`publish` and `activate` also rebuild the API's read model at once (`derived/corpus/`: the
active version and its library of takes, `corpus_read_model`), so the library follows a newly
active version without waiting for an upload. A take uploaded to the PC is accepted only when
the catalog version it names is the one this store published under that id and number, and
that version counts its prompt; until the store holds that version, the take waits in
`incoming/` (docs/HOME_HOST_INTAKE.md, "Take packages"). Retiring a prompt or rewording it
without `--keep-recordings` stops its takes counting in the library, but never removes them.

Seeding a library, as the rollout's Step 7 does:

```sh
voice-lab catalog init --asset-root <library> --subject-id cole-self --from-builtin pilot-v2 --drop-room-tones
voice-lab catalog import-harvard --asset-root <library> --from-file <harvard.html> \
    --fetched-at 2026-09-16T04:17:51Z --source-url https://www.cs.columbia.edu/~hgs/audio/harvard.html
voice-lab catalog import-arctic --asset-root <library> --from-file <cmuarctic.data> \
    --fetched-at 2026-09-16T04:17:51Z --source-url http://festvox.org/cmu_arctic/cmuarctic.data
voice-lab catalog publish --asset-root <library>
voice-lab catalog show --asset-root <library> --version 2 --json   # check reference.catalog_sha256 (table below)
voice-lab catalog activate --asset-root <library> --subject-id cole-self --version 2
voice-lab catalog list --asset-root <library> --subject-id cole-self
```

Activate only after `reference.catalog_sha256` matches the table below. With other input
copies the digest differs; stop and find out why before activating.

With these inputs the versions are deterministic:

| Version | Prompts | `catalog_sha256` | Expected reading time |
| --- | --- | --- | --- |
| 1 (pilot-v2 without room tones, suite `cole-corpus`) | 34 | `e234266eccb7b8a1c55e7454b32a2a61a99e72a3939bc4850a6951acc25fd7b6` | 282.0 s |
| 2 (plus Harvard, then ARCTIC) | 1,886 | `1ebd48cbc6f6a2b2ea0fe70d0cff8f1df1e91b3bbf2a5be52959ffba141ff90f` | 8,376.0 s |

Version 2 has no aliases; its snapshot file is 399,986 bytes with `snapshot_sha256`
`db5037567f4688c7ebb871b990d9123e06d67b4df067d26105d124cf33cdb452`.
`init --from-builtin pilot-v2 --suite-id cole-pilot-v2` without `--drop-room-tones`
reproduces the built-in digest `0f1f399d…a028` exactly, and
`init --from-builtin physical-test-v1` on a marked root, with the marker's subject, reproduces
`b17cc176…750b`.

Adding a prompt the app will show:

```sh
voice-lab catalog add-prompt warm-05 --asset-root <library> --text "The kettle clicked off just as the rain began." --category warm-narration
voice-lab catalog publish --asset-root <library>
voice-lab catalog activate --asset-root <library> --subject-id cole-self --version 3
# relaunch the app
```

Other examples:

```sh
voice-lab catalog create cole-evening --asset-root <library> --purpose capture-pilot --description "Evening session"
voice-lab catalog show --asset-root <library> --version 2 --json
voice-lab catalog export --asset-root <library> --version 1 --json > cole-corpus-v1.json
voice-lab catalog import-json --asset-root <library> extra-prompts.json
voice-lab catalog retire-prompt warm-05 --asset-root <library>
voice-lab catalog import-harvard --asset-root <library> --fetch
```

### Every live prompt fits every catalog

`publish` takes every live prompt, so one live prompt that a catalog's purpose refuses would
stop every later version of that catalog, and retiring it would use its id up for good. The
store therefore refuses, inside the write transaction:

- a new prompt or an edited revision that the purpose of one of its catalogs cannot hold
  (`CORPUS.PROMPT_UNPUBLISHABLE`, naming each prompt, the catalog and the reason: for example
  a held-out prompt in a store with a capture catalog, or a `test` label outside a
  physical-test catalog); an import is refused whole and names up to 10 prompts;
- a new catalog whose purpose cannot hold every live prompt
  (`CORPUS.CATALOG_UNPUBLISHABLE`); retired prompts do not count.

A prompt that no `prompt-suite/v2` catalog of any purpose could hold, such as a room tone
with text, is refused whatever the catalogs are (`CORPUS.PROMPT_INVALID`). Nothing is written
in any of these cases. A store without catalogs takes any storable prompt; its first catalog
must then fit them. `publish` still validates the whole suite and names the prompts its
purpose refuses, for a store whose rows were written some other way.

### Every editor goes through the store

The rules above, the keep-recordings rules under "Editing" and the reading rule are the
store's, not the command line's: `CatalogEditor.add_prompt`, `import_prompt_suite`,
`initialize_catalog`, `edit_prompt` and `create_catalog` apply them inside their write
transaction, and `edit_prompt(dry_run=True)` applies them to a preview. Any editor (the
`voice-lab catalog` commands today, an in-app editor later) must write through these methods
and never through its own SQL. Only `edit_prompt(keep_recordings=True, allow_rewording=True)`
passes the typo-class bound, and an editor passes it only on the operator's explicit word
(`--allow-rewording`). A standard-set importer builds its prompts with
`standard_prompt_sets.standard_prompt_set`, which applies the strict parse and the pinned
digest (see "Standard sentence sets"). The command line decides only what the store cannot
know: that a new catalog id is not a built-in name, and which flag combinations make sense.

### A second implementation of the ports

`catalog_ports` is the whole contract, and `tests/catalog_conformance.py` is its test: it is
written only against the ports, obtains stores only through `storage_binding`, and any second
implementation must pass it unchanged. What is the SQLite store's own — its lock, lease,
directory guards, migrations and clock — is tested in `tests/test_corpus_store_sqlite.py`, and
the shared catalog logic in `tests/test_corpus_store.py`.

One rule is settled in advance for an implementation that does not keep the library's files: it
returns snapshot bytes, it does not publish them. One Mac-side mirror checks those bytes with
`corpus_store.require_referenced_snapshot` and writes them under `provenance/corpus/snapshots`
while holding `.corpus.lock`, so every reader still finds a verified file at the path each
reference names and nothing reads a snapshot it has not checked. That mirror belongs to the plan
that adds such an implementation; no such implementation exists today.

## `--catalog id[@version]`

`prompts show <name>`, `prompts coverage-report --catalog/--baseline` and
`finetune corpus|dataset|run --catalog` resolve a name in this order:

1. An exact built-in name wins and never touches the library: `pilot-v1`, `evaluation-v1`,
   `pilot-v2`, `physical-test-v1` (`prompts show` also accepts `pilot` and `evaluation`).
2. Otherwise `<catalog_id>` is that catalog's latest published version, and
   `<catalog_id>@<N>` is version N. N is ASCII digits without a leading zero, at most
   2^63 − 1; `--version` and `--expected-revision` take exactly the same spelling (`7`,
   never `07`), and argparse refuses anything else.
3. The name is resolved in the `--asset-root` library's store (`resolve_reference`, then
   `materialize`), and the store is closed before any evidence is read.

An unknown name is `not a released built-in prompt suite or a published catalog: <name>
(<detail>); built-in names: …`, with code `CORPUS.CATALOG_UNKNOWN`, `CORPUS.VERSION_UNKNOWN`,
or `CORPUS.STORE_MISSING` (no library named, or no store), exit 2. A published name makes the
coverage report print `catalog <id>@<version> <sha>` and add `catalog_id`,
`catalog_version`, `baseline_catalog_id` and `baseline_catalog_version` to its JSON; the
fine-tune summaries add `catalog_reference`.

## Editing, coverage and the fine-tune corpus

An edit changes nothing the app shows until a version containing it is published, activated
and the app relaunched. `coverage-report --catalog cole-corpus` (latest version) shows its
effect as soon as it is published.

**A default edit invalidates.** The new revision has a new `prompt_sha256` and accepts no
earlier digest. In every version that contains it, earlier recordings of the prompt stop
counting: the Library shows it Missing, Save & Next and Continue Missing reach it, and the
fine-tune corpus lists it as missing until it is recorded again. The old takes stay on disk
unchanged and still count in every older version. Opening one for review under the new
version is refused as `RECORDING.STALE_REVISION`, for native and old-app takes alike.

**`--keep-recordings` keeps.** The new revision accepts the previous revision's digest and
everything the previous revision accepted. The chain breaks at the first revision made
without the flag. Earlier recordings then count for the current prompt in every reader:
Library coverage, history counts, Save & Next, selection and review, the coverage report
(the prompt is listed as changed, not lost, and the exit status stays 0) and the fine-tune
corpus. The corrected text is what the app shows and what training uses.

A kept recording counts only if the prompt keeps its identity
(`recording_contracts.keep_recordings_identity`): the same `prompt_id`, the same held-out flag,
and the same behaviour (spoken, or the same silent category). The store refuses a
keep-recordings edit that changes any of these (`CORPUS.PROMPT_INVALID`); make such a change as
a default edit. The reason: held-out evaluation audio must never reach training, and silence
must never cover speech. The index and the snapshot apply the same function, so an alias that
breaks the rule never counts even if it were written.

The store adds one more limit, because kept audio trains with the new text: a keep-recordings
edit accepts only a typo-class text change, measured on the words read aloud after ignoring
case, punctuation and spacing: at most 2 word edits and at most 3 character edits
(`corpus_store.text_change`). A real rewording is refused (`CORPUS.REWORDING_REFUSED`) unless
the caller passes `allow_rewording=True` (`--allow-rewording`), for recordings that already
say the new text. The identity rule is checked first, and silent prompts have no text to
compare. The bound is a guard, not a proof: a same-length three-letter substitution passes
it, which is why the preview shows both texts.

**The preview.** `edit-prompt` runs every store check in a rolled-back transaction first, then
prints, before writing: the old and new text with the kind of change, and how many recordings
of the subject count for the prompt now and whether the edit keeps or invalidates them
(records whose prompt cannot be told are listed separately). The recording count reads files
only. `--dry-run` stops there. With `--json` the preview goes to stderr and stdout carries one
document after the write. The agent flow is:

```sh
voice-lab catalog edit-prompt harvard-01-01 --asset-root <library> \
    --text "The birch canoe slid on the smooth plank." --keep-recordings --dry-run --json
# read the preview and the "previous".revision, then write exactly over that revision
voice-lab catalog edit-prompt harvard-01-01 --asset-root <library> \
    --text "The birch canoe slid on the smooth plank." --keep-recordings --expected-revision 1
```

A write whose prompt moved past `--expected-revision` is refused (`CORPUS.PROMPT_CHANGED`).

**Reverting.** A keep-recordings edit that returns to an earlier text is allowed. If the
resulting content equals an already published version, `publish` refuses with
`CORPUS.VERSION_UNCHANGED`, names that version, and names the prompts whose accepted earlier
recordings differ from it. Such recordings count again only once a version with other content
is published, for example after another prompt changes.

**The fine-tune corpus** (`finetune.select_finetune_corpus`) picks, per spoken prompt, the
latest retained take across the current digest and every accepted one. The entry's `prompt` is
the current prompt, so `metadata.csv` carries the current text; `prompt_sha256` is the current
digest; `recorded_prompt_sha256` is the digest the audio was recorded against (equal unless an
alias matched). Held-out and silent recordings never enter, takes from held-out groups are
skipped, and a held-out-evaluation catalog is refused (`FINETUNE.CATALOG_HELD_OUT`). A catalog
with unrecorded prompts, such as `cole-corpus` with the standard sets, needs `--allow-missing`.
See [FINETUNE.md](FINETUNE.md).

**Retiring** leaves the prompt out of later versions. Its recordings stay on disk and count in
the versions that still contain it.

## How the app gets its catalog

The app's configuration carries no catalog
([NATIVE_RECORDING_APPLICATION.md](NATIVE_RECORDING_APPLICATION.md)). At launch:

1. The app and the service complete their handshake.
2. The app sends `CatalogCommand` (30 s deadline).
3. On the first catalog request (or open-context, or pre-context snapshot) the service opens
   the store, reads the subject's active version (`active_catalog`), reads its snapshot with
   aliases (`materialize`), closes the store and pins the result for the life of the process
   (`RecordingWorkspaceBackend.pinned_catalog`). A failure pins nothing, so a later request
   resolves again.
4. The service checks the pinned suite's purpose against the root (`require_catalog_root`) and
   answers with `CatalogEvent`: the exact `PromptSuite` and its `CatalogReference`. The event is
   valid only if the suite hashes to the reference and repeats its suite id and purpose.
5. The app builds its prompt list and Library from the event and takes `catalogSHA256` from the
   reference; Swift never computes the digest.
6. The app requests the durable snapshot, then sends `OpenContextCommand` with
   `catalog_reference`. The service opens a `recording-group/v2` context on the pinned version.

The pre-context snapshot names the pinned catalog's digest. A workspace service
(`--native-workspace`) therefore refuses `--catalog-sha256` as a usage error (argparse, exit
2); only group and unprepared services take a launch digest.

Because the catalog is pinned per process, a `catalog activate` while the app runs takes effect
at the next launch. Fine-tune model follows the same pin: each job records the version the
service pinned and trains on that snapshot, so jobs and the Library move to a newly
activated version together at the next launch
([NATIVE_RECORDING_UX.md](NATIVE_RECORDING_UX.md) §16, D-018). The service does not keep
the store open, so the CLI can edit it while the app runs. Switching catalogs, adding
prompts and editing prompts are all `publish` + `activate` + relaunch.

The service refuses:

| Request | Refusal |
| --- | --- |
| open-context with an embedded `catalog` | `RECORDING.INVALID_MESSAGE`; the embedded form is accepted only inside `continue-legacy-recording` |
| open-context naming another reference than the pinned one | `RECORDING.STALE_REVISION` ("request the catalog again") |
| an active suite larger than `MAX_CATALOG_SUITE_BYTES` | `RECORDING.GATE_NOT_READY`, naming the version, bytes and prompt count |
| a catalog whose purpose the root does not allow | `RECORDING.GATE_NOT_READY` (the same guard text as open-context) |

The app shows a refused catalog's message verbatim as its status and context reason. The header
reads "Catalog unavailable", there is no Library, and Record is unavailable; no snapshot or
open-context is sent. A build configured with a pilot approval refuses a delivered catalog
that is not `capture-corpus` (`RECORDING.GATE_NOT_READY`, naming both purposes).

`continue-legacy-recording` is the one exception: an old-app session is bound to its frozen
`prompt-suite/v1` catalog, which the store (v2 suites only) can never publish, so it still opens
a `recording-group/v1` group with that embedded catalog.

## Failures

Store codes (`CorpusStoreCode`). The CLI prints them as `voice-lab: CODE: message`.

| Code | Raised when | What to do |
| --- | --- | --- |
| `CORPUS.STORE_MISSING` | no `provenance/corpus/corpus.sqlite3` (or no library named for a published `--catalog`) | `voice-lab catalog init --subject-id S --from-builtin pilot-v2 [--drop-room-tones]`, then relaunch the app |
| `CORPUS.STORE_UNAVAILABLE` | the database cannot be opened or migrated, fails `quick_check` or `foreign_key_check`, has an unsupported schema version, holds a row that no longer validates, or a query fails | do not edit the database; restore `provenance/corpus/` from backup. An unsupported version means a newer build wrote it: use that build |
| `CORPUS.STORE_UNSAFE` | a store directory or file is not owned by you, has group or other permission bits, is a symlink or a hard-linked file, was replaced while open, or an unexpected entry exists | fix ownership and modes, move the unexpected entry out of `provenance/corpus/`; investigate a replacement before continuing |
| `CORPUS.PROMPT_DUPLICATE` | the id exists, a live prompt has the same text or text that reads the same (both prompts are named), an id is listed twice, or the standard set is already imported | choose another id or edit/retire the other prompt; a repeated import needs nothing |
| `CORPUS.PROMPT_UNKNOWN` | no such prompt | check the id with `catalog show` (published prompts only) or in the output of the `add-prompt` or import command that created it; no command lists unpublished prompt ids |
| `CORPUS.PROMPT_RETIRED` | editing, publishing or retiring a retired prompt | add a new prompt with another id |
| `CORPUS.PROMPT_INVALID` | the prompt fails the `Prompt` contract, carries control or format characters, fits no v2 catalog of any purpose, or a keep-recordings edit changes its identity | fix the fields; make an identity change as a default edit |
| `CORPUS.PROMPT_UNCHANGED` | the edit changes nothing | nothing |
| `CORPUS.PROMPT_CHANGED` | the prompt is no longer at `--expected-revision` | preview again and write with the new revision |
| `CORPUS.PROMPT_UNPUBLISHABLE` | a new or edited prompt that the purpose of one of the store's catalogs cannot hold | fix the fields, or keep such prompts in a store whose catalogs hold them |
| `CORPUS.REWORDING_REFUSED` | a keep-recordings edit changes more than a typo-class amount of text | edit without `--keep-recordings` (the prompt is recorded again), or add `--allow-rewording` if the recordings already say the new text |
| `CORPUS.SOURCE_INVALID` | a standard-set file does not parse to exactly 720 or 1,132 sentences, is larger than 4 MiB, or does not have the pinned SHA-256 of the published list (checked last); or the prompt source is unknown | check the file is the published list; for a list that changed on purpose, check it and add `--accept-unpinned-source`; nothing was imported |
| `CORPUS.SOURCE_UNAVAILABLE` | `--fetch` failed or did not answer 200 | retry, or download a copy and use `--from-file` |
| `CORPUS.SOURCE_REFERENCE_INVALID` | the source URL is not a plain http(s) URL; or, from Python callers only (`standard_prompt_set`, the store API), a fetch time without an offset, a malformed source reference, or a standard-set prompt without one | correct `--source-url`. The CLI refuses a `--fetched-at` without an offset itself, as a usage error with no code (exit 2) |
| `CORPUS.CATALOG_DUPLICATE` | the catalog id exists, or `init` found a store that already holds prompts or catalogs | follow the next step the message names |
| `CORPUS.CATALOG_UNKNOWN` | no such catalog (also an unknown `--catalog` name) | `catalog list` |
| `CORPUS.CATALOG_INVALID` | a bad catalog id, suite id, purpose or description; a publish whose suite the contract refuses (the message names the prompts and why); more than 10,000 prompts; over the one-message byte budget | retire or edit the named prompts, or publish fewer |
| `CORPUS.CATALOG_UNPUBLISHABLE` | a new catalog's purpose cannot hold every live prompt | choose another purpose, or retire or edit the named prompts first |
| `CORPUS.VERSION_UNKNOWN` | no such version, nothing published yet, or a malformed version | `catalog show`; publish first |
| `CORPUS.VERSION_UNCHANGED` | the live prompts equal an already published version | nothing to publish; activate the named version if needed |
| `CORPUS.SUBJECT_INVALID` | the subject id is not a stable id | use a lowercase stable id |
| `CORPUS.NO_ACTIVE_CATALOG` | the subject has no active version | `voice-lab catalog activate --subject-id S --version N` (or `init`), then relaunch |
| `CORPUS.SNAPSHOT_MISSING` | a snapshot a reference names is gone | restore `provenance/corpus/snapshots/` from backup |
| `CORPUS.SNAPSHOT_MISMATCH` | a snapshot's bytes differ from its reference, it is not canonical, not a private regular file, disagrees with the version's rows, or a reference disagrees with the stored version | restore the file from backup; never edit a snapshot |
| `CORPUS.EMPTY_CATALOG` | publishing with no live prompts | add prompts |

`CorpusStoreSecurityError` (`STORE_UNSAFE`) and `CorpusSnapshotSecurityError` (`SNAPSHOT_*`) are
`SecurityError` subclasses, so every reader that fails closed on a security error does so here.
The coverage report and the fine-tune selection pass them through unwrapped (exit 2).

What the app receives (the wire `ErrorCode` vocabulary):

| Store failure | Wire code | Message |
| --- | --- | --- |
| `STORE_MISSING` | `CORPUS.STORE_MISSING` | the store's, naming `voice-lab catalog init` |
| `NO_ACTIVE_CATALOG` | `CORPUS.NO_ACTIVE_CATALOG` | the store's, naming `catalog init` and `catalog activate` |
| `SNAPSHOT_MISSING`, `SNAPSHOT_MISMATCH` | `CORPUS.SNAPSHOT_MISMATCH` | fixed text saying "missing" or "altered or unsafe", without file content |
| any other store failure | `RECORDING.GATE_NOT_READY` | "the corpus store cannot serve the active catalog: …" (at most 1,024 characters) |

The wire `ErrorCode` vocabulary holds exactly these three `CORPUS` codes. Every other store
code, `CORPUS.CATALOG_UNKNOWN` included, is printed by the command line only and never
reaches the app.

Command codes (`cli.CatalogCommandError`):

| Code | Raised when |
| --- | --- |
| `CATALOG_CLI.USAGE_INVALID` | a flag combination the command refuses (`init` without a seed or `--purpose`, `--drop-room-tones` without a seed, `--allow-rewording` without `--keep-recordings`, `--from-file` without `--fetched-at` and `--source-url`, `--fetch` with either, an edit with no field, a prompt without text or expected seconds, a physical-test catalog without a `cole-physical-test-` suite id) |
| `CATALOG_CLI.CATALOG_ID_RESERVED` | a new catalog named like a built-in |

Every other refusal of a catalog command is the store's (`CORPUS.*`), so any other editor
gets the same one.

## Integrity model

What the store checks:

- **Ownership and modes.** Directories are created 0700 and files 0600; every entry must be
  owned by the current user with no group or other permission bits, and every file must be
  regular with one link, never reached through a symlink (`canonical.require_private_metadata`).
  The root, `provenance`, `corpus` and `snapshots` directories stay bound by descriptor while
  the store is open, and every transaction rechecks their identities, the member list and each
  member's owner and mode, before `BEGIN` and before `COMMIT`.
- **Locks.** `.corpus.lock` serializes opening, migration and every transaction across
  processes; `.corpus.lease` is held shared while a store is open.
- **Structure.** `PRAGMA quick_check` and `foreign_key_check` run at every open; foreign keys
  are enforced and restrict deletion; `trusted_schema` is off; writes are `synchronous=FULL`.
- **Content-addressed snapshots.** A snapshot is written to a temporary name and linked into
  place, so an existing name is never overwritten; if the name exists, its bytes must equal
  what the publish would write (`CORPUS.SNAPSHOT_MISMATCH` otherwise).
- **Whole-file digest.** Every reader checks the file's SHA-256 against the reference's
  `snapshot_sha256` before parsing, then checks canonical form, `catalog_sha256`, suite id and
  purpose. `catalog_sha256` alone binds only the suite; the whole-file digest also binds the
  aliases, which decide which recordings count.
- **Row cross-check.** `CorpusStore.materialize` also requires the reference to equal the stored
  version and the file's prompt digests and aliases to equal the version's revision rows.
- **Sealed references.** Every v2 group seals its full reference, so a later change to the
  snapshot file an existing take depends on is detected by the next reader that opens it (a
  relaunched service, a new discovery worker, a new index). A reader that already verified
  the file keeps its verified copy and does not see the change.

What it does not defend against:

- **A writer with your account.** Anyone who can write as you can edit the database and write a
  matching new snapshot; the store alone cannot tell that from a genuine publish. Existing
  groups still detect changes to the snapshots they sealed, and the identity rule still keeps
  an alias from moving held-out or silent audio into training.
- **Deletion.** Removing `provenance/corpus/` or a snapshot is detected (`STORE_MISSING`,
  `SNAPSHOT_MISSING`), not prevented. Only a backup restores it.
- **Confidentiality.** Prompt text is stored in plain text; the file modes are the only
  protection.
- **Source provenance.** For `--from-file` imports, the recorded URL and time are the
  operator's statements, unchecked. Neither a saved copy nor a `--fetch` (ARCTIC is served
  over plain http) is authenticated by its transfer; what binds the content is the pinned
  SHA-256 of the published list, which every import must match unless the operator passes
  `--accept-unpinned-source`. Such an import is recorded in every prompt's source reference
  (`"unpinned_source_accepted": true`), and its content is only as good as the operator's
  check.
- **Rewording under `--allow-rewording`.** The operator vouches that the recordings say the new
  text.

## Backup and restore

Back up the database and `snapshots/` together, as one copy of `provenance/corpus/`. A library
backup that copies all of `provenance/` already does this.

They depend on each other. Without the database the app has no active catalog and nothing can
be published or edited. Without the snapshots every version is unreadable
(`CORPUS.SNAPSHOT_MISSING`), and so is every v2 group that references one, including the
history of its takes.

Copy the files while nothing has the store open. The app opens it only for a moment at launch; a
`voice-lab catalog` command holds it for its whole run (the importers release it while they
read or fetch the source); a published `--catalog` name holds it only while the name is
resolved. If a `corpus.sqlite3-wal` file is present, copy it with the
database.

The library vault copies the database another way and needs no quiet moment
([LIBRARY_VAULT.md](LIBRARY_VAULT.md), D-020). The Mac keeps the working database;
[RUNBOOK.md](RUNBOOK.md#9-library-vault) covers restoring its encrypted copy onto a
new Mac. The copier opens the database read-only through a `mode=ro`
URI, takes SQLite's one-step online backup into a private scratch file under
`tmp/.vault-<random>/` (0700 directory, 0600 file), sets the copy's journal mode to `DELETE`,
checks it with `PRAGMA quick_check`, and copies those bytes under the database's own path
(`library_replication.backup_database`). It never opens the store through
`open_catalog_reader`, never opens the database file outside SQLite (an `os.open` would drop
the calling process's own database locks), and never takes `.corpus.lock` or any other lock a
lab writer takes, so a copy runs while the app and the command line are using the store and
still records the last committed state. It lists `snapshots/` after the backup,
so every snapshot the copied database names is present; a snapshot file still hard-linked to a
publish's scratch name is left for the next copy.

A restored store must again be owned by you, with 0700 directories and 0600 files, no symlinks,
no hard links and no extra entries, or it refuses to open (`CORPUS.STORE_UNSAFE`). The store is
never rebuilt from takes or built-ins: a lost store without a backup means seeding a new one,
and v2 groups that referenced the lost snapshots stay unreadable until those exact files return.

## Standard sentence sets

| | Harvard sentences | CMU ARCTIC |
| --- | --- | --- |
| Command | `catalog import-harvard` | `catalog import-arctic` |
| Prompts | 720, in 72 lists of 10 | 1,132 (593 `a` and 539 `b` utterances) |
| Ids | `harvard-LL-NN` (`harvard-01-01` … `harvard-72-10`) | the utterance id with `_` → `-` (`arctic-a0001` … `arctic-b0539`) |
| Source | https://www.cs.columbia.edu/~hgs/audio/harvard.html | http://festvox.org/cmu_arctic/cmuarctic.data (served over plain http only) |
| Pinned SHA-256 (`STANDARD_SET_SHA256`, saved 2026-09-16) | `f8c981aed97020642fd2423de787b3ccd2a7233f32fe07ebd26d2279c195cb75` | `60e3d9a4dc33732c9100baadd747312bdc1a200fc891766507397289753a25c7` |
| Expected reading time | 2,991.4 s | 5,102.6 s |

**Harvard.** IEEE Recommended Practice for Speech Quality Measurements, IEEE Std 297-1969
(DOI 10.1109/IEEESTD.1969.7405210; IEEE Transactions on Audio and Electroacoustics 17(3),
225–246, 1969, DOI 10.1109/TAU.1969.1162058), Appendix C, "1965 Revised List of Phonetically
Balanced Sentences"; the list is reproduced from the source page above. This citation is the
stored license text (`standard_prompt_sets.HARVARD_LICENSE`).

**CMU ARCTIC.** Carnegie Mellon University, Copyright (c) 2003. The notice permits use, copying,
modification and licensing for any purpose without fee, provided the copyright notice is kept,
modifications are marked, and the authors' names are not deleted; it disclaims all warranties.
The verbatim COPYING text is stored as `standard_prompt_sets.ARCTIC_LICENSE` (2,285 characters)
and in every imported prompt's source reference; the test fixture copy is
`tests/fixtures/standard_sets/arctic-COPYING.txt`.

How an import works:

- Parsing is strict. Harvard: the page splits at each `<h2>List N</h2>`; lists must be numbered
  in order and hold exactly 10 `<li>` items, and no list item may come before the first list;
  whitespace is collapsed and HTML entities decoded; markup inside a sentence is refused.
  ARCTIC: every line must be `( arctic_a0001 "…" )`; whitespace inside the quotes is collapsed
  (in the published list this only drops the trailing space of `arctic-a0028`); quotes and
  backslashes inside a sentence are refused. Two sentences that read the same
  (`corpus_store.spoken_words`: case, punctuation and spacing ignored) are refused. Any count
  other than 720 or 1,132 imports nothing (`CORPUS.SOURCE_INVALID`).
- The file must be the published list: its SHA-256 must equal the pinned digest in the
  table above (`CORPUS.SOURCE_INVALID`, naming both digests, checked after parsing). With
  `--accept-unpinned-source` another copy is imported, and every prompt's source reference
  records `"unpinned_source_accepted": true`. The command's JSON reports `sha256`,
  `pinned_sha256` and `unpinned_source_accepted`.
- No sentence may read the same as a live prompt of the store (`CORPUS.PROMPT_DUPLICATE`,
  naming the sentence's id and the live prompt's).
- Every sentence becomes a `neutral` prompt with style "Plain reading, natural pace." and
  `expected_seconds = round(0.42 × words + 0.8, 1)`, source `harvard` or `arctic`.
- Each prompt stores `prompt_source_reference`: canonical JSON
  `{"url", "fetched_at", "sha256", "license"}`, plus `"unpinned_source_accepted": true`
  only for an accepted unpinned copy (the key is omitted otherwise, so earlier references
  keep their bytes).
- `--from-file` needs `--fetched-at` (ISO 8601 with an offset; argparse refuses one without)
  and `--source-url`. The URL is recorded as given. The time is recorded as the same instant
  in UTC: `2026-09-16T06:17:51+02:00` is stored as `2026-09-16T04:17:51Z` and printed as
  `2026-09-16T04:17:51+00:00`.
- `--fetch` downloads (30 s timeout, 4 MiB limit) and records the time it fetched and the URL
  the response came from; it is the only network access and is never implied.
- The store must exist. If it already holds the set's first sentence, the import is refused
  before anything is read or fetched. The import is one transaction: the whole set or nothing
  (every catalog must hold every sentence, see "Every live prompt fits every catalog").
