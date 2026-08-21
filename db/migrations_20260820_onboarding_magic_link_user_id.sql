ALTER TABLE main.onboarding_magic_links
    ADD COLUMN IF NOT EXISTS user_id VARCHAR;

CREATE INDEX IF NOT EXISTS idx_onboarding_magic_links_user_id
    ON main.onboarding_magic_links(user_id);
