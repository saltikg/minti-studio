ALTER TABLE main.youtube_videos
    ADD COLUMN IF NOT EXISTS media_duration_seconds DOUBLE PRECISION;
