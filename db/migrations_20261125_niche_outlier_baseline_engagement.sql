ALTER TABLE main.niche_outlier_shorts
    ADD COLUMN IF NOT EXISTS baseline_engagement DOUBLE PRECISION;
