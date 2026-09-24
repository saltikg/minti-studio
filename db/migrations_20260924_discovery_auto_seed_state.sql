-- Track the once-per-Pacific-day scheduled keyword auto-generation attempt.

ALTER TABLE main.discovery_automation_control
  ADD COLUMN IF NOT EXISTS last_keyword_auto_generated_at TIMESTAMP NULL;
