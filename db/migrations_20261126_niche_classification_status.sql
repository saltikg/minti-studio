ALTER TABLE main.niche_channels
    ADD COLUMN IF NOT EXISTS classification_status TEXT;

ALTER TABLE main.niche_channels
    ADD COLUMN IF NOT EXISTS classification_title_count INTEGER;

ALTER TABLE main.niche_channels
    DROP CONSTRAINT IF EXISTS niche_channels_classification_status_check;

ALTER TABLE main.niche_channels
    ADD CONSTRAINT niche_channels_classification_status_check
    CHECK (classification_status IS NULL OR classification_status IN ('classified', 'no_titles'));

ALTER TABLE main.niche_channels
    DROP CONSTRAINT IF EXISTS niche_channels_classification_title_count_check;

ALTER TABLE main.niche_channels
    ADD CONSTRAINT niche_channels_classification_title_count_check
    CHECK (classification_title_count IS NULL OR classification_title_count BETWEEN 0 AND 15);
