-- Disposable, subject-scoped recovery inventory owned by the service index handle.
-- Canonical evidence is unchanged. Expand-only cache migration; no maintenance window.
-- Rolling back this cache schema drops only findings, which a later open can rescan.
-- +migrate Up
CREATE TABLE IF NOT EXISTS recording_recovery_scans (
    recording_subject_id TEXT PRIMARY KEY NOT NULL,
    recording_recovery_scan_generation TEXT NOT NULL,
    recording_recovery_inventory_revision INTEGER NOT NULL CHECK (recording_recovery_inventory_revision >= 0),
    recording_recovery_next_token INTEGER NOT NULL CHECK (recording_recovery_next_token >= 0),
    recording_recovery_scan_state TEXT NOT NULL CHECK (recording_recovery_scan_state IN ('scanning','complete','error')),
    recording_recovery_enumeration_complete INTEGER NOT NULL CHECK (recording_recovery_enumeration_complete IN (0,1)),
    recording_recovery_covered_store_revision INTEGER CHECK (recording_recovery_covered_store_revision >= 0),
    recording_recovery_error_json TEXT,
    CHECK ((recording_recovery_scan_state = 'error') = (recording_recovery_error_json IS NOT NULL)),
    CHECK (recording_recovery_scan_state != 'complete' OR (recording_recovery_enumeration_complete = 1 AND recording_recovery_covered_store_revision IS NOT NULL))
);
-- No FK to a history take: an unreadable or missing head still needs a finding.
-- Tokens and generation protect concurrent rechecks; protection covers Reserve -> Start.
CREATE TABLE IF NOT EXISTS recording_recovery_findings (
    recording_subject_id TEXT NOT NULL REFERENCES recording_recovery_scans(recording_subject_id) ON DELETE CASCADE,
    recording_take_id TEXT NOT NULL,
    recording_recovery_observed_take_revision INTEGER CHECK (recording_recovery_observed_take_revision >= 0),
    recording_recovery_work_token INTEGER NOT NULL CHECK (recording_recovery_work_token >= 0),
    recording_recovery_pending INTEGER NOT NULL CHECK (recording_recovery_pending IN (0,1)),
    recording_recovery_protected INTEGER NOT NULL CHECK (recording_recovery_protected IN (0,1)),
    recording_recovery_classified_generation TEXT,
    recording_recovery_claimed_generation TEXT,
    recording_recovery_result_json TEXT,
    recording_recovery_result_sha256 TEXT CHECK (length(recording_recovery_result_sha256) = 64),
    recording_recovery_recovered_seal INTEGER NOT NULL CHECK (recording_recovery_recovered_seal IN (0,1)),
    PRIMARY KEY (recording_subject_id, recording_take_id),
    CHECK ((recording_recovery_result_json IS NULL) = (recording_recovery_result_sha256 IS NULL)),
    CHECK (recording_recovery_protected = 0 OR recording_recovery_pending = 1)
);
CREATE INDEX IF NOT EXISTS recording_recovery_pending_work ON recording_recovery_findings(recording_subject_id,recording_recovery_pending,recording_recovery_protected,recording_take_id);
PRAGMA user_version = 3;
-- +migrate Down
DROP INDEX IF EXISTS recording_recovery_pending_work;
DROP TABLE IF EXISTS recording_recovery_findings;
DROP TABLE IF EXISTS recording_recovery_scans;
PRAGMA user_version = 2;
