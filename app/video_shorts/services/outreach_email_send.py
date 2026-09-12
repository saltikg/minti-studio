from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from flask import current_app

from app.video_shorts.services.db import get_db, table_columns
from app.video_shorts.services.email_verification import send_resend_email
from app.video_shorts.services.outreach_email_templates import (
    normalize_outreach_template_language,
    normalize_outreach_template_stage,
    render_outreach_clipboard_text,
    render_outreach_email,
)
from app.video_shorts.services.trial_copy import DEFAULT_SHARE_TRIAL_DAYS, normalize_trial_days


SCHEDULED_OUTREACH_STATUSES_ACTIVE = {"scheduled", "processing"}
SCHEDULED_OUTREACH_MAX_ATTEMPTS = 3
SCHEDULED_OUTREACH_BACKOFF_MINUTES = 5
OUTREACH_RESEND_GUARD_MINUTES = 10


def ensure_outreach_scheduled_email_schema(conn) -> None:
    backend_name = getattr(conn, "backend_name", "")
    id_sql = "BIGSERIAL PRIMARY KEY" if backend_name == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    scheduled_at_sql = "TIMESTAMPTZ" if backend_name == "postgres" else "TIMESTAMP"
    timestamp_sql = "TIMESTAMPTZ" if backend_name == "postgres" else "TIMESTAMP"
    now_sql = "now()" if backend_name == "postgres" else "CURRENT_TIMESTAMP"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS outreach_scheduled_emails (
            id {id_sql},
            share_link_id BIGINT NOT NULL,
            stage VARCHAR NOT NULL,
            language VARCHAR NOT NULL,
            scheduled_at {scheduled_at_sql} NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'scheduled',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT {SCHEDULED_OUTREACH_MAX_ATTEMPTS},
            provider_message_id VARCHAR,
            error TEXT,
            sent_at {timestamp_sql},
            created_at {timestamp_sql} NOT NULL DEFAULT {now_sql},
            updated_at {timestamp_sql} NOT NULL DEFAULT {now_sql},
            created_by VARCHAR
        )
        """
    )
    cols = table_columns(conn, "outreach_scheduled_emails")
    for col_name, col_type in (
        ("share_link_id", "BIGINT"),
        ("stage", "VARCHAR"),
        ("language", "VARCHAR"),
        ("scheduled_at", scheduled_at_sql),
        ("status", "VARCHAR"),
        ("attempts", "INTEGER DEFAULT 0"),
        ("max_attempts", f"INTEGER DEFAULT {SCHEDULED_OUTREACH_MAX_ATTEMPTS}"),
        ("provider_message_id", "VARCHAR"),
        ("error", "TEXT"),
        ("sent_at", timestamp_sql),
        ("created_at", timestamp_sql),
        ("updated_at", timestamp_sql),
        ("created_by", "VARCHAR"),
    ):
        if col_name in cols:
            continue
        conn.execute(f"ALTER TABLE outreach_scheduled_emails ADD COLUMN {col_name} {col_type}")
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outreach_scheduled_due
            ON outreach_scheduled_emails(status, scheduled_at)
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outreach_scheduled_share_stage
            ON outreach_scheduled_emails(share_link_id, stage, status)
            """
        )
    except Exception:
        pass


def _share_public_url(token: str) -> str:
    base_url = (current_app.config.get("BASE_URL") or "https://mintistudio.com").rstrip("/")
    return f"{base_url}/w/{token}"


def resend_sender_domain_verified(sender_email: str) -> bool:
    sender_domain = str(sender_email or "").strip().lower().rsplit("@", 1)[-1]
    api_key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not sender_domain or not api_key:
        return False
    try:
        response = requests.get(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=8,
        )
        if response.status_code >= 400:
            current_app.logger.warning("Could not verify Resend sender domain %s: status=%s", sender_domain, response.status_code)
            return False
        payload = response.json()
    except Exception:
        current_app.logger.exception("Could not verify Resend sender domain %s", sender_domain)
        return False
    domains = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(domains, list):
        return False
    for domain in domains:
        if not isinstance(domain, dict):
            continue
        if str(domain.get("name") or "").strip().lower() != sender_domain:
            continue
        status = str(domain.get("status") or "").strip().lower()
        return status in {"verified", "success", "active"}
    return False


def render_share_link_outreach_email(conn, share_link_id: int, *, stage: object, language: object) -> dict[str, Any]:
    normalized_stage = normalize_outreach_template_stage(stage)
    normalized_language = normalize_outreach_template_language(language)
    row = conn.execute(
        """
        SELECT
          sl.id,
          sl.token,
          sl.recipient_name,
          sl.recipient_email,
          sl.language,
          COALESCE(sl.trial_days, ?) AS trial_days,
          sl.emailed_at,
          sl.first_email_template_key,
          sl.followup_sent,
          sl.followup_sent_at,
          sl.followup_template_key,
          COALESCE(sl.archived, false) AS archived
        FROM short_share_links sl
        WHERE sl.id = ?
        LIMIT 1
        """,
        [DEFAULT_SHARE_TRIAL_DAYS, share_link_id],
    ).fetchone()
    if not row:
        raise LookupError("not_found")
    token = str(row[1] or "").strip()
    if not token:
        raise ValueError("missing_share_token")
    recipient_email = str(row[3] or "").strip()
    if not recipient_email or "@" not in recipient_email:
        raise ValueError("missing_recipient_email")
    if bool(row[11]):
        raise ValueError("share_link_archived")
    share_url = _share_public_url(token)
    trial_days = normalize_trial_days(row[5], default=DEFAULT_SHARE_TRIAL_DAYS)
    rendered_email = render_outreach_email(
        stage=normalized_stage,
        language=normalized_language,
        recipient_name=row[2],
        share_url=share_url,
        trial_days=trial_days,
    )
    return {
        "row": row,
        "recipient_email": recipient_email,
        "recipient_name": str(row[2] or "").strip(),
        "share_url": share_url,
        "trial_days": trial_days,
        "email": rendered_email,
        "clipboard_text": render_outreach_clipboard_text(
            stage=normalized_stage,
            language=normalized_language,
            recipient_name=row[2],
            share_url=share_url,
            trial_days=trial_days,
        ),
        "emailed_at": row[6],
        "first_email_template_key": str(row[7] or "").strip(),
        "followup_sent_at": row[9],
        "followup_template_key": str(row[10] or "").strip(),
    }


def _as_utc(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def send_share_link_outreach_email(
    conn,
    *,
    share_link_id: int,
    stage: object,
    language: object,
    confirm_resend: bool = False,
) -> dict[str, Any]:
    normalized_stage = normalize_outreach_template_stage(stage)
    rendered = render_share_link_outreach_email(conn, share_link_id, stage=normalized_stage, language=language)
    rendered_email = rendered["email"]
    template_key = rendered_email["key"]
    previous_sent_at = rendered["followup_sent_at"] if normalized_stage == "followup" else rendered["emailed_at"]
    previous_template_key = rendered["followup_template_key"] if normalized_stage == "followup" else rendered["first_email_template_key"]
    sent_at_utc = _as_utc(previous_sent_at)
    if (
        sent_at_utc
        and previous_template_key == template_key
        and not confirm_resend
        and sent_at_utc >= (datetime.now(timezone.utc) - timedelta(minutes=OUTREACH_RESEND_GUARD_MINUTES))
    ):
        return {
            "ok": False,
            "error": "recently_sent",
            "requires_confirmation": True,
            "sent_at": previous_sent_at,
            "template_key": template_key,
        }

    requested_from_email = "info@mintistudio.com"
    verified_info_sender = resend_sender_domain_verified(requested_from_email)
    outreach_from_email = requested_from_email if verified_info_sender else ""
    send_result = send_resend_email(
        to_email=rendered["recipient_email"],
        subject=rendered_email["subject"],
        html=rendered_email["html"],
        text=rendered_email["text"],
        from_display_name="Gokhan Saltik",
        from_email=outreach_from_email,
        reply_to_email="info@mintistudio.com",
        error_message="Outreach email could not be sent.",
    )
    provider_message_id = str(send_result.get("request_id") or "").strip()
    if normalized_stage == "followup":
        conn.execute(
            """
            UPDATE short_share_links
               SET followup_sent = TRUE,
                   followup_sent_at = CURRENT_TIMESTAMP,
                   followup_provider_message_id = ?,
                   followup_template_key = ?
             WHERE id = ?
            """,
            [provider_message_id or None, template_key, share_link_id],
        )
    else:
        conn.execute(
            """
            UPDATE short_share_links
               SET emailed_at = COALESCE(emailed_at, CURRENT_TIMESTAMP),
                   first_email_provider_message_id = ?,
                   first_email_template_key = ?
             WHERE id = ?
            """,
            [provider_message_id or None, template_key, share_link_id],
        )
    return {
        "ok": True,
        "stage": normalized_stage,
        "language": rendered_email["language"],
        "template_key": template_key,
        "provider_message_id": provider_message_id,
        "from_email": requested_from_email if verified_info_sender else os.getenv("MAIL_FROM", ""),
        "info_sender_verified": verified_info_sender,
    }


def schedule_outreach_email(
    conn,
    *,
    share_link_id: int,
    stage: object,
    language: object,
    scheduled_at: datetime,
    created_by: str = "",
) -> dict[str, Any]:
    ensure_outreach_scheduled_email_schema(conn)
    normalized_stage = normalize_outreach_template_stage(stage)
    normalized_language = normalize_outreach_template_language(language)
    conn.execute(
        """
        UPDATE outreach_scheduled_emails
           SET status = 'cancelled',
               error = 'replaced_by_new_schedule',
               updated_at = CURRENT_TIMESTAMP
         WHERE share_link_id = ?
           AND stage = ?
           AND status IN ('scheduled', 'processing')
        """,
        [share_link_id, normalized_stage],
    )
    conn.execute(
        """
        INSERT INTO outreach_scheduled_emails (
            share_link_id,
            stage,
            language,
            scheduled_at,
            status,
            attempts,
            max_attempts,
            created_by,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 'scheduled', 0, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        [
            share_link_id,
            normalized_stage,
            normalized_language,
            scheduled_at,
            SCHEDULED_OUTREACH_MAX_ATTEMPTS,
            created_by or None,
        ],
    )
    row = conn.execute(
        """
        SELECT id, share_link_id, stage, language, scheduled_at, status
        FROM outreach_scheduled_emails
        WHERE share_link_id = ? AND stage = ? AND status = 'scheduled'
        ORDER BY id DESC
        LIMIT 1
        """,
        [share_link_id, normalized_stage],
    ).fetchone()
    return {
        "id": int(row[0]),
        "share_link_id": int(row[1]),
        "stage": str(row[2] or ""),
        "language": str(row[3] or ""),
        "scheduled_at": row[4],
        "status": str(row[5] or ""),
    }


def cancel_scheduled_outreach_email(conn, *, schedule_id: int, share_link_id: int) -> bool:
    ensure_outreach_scheduled_email_schema(conn)
    updated = conn.execute(
        """
        UPDATE outreach_scheduled_emails
           SET status = 'cancelled',
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
           AND share_link_id = ?
           AND status = 'scheduled'
        """,
        [schedule_id, share_link_id],
    )
    return getattr(updated, "rowcount", 0) > 0


def claim_due_scheduled_outreach_email(conn) -> dict[str, Any] | None:
    ensure_outreach_scheduled_email_schema(conn)
    if getattr(conn, "backend_name", "") == "postgres":
        row = conn.execute(
            """
            UPDATE outreach_scheduled_emails
               SET status = 'processing',
                   updated_at = now()
             WHERE id = (
                 SELECT id
                 FROM outreach_scheduled_emails
                 WHERE status = 'scheduled'
                   AND scheduled_at <= now()
                 ORDER BY scheduled_at ASC, id ASC
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
             )
             RETURNING id, share_link_id, stage, language, attempts, max_attempts
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id, share_link_id, stage, language, attempts, max_attempts
            FROM outreach_scheduled_emails
            WHERE status = 'scheduled'
              AND scheduled_at <= CURRENT_TIMESTAMP
            ORDER BY scheduled_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE outreach_scheduled_emails
                   SET status = 'processing',
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                   AND status = 'scheduled'
                """,
                [row[0]],
            )
    if not row:
        return None
    conn.commit()
    return {
        "id": int(row[0]),
        "share_link_id": int(row[1]),
        "stage": str(row[2] or "first"),
        "language": str(row[3] or "EN"),
        "attempts": int(row[4] or 0),
        "max_attempts": int(row[5] or SCHEDULED_OUTREACH_MAX_ATTEMPTS),
    }


def mark_scheduled_outreach_sent(conn, *, schedule_id: int, provider_message_id: str = "") -> None:
    conn.execute(
        """
        UPDATE outreach_scheduled_emails
           SET status = 'sent',
               provider_message_id = ?,
               sent_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP,
               error = NULL
         WHERE id = ?
        """,
        [provider_message_id or None, schedule_id],
    )


def mark_scheduled_outreach_failed_or_retry(conn, *, job: dict[str, Any], error: str) -> str:
    attempts = int(job.get("attempts") or 0) + 1
    max_attempts = int(job.get("max_attempts") or SCHEDULED_OUTREACH_MAX_ATTEMPTS)
    if attempts < max_attempts:
        conn.execute(
            """
            UPDATE outreach_scheduled_emails
               SET status = 'scheduled',
                   attempts = ?,
                   scheduled_at = CURRENT_TIMESTAMP + INTERVAL '5 minutes',
                   error = ?,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """
            if getattr(conn, "backend_name", "") == "postgres"
            else """
            UPDATE outreach_scheduled_emails
               SET status = 'scheduled',
                   attempts = ?,
                   scheduled_at = datetime(CURRENT_TIMESTAMP, '+5 minutes'),
                   error = ?,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            [attempts, error[:2000], job["id"]],
        )
        return "scheduled"
    conn.execute(
        """
        UPDATE outreach_scheduled_emails
           SET status = 'failed',
               attempts = ?,
               error = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
        """,
        [attempts, error[:2000], job["id"]],
    )
    return "failed"


def mark_scheduled_outreach_failed(conn, *, job: dict[str, Any], error: str) -> None:
    attempts = int(job.get("attempts") or 0) + 1
    conn.execute(
        """
        UPDATE outreach_scheduled_emails
           SET status = 'failed',
               attempts = ?,
               error = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
        """,
        [attempts, error[:2000], job["id"]],
    )


def process_due_scheduled_outreach_email() -> bool:
    conn = get_db()
    try:
        job = claim_due_scheduled_outreach_email(conn)
        if not job:
            return False
        try:
            result = send_share_link_outreach_email(
                conn,
                share_link_id=job["share_link_id"],
                stage=job["stage"],
                language=job["language"],
                confirm_resend=False,
            )
            if not result.get("ok"):
                if result.get("error") == "recently_sent":
                    mark_scheduled_outreach_failed(conn, job=job, error="recently_sent_guard_prevented_duplicate")
                    conn.commit()
                    return True
                raise RuntimeError(str(result.get("error") or "scheduled_send_not_sent"))
            mark_scheduled_outreach_sent(conn, schedule_id=job["id"], provider_message_id=str(result.get("provider_message_id") or ""))
            conn.commit()
            return True
        except Exception as exc:
            try:
                mark_scheduled_outreach_failed_or_retry(conn, job=job, error=str(exc) or exc.__class__.__name__)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return True
    finally:
        conn.close()
