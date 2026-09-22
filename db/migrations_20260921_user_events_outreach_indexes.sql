-- Speed up outreach-emails engagement lookups.
-- Run each CREATE INDEX CONCURRENTLY statement outside a transaction.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_events_outreach_share_link_id
ON main.user_events (
    event_name,
    (NULLIF(metadata->>'share_link_id', '')),
    created_at
)
WHERE event_name IN ('share_view', 'share_play', 'share_cta_click', 'share_watch_progress', 'lead_feed_view');

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_events_outreach_token
ON main.user_events (
    event_name,
    (NULLIF(metadata->>'token', '')),
    created_at
)
WHERE event_name IN ('share_view', 'share_play', 'share_cta_click', 'share_watch_progress')
  AND NULLIF(metadata->>'share_link_id', '') IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_events_outreach_autopilot_lead_id
ON main.user_events (
    event_name,
    (NULLIF(metadata->>'autopilot_lead_id', '')),
    created_at
)
WHERE event_name = 'lead_feed_view';

ANALYZE main.user_events;
