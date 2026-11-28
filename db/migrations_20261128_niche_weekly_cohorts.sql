ALTER TABLE main.niches
    ADD COLUMN IF NOT EXISTS shorts_views_floor BIGINT NOT NULL DEFAULT 10000;

UPDATE main.niches SET shorts_views_floor = 50000 WHERE slug = 'turkish_religious_talks';
UPDATE main.niches SET shorts_views_floor = 10000 WHERE slug = 'menopause_hormones';

ALTER TABLE main.niche_outlier_shorts
    ADD COLUMN IF NOT EXISTS signal_type TEXT NOT NULL DEFAULT 'weekly',
    ADD COLUMN IF NOT EXISTS cohort_start DATE,
    ADD COLUMN IF NOT EXISTS theme_label TEXT,
    ADD COLUMN IF NOT EXISTS format_label TEXT,
    ADD COLUMN IF NOT EXISTS age_days DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS speed_ratio DOUBLE PRECISION;

UPDATE main.niche_outlier_shorts
SET cohort_start = as_of - ((EXTRACT(ISODOW FROM as_of)::INTEGER - 1) + 14)
WHERE cohort_start IS NULL;

CREATE INDEX IF NOT EXISTS idx_niche_outlier_shorts_cohort
    ON main.niche_outlier_shorts (niche_id, signal_type, cohort_start DESC, outlier_ratio DESC);
