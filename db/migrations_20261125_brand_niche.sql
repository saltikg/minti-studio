ALTER TABLE main.shorts_brands
    ADD COLUMN IF NOT EXISTS niche_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_shorts_brands_niche_id'
          AND conrelid = 'main.shorts_brands'::regclass
    ) THEN
        ALTER TABLE main.shorts_brands
            ADD CONSTRAINT fk_shorts_brands_niche_id
            FOREIGN KEY (niche_id) REFERENCES main.niches(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_shorts_brands_niche_id
    ON main.shorts_brands (niche_id);
