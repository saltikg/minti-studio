ALTER TABLE IF EXISTS main.shorts_storage_assets
ADD COLUMN IF NOT EXISTS brand_id VARCHAR;

ALTER TABLE IF EXISTS main.shorts_storage_assets
ADD COLUMN IF NOT EXISTS label VARCHAR;

CREATE INDEX IF NOT EXISTS idx_shorts_storage_assets_brand_id
ON main.shorts_storage_assets(brand_id);
