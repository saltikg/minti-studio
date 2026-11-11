CREATE TABLE IF NOT EXISTS scheduled_clip_customer_actions (
    id VARCHAR PRIMARY KEY,
    owner_user_id VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    video_pk BIGINT NOT NULL,
    plan_index INTEGER NOT NULL,
    brand_name TEXT,
    clip_title TEXT,
    action VARCHAR NOT NULL,
    changed_fields_json TEXT NOT NULL,
    schedules_json TEXT NOT NULL,
    clip_url TEXT,
    actor_user_id VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scheduled_clip_customer_actions_due
    ON scheduled_clip_customer_actions(sent_at, created_at);
