CREATE TABLE IF NOT EXISTS main.blog_sources (
    id SERIAL PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    channel_id TEXT NULL,
    enabled BOOLEAN DEFAULT true,
    last_checked_at TIMESTAMPTZ NULL,
    last_error TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_blog_sources_url_unique
    ON main.blog_sources(url);

ALTER TABLE main.blog_topics
    ADD COLUMN IF NOT EXISTS source_summary TEXT NULL;

ALTER TABLE main.blog_topics
    ADD COLUMN IF NOT EXISTS adapted_from TEXT NULL;

INSERT INTO main.blog_sources (type, name, url, channel_id, enabled, last_error)
VALUES
    ('rss', 'OpusClip blog', 'https://www.opus.pro/sitemap.xml', NULL, true, NULL),
    ('rss', 'Klap blog', 'https://klap.app/sitemap.xml', NULL, true, NULL),
    ('rss', 'Vizard blog', 'https://vizard.ai/blog/feed', NULL, true, NULL),
    ('rss', 'Descript blog', 'https://www.descript.com/sitemap.xml', NULL, false, 'disabled: robots.txt disallows /blog'),
    ('rss', 'YouTube creator news', 'https://blog.youtube/sitemap.xml', NULL, true, NULL),
    ('youtube_channel', 'Think Media', 'https://www.youtube.com/@ThinkMediaTV/videos', 'UCGxjDWAN1KwrkXYVi8CXtjQ', true, NULL),
    ('youtube_channel', 'vidIQ', 'https://www.youtube.com/@vidIQ/videos', 'UCZLFu8bHbwtnIgWLg5UtINw', true, NULL),
    ('youtube_channel', 'Creator Insider', 'https://www.youtube.com/@creatorinsider/videos', 'UCGg-UqjRgzhYDPJMr-9HXCg', true, NULL),
    ('youtube_channel', 'YouTube Creators', 'https://www.youtube.com/@YouTubeCreators/videos', NULL, false, 'disabled: handle did not resolve via prod metadata probe')
ON CONFLICT (url) DO UPDATE
SET type = EXCLUDED.type,
    name = EXCLUDED.name,
    channel_id = EXCLUDED.channel_id,
    enabled = EXCLUDED.enabled,
    last_error = EXCLUDED.last_error;
