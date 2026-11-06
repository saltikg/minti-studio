CREATE TABLE IF NOT EXISTS main.autopilot_monthly_reports (
    id VARCHAR PRIMARY KEY,
    owner_user_id VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    month DATE NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'sent')),
    s3_key TEXT NOT NULL,
    recipient TEXT NOT NULL,
    headline TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMPTZ,
    provider_message_id TEXT,
    error TEXT,
    tracking_token VARCHAR NOT NULL UNIQUE,
    first_clicked_at TIMESTAMPTZ,
    click_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (owner_user_id, brand_id, month)
);

CREATE INDEX IF NOT EXISTS idx_autopilot_monthly_reports_tracking
    ON main.autopilot_monthly_reports (tracking_token);

CREATE TABLE IF NOT EXISTS main.autopilot_monthly_report_sends (
    id VARCHAR PRIMARY KEY,
    report_id VARCHAR NOT NULL REFERENCES main.autopilot_monthly_reports(id),
    recipient TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    provider_message_id TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_autopilot_monthly_report_sends_report
    ON main.autopilot_monthly_report_sends (report_id, sent_at DESC);
