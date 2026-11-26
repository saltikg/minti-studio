ALTER TABLE main.niche_outlier_shorts
    ADD COLUMN IF NOT EXISTS topic_status TEXT;

UPDATE main.niche_outlier_shorts
SET topic_status = CASE WHEN on_topic THEN 'on_topic' ELSE 'off_topic' END
WHERE topic_status IS NULL AND on_topic IS NOT NULL;

ALTER TABLE main.niche_outlier_shorts
    DROP CONSTRAINT IF EXISTS niche_outlier_shorts_topic_status_check;

ALTER TABLE main.niche_outlier_shorts
    ADD CONSTRAINT niche_outlier_shorts_topic_status_check
    CHECK (topic_status IS NULL OR topic_status IN ('on_topic', 'adjacent', 'off_topic'));

CREATE TABLE IF NOT EXISTS main.niche_outlier_short_topic_overrides (
    niche_id BIGINT NOT NULL REFERENCES main.niches(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    topic_status TEXT NOT NULL CHECK (topic_status IN ('on_topic', 'off_topic')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (niche_id, video_id)
);
