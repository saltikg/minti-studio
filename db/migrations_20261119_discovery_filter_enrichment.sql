ALTER TABLE main.discovery_leads
    ADD COLUMN IF NOT EXISTS already_posting_shorts BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS last_upload_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS email_enrichment_retry_count INTEGER NOT NULL DEFAULT 0;

UPDATE main.discovery_leads
SET already_posting_shorts = COALESCE(shorts_last_15d, 0) > 8;

CREATE INDEX IF NOT EXISTS idx_discovery_leads_email_retry
    ON main.discovery_leads (status, enrichment_attempted_at, email_enrichment_retry_count)
    WHERE creator_email IS NULL OR creator_email = '';
