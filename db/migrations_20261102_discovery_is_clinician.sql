ALTER TABLE main.discovery_leads
ADD COLUMN IF NOT EXISTS is_clinician BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE main.discovery_leads
SET is_clinician = TRUE
WHERE COALESCE(channel_title, '') || ' ' || COALESCE(channel_description, '')
      ~* '(^|[^a-z0-9])(dr\.?|doctor|m\.?d\.?|d\.?o\.?|dpt|pt|rd|rdn|np|pa-c|pharmd|dnp|rn|lcsw|psyd)([^a-z0-9]|$)';
