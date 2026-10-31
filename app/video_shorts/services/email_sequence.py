from __future__ import annotations

import os
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo


SIGNUP_SEQUENCE_KEY = "signup"
SIGNUP_SUBSCRIPTION_LIST = "signup_sequence"
SIGNUP_SEQUENCE_DELAYS = (2, 5, 9, 14, 21, 28, 35)
NURTURE_MAX_ATTEMPTS = 3
NURTURE_BACKOFF_MINUTES = 5
NURTURE_PACIFIC_ZONE = ZoneInfo("America/Los_Angeles")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+")


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
            enrolled_at {timestamp_type},
            enrolled_by VARCHAR,
            removed_at {timestamp_type},
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
            link_urls TEXT NOT NULL DEFAULT '[]',
            is_test BOOLEAN NOT NULL DEFAULT FALSE,
            created_at {timestamp_type} NOT NULL DEFAULT {now_sql}
        )
        """
    )
    conn.execute(f"ALTER TABLE email_subscriptions ADD COLUMN IF NOT EXISTS enrolled_at {timestamp_type}")
    conn.execute("ALTER TABLE email_subscriptions ADD COLUMN IF NOT EXISTS enrolled_by VARCHAR")
    conn.execute(f"ALTER TABLE email_subscriptions ADD COLUMN IF NOT EXISTS removed_at {timestamp_type}")
    if backend_name == "postgres":
        conn.execute("ALTER TABLE email_sends ADD COLUMN IF NOT EXISTS link_urls TEXT NOT NULL DEFAULT '[]'")
    else:
        conn.execute("ALTER TABLE email_sends ADD COLUMN IF NOT EXISTS link_urls TEXT DEFAULT '[]'")
    if backend_name == "postgres":
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_email_sends_sequence_step_user_live
            ON email_sends(sequence_key, step, user_id)
            WHERE is_test = FALSE
            """
        )
    else:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_email_sends_sequence_step_user_live
            ON email_sends(sequence_key, step, user_id)
            """
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_email_sends_due
        ON email_sends(status, scheduled_at)
        """
    )
    eligibility_where = " WHERE removed_at IS NULL" if backend_name == "postgres" else ""
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_email_subscriptions_nurture_eligible
        ON email_subscriptions(list, status, enrolled_at){eligibility_where}
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


def render_sequence_email(
    *,
    subject: object,
    body_text: object,
    recipient_name: object,
) -> dict[str, str]:
    rendered_subject = render_sequence_placeholders(subject, name=recipient_name)
    return {
        "subject": rendered_subject,
        "body_text": render_sequence_placeholders(body_text, name=recipient_name),
    }


def new_email_token() -> str:
    return secrets.token_urlsafe(32)


def nurture_enabled() -> bool:
    return str(os.getenv("NURTURE_ENABLED") or "false").strip().lower() in {"1", "true", "yes", "on"}


def nurture_daily_cap() -> int:
    try:
        return max(1, int(os.getenv("NURTURE_DAILY_CAP") or "40"))
    except (TypeError, ValueError):
        return 40


def rewrite_nurture_links(
    body_text: str,
    *,
    send_token: str,
    unsubscribe_link: str,
    base_url: str = "https://mintistudio.com",
) -> tuple[str, list[str]]:
    links: list[str] = []
    clean_unsubscribe = str(unsubscribe_link or "").strip()

    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;:!?)":
            trailing = url[-1] + trailing
            url = url[:-1]
        if not url or url == clean_unsubscribe:
            return match.group(0)
        links.append(url)
        tracked = f"{str(base_url or '').rstrip('/')}/e/c/{quote(send_token, safe='')}/{len(links)}"
        return tracked + trailing

    return _HTTP_URL_RE.sub(replace, str(body_text or "")), links


def _pacific_day_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    now_pt = now_utc.astimezone(NURTURE_PACIFIC_ZONE)
    start_pt = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_pt.astimezone(timezone.utc), (start_pt + timedelta(days=1)).astimezone(timezone.utc)


def _claim_due_nurture_send(conn, *, now_utc: datetime) -> dict[str, Any] | None:
    day_start, day_end = _pacific_day_bounds(now_utc)
    sent_today = conn.execute(
        """
        SELECT COUNT(*) FROM email_sends
        WHERE sequence_key = 'signup' AND is_test = FALSE AND status = 'sent'
          AND sent_at >= ? AND sent_at < ?
        """,
        [day_start, day_end],
    ).fetchone()
    if int(sent_today[0] or 0) >= nurture_daily_cap():
        return None
    retry = conn.execute(
        """
        UPDATE email_sends
        SET status = 'processing', attempts = attempts + 1
        WHERE id = (
            SELECT id FROM email_sends
            WHERE sequence_key = 'signup' AND is_test = FALSE
              AND status = 'scheduled' AND scheduled_at <= CURRENT_TIMESTAMP
              AND attempts < ?
            ORDER BY scheduled_at, created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, step, user_id, email_normalized, subject_sent, body_sent,
                  send_token, attempts, link_urls
        """,
        [NURTURE_MAX_ATTEMPTS],
    ).fetchone()
    if retry:
        conn.commit()
        return {
            "id": str(retry[0]), "step": int(retry[1]), "user_id": str(retry[2]),
            "email": str(retry[3]), "subject": str(retry[4]), "body": str(retry[5]),
            "send_token": str(retry[6]), "attempts": int(retry[7]),
            "link_urls": str(retry[8] or "[]"),
        }

    reserved_today = conn.execute(
        """
        SELECT COUNT(*) FROM email_sends
        WHERE sequence_key = 'signup' AND is_test = FALSE
          AND (
            (status = 'sent' AND sent_at >= ? AND sent_at < ?)
            OR (status IN ('scheduled', 'processing') AND created_at >= ? AND created_at < ?)
          )
        """,
        [day_start, day_end, day_start, day_end],
    ).fetchone()
    if int(reserved_today[0] or 0) >= nurture_daily_cap():
        return None

    row = conn.execute(
        """
        SELECT s.id, s.user_id, s.email_normalized, u.name,
               st.step, st.subject, st.body_text, st.active, s.unsubscribe_token
        FROM email_subscriptions s
        JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(s.user_id AS VARCHAR)
        JOIN email_sequence_steps st ON st.sequence_key = 'signup'
        WHERE s.list = 'signup_sequence' AND s.enrolled_at IS NOT NULL
          AND s.removed_at IS NULL AND s.status = 'subscribed'
          AND COALESCE(s.global_unsubscribed, FALSE) = FALSE
          AND CURRENT_TIMESTAMP >= s.enrolled_at + (st.delay_days * INTERVAL '1 day')
          AND NOT EXISTS (
              SELECT 1 FROM email_sends es
              WHERE es.sequence_key = 'signup' AND es.step = st.step
                AND CAST(es.user_id AS VARCHAR) = CAST(s.user_id AS VARCHAR)
                AND es.is_test = FALSE
          )
          AND NOT EXISTS (
              SELECT 1 FROM email_sequence_steps earlier
              WHERE earlier.sequence_key = 'signup' AND earlier.step < st.step
                AND NOT EXISTS (
                    SELECT 1 FROM email_sends prior
                    WHERE prior.sequence_key = 'signup' AND prior.step = earlier.step
                      AND CAST(prior.user_id AS VARCHAR) = CAST(s.user_id AS VARCHAR)
                      AND prior.is_test = FALSE
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM email_sends today
              WHERE today.sequence_key = 'signup' AND today.is_test = FALSE
                AND CAST(today.user_id AS VARCHAR) = CAST(s.user_id AS VARCHAR)
                AND (
                  (today.status = 'sent' AND today.sent_at >= ? AND today.sent_at < ?)
                  OR (today.status IN ('scheduled', 'processing')
                      AND today.created_at >= ? AND today.created_at < ?)
                )
          )
        ORDER BY s.enrolled_at + (st.delay_days * INTERVAL '1 day'), st.step
        FOR UPDATE OF s SKIP LOCKED
        LIMIT 1
        """,
        [day_start, day_end, day_start, day_end],
    ).fetchone()
    if not row:
        return None

    _subscription_id, user_id, email, name, step, subject, body_text, active, _token = row
    send_id = str(uuid4())
    send_token = new_email_token()
    if not bool(active) or not str(subject or "").strip() or not str(body_text or "").strip():
        conn.execute(
            """
            INSERT INTO email_sends (
                id, sequence_key, step, user_id, email_normalized, subject_sent,
                body_sent, send_token, status, error, attempts, scheduled_at,
                is_test, created_at
            ) VALUES (?, 'signup', ?, ?, ?, ?, ?, ?, 'skipped', ?, 0,
                      CURRENT_TIMESTAMP, FALSE, CURRENT_TIMESTAMP)
            """,
            [send_id, step, str(user_id), str(email), str(subject or ""), str(body_text or ""),
             send_token, "inactive_or_empty_step"],
        )
        conn.commit()
        return {"skipped": True, "id": send_id}

    rendered = render_sequence_email(
        subject=subject,
        body_text=body_text,
        recipient_name=name,
    )
    tracked_body, link_urls = rewrite_nurture_links(
        rendered["body_text"],
        send_token=send_token,
        unsubscribe_link="",
        base_url=os.getenv("BASE_URL") or "https://mintistudio.com",
    )
    conn.execute(
        """
        INSERT INTO email_sends (
            id, sequence_key, step, user_id, email_normalized, subject_sent,
            body_sent, send_token, status, attempts, scheduled_at, link_urls,
            is_test, created_at
        ) VALUES (?, 'signup', ?, ?, ?, ?, ?, ?, 'processing', 1,
                  CURRENT_TIMESTAMP, ?, FALSE, CURRENT_TIMESTAMP)
        """,
        [send_id, step, str(user_id), str(email), rendered["subject"], tracked_body,
         send_token, json.dumps(link_urls)],
    )
    conn.commit()
    return {
        "id": send_id, "step": int(step), "user_id": str(user_id), "email": str(email),
        "subject": rendered["subject"], "body": tracked_body, "send_token": send_token,
        "attempts": 1,
    }


def process_due_signup_nurture_email() -> bool:
    if not nurture_enabled():
        return False
    now_utc = datetime.now(timezone.utc)
    if now_utc.astimezone(NURTURE_PACIFIC_ZONE).hour < 9:
        return False

    from app.video_shorts.services.db import get_db
    from app.video_shorts.services.outreach_email_send import is_recipient_declined, send_zoho_email

    conn = get_db()
    try:
        job = _claim_due_nurture_send(conn, now_utc=now_utc)
        if not job:
            return False
        if job.get("skipped"):
            return True
        eligible = conn.execute(
            """
            SELECT 1 FROM email_subscriptions
            WHERE list = 'signup_sequence' AND CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND enrolled_at IS NOT NULL AND removed_at IS NULL
              AND status = 'subscribed' AND COALESCE(global_unsubscribed, FALSE) = FALSE
            LIMIT 1
            """,
            [job["user_id"]],
        ).fetchone()
        if not eligible:
            conn.execute(
                "UPDATE email_sends SET status = 'skipped', error = 'subscription_ineligible' WHERE id = ?",
                [job["id"]],
            )
            conn.commit()
            return True
        if is_recipient_declined(conn, job["email"]):
            conn.execute(
                "UPDATE email_sends SET status = 'skipped', error = 'recipient_declined' WHERE id = ?",
                [job["id"]],
            )
            conn.commit()
            return True
        try:
            result = send_zoho_email(
                to_email=job["email"], subject=job["subject"], text=job["body"],
            )
        except Exception as exc:
            next_status = "scheduled" if int(job["attempts"]) < NURTURE_MAX_ATTEMPTS else "failed"
            conn.execute(
                """
                UPDATE email_sends
                SET status = ?, error = ?, scheduled_at = CURRENT_TIMESTAMP + (? * INTERVAL '1 minute')
                WHERE id = ?
                """,
                [next_status, str(exc)[:2000], NURTURE_BACKOFF_MINUTES, job["id"]],
            )
            conn.commit()
            return True
        conn.execute(
            """
            UPDATE email_sends
            SET status = 'sent', provider_message_id = ?, sent_at = CURRENT_TIMESTAMP, error = NULL
            WHERE id = ?
            """,
            [str(result.get("message_id") or result.get("request_id") or ""), job["id"]],
        )
        conn.commit()
        return True
    finally:
        conn.close()


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
