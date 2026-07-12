CREATE TABLE IF NOT EXISTS main.user_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR NOT NULL,
    event_name VARCHAR NOT NULL,
    video_id VARCHAR,
    short_id VARCHAR,
    platform VARCHAR,
    status VARCHAR,
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_events_user_created_at
ON main.user_events(user_id, created_at DESC);
