CREATE TABLE IF NOT EXISTS shorts_clip_covers (
  id VARCHAR PRIMARY KEY,
  generated_video_id BIGINT NOT NULL UNIQUE REFERENCES shorts_generated_videos(id) ON DELETE CASCADE,
  owner_user_id VARCHAR NOT NULL,
  brand_id VARCHAR NOT NULL,
  video_pk BIGINT NOT NULL REFERENCES youtube_videos(id) ON DELETE CASCADE,
  plan_index INTEGER NOT NULL,
  clip_filename VARCHAR NOT NULL,
  storage_key TEXT,
  cover_text TEXT,
  highlight_phrase TEXT,
  cover_template VARCHAR NOT NULL DEFAULT 'clean',
  kicker_text TEXT,
  main_text TEXT,
  bold_highlight_text TEXT,
  candidate_frames_json JSONB,
  selected_timestamp DOUBLE PRECISION,
  used_face_crop BOOLEAN NOT NULL DEFAULT FALSE,
  status VARCHAR NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'ready', 'failed')),
  error TEXT,
  generation_ms INTEGER,
  segmentation_ms INTEGER,
  segmentation_confidence DOUBLE PRECISION,
  peak_memory_mb DOUBLE PRECISION,
  size_bytes BIGINT,
  generated_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_shorts_clip_covers_tenant
  ON shorts_clip_covers(owner_user_id, brand_id, video_pk);
