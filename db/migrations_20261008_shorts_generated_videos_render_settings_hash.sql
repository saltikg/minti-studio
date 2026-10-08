ALTER TABLE shorts_generated_videos
ADD COLUMN IF NOT EXISTS render_settings_hash TEXT;
