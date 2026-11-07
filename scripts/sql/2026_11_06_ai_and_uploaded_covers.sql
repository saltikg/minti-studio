ALTER TABLE shorts_clip_covers ADD COLUMN IF NOT EXISTS source VARCHAR;
ALTER TABLE shorts_clip_covers ADD COLUMN IF NOT EXISTS generated_storage_key TEXT;
ALTER TABLE shorts_clip_covers ADD COLUMN IF NOT EXISTS generated_source VARCHAR;
ALTER TABLE shorts_clip_covers ADD COLUMN IF NOT EXISTS ai_notice TEXT;
ALTER TABLE shorts_clip_covers ADD COLUMN IF NOT EXISTS ai_path VARCHAR;

CREATE TABLE IF NOT EXISTS shorts_cover_ai_calls (
  id VARCHAR PRIMARY KEY,
  owner_user_id VARCHAR NOT NULL,
  brand_id VARCHAR NOT NULL,
  generated_video_id BIGINT NOT NULL REFERENCES shorts_generated_videos(id) ON DELETE CASCADE,
  model VARCHAR NOT NULL,
  prompt_version VARCHAR NOT NULL,
  request_kind VARCHAR NOT NULL,
  result_path VARCHAR NOT NULL,
  image_returned BOOLEAN NOT NULL DEFAULT FALSE,
  latency_ms INTEGER NOT NULL,
  cost_usd DOUBLE PRECISION,
  cost_estimated BOOLEAN NOT NULL DEFAULT TRUE,
  input_tokens INTEGER,
  output_tokens INTEGER,
  error TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cover_ai_cap
  ON shorts_cover_ai_calls(brand_id, created_at, image_returned);
