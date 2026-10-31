ALTER TABLE main.email_sequence_steps
    ADD COLUMN IF NOT EXISTS language VARCHAR NOT NULL DEFAULT 'en';

ALTER TABLE main.email_sequence_steps
    DROP CONSTRAINT IF EXISTS email_sequence_steps_sequence_key_step_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_email_sequence_steps_key_step_language
    ON main.email_sequence_steps(sequence_key, step, language);

INSERT INTO main.email_sequence_steps (
    sequence_key, step, delay_days, subject, body_text, active, updated_at, updated_by, language
)
SELECT
    sequence_key, step, delay_days, '', '', FALSE, now(), NULL, 'tr'
FROM main.email_sequence_steps
WHERE sequence_key = 'signup' AND language = 'en'
ON CONFLICT (sequence_key, step, language) DO NOTHING;

UPDATE main.email_sequence_steps target
SET delay_days = source.delay_days
FROM main.email_sequence_steps source
WHERE target.sequence_key = 'signup' AND target.language = 'tr'
  AND source.sequence_key = target.sequence_key
  AND source.step = target.step AND source.language = 'en'
  AND target.delay_days <> source.delay_days;

ALTER TABLE main.email_subscriptions
    ADD COLUMN IF NOT EXISTS language VARCHAR NOT NULL DEFAULT 'en';

ALTER TABLE main.email_sends
    ADD COLUMN IF NOT EXISTS language VARCHAR;

UPDATE main.email_sends
SET language = 'en'
WHERE language IS NULL;
