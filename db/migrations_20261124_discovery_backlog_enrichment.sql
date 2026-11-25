ALTER TABLE main.discovery_leads
    ADD COLUMN IF NOT EXISTS email_enrichment_lane VARCHAR;

CREATE TABLE IF NOT EXISTS main.discovery_backlog_enrichment_runs (
    id VARCHAR PRIMARY KEY,
    slot_key VARCHAR NOT NULL UNIQUE,
    date_pt DATE NOT NULL,
    scheduled_for TIMESTAMP,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status VARCHAR NOT NULL DEFAULT 'running',
    claimed INTEGER NOT NULL DEFAULT 0,
    enriched INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    categories TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_discovery_backlog_enrichment_runs_date
    ON main.discovery_backlog_enrichment_runs (date_pt, started_at);
