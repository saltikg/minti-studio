CREATE TABLE IF NOT EXISTS shorts_kb_generations (
  id UUID PRIMARY KEY,
  owner_user_id TEXT,
  source_video_pk INTEGER NOT NULL,
  source_entry_key TEXT,
  source_video_id TEXT,
  source_plan_index TEXT,
  source_short_video_id TEXT,
  source_title TEXT,
  source_published_at TIMESTAMP,
  generation_status TEXT DEFAULT 'draft',
  main_question TEXT,
  short_answer TEXT,
  transcript_summary TEXT,
  source_video_url TEXT,
  generated_with_model TEXT,
  raw_payload_json TEXT,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_kb_generations_source
  ON shorts_kb_generations(source_video_pk);

CREATE INDEX IF NOT EXISTS idx_shorts_kb_generations_owner
  ON shorts_kb_generations(owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS shorts_kb_similar_questions (
  id UUID PRIMARY KEY,
  generation_id UUID NOT NULL,
  question TEXT NOT NULL,
  sort_order INTEGER DEFAULT 0,
  decision TEXT DEFAULT 'pending',
  page_id UUID,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_shorts_kb_similar_generation
  ON shorts_kb_similar_questions(generation_id, sort_order);

CREATE TABLE IF NOT EXISTS shorts_kb_pages (
  id UUID PRIMARY KEY,
  owner_user_id TEXT,
  source_video_pk INTEGER NOT NULL,
  generation_id UUID,
  similar_question_id UUID,
  page_type TEXT NOT NULL,
  status TEXT DEFAULT 'draft',
  question TEXT NOT NULL,
  answer TEXT,
  transcript_summary TEXT,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_shorts_kb_pages_source
  ON shorts_kb_pages(source_video_pk, page_type);

CREATE INDEX IF NOT EXISTS idx_shorts_kb_pages_status
  ON shorts_kb_pages(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS shorts_kb_source_reviews (
  source_entry_key TEXT PRIMARY KEY,
  owner_user_id TEXT,
  is_relevant BOOLEAN DEFAULT true,
  updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_shorts_kb_source_reviews_relevant
  ON shorts_kb_source_reviews(is_relevant, updated_at DESC);

ALTER TABLE shorts_kb_generations ADD COLUMN IF NOT EXISTS source_entry_key TEXT;
ALTER TABLE shorts_kb_generations ADD COLUMN IF NOT EXISTS source_video_id TEXT;
ALTER TABLE shorts_kb_generations ADD COLUMN IF NOT EXISTS source_plan_index TEXT;
ALTER TABLE shorts_kb_generations ADD COLUMN IF NOT EXISTS source_short_video_id TEXT;
ALTER TABLE shorts_kb_generations ADD COLUMN IF NOT EXISTS source_title TEXT;
ALTER TABLE shorts_kb_generations ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMP;

CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_kb_generations_entry_key
  ON shorts_kb_generations(source_entry_key);
