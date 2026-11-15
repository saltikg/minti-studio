-- Speed up the Admin Share Links loader without replacing the September indexes.
-- Run each CREATE INDEX CONCURRENTLY statement outside a transaction.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_events_share_links_free_tool_lead_id
ON main.user_events (
    (NULLIF(metadata->>'lead_id', '')),
    created_at
)
WHERE event_name IN ('free_tool_landing_view', 'free_tool_submit', 'free_tool_claim');

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_events_share_links_v2_share_link_id
ON main.user_events (
    event_name,
    (NULLIF(metadata->>'share_link_id', '')),
    created_at
)
WHERE event_name IN (
    'share_view',
    'share_play',
    'share_cta_click',
    'share_watch_progress',
    'share_yes_intent',
    'share_video_loaded',
    'share_video_error',
    'lead_feed_view',
    'lead_feed_watch_start',
    'lead_feed_item_active',
    'lead_feed_card_seen',
    'lead_feed_trial_cta_click'
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_events_share_links_v2_token
ON main.user_events (
    event_name,
    (NULLIF(metadata->>'token', '')),
    created_at
)
WHERE event_name IN (
    'share_view',
    'share_play',
    'share_cta_click',
    'share_watch_progress',
    'share_yes_intent',
    'share_video_loaded',
    'share_video_error',
    'lead_feed_view',
    'lead_feed_watch_start',
    'lead_feed_item_active',
    'lead_feed_card_seen',
    'lead_feed_trial_cta_click'
)
  AND NULLIF(metadata->>'share_link_id', '') IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_events_share_links_v2_autopilot_lead_id
ON main.user_events (
    event_name,
    (NULLIF(metadata->>'autopilot_lead_id', '')),
    created_at
)
WHERE event_name IN (
    'share_view',
    'share_play',
    'share_cta_click',
    'share_watch_progress',
    'share_yes_intent',
    'share_video_loaded',
    'share_video_error',
    'lead_feed_view',
    'lead_feed_watch_start',
    'lead_feed_item_active',
    'lead_feed_card_seen',
    'lead_feed_trial_cta_click'
);

ANALYZE main.user_events;
