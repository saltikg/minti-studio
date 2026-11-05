ALTER TABLE main.autopilot_customer_video_notifications
    ADD COLUMN IF NOT EXISTS click_token VARCHAR,
    ADD COLUMN IF NOT EXISTS first_clicked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS click_count INTEGER NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_autopilot_customer_notifications_click_token
ON main.autopilot_customer_video_notifications(click_token)
WHERE click_token IS NOT NULL;
