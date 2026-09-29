ALTER TABLE main.blog_articles
    ADD COLUMN IF NOT EXISTS content_updated_at TIMESTAMP;

UPDATE main.blog_articles
SET content_updated_at = COALESCE(content_updated_at, published_at, created_at);

CREATE INDEX IF NOT EXISTS idx_blog_articles_content_updated_at
    ON main.blog_articles(content_updated_at DESC);
