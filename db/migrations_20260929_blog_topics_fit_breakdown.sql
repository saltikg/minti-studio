ALTER TABLE main.blog_topics
    ADD COLUMN IF NOT EXISTS fit_breakdown JSONB NULL;
