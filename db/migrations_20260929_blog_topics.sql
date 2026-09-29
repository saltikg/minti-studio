CREATE TABLE IF NOT EXISTS main.blog_topics (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now(),
    title TEXT NOT NULL,
    primary_keyword TEXT,
    category TEXT,
    intent TEXT,
    source_type TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT,
    source_title TEXT,
    fit_score INTEGER,
    status TEXT NOT NULL DEFAULT 'candidate',
    judge_reason TEXT,
    angle TEXT,
    brief TEXT,
    duplicate_of TEXT NULL,
    is_timely BOOLEAN DEFAULT false,
    expires_at TIMESTAMPTZ NULL,
    article_id INTEGER NULL,
    CONSTRAINT blog_topics_status_check CHECK (
        status IN (
            'candidate',
            'queued',
            'rejected_duplicate',
            'rejected_offtopic',
            'rejected_lowfit',
            'in_production',
            'draft_ready',
            'published',
            'failed'
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_blog_topics_source_url_unique
    ON main.blog_topics(source_url)
    WHERE source_url IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_blog_topics_active_primary_keyword_unique
    ON main.blog_topics(primary_keyword)
    WHERE status IN ('queued', 'in_production', 'draft_ready', 'published')
      AND primary_keyword IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_blog_topics_status_fit
    ON main.blog_topics(status, fit_score DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS main.blog_llm_usage (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now(),
    stage TEXT,
    topic_id INTEGER NULL,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    images INTEGER,
    cost_usd NUMERIC(10,5)
);

CREATE INDEX IF NOT EXISTS idx_blog_llm_usage_created_stage
    ON main.blog_llm_usage(created_at, stage);
