ALTER TABLE main.short_share_links
    ADD COLUMN IF NOT EXISTS language VARCHAR;

ALTER TABLE main.onboarding_magic_links
    ADD COLUMN IF NOT EXISTS language VARCHAR;
