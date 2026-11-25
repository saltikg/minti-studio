ALTER TABLE main.discovery_leads
    ADD COLUMN IF NOT EXISTS category TEXT,
    ADD COLUMN IF NOT EXISTS is_blocklisted BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE main.discovery_leads AS d
SET category = COALESCE(
    NULLIF((
        SELECT dc.name
        FROM main.keyword_queue kq
        JOIN main.discovery_categories dc ON dc.id = kq.category_id
        WHERE lower(kq.keyword) = lower(COALESCE(d.matched_keyword, ''))
        ORDER BY kq.updated_at DESC NULLS LAST, kq.id DESC
        LIMIT 1
    ), ''),
    NULLIF(d.raw_json->>'category', ''),
    NULLIF(d.raw_json->>'niche', ''),
    ''
);

UPDATE main.discovery_leads AS d
SET is_blocklisted = (
    lower(COALESCE(d.matched_keyword, '')) LIKE ANY (ARRAY[
        '%textile%', '%upcycling%', '%sewing%', '%craft%', '%arts and crafts%',
        '%birdwatching%', '%wildlife%', '%journaling%', '%beekeeping%',
        '%woodworking%', '%cooking%', '%gardening%', '%ai tools%'
    ])
    OR lower(COALESCE(d.category, '')) LIKE ANY (ARRAY[
        '%textile%', '%upcycling%', '%sewing%', '%craft%', '%arts and crafts%',
        '%birdwatching%', '%wildlife%', '%journaling%', '%beekeeping%',
        '%woodworking%', '%cooking%', '%gardening%', '%ai tools%'
    ])
);

CREATE INDEX IF NOT EXISTS idx_discovery_leads_ready_found
    ON main.discovery_leads (
        first_seen_at DESC NULLS LAST,
        last_discovered_at DESC NULLS LAST,
        last_seen_at DESC NULLS LAST,
        id DESC
    )
    WHERE icp_fit IS TRUE
      AND creator_email IS NOT NULL
      AND creator_email <> ''
      AND (autopilot_lead_id IS NULL OR CAST(autopilot_lead_id AS VARCHAR) = '')
      AND (status IS NULL OR status NOT IN ('already_lead', 'dismissed'))
      AND NOT (is_blocklisted IS TRUE);

CREATE INDEX IF NOT EXISTS idx_keyword_queue_keyword_lower
    ON main.keyword_queue (lower(keyword));
