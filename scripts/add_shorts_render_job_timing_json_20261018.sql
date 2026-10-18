BEGIN;

ALTER TABLE shorts_render_jobs
    ADD COLUMN IF NOT EXISTS timing_json JSONB;

COMMIT;
