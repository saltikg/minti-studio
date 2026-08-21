CREATE TABLE IF NOT EXISTS main.onboarding_magic_links (
    id BIGSERIAL PRIMARY KEY,
    token_hash VARCHAR NOT NULL,
    recipient_email VARCHAR NOT NULL,
    recipient_name VARCHAR,
    share_link_id BIGINT,
    share_link_token VARCHAR,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_onboarding_magic_links_token_hash
    ON main.onboarding_magic_links(token_hash);

CREATE INDEX IF NOT EXISTS idx_onboarding_magic_links_recipient_email
    ON main.onboarding_magic_links(recipient_email);

CREATE INDEX IF NOT EXISTS idx_onboarding_magic_links_share_link_id
    ON main.onboarding_magic_links(share_link_id);
