CREATE TABLE IF NOT EXISTS main.autopilot_channel_videos (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    channel_id VARCHAR NOT NULL,
    youtube_video_id VARCHAR NOT NULL,
    title TEXT NOT NULL,
    published_at TIMESTAMP,
    duration_s INTEGER,
    detected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR NOT NULL,
    CONSTRAINT autopilot_channel_videos_status_check
        CHECK (status IN ('new', 'seen', 'processed', 'skipped')),
    CONSTRAINT autopilot_channel_videos_brand_video_unique
        UNIQUE (brand_id, youtube_video_id)
);

CREATE INDEX IF NOT EXISTS idx_autopilot_channel_videos_status_detected
    ON main.autopilot_channel_videos(status, detected_at);

CREATE INDEX IF NOT EXISTS idx_autopilot_channel_videos_customer
    ON main.autopilot_channel_videos(user_id, brand_id);
