CREATE TABLE IF NOT EXISTS main.blog_pipeline_runs (
    id SERIAL PRIMARY KEY,
    topic_id INTEGER NOT NULL,
    article_id INTEGER NULL,
    status TEXT NOT NULL,
    current_stage TEXT NULL,
    final_review_score INTEGER NULL,
    total_cost_usd NUMERIC(10,5) DEFAULT 0,
    error TEXT NULL,
    started_at TIMESTAMPTZ DEFAULT now(),
    finished_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS main.blog_pipeline_stages (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT NULL,
    output JSONB NULL,
    score INTEGER NULL,
    notes TEXT NULL,
    cost_usd NUMERIC(10,5),
    started_at TIMESTAMPTZ DEFAULT now(),
    finished_at TIMESTAMPTZ NULL
);

ALTER TABLE main.blog_llm_usage
    ADD COLUMN IF NOT EXISTS run_id INTEGER NULL;

CREATE INDEX IF NOT EXISTS idx_blog_pipeline_runs_topic_id
    ON main.blog_pipeline_runs(topic_id);

CREATE INDEX IF NOT EXISTS idx_blog_pipeline_stages_run_id
    ON main.blog_pipeline_stages(run_id);

CREATE INDEX IF NOT EXISTS idx_blog_llm_usage_run_id
    ON main.blog_llm_usage(run_id);
