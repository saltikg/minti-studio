ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS generated_title TEXT;

ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS generated_description TEXT;

ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS generated_excerpt TEXT;

ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS generated_transcript_full TEXT;

ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS youtube_published_at TIMESTAMP;

ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS instagram_published_at TIMESTAMP;

ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS facebook_published_at TIMESTAMP;

ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS tiktok_published_at TIMESTAMP;

ALTER TABLE main.shorts_generated_videos
ADD COLUMN IF NOT EXISTS primary_publish_platform VARCHAR;
