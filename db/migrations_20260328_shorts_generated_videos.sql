CREATE TABLE IF NOT EXISTS main.shorts_generated_videos (
    id BIGSERIAL PRIMARY KEY,
    brand_id VARCHAR,
    source_video_id VARCHAR NOT NULL,
    source_channel_type VARCHAR NOT NULL DEFAULT 'youtube',
    clip_filename VARCHAR NOT NULL,
    output_filename VARCHAR,
    storage_file_key VARCHAR,
    generation_status VARCHAR,
    publish_status VARCHAR,
    youtube_video_id VARCHAR,
    instagram_media_id VARCHAR,
    facebook_video_id VARCHAR,
    tiktok_video_id VARCHAR,
    planned_publish_at TIMESTAMP,
    published_at TIMESTAMP,
    plan_run_id VARCHAR,
    raw_plan_entry_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_generated_videos_source_clip
ON main.shorts_generated_videos(source_video_id, source_channel_type, clip_filename);

CREATE INDEX IF NOT EXISTS idx_shorts_generated_videos_source_video_id
ON main.shorts_generated_videos(source_video_id);

CREATE INDEX IF NOT EXISTS idx_shorts_generated_videos_clip_filename
ON main.shorts_generated_videos(clip_filename);

CREATE INDEX IF NOT EXISTS idx_shorts_generated_videos_youtube_video_id
ON main.shorts_generated_videos(youtube_video_id);

CREATE INDEX IF NOT EXISTS idx_shorts_generated_videos_publish_status
ON main.shorts_generated_videos(publish_status);

CREATE INDEX IF NOT EXISTS idx_shorts_generated_videos_created_at
ON main.shorts_generated_videos(created_at);

CREATE INDEX IF NOT EXISTS idx_shorts_generated_videos_brand_id
ON main.shorts_generated_videos(brand_id);

CREATE OR REPLACE FUNCTION main.set_shorts_generated_videos_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_shorts_generated_videos_updated_at
ON main.shorts_generated_videos;

CREATE TRIGGER trg_shorts_generated_videos_updated_at
BEFORE UPDATE ON main.shorts_generated_videos
FOR EACH ROW
EXECUTE FUNCTION main.set_shorts_generated_videos_updated_at();
