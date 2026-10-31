from __future__ import annotations

import os
import secrets
from typing import Any
from urllib.parse import quote
from uuid import uuid4


SIGNUP_SEQUENCE_KEY = "signup"
SIGNUP_SUBSCRIPTION_LIST = "signup_sequence"
SIGNUP_SEQUENCE_DELAYS = (2, 5, 9, 14, 21, 28, 35)


def ensure_email_sequence_schema(conn) -> None:
    backend_name = getattr(conn, "backend_name", "")
    timestamp_type = "TIMESTAMPTZ" if backend_name == "postgres" else "TIMESTAMP"
    now_sql = "now()" if backend_name == "postgres" else "CURRENT_TIMESTAMP"
    uuid_default = "gen_random_uuid()" if backend_name == "postgres" else "uuid()"

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS email_subscriptions (
            id UUID PRIMARY KEY DEFAULT {uuid_default},
            email_normalized VARCHAR NOT NULL,
            user_id VARCHAR,
            list VARCHAR NOT NULL DEFAULT 'signup_sequence',
            status VARCHAR NOT NULL DEFAULT 'subscribed'
                CHECK (status IN ('subscribed', 'unsubscribed')),
            source VARCHAR,
            unsubscribe_token VARCHAR NOT NULL UNIQUE,
            subscribed_at {timestamp_type} NOT NULL DEFAULT {now_sql},
            unsubscribed_at {timestamp_type},
            global_unsubscribed BOOLEAN NOT NULL DEFAULT FALSE,
            UNIQUE (list, email_normalized)
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS email_sequence_steps (
            id UUID PRIMARY KEY DEFAULT {uuid_default},
            sequence_key VARCHAR NOT NULL DEFAULT 'signup',
            step SMALLINT NOT NULL CHECK (step BETWEEN 1 AND 7),
            delay_days INTEGER NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            body_text TEXT NOT NULL DEFAULT '',
            active BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at {timestamp_type} NOT NULL DEFAULT {now_sql},
            updated_by VARCHAR,
            UNIQUE (sequence_key, step)
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS email_sends (
            id UUID PRIMARY KEY DEFAULT {uuid_default},
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
            scheduled_at {timestamp_type},
            sent_at {timestamp_type},
            clicked_at {timestamp_type},
            is_test BOOLEAN NOT NULL DEFAULT FALSE,
            created_at {timestamp_type} NOT NULL DEFAULT {now_sql}
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_email_sends_sequence_step_user_live
        ON email_sends(sequence_key, step, user_id)
        WHERE is_test = FALSE
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_email_sends_due
        ON email_sends(status, scheduled_at)
        """
    )
    for step, delay_days in enumerate(SIGNUP_SEQUENCE_DELAYS, start=1):
        conn.execute(
            """
            INSERT INTO email_sequence_steps (
                id, sequence_key, step, delay_days, subject, body_text, active
            )
            VALUES (?, 'signup', ?, ?, '', '', FALSE)
            ON CONFLICT (sequence_key, step) DO NOTHING
            """,
            [str(uuid4()), step, delay_days],
        )


def normalize_sequence_email(value: object) -> str:
    return str(value or "").strip().lower()


def render_sequence_placeholders(text: object, *, name: object = "") -> str:
    resolved_name = str(name or "").strip() or "there"
    first_name = resolved_name.split()[0] if resolved_name else "there"
    return str(text or "").replace("{name}", resolved_name).replace("{first_name}", first_name)


def unsubscribe_url(*, base_url: str, token: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/email/unsubscribe/{quote(str(token or '').strip(), safe='')}"


def sequence_email_headers(*, unsubscribe_link: str) -> dict[str, str]:
    mailto = "mailto:info@mintistudio.com?subject=unsubscribe"
    return {
        "List-Unsubscribe": f"<{unsubscribe_link}>, <{mailto}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def render_sequence_email(
    *,
    subject: object,
    body_text: object,
    recipient_name: object,
    unsubscribe_link: str,
    postal_address: str | None = None,
) -> dict[str, str]:
    address = str(postal_address if postal_address is not None else os.getenv("COMPANY_POSTAL_ADDRESS") or "").strip()
    rendered_subject = render_sequence_placeholders(subject, name=recipient_name)
    rendered_body = render_sequence_placeholders(body_text, name=recipient_name).rstrip()
    footer_lines = ["---", f"Unsubscribe: {unsubscribe_link}"]
    if address:
        footer_lines.append(address)
    return {
        "subject": rendered_subject,
        "body_text": f"{rendered_body}\n\n" + "\n".join(footer_lines),
    }


def new_email_token() -> str:
    return secrets.token_urlsafe(32)


def load_signup_sequence_steps(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, step, delay_days, subject, body_text, active, updated_at, updated_by
        FROM email_sequence_steps
        WHERE sequence_key = 'signup'
        ORDER BY step
        """
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "step": int(row[1]),
            "delay_days": int(row[2]),
            "subject": str(row[3] or ""),
            "body_text": str(row[4] or ""),
            "active": bool(row[5]),
            "updated_at": row[6],
            "updated_by": str(row[7] or ""),
        }
        for row in rows
    ]
