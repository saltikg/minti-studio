CREATE TABLE IF NOT EXISTS main.niche_short_transcripts (
    video_id VARCHAR PRIMARY KEY,
    transcript TEXT NOT NULL,
    detected_language VARCHAR,
    duration_s INTEGER NOT NULL DEFAULT 0,
    whisper_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
    whisper_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS main.niche_short_ideas (
    niche_id BIGINT NOT NULL REFERENCES main.niches(id) ON DELETE CASCADE,
    as_of DATE NOT NULL,
    source_video_id VARCHAR NOT NULL,
    source_channel_id VARCHAR,
    source_channel_title TEXT,
    source_title TEXT NOT NULL,
    source_views BIGINT NOT NULL DEFAULT 0,
    source_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
    source_url TEXT NOT NULL,
    source_duration_s INTEGER NOT NULL DEFAULT 0,
    selected_rank INTEGER NOT NULL,
    why_it_worked TEXT NOT NULL,
    idea_title TEXT NOT NULL,
    first_line TEXT NOT NULL,
    beats_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    ideal_length TEXT NOT NULL,
    format_tag VARCHAR NOT NULL,
    llm_input_tokens INTEGER NOT NULL DEFAULT 0,
    llm_output_tokens INTEGER NOT NULL DEFAULT 0,
    llm_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (niche_id, as_of, source_video_id)
);

CREATE INDEX IF NOT EXISTS idx_niche_short_ideas_latest
    ON main.niche_short_ideas(niche_id, as_of DESC, selected_rank);
