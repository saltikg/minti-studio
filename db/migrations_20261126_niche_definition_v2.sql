ALTER TABLE main.niches
    ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS core_topics JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS adjacent_topics JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS excluded_topics JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS definition_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE main.niche_channels
    ADD COLUMN IF NOT EXISTS relevance INTEGER,
    ADD COLUMN IF NOT EXISTS relevance_reason TEXT;

ALTER TABLE main.niche_channels
    DROP CONSTRAINT IF EXISTS niche_channels_relevance_check;

ALTER TABLE main.niche_channels
    ADD CONSTRAINT niche_channels_relevance_check
    CHECK (relevance IS NULL OR relevance BETWEEN 0 AND 100);

ALTER TABLE main.niche_outlier_shorts
    ADD COLUMN IF NOT EXISTS in_category BOOLEAN;
