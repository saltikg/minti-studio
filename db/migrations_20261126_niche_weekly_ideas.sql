CREATE TABLE IF NOT EXISTS main.niche_brand_channel_uploads (
    brand_id VARCHAR NOT NULL,
    youtube_video_id VARCHAR NOT NULL,
    title TEXT NOT NULL,
    published_at TIMESTAMP,
    duration_s INTEGER,
    youtube_channel_id VARCHAR,
    refreshed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (brand_id, youtube_video_id)
);

CREATE INDEX IF NOT EXISTS idx_niche_brand_uploads_recent
    ON main.niche_brand_channel_uploads(brand_id, published_at DESC);

CREATE TABLE IF NOT EXISTS main.niche_brand_video_archive (
    brand_id VARCHAR NOT NULL,
    youtube_video_id VARCHAR NOT NULL,
    source_video_pk BIGINT,
    title TEXT NOT NULL,
    published_at TIMESTAMP,
    language VARCHAR NOT NULL,
    transcript_source VARCHAR NOT NULL,
    segments_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    windows_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    embedding_model VARCHAR,
    caption_kind VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (brand_id, youtube_video_id)
);

CREATE INDEX IF NOT EXISTS idx_niche_brand_archive_brand
    ON main.niche_brand_video_archive(brand_id, published_at DESC);

CREATE TABLE IF NOT EXISTS main.niche_weekly_ideas (
    niche_id BIGINT NOT NULL REFERENCES main.niches(id) ON DELETE CASCADE,
    as_of DATE NOT NULL,
    brand_id VARCHAR NOT NULL,
    source_video_id VARCHAR NOT NULL,
    source_channel_id VARCHAR,
    source_channel_title TEXT,
    source_title TEXT NOT NULL,
    source_views BIGINT NOT NULL DEFAULT 0,
    source_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
    source_url TEXT NOT NULL,
    angle_label TEXT NOT NULL,
    theme_label TEXT NOT NULL,
    worked_creator_count INTEGER NOT NULL DEFAULT 1,
    selected_rank INTEGER,
    skip_reason TEXT,
    skip_evidence TEXT,
    status VARCHAR NOT NULL,
    archive_youtube_video_id VARCHAR,
    archive_video_pk BIGINT,
    archive_title TEXT,
    archive_published_at TIMESTAMP,
    archive_start_seconds DOUBLE PRECISION,
    archive_snippet TEXT,
    archive_url TEXT,
    suggested_title TEXT,
    suggested_hook TEXT,
    audit_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    llm_input_tokens INTEGER NOT NULL DEFAULT 0,
    llm_output_tokens INTEGER NOT NULL DEFAULT 0,
    embedding_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (niche_id, as_of, brand_id, source_video_id)
);

CREATE INDEX IF NOT EXISTS idx_niche_weekly_ideas_brand
    ON main.niche_weekly_ideas(brand_id, niche_id, as_of DESC, selected_rank);
