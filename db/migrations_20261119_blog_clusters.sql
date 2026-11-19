CREATE TABLE IF NOT EXISTS main.blog_clusters (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    audience TEXT NOT NULL,
    seed_topics TEXT NOT NULL DEFAULT '[]',
    weight INTEGER NOT NULL CHECK (weight >= 0),
    boost_weight INTEGER NOT NULL CHECK (boost_weight >= 0),
    boost_until DATE,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO main.blog_clusters (slug, name, audience, seed_topics, weight, boost_weight, boost_until, active)
VALUES
    (
        'doctors',
        'Doctors',
        'Doctors and clinicians who teach on camera, including OB/GYNs, menopause doctors, physical therapists, cardiologists, and family-medicine clinicians. Content is about growing an educational channel, never practicing medicine.',
        '["YouTube Shorts for doctors complete guide (pillar)", "how busy doctors grow a channel in 1 hour a week", "will Shorts make me look less professional", "clipping medical advice without losing context/caveats", "do Shorts hurt long videos and ad revenue", "patient privacy — what doctors should never post", "patient questions that make great Shorts", "Shorts for OB/GYNs", "Shorts for menopause doctors", "Shorts for physical therapists", "Shorts for cardiologists", "Shorts for family medicine doctors"]',
        20,
        60,
        DATE '2026-12-10',
        true
    ),
    (
        'podcasters',
        'Podcasters',
        'Podcasters and hosts of interview or conversation shows who want long episodes to create consistent discovery and audience growth.',
        '["turning a 1-hour interview into 10 Shorts", "guest clips that promote both host and guest", "finding standalone moments in long conversations", "multi-speaker framing and captions in vertical", "video podcast growth on YouTube via Shorts"]',
        40,
        20,
        DATE '2026-12-10',
        true
    ),
    (
        'general',
        'General creators',
        'Solo creators, coaches, educators, and consultants who publish long-form expertise and want a sustainable Shorts workflow.',
        '[]',
        40,
        20,
        DATE '2026-12-10',
        true
    )
ON CONFLICT (slug) DO NOTHING;

ALTER TABLE main.blog_topics
    ADD COLUMN IF NOT EXISTS cluster_slug TEXT;

UPDATE main.blog_topics
SET cluster_slug = 'general'
WHERE cluster_slug IS NULL OR btrim(cluster_slug) = '';

ALTER TABLE main.blog_topics
    ALTER COLUMN cluster_slug SET DEFAULT 'general';

ALTER TABLE main.blog_topics
    ALTER COLUMN cluster_slug SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_blog_topics_cluster_status_fit
    ON main.blog_topics(cluster_slug, status, fit_score DESC, created_at DESC);
