-- Adds the disposable prompt-alias projection behind alias-aware coverage.
-- What: each registered catalog's alias set (the earlier prompt digests a keep-recordings
-- revision still accepts) as one canonical root row plus one row per accepted digest.
-- Why: corpus-store catalog versions accept earlier digests, so Save & Next, history counts
-- and prompt summaries count a retained take recorded against an accepted digest for its
-- current prompt, and legacy rows on those digests stay published, without reading
-- snapshot files. Embedded (recording-group/v1) catalogs accept nothing.
-- Depends on: recording index schema version 3 (recording_catalogs,
-- recording_catalog_prompts, recording_prompts from version 1).
-- No maintenance window: applied while opening this private cache exclusively.
-- Expand-only. Every catalog registered before this version came from an embedded catalog,
-- so the Up step records an empty alias set for each of them.
-- Reversible: Down drops only rebuildable projection rows, never evidence or snapshots.
-- +migrate Up
-- One row per registered catalog. recording_alias_set_json is the canonical JSON array of the
-- registered snapshot's prompt_aliases ('[]' for an embedded catalog): the root the alias
-- rows are audited against, as recording_catalog_json is for recording_catalog_prompts.
CREATE TABLE IF NOT EXISTS recording_catalog_alias_sets (
    recording_catalog_sha256 TEXT NOT NULL, -- the registered catalog (PromptSuite digest)
    recording_subject_id TEXT NOT NULL, -- projection subject, as in recording_catalogs
    recording_alias_set_json TEXT NOT NULL, -- canonical JSON array of PromptAlias objects
    PRIMARY KEY (recording_catalog_sha256, recording_subject_id),
    FOREIGN KEY (recording_catalog_sha256, recording_subject_id) REFERENCES recording_catalogs(recording_catalog_sha256, recording_subject_id) ON DELETE CASCADE
);
-- A retained take recorded against recording_accepted_prompt_sha256 counts for the current
-- prompt recording_prompt_sha256 of this catalog when both prompts share a prompt id. An
-- accepted digest belongs to one prompt and is never a current digest of the same catalog
-- (the audit re-checks the latter against recording_catalog_prompts).
CREATE TABLE IF NOT EXISTS recording_prompt_aliases (
    recording_catalog_sha256 TEXT NOT NULL, -- the registered catalog
    recording_subject_id TEXT NOT NULL, -- projection subject
    recording_prompt_sha256 TEXT NOT NULL, -- current prompt digest in that catalog
    recording_accepted_prompt_sha256 TEXT NOT NULL CHECK (length(recording_accepted_prompt_sha256) = 64), -- earlier digest it still counts
    PRIMARY KEY (recording_catalog_sha256, recording_subject_id, recording_prompt_sha256, recording_accepted_prompt_sha256),
    UNIQUE (recording_catalog_sha256, recording_subject_id, recording_accepted_prompt_sha256),
    CHECK (recording_accepted_prompt_sha256 != recording_prompt_sha256),
    FOREIGN KEY (recording_catalog_sha256, recording_subject_id) REFERENCES recording_catalog_alias_sets(recording_catalog_sha256, recording_subject_id) ON DELETE CASCADE,
    FOREIGN KEY (recording_catalog_sha256, recording_subject_id, recording_prompt_sha256) REFERENCES recording_catalog_prompts(recording_catalog_sha256, recording_subject_id, recording_prompt_sha256) ON DELETE CASCADE
);
-- Finds the catalogs that count one recorded digest through an alias (take invalidation).
CREATE INDEX IF NOT EXISTS recording_prompt_aliases_accepted ON recording_prompt_aliases(recording_accepted_prompt_sha256, recording_subject_id);
INSERT OR IGNORE INTO recording_catalog_alias_sets SELECT recording_catalog_sha256, recording_subject_id, '[]' FROM recording_catalogs;
PRAGMA user_version = 4;
-- +migrate Down
DROP INDEX IF EXISTS recording_prompt_aliases_accepted;
DROP TABLE IF EXISTS recording_prompt_aliases;
DROP TABLE IF EXISTS recording_catalog_alias_sets;
PRAGMA user_version = 3;
