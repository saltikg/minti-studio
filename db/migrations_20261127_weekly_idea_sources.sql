ALTER TABLE main.niche_brand_video_archive
    ADD COLUMN IF NOT EXISTS source_channel_title TEXT;

ALTER TABLE main.niche_weekly_ideas
    ADD COLUMN IF NOT EXISTS archive_source_channel_title TEXT;

ALTER TABLE main.niche_weekly_ideas
    ADD COLUMN IF NOT EXISTS idea_note TEXT;
