ALTER TABLE main.discovery_leads
    ADD COLUMN IF NOT EXISTS face_check_verdict VARCHAR,
    ADD COLUMN IF NOT EXISTS face_check_presenter_type VARCHAR,
    ADD COLUMN IF NOT EXISTS face_check_same_person_count INTEGER,
    ADD COLUMN IF NOT EXISTS face_check_reason TEXT,
    ADD COLUMN IF NOT EXISTS face_checked_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_discovery_leads_face_check_verdict
    ON main.discovery_leads (face_check_verdict);
