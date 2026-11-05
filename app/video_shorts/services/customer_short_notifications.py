from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo


PT = ZoneInfo("America/Los_Angeles")
MAX_PERSONAL_NOTE_LENGTH = 300


def _clean(value: Any) -> str:
    return str(value or "").strip()


def format_pt_date(value: Any) -> str:
    if not value:
        return ""
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if not isinstance(parsed, datetime):
        return str(parsed)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(PT)
    return f"{local:%b} {local.day}, {local:%Y}"


def truncate_subject_title(title: str, limit: int = 58) -> str:
    clean_title = " ".join(_clean(title).split()) or "your video"
    if len(clean_title) <= limit:
        return clean_title
    return clean_title[: max(1, limit - 1)].rstrip() + "…"


def normalize_personal_note(value: Any) -> str:
    note = _clean(value)
    if len(note) > MAX_PERSONAL_NOTE_LENGTH:
        raise ValueError(f"Personal note must be {MAX_PERSONAL_NOTE_LENGTH} characters or fewer.")
    return note


def customer_notify_mailer() -> str:
    mailer = str(os.getenv("CUSTOMER_NOTIFY_MAILER") or "zoho").strip().lower() or "zoho"
    if mailer not in {"zoho", "resend"}:
        raise RuntimeError(f"Unsupported CUSTOMER_NOTIFY_MAILER: {mailer}")
    return mailer


def send_customer_notification_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> Dict[str, Any]:
    """Send customer-ready notifications without changing other mail paths."""
    mailer = customer_notify_mailer()
    if mailer == "resend":
        from app.video_shorts.services.email_verification import send_resend_email

        result = send_resend_email(
            to_email=to_email,
            subject=subject,
            html=html_body,
            text=text_body,
            reply_to_email="info@mintistudio.com",
            error_message="Customer notification email could not be sent.",
        )
    else:
        from app.video_shorts.services.outreach_email_send import send_zoho_email

        try:
            result = send_zoho_email(
                to_email=to_email,
                subject=subject,
                html=html_body,
                text=text_body,
            )
        except Exception as exc:
            raise RuntimeError(f"Zoho SMTP failed: {exc}") from exc
    return {**result, "transport": mailer}


def build_customer_shorts_email(
    *,
    first_name: str,
    video_title: str,
    short_titles: Iterable[str],
    youtube_connected: bool,
    my_videos_url: str,
    personal_note: str = "",
) -> Dict[str, str]:
    titles = [_clean(title) or "Untitled Short" for title in short_titles]
    count = len(titles)
    note = normalize_personal_note(personal_note)
    greeting_name = _clean(first_name) or "there"
    clean_video_title = _clean(video_title) or "Untitled video"
    subject = f"{count} new Shorts are ready from '{truncate_subject_title(clean_video_title)}'"
    connection_copy = (
        "No action needed. They'll go out on your channel on the scheduled dates."
        if youtube_connected
        else "Watch them and download the ones you like. Connect your channel and we'll publish them for you."
    )
    safe_titles = "".join(f"<li style=\"margin:0 0 8px\">{html.escape(title)}</li>" for title in titles)
    note_html = (
        f'<p data-customer-note style="margin:22px 0 0;white-space:pre-wrap">{html.escape(note)}</p>'
        if note
        else '<p data-customer-note style="margin:22px 0 0;white-space:pre-wrap;display:none"></p>'
    )
    email_html = f"""<!doctype html>
<html><body style="margin:0;background:#f5f7f8;color:#17202a;font-family:Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7f8"><tr><td style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px"><tr><td style="padding:28px 24px;font-size:16px;line-height:1.55">
<p style="margin:0 0 18px">Hi {html.escape(greeting_name)},</p>
<p style="margin:0 0 16px">We turned your video '{html.escape(clean_video_title)}' into {count} Shorts. Each one was reviewed by our team for clean starts and endings:</p>
<ul style="margin:0 0 18px;padding-left:22px">{safe_titles}</ul>
<p style="margin:0 0 24px">{html.escape(connection_copy)}</p>
<p style="margin:0 0 24px"><a href="{html.escape(my_videos_url, quote=True)}" style="display:inline-block;background:#14b8a6;color:#ffffff;text-decoration:none;font-weight:700;padding:12px 18px;border-radius:8px">See your Shorts</a></p>
{note_html}
<p style="margin:22px 0 0">Gokhan — Minti Studio</p>
</td></tr></table></td></tr></table>
</body></html>"""
    bullets = "\n".join(f"- {title}" for title in titles)
    note_text = f"\n\n{note}" if note else ""
    text = (
        f"Hi {greeting_name},\n\n"
        f"We turned your video '{clean_video_title}' into {count} Shorts. Each one was reviewed by our team for clean starts and endings:\n\n"
        f"{bullets}\n\n{connection_copy}\n\nSee your Shorts: {my_videos_url}"
        f"{note_text}\n\nGokhan — Minti Studio"
    )
    return {"subject": subject, "html": email_html, "text": text}


def latest_successful_notifications(conn, *, owner_user_id: str, brand_id: str) -> Dict[int, Any]:
    rows = conn.execute(
        """
        SELECT video_pk, MAX(sent_at)
        FROM autopilot_customer_video_notifications
        WHERE CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND NULLIF(trim(coalesce(error, '')), '') IS NULL
        GROUP BY video_pk
        """,
        [owner_user_id, brand_id],
    ).fetchall()
    return {int(row[0]): row[1] for row in rows}


def latest_successful_notification(conn, *, video_pk: int, owner_user_id: str, brand_id: str) -> Optional[Any]:
    row = conn.execute(
        """
        SELECT sent_at
        FROM autopilot_customer_video_notifications
        WHERE video_pk = ?
          AND CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND NULLIF(trim(coalesce(error, '')), '') IS NULL
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        [video_pk, owner_user_id, brand_id],
    ).fetchone()
    return row[0] if row else None


def record_notification_attempt(
    conn,
    *,
    video_pk: int,
    owner_user_id: str,
    brand_id: str,
    recipient: str,
    personal_note: str,
    short_count: int,
    provider_message_id: str = "",
    error: str = "",
) -> str:
    row_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO autopilot_customer_video_notifications (
            id, video_pk, owner_user_id, brand_id, recipient, personal_note,
            short_count, sent_at, provider_message_id, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
        """,
        [
            row_id,
            video_pk,
            owner_user_id,
            brand_id,
            recipient,
            personal_note or None,
            int(short_count),
            provider_message_id or None,
            error or None,
        ],
    )
    return row_id
