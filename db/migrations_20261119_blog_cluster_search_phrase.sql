ALTER TABLE main.blog_clusters
    ADD COLUMN IF NOT EXISTS primary_search_phrase TEXT;

UPDATE main.blog_clusters
SET primary_search_phrase = CASE slug
    WHEN 'doctors' THEN 'YouTube Shorts for doctors'
    WHEN 'podcasters' THEN 'podcast Shorts'
    WHEN 'general' THEN 'YouTube Shorts'
    ELSE COALESCE(NULLIF(btrim(primary_search_phrase), ''), 'YouTube Shorts')
END
WHERE primary_search_phrase IS NULL
   OR btrim(primary_search_phrase) = '';

ALTER TABLE main.blog_clusters
    ALTER COLUMN primary_search_phrase SET NOT NULL;
