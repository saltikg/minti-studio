CREATE TABLE IF NOT EXISTS main.niche_rising_topics (
    niche_id BIGINT NOT NULL REFERENCES main.niches(id) ON DELETE CASCADE,
    as_of DATE NOT NULL,
    topic_key VARCHAR NOT NULL,
    label TEXT NOT NULL,
    channel_count INTEGER NOT NULL,
    total_views BIGINT NOT NULL,
    examples_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    model VARCHAR NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (niche_id, as_of, topic_key)
);

CREATE TABLE IF NOT EXISTS main.niche_rising_topic_brand_matches (
    niche_id BIGINT NOT NULL REFERENCES main.niches(id) ON DELETE CASCADE,
    as_of DATE NOT NULL,
    topic_key VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    evidence TEXT,
    archive_video_id BIGINT,
    archive_url TEXT,
    model VARCHAR NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (niche_id, as_of, topic_key, brand_id),
    FOREIGN KEY (niche_id, as_of, topic_key)
        REFERENCES main.niche_rising_topics(niche_id, as_of, topic_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_niche_rising_matches_brand
    ON main.niche_rising_topic_brand_matches(brand_id, niche_id, as_of);
