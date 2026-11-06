ALTER TABLE main.facebook_page_tokens
    ADD COLUMN IF NOT EXISTS insights_status VARCHAR,
    ADD COLUMN IF NOT EXISTS insights_error TEXT,
    ADD COLUMN IF NOT EXISTS insights_status_updated_at TIMESTAMPTZ;

UPDATE main.facebook_page_tokens
SET insights_status = 'needs_reconnect',
    insights_error = 'Facebook permission read_insights is missing. Reconnect Facebook.',
    insights_status_updated_at = now()
WHERE NOT ('read_insights' = ANY(string_to_array(COALESCE(scopes, ''), ',')));

UPDATE main.facebook_page_tokens
SET insights_status = COALESCE(insights_status, 'active'),
    insights_status_updated_at = COALESCE(insights_status_updated_at, now())
WHERE insights_status IS NULL;
