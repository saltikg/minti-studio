CREATE TABLE IF NOT EXISTS main.blog_articles (
    id BIGSERIAL PRIMARY KEY,
    title VARCHAR NOT NULL,
    slug VARCHAR NOT NULL,
    summary TEXT,
    content TEXT NOT NULL,
    cover_image_url TEXT,
    meta_title VARCHAR,
    meta_description TEXT,
    author_name VARCHAR,
    reading_time INTEGER,
    view_count INTEGER NOT NULL DEFAULT 0,
    status VARCHAR NOT NULL DEFAULT 'draft',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP,
    import_source TEXT,
    import_source_id TEXT,
    CONSTRAINT blog_articles_status_check CHECK (status IN ('draft', 'published', 'archived'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_blog_articles_slug
    ON main.blog_articles(slug);

CREATE UNIQUE INDEX IF NOT EXISTS idx_blog_articles_import_source_pair
    ON main.blog_articles(import_source, import_source_id)
    WHERE import_source IS NOT NULL AND import_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_blog_articles_status_published_at
    ON main.blog_articles(status, published_at DESC);
