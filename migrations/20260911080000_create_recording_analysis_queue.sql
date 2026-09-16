-- Rebuildable scheduler projection of durable job heads in recording-analysis/.
-- Apply at guarded index open; no audio/evidence mutation or maintenance window.
-- Down removes only disposable scheduling state; jobs replay from canonical heads.
-- +migrate Up
CREATE TABLE IF NOT EXISTS recording_analysis_jobs (
    recording_analysis_job_sha256 TEXT PRIMARY KEY NOT NULL CHECK(length(recording_analysis_job_sha256)=64),
    recording_analysis_sequence INTEGER NOT NULL UNIQUE,
    recording_analysis_priority TEXT NOT NULL CHECK(recording_analysis_priority IN ('new','historical')),
    recording_analysis_status TEXT NOT NULL CHECK(recording_analysis_status IN ('pending','running','verified','needs-review','invalid','failed','stale')),
    recording_analysis_attempt INTEGER NOT NULL CHECK(recording_analysis_attempt>=0),
    recording_analysis_job_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recording_analysis_schedule ON recording_analysis_jobs (
    recording_analysis_status, recording_analysis_priority, recording_analysis_sequence
);
-- +migrate Down
DROP TABLE IF EXISTS recording_analysis_jobs;
