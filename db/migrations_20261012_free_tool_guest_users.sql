ALTER TABLE main.shorts_users
  ADD COLUMN IF NOT EXISTS is_guest BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE main.shorts_users
  ADD COLUMN IF NOT EXISTS guest_created_at TIMESTAMPTZ;

ALTER TABLE main.shorts_users
  ADD COLUMN IF NOT EXISTS guest_ip_hash TEXT;

ALTER TABLE main.shorts_users
  ADD COLUMN IF NOT EXISTS guest_render_count INT NOT NULL DEFAULT 0;

ALTER TABLE main.shorts_users
  ADD COLUMN IF NOT EXISTS guest_video_count INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_shorts_users_guest_created_at
  ON main.shorts_users (is_guest, guest_created_at);
