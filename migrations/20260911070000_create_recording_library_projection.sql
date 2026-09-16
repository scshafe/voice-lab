-- Creates the disposable, metadata-only recording library projection.
-- Exact prompt joins and retained-take indexes support coherent bounded history
-- and Save & Next. Filesystem evidence remains authoritative. No maintenance
-- window is required: apply only while opening this private cache exclusively.
-- Reversible: Down drops only rebuildable projection data, never evidence/audio.
-- +migrate Up
CREATE TABLE IF NOT EXISTS recording_index_states (
    recording_index_state_id INTEGER PRIMARY KEY CHECK (recording_index_state_id = 1),
    recording_index_instance_id TEXT NOT NULL,
    recording_index_revision INTEGER NOT NULL CHECK (recording_index_revision >= 0)
);
CREATE TABLE IF NOT EXISTS recording_catalogs (
    recording_catalog_sha256 TEXT NOT NULL CHECK (length(recording_catalog_sha256) = 64),
    recording_subject_id TEXT NOT NULL,
    recording_catalog_json TEXT NOT NULL,
    recording_catalog_completeness TEXT NOT NULL CHECK (recording_catalog_completeness IN ('complete','building','unknown')),
    PRIMARY KEY (recording_catalog_sha256, recording_subject_id)
);
-- Prompt digest includes every prompt field, including style/holdout/order-independent identity.
CREATE TABLE IF NOT EXISTS recording_prompts (
    recording_prompt_sha256 TEXT PRIMARY KEY NOT NULL CHECK (length(recording_prompt_sha256) = 64),
    recording_prompt_id TEXT NOT NULL,
    recording_prompt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recording_catalog_prompts (
    recording_catalog_sha256 TEXT NOT NULL,
    recording_subject_id TEXT NOT NULL,
    recording_prompt_sha256 TEXT NOT NULL REFERENCES recording_prompts(recording_prompt_sha256),
    recording_prompt_ordinal INTEGER NOT NULL CHECK (recording_prompt_ordinal >= 0),
    PRIMARY KEY (recording_catalog_sha256, recording_subject_id, recording_prompt_ordinal),
    UNIQUE (recording_catalog_sha256, recording_subject_id, recording_prompt_sha256),
    FOREIGN KEY (recording_catalog_sha256, recording_subject_id) REFERENCES recording_catalogs(recording_catalog_sha256, recording_subject_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS recording_groups (
    recording_group_key TEXT NOT NULL,
    recording_subject_id TEXT NOT NULL,
    recording_group_id TEXT,
    recording_legacy_session_id TEXT,
    recording_group_opened_at TEXT,
    recording_group_date_precision TEXT NOT NULL CHECK (recording_group_date_precision IN ('take','session','unknown')),
    CHECK ((recording_group_id IS NULL) != (recording_legacy_session_id IS NULL)),
    CHECK ((recording_group_date_precision = 'unknown') = (recording_group_opened_at IS NULL)),
    PRIMARY KEY (recording_group_key, recording_subject_id)
);
-- Explicitly discovered empty/interrupted legacy groups still have catalog context.
CREATE TABLE IF NOT EXISTS recording_catalog_groups (
    recording_catalog_sha256 TEXT NOT NULL,
    recording_subject_id TEXT NOT NULL,
    recording_group_key TEXT NOT NULL,
    PRIMARY KEY (recording_catalog_sha256, recording_subject_id, recording_group_key),
    FOREIGN KEY (recording_catalog_sha256, recording_subject_id) REFERENCES recording_catalogs(recording_catalog_sha256, recording_subject_id) ON DELETE CASCADE,
    FOREIGN KEY (recording_group_key, recording_subject_id) REFERENCES recording_groups(recording_group_key, recording_subject_id)
);
CREATE TABLE IF NOT EXISTS recording_takes (
    recording_take_id TEXT PRIMARY KEY NOT NULL,
    recording_subject_id TEXT NOT NULL,
    recording_group_key TEXT NOT NULL,
    recording_prompt_sha256 TEXT NOT NULL REFERENCES recording_prompts(recording_prompt_sha256),
    recording_source_catalog_sha256 TEXT NOT NULL CHECK (length(recording_source_catalog_sha256) = 64),
    recording_take_origin TEXT NOT NULL CHECK (recording_take_origin IN ('native','legacy')),
    recording_take_recorded_at TEXT,
    recording_take_decision TEXT NOT NULL CHECK (recording_take_decision IN ('draft','kept','rejected','removed')),
    recording_take_analysis TEXT NOT NULL CHECK (recording_take_analysis IN ('pending','running','verified','needs-review','invalid','failed')),
    recording_take_state_revision INTEGER NOT NULL CHECK (recording_take_state_revision >= 0),
    recording_take_payload_json TEXT NOT NULL,
    recording_take_payload_sha256 TEXT NOT NULL CHECK (length(recording_take_payload_sha256) = 64),
    FOREIGN KEY (recording_group_key, recording_subject_id) REFERENCES recording_groups(recording_group_key, recording_subject_id)
);
-- Aliases identify one recording event, not PCM equivalence. Distinct events
-- with identical bytes retain separate take IDs and coverage/history entries.
CREATE TABLE IF NOT EXISTS recording_take_aliases (
    recording_alias_id TEXT PRIMARY KEY NOT NULL,
    recording_take_id TEXT NOT NULL REFERENCES recording_takes(recording_take_id) ON DELETE CASCADE,
    recording_alias_path TEXT NOT NULL,
    recording_alias_kind TEXT NOT NULL CHECK (recording_alias_kind IN ('native-original','legacy-tmp','legacy-raw','rejected','package-tmp','package-raw')),
    recording_alias_sha256 TEXT NOT NULL CHECK (length(recording_alias_sha256) = 64),
    UNIQUE (recording_alias_path, recording_alias_sha256)
);
-- Unknown records are named evidence failures. They are never missing coverage.
CREATE TABLE IF NOT EXISTS recording_unknown_records (
    recording_unknown_record_id TEXT NOT NULL,
    recording_catalog_sha256 TEXT NOT NULL,
    recording_subject_id TEXT NOT NULL,
    recording_unknown_error_json TEXT NOT NULL,
    PRIMARY KEY (recording_unknown_record_id, recording_catalog_sha256, recording_subject_id),
    FOREIGN KEY (recording_catalog_sha256, recording_subject_id) REFERENCES recording_catalogs(recording_catalog_sha256, recording_subject_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS recording_takes_exact_prompt_coverage ON recording_takes(recording_subject_id, recording_prompt_sha256, recording_take_decision, recording_take_analysis, recording_take_id);
CREATE INDEX IF NOT EXISTS recording_takes_history_page ON recording_takes(recording_subject_id);
CREATE INDEX IF NOT EXISTS recording_takes_group_page ON recording_takes(recording_subject_id, recording_group_key, recording_prompt_sha256);
CREATE INDEX IF NOT EXISTS recording_aliases_take_lookup ON recording_take_aliases(recording_take_id);
PRAGMA user_version = 1;
-- +migrate Down
DROP INDEX IF EXISTS recording_aliases_take_lookup;
DROP INDEX IF EXISTS recording_takes_group_page;
DROP INDEX IF EXISTS recording_takes_history_page;
DROP INDEX IF EXISTS recording_takes_exact_prompt_coverage;
DROP TABLE IF EXISTS recording_unknown_records;
DROP TABLE IF EXISTS recording_take_aliases;
DROP TABLE IF EXISTS recording_takes;
DROP TABLE IF EXISTS recording_catalog_groups;
DROP TABLE IF EXISTS recording_groups;
DROP TABLE IF EXISTS recording_catalog_prompts;
DROP TABLE IF EXISTS recording_prompts;
DROP TABLE IF EXISTS recording_catalogs;
DROP TABLE IF EXISTS recording_index_states;
PRAGMA user_version = 0;
