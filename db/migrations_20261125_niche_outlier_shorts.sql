CREATE TABLE IF NOT EXISTS main.niche_outlier_shorts (
    niche_id BIGINT NOT NULL REFERENCES main.niches(id) ON DELETE CASCADE,
    as_of DATE NOT NULL,
    youtube_channel_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    duration_s INTEGER NOT NULL,
    views BIGINT NOT NULL,
    baseline DOUBLE PRECISION NOT NULL,
    outlier_ratio DOUBLE PRECISION NOT NULL,
    engagement DOUBLE PRECISION NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (niche_id, as_of, video_id)
);

CREATE INDEX IF NOT EXISTS idx_niche_outlier_shorts_latest
    ON main.niche_outlier_shorts (niche_id, as_of DESC, outlier_ratio DESC);
