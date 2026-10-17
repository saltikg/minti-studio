-- Run with psql outside a transaction.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_youtube_videos_owner_brand
    ON youtube_videos(owner_user_id, brand_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_youtube_transcripts_video_id
    ON youtube_transcripts(video_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shorts_quick_sessions_video_created
    ON shorts_quick_sessions(video_pk, created_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shorts_render_jobs_video_status_created
    ON shorts_render_jobs((payload_json->>'video_pk'), status, created_at);
