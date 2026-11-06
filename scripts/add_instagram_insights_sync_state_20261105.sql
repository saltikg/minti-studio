ALTER TABLE main.shorts_instagram_queue
    ADD COLUMN IF NOT EXISTS insights_status VARCHAR,
    ADD COLUMN IF NOT EXISTS insights_status_reason TEXT,
    ADD COLUMN IF NOT EXISTS insights_status_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_instagram_queue_insights_sync
    ON main.shorts_instagram_queue (insights_status, published_at)
    WHERE instagram_media_id IS NOT NULL AND status = 'published';

CREATE TABLE IF NOT EXISTS main.instagram_metrics_alert_state (
    alert_key VARCHAR PRIMARY KEY,
    last_sent_at TIMESTAMPTZ NOT NULL,
    summary_json JSONB
);

UPDATE main.shorts_instagram_queue
SET insights_status = 'unsyncable',
    insights_status_reason = 'missing_brand_id: Instagram queue row has no stored brand_id.',
    insights_status_updated_at = now()
WHERE instagram_media_id IS NOT NULL
  AND status = 'published'
  AND brand_id IS NULL
  AND COALESCE(insights_status, 'active') <> 'unsyncable';

UPDATE main.shorts_instagram_queue AS queue
SET insights_status = 'unsyncable',
    insights_status_reason = concat(
        'credential_account_mismatch: media account ',
        COALESCE(queue.instagram_business_account_id, '<missing>'),
        ' does not match current account ',
        COALESCE(token.instagram_business_account_id, '<missing>'),
        '.'
    ),
    insights_status_updated_at = now()
FROM main.instagram_oauth_tokens AS token
WHERE queue.instagram_media_id IS NOT NULL
  AND queue.status = 'published'
  AND queue.brand_id IS NOT NULL
  AND token.user_id = concat(split_part(queue.user_id, '::', 1), '::', queue.brand_id)
  AND COALESCE(queue.instagram_business_account_id, '')
      <> COALESCE(token.instagram_business_account_id, '')
  AND COALESCE(queue.insights_status, 'active') <> 'unsyncable';
