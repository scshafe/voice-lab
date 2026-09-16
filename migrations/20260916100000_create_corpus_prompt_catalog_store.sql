-- Creates the authoritative corpus prompt catalog store: prompts with append-only
-- revisions, the earlier digests a keep-recordings revision still accepts, catalogs,
-- their published hash-pinned versions (each backed by an immutable snapshot file under
-- provenance/corpus/snapshots) and each subject's active catalog version.
-- Why: prompts and catalogs are edited at runtime and published as versions the app
-- loads from the service, so a prompt change is an edit plus a relaunch, never a rebuild;
-- takes reference a published version by id, version and digest instead of embedding it.
-- Depends on: nothing; this is schema version 1 of provenance/corpus/corpus.sqlite3.
-- No maintenance window: applied while the store holds its exclusive open lock.
-- No ON DELETE cascades anywhere: nothing cascades and rows are never deleted. Every foreign
-- key states ON DELETE RESTRICT, so deleting a referenced prompt, revision, catalog or
-- version is refused at once. corpus_prompts and corpus_prompt_revisions reference each
-- other, so a Down on a populated store needs PRAGMA foreign_keys = OFF.
-- Ordinals: every *_ordinal column is 0-based, like the recording index projection.
-- IRREVERSIBLE: the Down step drops published catalogs and prompt revisions; snapshot files under provenance/corpus/snapshots stay on disk
-- +migrate Up
-- One prompt per id. Content lives in corpus_prompt_revisions; this row carries identity,
-- provenance and lifecycle only.
CREATE TABLE IF NOT EXISTS corpus_prompts (
    prompt_id TEXT PRIMARY KEY NOT NULL,
    -- 0-based publish order for a version built from every live prompt; assigned at insertion.
    prompt_insertion_ordinal INTEGER NOT NULL UNIQUE CHECK (prompt_insertion_ordinal >= 0),
    -- ISO-8601 UTC with an explicit offset, as every *_at column in this schema.
    prompt_created_at TEXT NOT NULL,
    -- NULL while live; a retired prompt leaves new versions but stays in old snapshots.
    prompt_retired_at TEXT,
    prompt_source TEXT NOT NULL CHECK (prompt_source IN ('pilot-v1','pilot-v2','harvard','arctic','manual','import')),
    -- JSON object {"url","fetched_at","sha256","license"} for imported sets; NULL otherwise.
    prompt_source_reference TEXT,
    -- The revision readers materialize; every earlier revision stays for history and aliases.
    prompt_current_revision INTEGER NOT NULL CHECK (prompt_current_revision >= 1),
    -- Deferred because the prompt row is written before its first revision inside one
    -- transaction; a dangling current revision is refused at COMMIT and reported by
    -- PRAGMA foreign_key_check when written with foreign keys off. RESTRICT is not
    -- deferred: deleting the current revision is refused immediately.
    FOREIGN KEY (prompt_id, prompt_current_revision) REFERENCES corpus_prompt_revisions(prompt_id, prompt_revision) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
-- Append-only content of one prompt revision. Editing inserts the next revision.
CREATE TABLE IF NOT EXISTS corpus_prompt_revisions (
    prompt_id TEXT NOT NULL REFERENCES corpus_prompts(prompt_id) ON DELETE RESTRICT,
    prompt_revision INTEGER NOT NULL CHECK (prompt_revision >= 1),
    -- canonical_sha256(Prompt) of this revision: the same digest coverage keys takes on.
    prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
    -- NULL only for silent categories (room-tone, procedure), which carry instructions.
    prompt_text TEXT,
    prompt_category TEXT NOT NULL CHECK (prompt_category IN ('room-tone','neutral','conversational','warm-narration','technical','numbers','expressive','reference','procedure')),
    prompt_style TEXT NOT NULL,
    prompt_instructions TEXT,
    -- Canonical JSON array of label strings; '[]' when the prompt has none.
    prompt_labels_json TEXT NOT NULL,
    prompt_expected_seconds REAL NOT NULL CHECK (prompt_expected_seconds > 0 AND prompt_expected_seconds <= 120),
    prompt_reference_candidate INTEGER NOT NULL CHECK (prompt_reference_candidate IN (0,1)),
    prompt_holdout INTEGER NOT NULL CHECK (prompt_holdout IN (0,1)),
    -- 1 when takes of the previous revision (and everything it accepted) still count.
    prompt_revision_keeps_recordings INTEGER NOT NULL CHECK (prompt_revision_keeps_recordings IN (0,1)),
    prompt_revision_created_at TEXT NOT NULL,
    PRIMARY KEY (prompt_id, prompt_revision),
    CHECK (prompt_text IS NOT NULL OR prompt_category IN ('room-tone','procedure')),
    CHECK (prompt_category != 'procedure' OR (prompt_text IS NULL AND prompt_instructions IS NOT NULL)),
    CHECK (prompt_revision > 1 OR prompt_revision_keeps_recordings = 0)
);
-- The earlier digests a keep-recordings revision accepts, materialized when the revision is
-- written so snapshot readers never walk the chain. A revision without the flag has no rows.
CREATE TABLE IF NOT EXISTS corpus_prompt_aliases (
    prompt_id TEXT NOT NULL,
    prompt_revision INTEGER NOT NULL,
    accepted_prompt_sha256 TEXT NOT NULL CHECK (length(accepted_prompt_sha256) = 64),
    PRIMARY KEY (prompt_id, prompt_revision, accepted_prompt_sha256),
    FOREIGN KEY (prompt_id, prompt_revision) REFERENCES corpus_prompt_revisions(prompt_id, prompt_revision) ON DELETE RESTRICT
);
-- A named, versioned selection of prompts. The suite id and purpose are what every
-- published version materializes into a PromptSuite.
CREATE TABLE IF NOT EXISTS corpus_catalogs (
    catalog_id TEXT PRIMARY KEY NOT NULL,
    -- PromptSuite.suite_id of every version; defaults to catalog_id at creation.
    catalog_suite_id TEXT NOT NULL,
    catalog_purpose TEXT NOT NULL CHECK (catalog_purpose IN ('capture-pilot','capture-corpus','held-out-evaluation','physical-test')),
    catalog_created_at TEXT NOT NULL,
    -- Reserved for a later retire operation; NULL while the catalog accepts new versions.
    catalog_retired_at TEXT,
    catalog_description TEXT NOT NULL
);
-- One published, immutable version. The digest is canonical_sha256 of the materialized
-- PromptSuite, so every existing catalog_sha256 field keeps its meaning.
CREATE TABLE IF NOT EXISTS corpus_catalog_versions (
    catalog_id TEXT NOT NULL REFERENCES corpus_catalogs(catalog_id) ON DELETE RESTRICT,
    catalog_version INTEGER NOT NULL CHECK (catalog_version >= 1),
    catalog_sha256 TEXT NOT NULL UNIQUE CHECK (length(catalog_sha256) = 64),
    -- Always provenance/corpus/snapshots/<catalog_sha256>.json, written once, never rewritten.
    catalog_snapshot_path TEXT NOT NULL,
    -- sha256 of the exact snapshot file bytes (canonical CatalogSnapshot JSON plus newline).
    -- catalog_sha256 binds only the suite half of the file; this binds the alias half too.
    -- Lowercase hex only, as CatalogReference.snapshot_sha256 requires.
    catalog_snapshot_sha256 TEXT NOT NULL CHECK (length(catalog_snapshot_sha256) = 64 AND catalog_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'),
    catalog_version_created_at TEXT NOT NULL,
    PRIMARY KEY (catalog_id, catalog_version),
    CHECK (catalog_snapshot_path = 'provenance/corpus/snapshots/' || catalog_sha256 || '.json')
);
-- The exact prompt revisions a version contains, in suite order.
CREATE TABLE IF NOT EXISTS corpus_catalog_version_prompts (
    catalog_id TEXT NOT NULL,
    catalog_version INTEGER NOT NULL,
    prompt_id TEXT NOT NULL,
    prompt_revision INTEGER NOT NULL,
    -- 0-based position of the prompt in the materialized PromptSuite.
    catalog_prompt_ordinal INTEGER NOT NULL CHECK (catalog_prompt_ordinal >= 0),
    PRIMARY KEY (catalog_id, catalog_version, prompt_id),
    UNIQUE (catalog_id, catalog_version, catalog_prompt_ordinal),
    FOREIGN KEY (catalog_id, catalog_version) REFERENCES corpus_catalog_versions(catalog_id, catalog_version) ON DELETE RESTRICT,
    FOREIGN KEY (prompt_id, prompt_revision) REFERENCES corpus_prompt_revisions(prompt_id, prompt_revision) ON DELETE RESTRICT
);
-- Which published version a subject records against; the service reads it at open-context.
CREATE TABLE IF NOT EXISTS corpus_subjects (
    subject_id TEXT PRIMARY KEY NOT NULL,
    subject_active_catalog_id TEXT NOT NULL,
    subject_active_catalog_version INTEGER NOT NULL CHECK (subject_active_catalog_version >= 1),
    FOREIGN KEY (subject_active_catalog_id, subject_active_catalog_version) REFERENCES corpus_catalog_versions(catalog_id, catalog_version) ON DELETE RESTRICT
);
-- Serves the live-prompt listing and the default publish order:
-- WHERE prompt_retired_at IS NULL ORDER BY prompt_insertion_ordinal.
CREATE INDEX IF NOT EXISTS corpus_prompts_live_order ON corpus_prompts(prompt_retired_at, prompt_insertion_ordinal);
PRAGMA user_version = 1;
-- +migrate Down
DROP INDEX IF EXISTS corpus_prompts_live_order;
DROP TABLE IF EXISTS corpus_subjects;
DROP TABLE IF EXISTS corpus_catalog_version_prompts;
DROP TABLE IF EXISTS corpus_catalog_versions;
DROP TABLE IF EXISTS corpus_catalogs;
DROP TABLE IF EXISTS corpus_prompt_aliases;
DROP TABLE IF EXISTS corpus_prompt_revisions;
DROP TABLE IF EXISTS corpus_prompts;
PRAGMA user_version = 0;
