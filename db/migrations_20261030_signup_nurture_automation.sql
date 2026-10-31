ALTER TABLE main.email_subscriptions
    ADD COLUMN IF NOT EXISTS enrolled_at TIMESTAMPTZ;

ALTER TABLE main.email_subscriptions
    ADD COLUMN IF NOT EXISTS enrolled_by VARCHAR;

ALTER TABLE main.email_subscriptions
    ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ;

ALTER TABLE main.email_sends
    ADD COLUMN IF NOT EXISTS link_urls TEXT NOT NULL DEFAULT '[]';

CREATE INDEX IF NOT EXISTS idx_email_subscriptions_nurture_eligible
    ON main.email_subscriptions(list, status, enrolled_at)
    WHERE removed_at IS NULL;
