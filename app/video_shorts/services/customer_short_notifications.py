from __future__ import annotations

import html
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo


PT = ZoneInfo("America/Los_Angeles")
MAX_PERSONAL_NOTE_LENGTH = 300
PENDING_DELIVERY_STATUSES = {"pending", "queued", "retry", "uploading", "scheduled", "uploaded"}


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


def format_pt_short_date(value: Any) -> str:
    parsed = _as_utc_datetime(value)
    if not parsed:
        return ""
    local = parsed.astimezone(PT)
    return f"{local:%b} {local.day}"


def _as_utc_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(parsed, datetime):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _matches_short(short: Dict[str, Any], row: Dict[str, Any]) -> bool:
    short_filename = _clean(short.get("clip_filename"))
    row_filename = _clean(row.get("clip_filename"))
    if short_filename and row_filename:
        return short_filename == row_filename
    short_plan_index = _clean(short.get("plan_index"))
    row_plan_index = _clean(row.get("plan_index"))
    return bool(short_plan_index and row_plan_index and short_plan_index == row_plan_index)


def load_video_short_delivery_items(
    conn,
    *,
    video_pk: int,
    source_video_id: str,
    owner_user_id: str,
    brand_id: str,
) -> list[Dict[str, Any]]:
    """Load tenant-scoped publish state for every durable Short, across all platforms."""
    rows = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(trim(gv.generated_title), ''), NULLIF(trim(gv.clip_filename), ''), 'Untitled Short'),
            gv.clip_filename,
            gv.raw_plan_entry_json,
            gv.publish_status,
            gv.planned_publish_at,
            gv.published_at,
            gv.youtube_published_at,
            gv.instagram_published_at,
            gv.facebook_published_at,
            gv.tiktok_published_at
        FROM shorts_generated_videos gv
        JOIN shorts_storage_assets render_asset
          ON render_asset.file_key = gv.storage_file_key
         AND CAST(render_asset.user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
         AND render_asset.brand_id IS NOT DISTINCT FROM gv.brand_id
         AND lower(coalesce(render_asset.status, 'active')) = 'active'
         AND render_asset.size_bytes > 0
        JOIN youtube_videos source_video
          ON source_video.video_id = gv.source_video_id
         AND source_video.owner_user_id = gv.user_id
         AND source_video.brand_id IS NOT DISTINCT FROM gv.brand_id
         AND source_video.deleted_at IS NULL
        WHERE source_video.id = ?
          AND CAST(gv.user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND gv.brand_id IS NOT DISTINCT FROM ?
          AND NULLIF(trim(coalesce(gv.clip_filename, '')), '') IS NOT NULL
          AND NULLIF(trim(coalesce(gv.storage_file_key, '')), '') IS NOT NULL
          AND lower(coalesce(gv.generation_status, '')) NOT IN ('pending', 'queued', 'processing', 'rendering')
        ORDER BY gv.created_at, gv.id
        """,
        [video_pk, owner_user_id, brand_id],
    ).fetchall()
    shorts = []
    for row in rows:
        raw_entry = _json_object(row[2])
        shorts.append({
            "title": _clean(row[0]) or "Untitled Short",
            "clip_filename": _clean(row[1]),
            "plan_index": _clean(raw_entry.get("plan_index")),
            "publish_status": _clean(row[3]).lower(),
            "planned_publish_at": row[4],
            "published": any(row[index] is not None for index in range(5, 10)) or _clean(row[3]).lower() == "published",
        })

    social_rows: list[Dict[str, Any]] = []
    for table_name in ("shorts_instagram_queue", "shorts_facebook_queue", "shorts_tiktok_queue"):
        queue_rows = conn.execute(
            f"""
            SELECT clip_filename, plan_index, status, publish_at, published_at
            FROM {table_name}
            WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND brand_id IS NOT DISTINCT FROM ?
              AND video_id = ?
              AND lower(coalesce(status, '')) NOT IN ('cancelled', 'canceled')
            """,
            [owner_user_id, brand_id, source_video_id],
        ).fetchall()
        social_rows.extend({
            "clip_filename": row[0], "plan_index": row[1], "status": _clean(row[2]).lower(),
            "publish_at": row[3], "published_at": row[4],
        } for row in queue_rows)

    youtube_rows = conn.execute(
        """
        SELECT payload_json, status, publish_at, youtube_video_id
        FROM shorts_youtube_schedules
        WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND (
              CAST(payload_json AS JSON) ->> 'video_pk' = CAST(? AS VARCHAR)
              OR asset_key LIKE ?
          )
          AND lower(coalesce(status, '')) NOT IN ('cancelled', 'canceled')
        """,
        [owner_user_id, brand_id, video_pk, f"clip:{video_pk}:%"],
    ).fetchall()
    now_utc = datetime.now(timezone.utc)
    for row in youtube_rows:
        payload = _json_object(row[0])
        youtube_row = {
            "clip_filename": payload.get("filename"), "plan_index": payload.get("plan_index"),
            "status": _clean(row[1]).lower(), "publish_at": row[2], "youtube_video_id": row[3],
        }
        for short in shorts:
            if not _matches_short(short, youtube_row):
                continue
            publish_at = _as_utc_datetime(youtube_row.get("publish_at"))
            if youtube_row["status"] == "published" or (
                youtube_row["status"] == "uploaded"
                and bool(youtube_row.get("youtube_video_id"))
                and (publish_at is None or publish_at <= now_utc)
            ):
                short["published"] = True
            elif youtube_row["status"] in PENDING_DELIVERY_STATUSES and publish_at:
                short.setdefault("schedule_candidates", []).append(publish_at)

    for short in shorts:
        for social_row in social_rows:
            if not _matches_short(short, social_row):
                continue
            if social_row["status"] == "published" or social_row.get("published_at"):
                short["published"] = True
            elif social_row["status"] in PENDING_DELIVERY_STATUSES:
                publish_at = _as_utc_datetime(social_row.get("publish_at"))
                if publish_at:
                    short.setdefault("schedule_candidates", []).append(publish_at)
        planned = _as_utc_datetime(short.get("planned_publish_at"))
        if short.get("publish_status") in PENDING_DELIVERY_STATUSES and planned:
            short.setdefault("schedule_candidates", []).append(planned)
        if short["published"]:
            short.update(state="published", scheduled_at=None)
        elif short.get("schedule_candidates"):
            short.update(state="scheduled", scheduled_at=min(short["schedule_candidates"]))
        else:
            short.update(state="unscheduled", scheduled_at=None)
        short.pop("schedule_candidates", None)
    return shorts


def short_delivery_label(item: Dict[str, Any]) -> str:
    title = _clean(item.get("title")) or "Untitled Short"
    if item.get("state") == "published":
        return f"{title} · Live"
    if item.get("state") == "scheduled":
        date_label = format_pt_short_date(item.get("scheduled_at"))
        return f"{title} · {date_label}" if date_label else title
    return title


def connected_delivery_sentence(items: Iterable[Dict[str, Any]]) -> str:
    states = [_clean(item.get("state")).lower() for item in items]
    total = len(states)
    live_count = states.count("published")
    scheduled_count = states.count("scheduled")
    unscheduled_count = total - live_count - scheduled_count
    if total and live_count == total:
        return "They're already live on your channel."
    if total and scheduled_count == total:
        return "No action needed. They'll go out on your channel on the scheduled dates."
    if not unscheduled_count and live_count and scheduled_count:
        return f"{live_count} are already live, and the other {scheduled_count} will go out on the scheduled dates."
    if unscheduled_count and live_count:
        return f"{live_count} are already live. No action needed. We'll schedule the unscheduled Shorts on your channel shortly."
    return "No action needed. We'll schedule them on your channel shortly."


def truncate_subject_title(title: str, limit: int = 58) -> str:
    clean_title = " ".join(_clean(title).split()) or "your video"
    if len(clean_title) <= limit:
        return clean_title
    return clean_title[: max(1, limit - 1)].rstrip() + "…"


def shorten_video_title_for_email(title: str, limit: int = 70) -> str:
    clean_title = " ".join(_clean(title).split()) or "Untitled video"
    clean_title = clean_title.split(" | ", 1)[0].rstrip()
    if len(clean_title) <= limit:
        return clean_title
    candidate = clean_title[: max(1, limit - 1)].rstrip()
    if " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0].rstrip()
    return f"{candidate or clean_title[: max(1, limit - 1)].rstrip()}…"


def normalize_personal_note(value: Any) -> str:
    note = _clean(value)
    if len(note) > MAX_PERSONAL_NOTE_LENGTH:
        raise ValueError(f"Personal note must be {MAX_PERSONAL_NOTE_LENGTH} characters or fewer.")
    return note


def generate_click_token() -> str:
    return secrets.token_urlsafe(32)


def customer_notify_mailer() -> str:
    mailer = str(os.getenv("CUSTOMER_NOTIFY_MAILER") or "zoho").strip().lower() or "zoho"
    if mailer not in {"zoho", "resend"}:
        raise RuntimeError(f"Unsupported CUSTOMER_NOTIFY_MAILER: {mailer}")
    return mailer


def customer_notify_from_name() -> str:
    return " ".join(str(os.getenv("CUSTOMER_NOTIFY_FROM_NAME") or "Minti Studio").split()) or "Minti Studio"


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
                from_name=customer_notify_from_name(),
            )
        except Exception as exc:
            raise RuntimeError(f"Zoho SMTP failed: {exc}") from exc
    return {**result, "transport": mailer}


def build_customer_shorts_email(
    *,
    first_name: str,
    video_title: str,
    short_titles: Iterable[Any],
    youtube_connected: bool,
    tracking_url: str,
    personal_note: str = "",
) -> Dict[str, str]:
    items = [
        item if isinstance(item, dict) else {"title": _clean(item) or "Untitled Short", "state": "unscheduled"}
        for item in short_titles
    ]
    count = len(items)
    note = normalize_personal_note(personal_note)
    greeting_name = _clean(first_name) or "there"
    clean_video_title = shorten_video_title_for_email(video_title)
    subject = f"{count} new Shorts are ready"
    connection_copy = (
        connected_delivery_sentence(items)
        if youtube_connected
        else "Watch them and download the ones you like. Connect your channel and we'll publish them for you."
    )
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
<p style="margin:0 0 10px">Your video is now {count} Shorts:</p>
<div style="margin:0 0 20px;padding:8px 0 8px 14px;border-left:3px solid #cbd5e1;color:#334155"><strong>{html.escape(clean_video_title)}</strong></div>
<p style="margin:0 0 24px">{html.escape(connection_copy)}</p>
<p style="margin:0 0 24px"><a href="{html.escape(tracking_url, quote=True)}" style="display:inline-block;background:#14b8a6;color:#ffffff;text-decoration:none;font-weight:700;padding:12px 18px;border-radius:8px">Watch your Shorts</a></p>
{note_html}
<p style="margin:22px 0 0">— The Minti Studio team</p>
</td></tr></table></td></tr></table>
</body></html>"""
    note_text = f"\n\n{note}" if note else ""
    text = (
        f"Hi {greeting_name},\n\n"
        f"Your video is now {count} Shorts:\n\n"
        f"{clean_video_title}\n\n"
        f"{connection_copy}\n\nWatch your Shorts: {tracking_url}"
        f"{note_text}\n\n— The Minti Studio team"
    )
    return {"subject": subject, "html": email_html, "text": text}


def latest_successful_notifications(conn, *, owner_user_id: str, brand_id: str) -> Dict[int, Any]:
    rows = conn.execute(
        """
        SELECT video_pk, sent_at, first_clicked_at, click_count
        FROM (
            SELECT video_pk, sent_at, first_clicked_at, click_count,
                   ROW_NUMBER() OVER (PARTITION BY video_pk ORDER BY sent_at DESC) AS row_number
            FROM autopilot_customer_video_notifications
            WHERE CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND NULLIF(trim(coalesce(error, '')), '') IS NULL
        ) ranked
        WHERE row_number = 1
        """,
        [owner_user_id, brand_id],
    ).fetchall()
    return {
        int(row[0]): {"sent_at": row[1], "first_clicked_at": row[2], "click_count": int(row[3] or 0)}
        for row in rows
    }


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
    click_token: str = "",
) -> str:
    row_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO autopilot_customer_video_notifications (
            id, video_pk, owner_user_id, brand_id, recipient, personal_note,
            short_count, sent_at, provider_message_id, error, click_token
        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
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
            click_token or None,
        ],
    )
    return row_id
