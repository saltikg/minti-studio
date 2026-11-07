ALTER TABLE shorts_clip_covers
  ADD COLUMN IF NOT EXISTS cover_template VARCHAR NOT NULL DEFAULT 'clean',
  ADD COLUMN IF NOT EXISTS kicker_text TEXT,
  ADD COLUMN IF NOT EXISTS main_text TEXT,
  ADD COLUMN IF NOT EXISTS bold_highlight_text TEXT,
  ADD COLUMN IF NOT EXISTS segmentation_ms INTEGER,
  ADD COLUMN IF NOT EXISTS segmentation_confidence DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS peak_memory_mb DOUBLE PRECISION;

ALTER TABLE shorts_clip_covers
  DROP CONSTRAINT IF EXISTS shorts_clip_covers_cover_template_check;

ALTER TABLE shorts_clip_covers
  ADD CONSTRAINT shorts_clip_covers_cover_template_check
  CHECK (cover_template IN ('bold', 'clean'));
