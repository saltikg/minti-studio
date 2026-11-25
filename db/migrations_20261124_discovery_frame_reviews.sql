CREATE TABLE IF NOT EXISTS main.discovery_lead_frame_reviews (
    lead_id BIGINT PRIMARY KEY,
    status VARCHAR NOT NULL DEFAULT 'pending',
    video_id VARCHAR,
    video_title TEXT,
    duration_seconds INTEGER,
    published_at TIMESTAMP,
    frame_keys TEXT,
    frame_timestamps TEXT,
    storage_backend VARCHAR,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    elapsed_seconds DOUBLE PRECISION,
    ai_label VARCHAR,
    ai_reason TEXT,
    ai_model VARCHAR,
    ai_input_tokens INTEGER NOT NULL DEFAULT 0,
    ai_output_tokens INTEGER NOT NULL DEFAULT 0,
    ai_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_discovery_frame_reviews_status
    ON main.discovery_lead_frame_reviews (status, updated_at);

CREATE TABLE IF NOT EXISTS main.discovery_lead_review_decisions (
    id VARCHAR PRIMARY KEY,
    lead_id BIGINT NOT NULL,
    ai_label VARCHAR,
    my_action VARCHAR NOT NULL,
    decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_discovery_review_decisions_lead
    ON main.discovery_lead_review_decisions (lead_id, decided_at DESC);

ALTER TABLE main.discovery_lead_frame_reviews
    ADD COLUMN IF NOT EXISTS retry_after TIMESTAMP;

ALTER TABLE main.discovery_lead_frame_reviews
    ADD COLUMN IF NOT EXISTS error_code VARCHAR;

ALTER TABLE main.discovery_lead_frame_reviews
    ADD COLUMN IF NOT EXISTS frame_width INTEGER;

CREATE INDEX IF NOT EXISTS idx_discovery_frame_reviews_retry
    ON main.discovery_lead_frame_reviews (status, retry_after);
