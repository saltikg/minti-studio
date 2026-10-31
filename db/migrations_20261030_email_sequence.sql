CREATE TABLE IF NOT EXISTS main.email_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_normalized VARCHAR NOT NULL,
    user_id VARCHAR,
    list VARCHAR NOT NULL DEFAULT 'signup_sequence',
    status VARCHAR NOT NULL DEFAULT 'subscribed'
        CHECK (status IN ('subscribed', 'unsubscribed')),
    source VARCHAR,
    unsubscribe_token VARCHAR NOT NULL UNIQUE,
    subscribed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    unsubscribed_at TIMESTAMPTZ,
    global_unsubscribed BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (list, email_normalized)
);

CREATE TABLE IF NOT EXISTS main.email_sequence_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence_key VARCHAR NOT NULL DEFAULT 'signup',
    step SMALLINT NOT NULL CHECK (step BETWEEN 1 AND 7),
    delay_days INTEGER NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR,
    UNIQUE (sequence_key, step)
);

CREATE TABLE IF NOT EXISTS main.email_sends (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence_key VARCHAR NOT NULL,
    step SMALLINT NOT NULL,
    user_id VARCHAR NOT NULL,
    email_normalized VARCHAR NOT NULL,
    subject_sent TEXT NOT NULL,
    body_sent TEXT NOT NULL,
    send_token VARCHAR NOT NULL UNIQUE,
    status VARCHAR NOT NULL
        CHECK (status IN ('scheduled', 'processing', 'sent', 'failed', 'skipped')),
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_message_id VARCHAR,
    scheduled_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    clicked_at TIMESTAMPTZ,
    is_test BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_email_sends_sequence_step_user_live
    ON main.email_sends(sequence_key, step, user_id)
    WHERE is_test = FALSE;

CREATE INDEX IF NOT EXISTS idx_email_sends_due
    ON main.email_sends(status, scheduled_at);

INSERT INTO main.email_sequence_steps (sequence_key, step, delay_days, subject, body_text, active)
VALUES
    ('signup', 1, 2,  '', '', FALSE),
    ('signup', 2, 5,  '', '', FALSE),
    ('signup', 3, 9,  '', '', FALSE),
    ('signup', 4, 14, '', '', FALSE),
    ('signup', 5, 21, '', '', FALSE),
    ('signup', 6, 28, '', '', FALSE),
    ('signup', 7, 35, '', '', FALSE)
ON CONFLICT (sequence_key, step) DO NOTHING;
