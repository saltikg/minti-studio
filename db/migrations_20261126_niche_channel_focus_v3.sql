ALTER TABLE main.niche_channels
    ADD COLUMN IF NOT EXISTS primary_focus TEXT;

ALTER TABLE main.niche_channels
    DROP CONSTRAINT IF EXISTS niche_channels_primary_focus_check;

ALTER TABLE main.niche_channels
    ADD CONSTRAINT niche_channels_primary_focus_check
    CHECK (
        primary_focus IS NULL OR primary_focus IN (
            'hormones_midlife_health',
            'fitness_training',
            'nutrition_diet',
            'general_health_wellness',
            'fertility_pregnancy',
            'other'
        )
    );
