CREATE TABLE IF NOT EXISTS main.autopilot_customer_video_notifications (
    id UUID PRIMARY KEY,
    video_pk BIGINT NOT NULL,
    owner_user_id VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    recipient VARCHAR NOT NULL,
    personal_note TEXT,
    short_count INTEGER NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    provider_message_id VARCHAR,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_autopilot_customer_video_notifications_video
ON main.autopilot_customer_video_notifications(owner_user_id, brand_id, video_pk, sent_at DESC);
