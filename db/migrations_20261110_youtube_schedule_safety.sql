-- Apply manually before deploying the scheduler safety code.
-- Existing rows intentionally keep created_at/created_by NULL so the new
-- automation cannot enqueue or mutate pre-deploy schedules.
SET search_path TO main, public;

ALTER TABLE shorts_youtube_schedules
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NULL;
ALTER TABLE shorts_youtube_schedules
    ADD COLUMN IF NOT EXISTS created_by VARCHAR NULL;

CREATE TABLE IF NOT EXISTS shorts_schedule_status_history (
    id BIGINT PRIMARY KEY,
    schedule_id VARCHAR NOT NULL,
    platform VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    reason TEXT,
    error_sanitized TEXT,
    actor VARCHAR,
    metadata_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_schedule_history_schedule
    ON shorts_schedule_status_history(schedule_id, created_at DESC);

CREATE OR REPLACE FUNCTION reject_schedule_history_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'shorts_schedule_status_history is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_schedule_history_append_only
    ON shorts_schedule_status_history;
CREATE TRIGGER trg_schedule_history_append_only
BEFORE UPDATE OR DELETE ON shorts_schedule_status_history
FOR EACH ROW EXECUTE FUNCTION reject_schedule_history_mutation();

CREATE TABLE IF NOT EXISTS shorts_schedule_alerts (
    alert_key VARCHAR PRIMARY KEY,
    schedule_id VARCHAR NOT NULL,
    platform VARCHAR NOT NULL,
    alert_type VARCHAR NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_schedule_alerts_created
    ON shorts_schedule_alerts(created_at DESC);

CREATE TABLE IF NOT EXISTS shorts_scheduler_state (
    name VARCHAR PRIMARY KEY,
    last_tick_at TIMESTAMP,
    last_worker_id VARCHAR,
    last_gap_alerted_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
