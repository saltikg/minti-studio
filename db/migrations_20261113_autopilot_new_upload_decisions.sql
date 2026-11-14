BEGIN;

ALTER TABLE main.autopilot_channel_videos
    ADD COLUMN IF NOT EXISTS decided_by VARCHAR;
ALTER TABLE main.autopilot_channel_videos
    ADD COLUMN IF NOT EXISTS decided_at TIMESTAMP;
ALTER TABLE main.autopilot_channel_videos
    ADD COLUMN IF NOT EXISTS decision_reason TEXT;

ALTER TABLE main.autopilot_leads
    ADD COLUMN IF NOT EXISTS youtube_channel_connected_at TIMESTAMP;

ALTER TABLE main.youtube_oauth_tokens_v2
    ADD COLUMN IF NOT EXISTS connected_at TEXT;

UPDATE main.youtube_oauth_tokens_v2
SET connected_at = updated_at
WHERE NULLIF(trim(COALESCE(connected_at, '')), '') IS NULL
  AND NULLIF(trim(COALESCE(updated_at, '')), '') IS NOT NULL;

UPDATE main.autopilot_leads
SET youtube_channel_connected_at = converted_at
WHERE youtube_channel_connected_at IS NULL
  AND converted_at IS NOT NULL
  AND youtube_channel_id LIKE 'UC%'
  AND LENGTH(youtube_channel_id) = 24;

WITH latest_leads AS (
    SELECT DISTINCT ON (CAST(user_id AS VARCHAR), CAST(brand_id AS VARCHAR))
           CAST(user_id AS VARCHAR) AS user_id,
           CAST(brand_id AS VARCHAR) AS brand_id,
           converted_at,
           youtube_channel_connected_at
    FROM main.autopilot_leads
    WHERE converted_at IS NOT NULL
    ORDER BY CAST(user_id AS VARCHAR), CAST(brand_id AS VARCHAR), converted_at DESC, id DESC
), customer_cutoffs AS (
    SELECT CAST(u.id AS VARCHAR) AS user_id,
           CAST(b.id AS VARCHAR) AS brand_id,
           GREATEST(
               u.service_mode_chosen_at,
               l.converted_at,
               CASE
                   WHEN t.youtube_channel_id LIKE 'UC%' AND LENGTH(t.youtube_channel_id) = 24
                       THEN COALESCE(NULLIF(t.connected_at, '')::timestamp, NULLIF(t.updated_at, '')::timestamp)
                   ELSE COALESCE(l.youtube_channel_connected_at, l.converted_at)
               END
           ) AS eligible_after
    FROM main.shorts_users u
    JOIN latest_leads l ON l.user_id = CAST(u.id AS VARCHAR)
    JOIN main.shorts_brands b
      ON CAST(b.id AS VARCHAR) = l.brand_id
     AND CAST(b.owner_user_id AS VARCHAR) = CAST(u.id AS VARCHAR)
    LEFT JOIN main.youtube_oauth_tokens_v2 t
      ON t.user_id = CAST(u.id AS VARCHAR) || '::' || CAST(b.id AS VARCHAR)
    WHERE lower(COALESCE(u.service_mode, '')) = 'autopilot'
)
UPDATE main.autopilot_channel_videos cv
SET status = 'skipped',
    decided_by = 'system',
    decided_at = CURRENT_TIMESTAMP,
    decision_reason = 'published before autopilot eligibility start'
FROM customer_cutoffs c
WHERE CAST(cv.user_id AS VARCHAR) = c.user_id
  AND CAST(cv.brand_id AS VARCHAR) = c.brand_id
  AND (cv.published_at IS NULL OR cv.published_at < c.eligible_after)
  AND cv.status <> 'skipped';

UPDATE main.autopilot_channel_videos cv
SET status = 'processed',
    decided_by = 'system',
    decided_at = CURRENT_TIMESTAMP,
    decision_reason = 'already imported'
WHERE cv.status = 'new'
  AND EXISTS (
      SELECT 1
      FROM main.youtube_videos yv
      WHERE CAST(yv.owner_user_id AS VARCHAR) = CAST(cv.user_id AS VARCHAR)
        AND CAST(yv.brand_id AS VARCHAR) = CAST(cv.brand_id AS VARCHAR)
        AND yv.video_id = cv.youtube_video_id
        AND yv.deleted_at IS NULL
  );

COMMIT;
