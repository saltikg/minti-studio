CREATE TABLE IF NOT EXISTS main.shorts_clip_mutation_leases (
    user_id VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    asset_key VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    lease_token VARCHAR NOT NULL,
    lease_until TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, brand_id, asset_key)
);

CREATE INDEX IF NOT EXISTS idx_shorts_clip_mutation_leases_until
    ON main.shorts_clip_mutation_leases (lease_until);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'minti_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE main.shorts_clip_mutation_leases TO minti_user;
    END IF;
END
$$;
