ALTER TABLE main.discovery_leads
ADD COLUMN IF NOT EXISTS is_clinician BOOLEAN NOT NULL DEFAULT FALSE;

-- Recompute existing values with scripts/backfill_discovery_clinician_flags.py.
-- The application matcher intentionally uses case-sensitive, contextual rules
-- that are too strict to duplicate with the former broad SQL expression.
