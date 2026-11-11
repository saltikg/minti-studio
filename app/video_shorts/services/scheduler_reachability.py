"""Read-only scheduler reachability audits and the owned-brand canary.

The audit deliberately evaluates persisted queue rows without invoking any
enqueue/fetch-due function: those functions claim rows or repair stale locks.
Platform-specific due-time gates remain in their owning scheduler modules;
YouTube exposes its full evaluator and the social queues use their persisted
selection fields here because their workers currently select directly in SQL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import json
import logging
import os
from zoneinfo import ZoneInfo

from app.video_shorts.services import tenant_media, youtube_scheduling
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.email_verification import send_resend_email
from app.video_shorts.services.social_queue_identity import queue_identity

logger = logging.getLogger(__name__)
PT = ZoneInfo("America/Los_Angeles")
CANARY_BRAND_NAME = "Hocaefendiden Kisa Kisa"
SOCIAL_TABLES = {
    "instagram": ("shorts_instagram_queue", "instagram_business_account_id", "instagram_media_id"),
    "facebook": ("shorts_facebook_queue", "page_id", "facebook_video_id"),
    "tiktok": ("shorts_tiktok_queue", "tiktok_open_id", "tiktok_video_id"),
}

_last_daily_date = None
_last_monitor_at = None


def _utc(value=None):
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _parse_time(value):
    try:
        return _utc(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _dict_rows(conn, sql, params=None):
    cursor = conn.execute(sql, params or [])
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _brand_names(conn):
    try:
        return {str(row[0]): str(row[1] or "") for row in conn.execute("SELECT id,name FROM shorts_brands").fetchall()}
    except Exception:
        return {}


def _billing_paused(conn, user_id):
    owner = str(user_id or "").partition("::")[0]
    row = conn.execute(
        "SELECT autopilot_billing_paused_at FROM shorts_users WHERE CAST(id AS VARCHAR)=?",
        [owner],
    ).fetchone()
    return bool(row and row[0])


def _token_row(conn, platform, owner, brand):
    token_owner = f"{owner}::{brand}" if owner and brand else ""
    if not token_owner:
        return None
    specs = {
        "instagram": ("instagram_oauth_tokens", "page_access_token,instagram_business_account_id"),
        "facebook": ("facebook_page_tokens", "page_access_token,page_id"),
        "tiktok": ("tiktok_oauth_tokens", "access_token,open_id"),
    }
    table, fields = specs[platform]
    try:
        return conn.execute(f"SELECT {fields} FROM {table} WHERE user_id=? LIMIT 1", [token_owner]).fetchone()
    except Exception:
        return None


def _social_blocker(conn, item):
    platform = item["platform"]
    try:
        owner, brand = queue_identity(item.get("user_id"), item.get("brand_id"))
    except ValueError as exc:
        return str(exc)
    if not brand:
        return "brand identity missing"
    if _billing_paused(conn, owner):
        return "billing_pause"
    target_field = SOCIAL_TABLES[platform][1]
    expected = str(item.get(target_field) or "").strip()
    if not expected:
        return f"missing_{target_field}"
    token = _token_row(conn, platform, owner, brand)
    if not token or not str(token[0] or "").strip():
        return "token:missing"
    if str(token[1] or "").strip() != expected:
        return "channel_mismatch" if platform == "instagram" else "target_mismatch"
    filename = str(item.get("clip_filename") or "").strip()
    if not filename:
        return "missing_file"
    try:
        if not tenant_media.clip_exists(filename, conn=conn):
            return "missing_file"
    except Exception:
        return "file_check_failed"
    return None


def _youtube_token_blocker(conn, item):
    from app.video_shorts.services.youtube_token_store import TOKEN_TABLE

    token_owner = f"{item.get('user_id')}::{item.get('brand_id')}"
    try:
        row = conn.execute(
            f"SELECT youtube_channel_id,COALESCE(reauth_required,0) FROM {TOKEN_TABLE} WHERE user_id=? LIMIT 1",
            [token_owner],
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return "token:missing"
    if bool(row[1]):
        return "token:reauth_required"
    expected = str(item.get("expected_youtube_channel_id") or "").strip()
    if expected and str(row[0] or "").strip() != expected:
        return "channel_mismatch"
    return None


def load_pending_schedules(conn, *, now=None, future_only=True, brand_id=None):
    """Broad, non-mutating inventory independent of each scheduler's due query."""
    now = _utc(now)
    names = _brand_names(conn)
    items = []
    youtube_rows = _dict_rows(
        conn,
        """SELECT * FROM shorts_youtube_schedules
           WHERE status='pending' AND youtube_video_id IS NULL""",
    )
    for row in youtube_rows:
        publish_at = _parse_time(row.get("publish_at"))
        if brand_id and str(row.get("brand_id") or "") != str(brand_id):
            continue
        if future_only and (publish_at is None or publish_at <= now):
            continue
        row.update(platform="youtube", publish_time=publish_at,
                   predicted_enqueue_at=_parse_time(row.get("upload_due_at")),
                   brand_name=names.get(str(row.get("brand_id") or ""), ""))
        items.append(row)
    for platform, (table, target_field, uploaded_field) in SOCIAL_TABLES.items():
        rows = _dict_rows(
            conn,
            f"""SELECT *, {uploaded_field} AS uploaded_marker FROM {table}
                WHERE status IN ('pending','retry','uploading')""",
        )
        for row in rows:
            try:
                owner, resolved_brand = queue_identity(row.get("user_id"), row.get("brand_id"))
                row["user_id"] = owner
                row["brand_id"] = resolved_brand
            except ValueError:
                pass
            publish_at = _parse_time(row.get("publish_at"))
            if brand_id and str(row.get("brand_id") or "") != str(brand_id):
                continue
            if future_only:
                raw_publish_at = str(row.get("publish_at") or "").strip()
                if not raw_publish_at or (publish_at is not None and publish_at <= now):
                    continue
            row.update(platform=platform, publish_time=publish_at,
                       predicted_enqueue_at=publish_at,
                       brand_name=names.get(str(row.get("brand_id") or ""), ""))
            items.append(row)
    return items


def evaluate_item(conn, item):
    """Return a blocker string, or None when the real due path is reachable."""
    due = item.get("predicted_enqueue_at")
    if due is None:
        return "invalid_or_missing_due_time"
    if item["platform"] == "youtube":
        reason = youtube_scheduling.evaluate_enqueue_reachability(conn, item, at_time=due)
        return reason or _youtube_token_blocker(conn, item)
    if str(item.get("status") or "") not in {"pending", "retry"}:
        return f"status:{item.get('status')}"
    if item.get("uploaded_marker"):
        return "already_published_marker_present"
    return _social_blocker(conn, item)


def audit_reachability(*, now=None, conn=None):
    """Audit every future pending schedule. Never writes, claims, or repairs."""
    own = conn is None
    conn = conn or get_db_readonly()
    try:
        results = []
        for item in load_pending_schedules(conn, now=now, future_only=True):
            reason = evaluate_item(conn, item)
            results.append({
                "platform": item["platform"],
                "id": str(item.get("id") or ""),
                "brand_id": str(item.get("brand_id") or ""),
                "brand_name": item.get("brand_name") or "",
                "title": item.get("plan_title") or _youtube_title(item),
                "publish_at": item.get("publish_time"),
                "predicted_enqueue_at": item.get("predicted_enqueue_at"),
                "reachable": reason is None,
                "reason": reason,
            })
        return results
    finally:
        if own:
            conn.close()


def _youtube_title(item):
    try:
        return str((json.loads(item.get("payload_json") or "{}") or {}).get("title") or "")
    except (TypeError, ValueError):
        return ""


def _admin_recipients():
    recipients = youtube_scheduling._admin_recipients()
    fallback = (os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip()
    if "@" in fallback:
        recipients.append(fallback)
    return list(dict.fromkeys(recipients))


def _send_email(subject, lines):
    text = "\n".join(lines)
    body = "<br>".join(html.escape(line) for line in lines)
    for recipient in _admin_recipients():
        try:
            send_resend_email(to_email=recipient, subject=subject, html=f"<p>{body}</p>", text=text,
                              error_message="Scheduler reachability email could not be sent.")
        except Exception:
            logger.exception("Scheduler reachability email failed subject=%s", subject)


def _format_result(item):
    due = item.get("predicted_enqueue_at")
    due_text = due.isoformat() + "Z" if due else "unknown"
    status = "reachable" if item.get("reachable") else f"BLOCKED: {item.get('reason')}"
    return (f"{item['platform']} {item['id']} | {item.get('brand_name') or item.get('brand_id')} | "
            f"enqueue {due_text} | {status}")


def run_reachability_check(*, trigger, now=None):
    results = audit_reachability(now=now)
    blocked = [item for item in results if not item["reachable"]]
    logger.info("Scheduler reachability check trigger=%s pending_future=%s blocked=%s", trigger, len(results), len(blocked))
    for item in results:
        logger.info("Scheduler reachability %s", _format_result(item))
    if blocked:
        _send_email(
            "Scheduler reachability failed",
            [f"Trigger: {trigger}", *[_format_result(item) for item in blocked]],
        )
    return results


def run_startup_check(*, now=None):
    global _last_daily_date
    current = _utc(now)
    results = run_reachability_check(trigger="worker_start", now=current)
    local = current.replace(tzinfo=timezone.utc).astimezone(PT)
    if local.hour >= 5:
        _last_daily_date = local.date()
    return results


def _claim_canary_alert(platform, schedule_id):
    key = f"scheduler_canary:{platform}:{schedule_id}"
    conn = get_db()
    try:
        row = conn.execute(
            """INSERT INTO shorts_schedule_alerts
               (alert_key,schedule_id,platform,alert_type,message,created_at)
               VALUES (?,?,?,'scheduler_canary','Scheduler canary failed',CURRENT_TIMESTAMP)
               ON CONFLICT(alert_key) DO NOTHING RETURNING alert_key""",
            [key, str(schedule_id), platform],
        ).fetchone()
        conn.commit()
        return bool(row)
    finally:
        conn.close()


def check_canary(*, now=None, conn=None, send=True):
    now = _utc(now)
    own = conn is None
    conn = conn or get_db_readonly()
    try:
        brand = conn.execute("SELECT id FROM shorts_brands WHERE name=? LIMIT 1", [CANARY_BRAND_NAME]).fetchone()
        if not brand:
            return []
        late = []
        for item in load_pending_schedules(conn, now=now, future_only=False, brand_id=brand[0]):
            publish_at = item.get("publish_time")
            if not publish_at or publish_at > now - timedelta(minutes=15):
                continue
            if item["platform"] != "youtube" and item.get("uploaded_marker"):
                continue
            reason = evaluate_item(conn, item) or f"status:{item.get('status')}"
            late.append({**item, "reason": reason})
    finally:
        if own:
            conn.close()
    if send:
        for item in late:
            if _claim_canary_alert(item["platform"], item["id"]):
                _send_email("Scheduler canary failed", [
                    f"{item['platform']} {item['id']} was not uploaded within 15 minutes.",
                    f"Blocking reason: {item['reason']}",
                ])
    return late


def process_monitors(*, now=None):
    """Run cheap cadence controls in memory; audit functions remain read-only."""
    global _last_daily_date, _last_monitor_at
    now = _utc(now)
    local = now.replace(tzinfo=timezone.utc).astimezone(PT)
    processed = False
    if local.hour >= 5 and _last_daily_date != local.date():
        run_reachability_check(trigger=f"daily:{local.date().isoformat()}", now=now)
        _last_daily_date = local.date()
        processed = True
    if _last_monitor_at is None or now - _last_monitor_at >= timedelta(minutes=1):
        check_canary(now=now)
        _last_monitor_at = now
    return processed
