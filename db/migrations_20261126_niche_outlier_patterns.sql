CREATE TABLE IF NOT EXISTS main.niche_outlier_patterns (
    niche_id BIGINT NOT NULL REFERENCES main.niches(id) ON DELETE CASCADE,
    as_of DATE NOT NULL,
    pattern_sentence TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (niche_id, as_of)
);
