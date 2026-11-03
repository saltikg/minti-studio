ALTER TABLE main.discovery_leads
    ADD COLUMN IF NOT EXISTS email_enrichment_job_id VARCHAR;

CREATE INDEX IF NOT EXISTS idx_discovery_leads_email_enrichment_job_id
    ON main.discovery_leads (email_enrichment_job_id);
