"""One scheduling boundary for every YouTube publisher.

The database owns the schedule, retry state and resumable session. Plan files
are UI projections only. Never log provider exceptions or resumable URLs.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import logging
import os
import re
import socket
from pathlib import Path
from uuid import uuid4

from flask import g, has_app_context
from httplib2 import HttpLib2Error

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.publish_target_guard import (
    PublishTargetMismatch,
    require_target_match,
    send_target_mismatch_alert,
)

logger = logging.getLogger(__name__)
TABLE = "shorts_youtube_schedules"
MUTATION_LEASE_TABLE = "shorts_clip_mutation_leases"
MUTATION_LEASE_MINUTES = 30
HISTORY_TABLE = "shorts_schedule_status_history"
ALERTS_TABLE = "shorts_schedule_alerts"
SCHEDULER_STATE_TABLE = "shorts_scheduler_state"
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shorts_youtube_schedules (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    expected_youtube_channel_id VARCHAR,
    asset_key VARCHAR NOT NULL,
    payload_json TEXT NOT NULL,
    status VARCHAR NOT NULL,
    publish_at TIMESTAMP,
    upload_due_at TIMESTAMP NOT NULL,
    youtube_video_id VARCHAR,
    resumable_uri TEXT,
    job_id VARCHAR,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    notification TEXT,
    notified_at TIMESTAMP,
    created_at TIMESTAMP,
    created_by VARCHAR,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, brand_id, asset_key)
)
"""
HISTORY_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
    id BIGINT PRIMARY KEY,
    schedule_id VARCHAR NOT NULL,
    platform VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    reason TEXT,
    error_sanitized TEXT,
    actor VARCHAR,
    metadata_json TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
MUTATION_LEASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shorts_clip_mutation_leases (
    user_id VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    asset_key VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    lease_token VARCHAR NOT NULL,
    lease_until TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, brand_id, asset_key)
)
"""


class YouTubeScheduleLocked(ValueError):
    pass


def studio_message(video_id):
    return f"Already uploaded. Manage this video in YouTube Studio: https://studio.youtube.com/video/{video_id}/edit"


def utc(value=None):
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def iso(value):
    return utc(value).isoformat() + "Z" if value else None


def lead_minutes():
    return max(0, int(os.environ.get("YOUTUBE_UPLOAD_LEAD_MINUTES", "120")))


def catchup_hours():
    return max(1, int(os.environ.get("YOUTUBE_CATCHUP_HOURS", "24")))


def _actor(user_id=None):
    if has_app_context():
        current = getattr(g, "vs_current_user", None) or {}
        if current.get("id"):
            return str(current["id"])
    return f"system:{str(user_id or 'unknown')}"


def _new_rules_apply(row):
    if not row or not row.get("created_at"):
        return False
    # Immediate publishes remain an explicit user action. Automated scheduled
    # behavior is limited to rows created before their future publish time, so
    # a post-deploy edit cannot turn an already-past timestamp into catch-up.
    return not row.get("publish_at") or utc(row["publish_at"]) > utc(row["created_at"])


def _sanitize_error(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    # HttpError.__str__ can contain the complete provider response body. Keep
    # the actionable status without persisting customer/provider payloads.
    if status is not None:
        return f"YouTube API HTTP {status}"
    message = str(exc or "").replace("\n", " ")
    message = re.sub(r"https?://\S+", "[url]", message)
    message = re.sub(r"(?i)(token|authorization|bearer|session)[=: ]+\S+", r"\1=[redacted]", message)
    message = " ".join(message.split())[:500]
    return f"HTTP {status}: {message}" if status else message or exc.__class__.__name__


def _history(conn, schedule_id, status, *, reason=None, error=None, actor=None, metadata=None):
    value_sql = "CAST(? AS JSONB)" if getattr(conn, "backend_name", "") == "postgres" else "?"
    conn.execute(
        f"""INSERT INTO {HISTORY_TABLE}
            (id,schedule_id,platform,status,reason,error_sanitized,actor,metadata_json,created_at)
            VALUES (?,?,?,?,?,?,?,{value_sql},?)""",
        [int.from_bytes(uuid4().bytes[:8], "big", signed=False) & ((1 << 63) - 1), schedule_id, "youtube", status,
         reason, error, actor or "scheduler", json.dumps(metadata or {}), utc()],
    )


def _history_once(conn, schedule_id, status, *, reason=None, error=None, actor=None, metadata=None):
    latest = conn.execute(
        f"SELECT status,reason FROM {HISTORY_TABLE} WHERE schedule_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        [schedule_id],
    ).fetchone()
    if latest and str(latest[0] or "") == str(status) and str(latest[1] or "") == str(reason or ""):
        return False
    _history(conn, schedule_id, status, reason=reason, error=error, actor=actor, metadata=metadata)
    return True


def _admin_recipients():
    conn = get_db_readonly()
    try:
        rows = conn.execute("SELECT email FROM shorts_users WHERE lower(COALESCE(role,''))='admin'").fetchall()
    finally:
        conn.close()
    values = [str(row[0] or "").strip() for row in rows if row and "@" in str(row[0] or "")]
    fallback = (os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip()
    if "@" in fallback:
        values.append(fallback)
    return list(dict.fromkeys(values))


def _alert(schedule_id, alert_type, message):
    """Persist one admin alert and email it once."""
    claimed = False
    with connection() as conn:
        row = conn.execute(
            f"""INSERT INTO {ALERTS_TABLE}(alert_key,schedule_id,platform,alert_type,message,created_at)
                VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(alert_key) DO NOTHING RETURNING alert_key""",
            [f"youtube:{schedule_id}:{alert_type}", schedule_id, "youtube", alert_type, str(message)[:2000]],
        ).fetchone()
        claimed = bool(row)
    if not claimed:
        return False
    from app.video_shorts.services.email_verification import send_resend_email
    for recipient in _admin_recipients():
        try:
            send_resend_email(
                to_email=recipient,
                subject=f"Minti YouTube schedule alert: {alert_type}",
                html=f"<p>{html.escape(str(message))}</p>",
                text=str(message),
                error_message="Schedule alert could not be sent.",
            )
        except Exception:
            logger.warning("YouTube schedule alert email failed schedule_id=%s type=%s", schedule_id, alert_type)
    return True


def pending_label():
    minutes = lead_minutes()
    return f"Scheduled — uploads to YouTube ~{minutes / 60:g}h before publish"


def asset_key(*, video_pk=None, plan_index=None, filename=None):
    return f"clip:{int(video_pk)}:{plan_index}" if video_pk and plan_index else f"file:{filename}"


@contextmanager
def connection():
    conn = get_db()
    try:
        if getattr(conn, "backend_name", "") != "postgres":
            conn.execute("BEGIN TRANSACTION")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mutation_lock_id(user_id, brand_id, key):
    value = f"{user_id}:{brand_id}:{key}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(value).digest()[:8], "big", signed=True)


def _lock_mutation_key(conn, *, user_id, brand_id, key):
    """Serialize lease and schedule transitions without a long transaction."""
    if getattr(conn, "backend_name", "") == "postgres":
        conn.execute(
            "SELECT pg_advisory_xact_lock(?)",
            [_mutation_lock_id(user_id, brand_id, key)],
        )


def current_mutation_lease_token(*, user_id, brand_id, key):
    if not has_app_context():
        return None
    lease = getattr(g, "youtube_clip_mutation_lease", None) or {}
    if (
        str(lease.get("user_id") or "") == str(user_id or "")
        and str(lease.get("brand_id") or "") == str(brand_id or "")
        and str(lease.get("asset_key") or "") == str(key or "")
    ):
        return str(lease.get("lease_token") or "") or None
    return None


def _active_mutation_lease(conn, *, user_id, brand_id, key, now):
    row = conn.execute(
        f"""
        SELECT state, lease_token, lease_until
        FROM {MUTATION_LEASE_TABLE}
        WHERE user_id=? AND brand_id=? AND asset_key=?
        """,
        [str(user_id), str(brand_id), key],
    ).fetchone()
    if not row:
        return None
    if utc(row[2]) <= utc(now):
        conn.execute(
            f"DELETE FROM {MUTATION_LEASE_TABLE} WHERE user_id=? AND brand_id=? AND asset_key=? AND lease_until<=?",
            [str(user_id), str(brand_id), key, utc(now)],
        )
        return None
    return {"state": row[0], "lease_token": row[1], "lease_until": row[2]}


def acquire_clip_mutation_lease(*, user_id, brand_id, video_pk, plan_index, state="editing"):
    """Verify the upload lock and persist a bounded lease in one short transaction."""
    key = asset_key(video_pk=video_pk, plan_index=plan_index)
    now = utc()
    lease_until = now + timedelta(minutes=MUTATION_LEASE_MINUTES)
    existing_token = current_mutation_lease_token(
        user_id=user_id, brand_id=brand_id, key=key
    )
    lease_token = existing_token or str(uuid4())
    with connection() as conn:
        _lock_mutation_key(conn, user_id=user_id, brand_id=brand_id, key=key)
        schedule = conn.execute(
            f"SELECT status,youtube_video_id,resumable_uri FROM {TABLE} WHERE user_id=? AND brand_id=? AND asset_key=?",
            [str(user_id), str(brand_id), key],
        ).fetchone()
        if schedule and schedule[1]:
            raise YouTubeScheduleLocked(studio_message(schedule[1]))
        if schedule and (schedule[0] in {"queued", "uploading", "uncertain"} or schedule[2]):
            raise YouTubeScheduleLocked(
                "YouTube upload is in progress; editing is locked. Check YouTube Studio."
            )
        active = _active_mutation_lease(
            conn, user_id=user_id, brand_id=brand_id, key=key, now=now
        )
        if active and active["lease_token"] != lease_token:
            raise YouTubeScheduleLocked("This clip is already being edited. Please try again shortly.")
        conn.execute(
            f"""
            INSERT INTO {MUTATION_LEASE_TABLE}
                (user_id,brand_id,asset_key,state,lease_token,lease_until,updated_at)
            VALUES (?,?,?,?,?,?,now())
            ON CONFLICT (user_id,brand_id,asset_key) DO UPDATE SET
                state=excluded.state,
                lease_token=excluded.lease_token,
                lease_until=excluded.lease_until,
                updated_at=now()
            """,
            [str(user_id), str(brand_id), key, str(state), lease_token, lease_until],
        )
    return {
        "user_id": str(user_id),
        "brand_id": str(brand_id),
        "asset_key": key,
        "state": str(state),
        "lease_token": lease_token,
        "lease_until": lease_until,
    }


def release_clip_mutation_lease(lease):
    """Clear only the caller's lease; stale workers cannot clear a newer lease."""
    value = lease or {}
    if not value.get("lease_token"):
        return False
    with connection() as conn:
        key = str(value.get("asset_key") or "")
        user_id = str(value.get("user_id") or "")
        brand_id = str(value.get("brand_id") or "")
        _lock_mutation_key(conn, user_id=user_id, brand_id=brand_id, key=key)
        row = conn.execute(
            f"""
            DELETE FROM {MUTATION_LEASE_TABLE}
            WHERE user_id=? AND brand_id=? AND asset_key=? AND lease_token=?
            RETURNING lease_token
            """,
            [user_id, brand_id, key, str(value["lease_token"])],
        ).fetchone()
        return bool(row)


def release_orphaned_rendering_leases(*, active_job_scopes=None, minimum_age_seconds=60):
    """Release render leases left by requests/workers that no longer own an active job."""
    active = {
        (str(user_id), str(brand_id), str(key))
        for user_id, brand_id, key in (active_job_scopes or [])
    }
    cutoff = utc() - timedelta(seconds=max(0, int(minimum_age_seconds or 0)))
    released = []
    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT user_id, brand_id, asset_key, updated_at
            FROM {MUTATION_LEASE_TABLE}
            WHERE state='rendering'
              AND updated_at<=?
            """,
            [cutoff],
        ).fetchall()
        for row in rows:
            scope = (str(row[0]), str(row[1]), str(row[2]))
            if scope in active:
                continue
            deleted = conn.execute(
                f"""
                DELETE FROM {MUTATION_LEASE_TABLE}
                WHERE user_id=? AND brand_id=? AND asset_key=?
                  AND state='rendering' AND updated_at<=?
                RETURNING asset_key
                """,
                [*scope, cutoff],
            ).fetchone()
            if deleted:
                released.append(scope)
    return released


def _row(conn, schedule_id):
    cur = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", [schedule_id])
    row = cur.fetchone()
    return dict(zip([col[0] for col in cur.description], row)) if row else None


def _result(row):
    status = row["status"]
    if status == "uploaded" and row.get("notification") == "late":
        status = "published"
    elif status == "uploaded":
        status = "scheduled" if row["publish_at"] and utc(row["publish_at"]) > utc() else "uploaded"
    return {"id": row.get("youtube_video_id"), "youtube_id": row.get("youtube_video_id"), "target": "youtube", "status": status,
            "message": row.get("error") or (pending_label() if status == "pending" else "YouTube publish submitted."),
            "schedule_id": row["id"], "upload_due_at": iso(row["upload_due_at"]),
            "job_id": row.get("job_id"), "publish_at": iso(row["publish_at"])}


def _existing_remote_id(*, user_id, brand_id, filename, video_pk=None, plan_index=None, kind="clip"):
    conn = get_db_readonly()
    try:
        row = conn.execute("SELECT youtube_video_id FROM shorts_generated_videos WHERE user_id=? AND brand_id=? AND (clip_filename=? OR output_filename=?) AND youtube_video_id IS NOT NULL AND youtube_video_id<>'' LIMIT 1", [str(user_id),brand_id,filename,filename]).fetchone()
        if row and row[0]:
            return row[0]
        if kind == "ai":
            row = conn.execute("SELECT youtube_video_id FROM shorts_ai_videos WHERE user_id=? AND brand_id=? AND clip_filename=?", [str(user_id),brand_id,filename]).fetchone()
            return row[0] if row else None
    finally:
        conn.close()
    if video_pk and plan_index:
        from app.video_shorts.services import tenant_media
        video = tenant_media.resolve_video(video_pk=video_pk,owner_user_id=user_id,brand_id=brand_id)
        plan = tenant_media.load_state(video)
        entry = next((item for item in (plan.get("plan") or plan.get("clips") or []) if str(item.get("plan_index"))==str(plan_index)), {})
        return entry.get("yt_video_id")


def schedule_youtube_upload(*, video_path, title, description, publish_at=None,
                            privacy_status="private", user_id, brand_id=None,
                            video_pk=None, plan_index=None, publish_at_local=None,
                            kind="clip", ai_video_id=None, tags=None,
                            async_immediate=False, quick_session_id=None,
                            existing_video_id=None, clip_filename=None):
    """Schedule/reschedule locally, or submit an immediate upload through the same executor."""
    from app.video_shorts.services.brands import current_brand_id
    if existing_video_id:
        raise YouTubeScheduleLocked(studio_message(existing_video_id))
    brand_id = str(brand_id or current_brand_id() or "")
    if not user_id or not brand_id:
        raise ValueError("A YouTube upload requires an explicit owner and brand.")
    from app.video_shorts.services.youtube_token_store import get_youtube_connection
    connection_info = get_youtube_connection(str(user_id), brand_id)
    expected_token_owner = f"{str(user_id)}::{brand_id}"
    if not connection_info or connection_info.get("user_id") != expected_token_owner:
        raise ValueError("YouTube scheduling requires a token owned by this user and brand.")
    expected_channel_id = str(connection_info.get("youtube_channel_id") or "").strip()
    if not expected_channel_id:
        raise ValueError("YouTube scheduling requires a verified target channel.")
    filename = clip_filename or Path(video_path).name
    remote_id = _existing_remote_id(user_id=user_id,brand_id=brand_id,filename=filename,video_pk=video_pk,plan_index=plan_index,kind=kind)
    if remote_id:
        raise YouTubeScheduleLocked(studio_message(remote_id))
    key = asset_key(video_pk=video_pk, plan_index=plan_index, filename=filename)
    now = utc()
    publish_dt = utc(publish_at) if publish_at else None
    due = publish_dt - timedelta(minutes=lead_minutes()) if publish_dt else now
    deferred = bool(publish_dt and due > now)
    # Keep timing/privacy semantics identical for every caller: an immediate
    # upload is public, while a future YouTube schedule is private + publishAt.
    effective_privacy_status = "private" if publish_dt else "public"
    payload = dict(title=title, description=description, privacy_status=effective_privacy_status,
                   filename=filename, video_pk=video_pk, plan_index=plan_index,
                   publish_at_local=publish_at_local, kind=kind, ai_video_id=ai_video_id,
                   tags=tags or [], quick_session_id=quick_session_id)
    with connection() as conn:
        _lock_mutation_key(conn, user_id=user_id, brand_id=brand_id, key=key)
        active_lease = _active_mutation_lease(
            conn, user_id=user_id, brand_id=brand_id, key=key, now=now
        )
        request_lease_token = current_mutation_lease_token(
            user_id=user_id, brand_id=brand_id, key=key
        )
        if active_lease and active_lease["lease_token"] != request_lease_token:
            raise YouTubeScheduleLocked("This clip is being edited; schedule it when rendering finishes.")
        schedule_id = str(uuid4())
        created_by = _actor(user_id)
        row = conn.execute(f"""
            INSERT INTO {TABLE} (id,user_id,brand_id,expected_youtube_channel_id,asset_key,payload_json,status,publish_at,upload_due_at,created_at,created_by)
            VALUES (?,?,?,?,?,?,'pending',?,?,?,?)
            ON CONFLICT (user_id,brand_id,asset_key) DO UPDATE SET
                payload_json=excluded.payload_json, status='pending', publish_at=excluded.publish_at,
                expected_youtube_channel_id=excluded.expected_youtube_channel_id,
                upload_due_at=excluded.upload_due_at, attempts=0, error=NULL,
                notification=NULL, notified_at=NULL, created_at=excluded.created_at,
                created_by=excluded.created_by, updated_at=now()
            WHERE {TABLE}.status IN ('pending','cancelled','missed','failed')
              AND {TABLE}.youtube_video_id IS NULL AND {TABLE}.resumable_uri IS NULL
            RETURNING id
        """, [schedule_id,str(user_id),brand_id,expected_channel_id,key,json.dumps(payload),publish_dt,due,now,created_by]).fetchone()
        if not row:
            old = conn.execute(f"SELECT youtube_video_id FROM {TABLE} WHERE user_id=? AND brand_id=? AND asset_key=?", [str(user_id),brand_id,key]).fetchone()
            raise YouTubeScheduleLocked(studio_message(old[0]) if old and old[0] else "YouTube upload is in progress; editing is locked.")
        schedule_id = row[0]
        _history(conn, schedule_id, "created", actor=created_by, metadata={"publish_at": iso(publish_dt)})
        if not deferred:
            _enqueue(conn, schedule_id, now)
        result = _result(_row(conn, schedule_id))
    if not deferred and not async_immediate:
        result = execute_scheduled_upload(schedule_id, video_path=video_path)
    return result


def publish_pending_now(*, video_path, title, description, user_id, brand_id,
                        video_pk, plan_index, kind="clip", ai_video_id=None,
                        tags=None, async_immediate=False, quick_session_id=None,
                        existing_video_id=None, clip_filename=None):
    """Reset a never-started pending schedule, then use the normal public-now path."""
    filename = clip_filename or Path(video_path).name
    key = asset_key(video_pk=video_pk, plan_index=plan_index, filename=filename)
    now = utc()
    with connection() as conn:
        _lock_mutation_key(conn, user_id=user_id, brand_id=brand_id, key=key)
        active_lease = _active_mutation_lease(
            conn, user_id=user_id, brand_id=brand_id, key=key, now=now
        )
        request_lease_token = current_mutation_lease_token(
            user_id=user_id, brand_id=brand_id, key=key
        )
        if active_lease and active_lease["lease_token"] != request_lease_token:
            raise YouTubeScheduleLocked("This clip is being edited; publish it when rendering finishes.")
        row = conn.execute(
            f"""
            SELECT id,status,youtube_video_id,resumable_uri
            FROM {TABLE}
            WHERE user_id=? AND brand_id=? AND asset_key=?
            """,
            [str(user_id), str(brand_id), key],
        ).fetchone()
        if not row:
            raise YouTubeScheduleLocked("Only a pending YouTube schedule can be published now.")
        if row[2]:
            raise YouTubeScheduleLocked(studio_message(row[2]))
        if row[1] != "pending" or row[3]:
            raise YouTubeScheduleLocked("YouTube upload is already queued or in progress.")
        changed = conn.execute(
            f"""
            UPDATE {TABLE}
            SET status='cancelled',publish_at=NULL,upload_due_at=?,job_id=NULL,
                attempts=0,error=NULL,notification=NULL,notified_at=NULL,updated_at=now()
            WHERE id=? AND status='pending'
              AND youtube_video_id IS NULL AND resumable_uri IS NULL
            RETURNING id
            """,
            [now, row[0]],
        ).fetchone()
        if not changed:
            raise YouTubeScheduleLocked("YouTube upload is already queued or in progress.")
        _history(conn, row[0], "cancelled", reason="publish_now_reset", actor=_actor(user_id))
    return schedule_youtube_upload(
        video_path=video_path,
        title=title,
        description=description,
        publish_at=None,
        privacy_status="public",
        user_id=user_id,
        brand_id=brand_id,
        video_pk=video_pk,
        plan_index=plan_index,
        publish_at_local=None,
        kind=kind,
        ai_video_id=ai_video_id,
        tags=tags,
        async_immediate=async_immediate,
        quick_session_id=quick_session_id,
        existing_video_id=existing_video_id,
        clip_filename=clip_filename,
    )


def cancel_schedule(*, user_id, brand_id, video_pk, plan_index):
    with connection() as conn:
        key = asset_key(video_pk=video_pk, plan_index=plan_index)
        row = conn.execute(f"SELECT id,status,youtube_video_id,resumable_uri FROM {TABLE} WHERE user_id=? AND brand_id=? AND asset_key=?", [str(user_id),str(brand_id),key]).fetchone()
        if not row:
            return
        if row[2]:
            raise YouTubeScheduleLocked(studio_message(row[2]))
        if row[1] not in {"pending", "cancelled", "missed", "failed"} or row[3]:
            raise YouTubeScheduleLocked("YouTube upload is in progress; editing is locked.")
        changed = conn.execute(f"UPDATE {TABLE} SET status='cancelled',updated_at=now() WHERE id=? AND status=? AND resumable_uri IS NULL RETURNING id", [row[0],row[1]]).fetchone()
        if not changed:
            raise YouTubeScheduleLocked("YouTube upload is in progress; editing is locked.")
        _history(conn, row[0], "cancelled", reason="user_cancelled", actor=_actor(user_id))


def _active_render_job(conn, candidate, payload):
    if not payload.get("video_pk") or not payload.get("plan_index"):
        return False
    rows = conn.execute(
        "SELECT payload_json FROM shorts_render_jobs WHERE user_id=? AND type='render_short' AND status IN ('queued','processing')",
        [candidate["user_id"]],
    ).fetchall()
    for (raw,) in rows:
        render = json.loads(raw) if isinstance(raw, str) else raw
        render_plan = render.get("plan_index") or (render.get("options") or {}).get("plan_index")
        if str(render.get("video_pk")) == str(payload["video_pk"]) and str(render_plan) == str(payload["plan_index"]):
            return True
    return False


def _file_exists(payload):
    from app.video_shorts.services import tenant_media
    try:
        return bool(tenant_media.clip_exists(payload.get("filename")))
    except Exception:
        return False


def _billing_paused(conn, user_id):
    row = conn.execute(
        "SELECT autopilot_billing_paused_at FROM shorts_users WHERE CAST(id AS VARCHAR)=?",
        [str(user_id)],
    ).fetchone()
    return bool(row and row[0])


def _blocking_reason(conn, candidate, payload, now, *, include_live_token=False):
    lease = _active_mutation_lease(
        conn,
        user_id=candidate["user_id"],
        brand_id=candidate["brand_id"],
        key=candidate["asset_key"],
        now=now,
    )
    if lease:
        return f"lease:{lease['state']}"
    if _billing_paused(conn, candidate["user_id"]):
        return "billing_pause"
    active_render = _active_render_job(conn, candidate, payload)
    if active_render:
        return "active_render"
    if not _file_exists(payload):
        return "missing_file"
    if payload.get("render_pending"):
        payload["render_pending"] = False
        conn.execute(
            f"UPDATE {TABLE} SET payload_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            [json.dumps(payload), candidate["id"]],
        )
        _history_once(conn, candidate["id"], "ready", reason="stale_render_pending_cleared")
        logger.warning(
            "Cleared stale YouTube render_pending schedule_id=%s brand_id=%s",
            candidate["id"], candidate["brand_id"],
        )
    if include_live_token:
        from app.video_shorts.services.youtube_oauth import get_connected_channel_info
        info, error = get_connected_channel_info(candidate["user_id"], candidate["brand_id"])
        if error:
            return f"token:{error}"
        if not info:
            return "token:missing"
        if str(info.get("id") or "") != str(candidate.get("expected_youtube_channel_id") or ""):
            return "channel_mismatch"
    return None


def _enqueue(conn, schedule_id, now):
    """Claim and enqueue in ONE transaction; no crash gap between claim and job creation."""
    candidate = _row(conn, schedule_id)
    if not candidate:
        return False
    if not _new_rules_apply(candidate):
        return False
    if candidate.get("youtube_video_id") or candidate["status"] != "pending":
        return False
    candidate_payload = json.loads(candidate["payload_json"])
    _lock_mutation_key(
        conn,
        user_id=candidate["user_id"],
        brand_id=candidate["brand_id"],
        key=candidate["asset_key"],
    )
    publish_dt = utc(candidate["publish_at"]) if candidate and candidate["publish_at"] else None
    if publish_dt and now - publish_dt > timedelta(hours=catchup_hours()):
        reason = _blocking_reason(conn, candidate, candidate_payload, now) or "catchup_window_expired"
        message = f"Missed after {catchup_hours()}h catch-up window; blocker={reason}."
        conn.execute(
            f"UPDATE {TABLE} SET status='missed',error=?,notification='missed',notified_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            [message, schedule_id],
        )
        _history_once(conn, schedule_id, "missed", reason=reason, error=message)
        _alert(schedule_id, "missed", f"YouTube schedule {schedule_id} missed. {message}")
        return False
    reason = _blocking_reason(conn, candidate, candidate_payload, now)
    if reason:
        _history_once(conn, schedule_id, "blocked", reason=reason)
        if utc(now) >= utc(candidate["upload_due_at"]) + timedelta(minutes=15):
            _alert(schedule_id, "enqueue_late", f"YouTube schedule {schedule_id} was not enqueued 15m after upload_due_at; blocker={reason}.")
        return False
    job_id = str(uuid4())
    row = conn.execute(f"UPDATE {TABLE} SET status='queued',job_id=?,updated_at=now() WHERE id=? AND status='pending' AND youtube_video_id IS NULL AND upload_due_at<=? RETURNING user_id,brand_id,payload_json", [job_id,schedule_id,now]).fetchone()
    if not row:
        return False
    payload = json.loads(row[2])
    job_payload = json.dumps({"youtube_schedule_id": schedule_id, "brand_id": row[1],
                              "quick_session_id": payload.get("quick_session_id"), "target": "youtube"})
    value_sql = "CAST(? AS JSONB)" if getattr(conn, "backend_name", "") == "postgres" else "?"
    conn.execute(f"INSERT INTO shorts_render_jobs (id,user_id,type,status,priority,payload_json,attempts,max_attempts) VALUES (?,?,'publish_short','queued',10,{value_sql},0,1000000)", [job_id,row[0],job_payload])
    _history(conn, schedule_id, "enqueued", actor="scheduler", metadata={"job_id": job_id})
    return True


def scheduler_health_check(*, now=None):
    now = utc(now)
    worker = f"{socket.gethostname()}:{os.getpid()}"
    with connection() as conn:
        row = conn.execute(
            f"SELECT last_tick_at,last_gap_alerted_at FROM {SCHEDULER_STATE_TABLE} WHERE name='youtube'"
        ).fetchone()
        if row and row[0] and now - utc(row[0]) > timedelta(minutes=10):
            last_alert = utc(row[1]) if row[1] else None
            if not last_alert or last_alert < utc(row[0]):
                gap = int((now - utc(row[0])).total_seconds())
                conn.execute(
                    f"UPDATE {SCHEDULER_STATE_TABLE} SET last_gap_alerted_at=?,updated_at=CURRENT_TIMESTAMP WHERE name='youtube'",
                    [now],
                )
                _alert("scheduler", f"heartbeat_gap:{iso(row[0])}", f"YouTube scheduler had no tick for {gap}s while worker {worker} is running.")
        conn.execute(
            f"""INSERT INTO {SCHEDULER_STATE_TABLE}(name,last_tick_at,last_worker_id,updated_at)
                VALUES ('youtube',?,?,?)
                ON CONFLICT(name) DO UPDATE SET last_tick_at=excluded.last_tick_at,
                    last_worker_id=excluded.last_worker_id,updated_at=excluded.updated_at""",
            [now, worker, now],
        )


def _run_preflights(conn, now):
    rows = conn.execute(
        f"""SELECT schedule.id FROM {TABLE} schedule
            WHERE schedule.status='pending'
              AND schedule.created_at IS NOT NULL
              AND schedule.publish_at>schedule.created_at
              AND schedule.upload_due_at>?
              AND schedule.upload_due_at<=?
              AND NOT EXISTS (
                  SELECT 1 FROM {HISTORY_TABLE} history
                  WHERE history.schedule_id=schedule.id
                    AND history.status IN ('preflight_ok','preflight_failed')
              )
            ORDER BY schedule.upload_due_at""",
        [now, now + timedelta(hours=3)],
    ).fetchall()
    for (schedule_id,) in rows:
        candidate = _row(conn, schedule_id)
        payload = json.loads(candidate["payload_json"])
        reason = _blocking_reason(conn, candidate, payload, now, include_live_token=True)
        status = "preflight_failed" if reason else "preflight_ok"
        _history(conn, schedule_id, status, reason=reason)
        if reason:
            _alert(schedule_id, "preflight_failed", f"YouTube schedule {schedule_id} pre-flight failed; blocker={reason}.")


def _alert_due_not_uploaded(conn, now):
    rows = conn.execute(
        f"""SELECT id FROM {TABLE}
            WHERE created_at IS NOT NULL
              AND publish_at>created_at
              AND publish_at IS NOT NULL AND publish_at<=?
              AND youtube_video_id IS NULL
              AND status NOT IN ('uploaded','published','cancelled','missed','failed','blocked_duplicate','blocked_target_mismatch')""",
        [now],
    ).fetchall()
    for (schedule_id,) in rows:
        candidate = _row(conn, schedule_id)
        payload = json.loads(candidate["payload_json"])
        reason = _blocking_reason(conn, candidate, payload, now) or f"status:{candidate['status']}"
        _alert(schedule_id, "publish_time_not_uploaded", f"YouTube schedule {schedule_id} was not uploaded at publish time; blocker={reason}.")


def scheduler_tick(*, now=None):
    now = utc(now)
    scheduler_health_check(now=now)
    with connection() as conn:
        _run_preflights(conn, now)
        lock = " FOR UPDATE SKIP LOCKED" if getattr(conn, "backend_name", "") == "postgres" else ""
        rows = conn.execute(
            f"""
            SELECT schedule.id
            FROM {TABLE} schedule
            WHERE schedule.status='pending'
              AND schedule.created_at IS NOT NULL
              AND (schedule.publish_at IS NULL OR schedule.publish_at>schedule.created_at)
              AND schedule.upload_due_at<=?
              AND schedule.youtube_video_id IS NULL
            ORDER BY schedule.upload_due_at
            LIMIT 100{lock}
            """,
            [now],
        ).fetchall()
        claimed = False
        for row in rows:
            claimed = _enqueue(conn, row[0], now) or claimed
        _alert_due_not_uploaded(conn, now)
    send_pending_notifications()
    return claimed


def _save_session(schedule_id, uri):
    # Called BEFORE the first byte of media is transmitted.
    with connection() as conn:
        conn.execute(f"UPDATE {TABLE} SET resumable_uri=?,updated_at=now() WHERE id=? AND status='uploading'", [uri,schedule_id])


def execute_scheduled_upload(schedule_id, *, video_path=None):
    """Called by the existing publish job (and synchronous immediate publishers).

    A PostgreSQL advisory lock covers the HTTP operation, not a long transaction.
    A requeued job can resume a dead worker's session, but never run concurrently.
    """
    from app.video_shorts.services.youtube_oauth import (
        find_exact_title_video,
        get_connected_channel_info,
        upload_video_with_refresh_token,
        verify_uploaded_video,
        YouTubeTransientError,
    )
    from app.video_shorts.services.youtube_token_store import get_youtube_connection
    from app.video_shorts.routes import generation
    lock_conn = get_db()
    lock_id = int.from_bytes(hashlib.sha256(schedule_id.encode()).digest()[:8], "big", signed=True)
    locked = False
    try:
        if getattr(lock_conn, "backend_name", "") == "postgres":
            locked = bool(lock_conn.execute("SELECT pg_try_advisory_lock(?)", [lock_id]).fetchone()[0])
            lock_conn.commit()
            if not locked:
                raise RuntimeError("YouTube upload is already running; retry this job.")
        with connection() as conn:
            row = _row(conn, schedule_id)
            if not row:
                raise ValueError("YouTube schedule not found.")
            if row["status"] not in {"queued", "uploading"}:
                if row["status"] == "uploaded":
                    _project_upload(row, json.loads(row["payload_json"]))
                return _result(row)
            if not _new_rules_apply(row):
                logger.warning("Legacy YouTube schedule blocked from automatic upload schedule_id=%s", schedule_id)
                return _result(row)
            now = utc()
            publish_dt = utc(row["publish_at"]) if row["publish_at"] else None
            missed = bool(publish_dt and now - publish_dt > timedelta(hours=catchup_hours()))
            if missed:
                payload = json.loads(row["payload_json"])
                reason = _blocking_reason(conn, row, payload, now) or "catchup_window_expired"
                message = f"Missed after {catchup_hours()}h catch-up window; blocker={reason}."
                conn.execute(f"UPDATE {TABLE} SET status='missed',error=?,notification='missed',updated_at=now() WHERE id=?", [message,schedule_id])
                _history_once(conn, schedule_id, "missed", reason=reason, error=message)
                row = _row(conn, schedule_id)
            else:
                payload = json.loads(row["payload_json"])
                duplicate = find_exact_title_video(
                    payload.get("title"),
                    channel_id=row.get("expected_youtube_channel_id"),
                    user_id=row["user_id"],
                    brand_id=row["brand_id"],
                )
                if duplicate:
                    reason = f"duplicate_title:{duplicate['id']}"
                    conn.execute(
                        f"UPDATE {TABLE} SET status='blocked_duplicate',error=?,notification='failed',updated_at=now() WHERE id=?",
                        [f"Upload blocked; a video with the same title already exists ({duplicate['id']}).", schedule_id],
                    )
                    _history(conn, schedule_id, "blocked", reason=reason)
                    row = _row(conn, schedule_id)
                else:
                    conn.execute(f"UPDATE {TABLE} SET status='uploading',attempts=attempts+1,updated_at=now() WHERE id=?", [schedule_id])
                    _history(conn, schedule_id, "uploading", actor="worker")
        payload = json.loads(row["payload_json"])
        if missed:
            _alert(schedule_id, "missed", f"YouTube schedule {schedule_id} missed. {row['error']}")
            _project_upload(row,payload)
            return _result(row)
        if row["status"] == "blocked_duplicate":
            _alert(schedule_id, "duplicate_title", f"YouTube schedule {schedule_id} blocked: {row['error']}")
            _project_upload(row, payload)
            return _result(row)
        late = bool(publish_dt and publish_dt <= now)
        temporary_path = None
        try:
            from app.video_shorts.services.media_access import authorize_file
            from app.video_shorts.services import tenant_media
            authorize_file(payload["filename"],owner_user_id=row["user_id"],brand_id=row["brand_id"],video_pk=payload.get("video_pk"))
            if not video_path or not Path(video_path).exists():
                video_path = generation._resolve_short_path_for_processing(payload["filename"])
                if video_path and Path(video_path).resolve() != tenant_media.short_path(payload["filename"]).resolve():
                    temporary_path = Path(video_path)
                if not video_path and payload.get("kind") == "ai":
                    from app.video_shorts.config import SHORTS_DIR
                    video_path = SHORTS_DIR / payload["filename"]
            if not video_path or not Path(video_path).exists():
                raise FileNotFoundError("Scheduled media is unavailable.")
            connection_info = get_youtube_connection(row["user_id"], row["brand_id"])
            require_target_match(
                platform="youtube_token_owner",
                schedule_id=schedule_id,
                brand_id=row["brand_id"],
                expected=f"{row['user_id']}::{row['brand_id']}",
                actual=(connection_info or {}).get("user_id"),
            )
            channel_info, _channel_error = get_connected_channel_info(
                row["user_id"], row["brand_id"]
            )
            actual_channel_id = (channel_info or {}).get("id")
            require_target_match(
                platform="youtube",
                schedule_id=schedule_id,
                brand_id=row["brand_id"],
                expected=row.get("expected_youtube_channel_id"),
                actual=actual_channel_id,
            )
            response = upload_video_with_refresh_token(
                video_path=str(video_path), title=payload["title"], description=payload["description"],
                publish_at=None if late else iso(publish_dt),
                privacy_status="public" if late else payload["privacy_status"],
                user_id=row["user_id"], brand_id=row["brand_id"], tags=payload.get("tags"),
                resumable_uri=row.get("resumable_uri"),
                save_resumable_uri=lambda uri: _save_session(schedule_id, uri),
            ) or {}
            youtube_id = response.get("id")
            if not youtube_id:
                raise ValueError("YouTube returned no video id; review the upload in YouTube Studio.")
            # Persist the id before touching any UI projection.
            with connection() as conn:
                conn.execute(f"UPDATE {TABLE} SET status='uploaded',youtube_video_id=?,error=NULL,notification=?,updated_at=now() WHERE id=?", [youtube_id,"late" if late else None,schedule_id])
                _history(conn, schedule_id, "uploaded", actor="worker", metadata={"youtube_video_id": youtube_id})
                row = _row(conn, schedule_id)
            try:
                verification = verify_uploaded_video(
                    youtube_id,
                    expected_channel_id=row.get("expected_youtube_channel_id"),
                    expected_title=payload["title"],
                    user_id=row["user_id"],
                    brand_id=row["brand_id"],
                )
            except Exception as exc:
                verification = {"ok": False, "reason": _sanitize_error(exc)}
            if not verification.get("ok"):
                _alert(schedule_id, "post_upload_verification_failed", f"YouTube schedule {schedule_id} uploaded id={youtube_id}, but videos.list verification failed: {verification.get('reason')}.")
        except PublishTargetMismatch as exc:
            with connection() as conn:
                conn.execute(
                    f"""UPDATE {TABLE}
                        SET status='blocked_target_mismatch', error=?, notification='failed',
                            resumable_uri=NULL, updated_at=now()
                        WHERE id=?""",
                    ["Upload blocked because the connected target no longer matches the schedule.", schedule_id],
                )
                row = _row(conn, schedule_id)
            logger.error(
                "YouTube publish target mismatch schedule_id=%s brand_id=%s expected=%s actual=%s",
                schedule_id,
                row["brand_id"],
                exc.expected,
                exc.actual,
            )
            send_target_mismatch_alert(exc)
        except Exception as exc:
            code = getattr(getattr(exc, "resp", None), "status", None)
            transient = code in {429,500,502,503,504} or isinstance(exc, (TimeoutError, ConnectionError, OSError, HttpLib2Error, YouTubeTransientError)) and not isinstance(exc, FileNotFoundError)
            clean_error = _sanitize_error(exc)
            with connection() as conn:
                latest = _row(conn, schedule_id)
                status = "pending" if transient else ("uncertain" if latest.get("resumable_uri") else "failed")
                delay = min(1800, 30 * 2 ** min(int(latest["attempts"]), 6))
                stored_error = f"Upload interrupted; retry scheduled. {clean_error}" if transient else f"Upload needs attention. {clean_error}"
                conn.execute(f"UPDATE {TABLE} SET status=?,upload_due_at=?,error=?,notification=?,updated_at=now() WHERE id=?", [status,utc()+timedelta(seconds=delay),stored_error,None if transient else "failed",schedule_id])
                _history(conn, schedule_id, "failed", reason="transient" if transient else "permanent", error=clean_error)
                row = _row(conn, schedule_id)
            logger.warning("YouTube schedule upload interrupted schedule_id=%s status=%s error=%s", schedule_id, row["status"], clean_error)
            if not transient:
                _alert(schedule_id, "upload_failed", f"YouTube schedule {schedule_id} failed: {clean_error}")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        result = _result(row)
        _project_upload(row, payload)
        return result
    finally:
        if locked:
            lock_conn.execute("SELECT pg_advisory_unlock(?)", [lock_id])
            lock_conn.commit()
        lock_conn.close()


def _project_upload(row, payload):
    from app.video_shorts.routes import generation
    try:
        if payload.get("kind") == "ai":
            from app.video_shorts.services.ai_video_workspace import update_ai_video_youtube_state
            update_ai_video_youtube_state(user_id=row["user_id"], brand_id=row["brand_id"], ai_video_id=payload["ai_video_id"], youtube_status=_result(row)["status"], youtube_video_id=row["youtube_video_id"], youtube_publish_at=iso(row["publish_at"]))
        elif payload.get("kind") == "long":
            generation._update_long_comp_publish_state(payload["filename"], {"youtube": {"status": _result(row)["status"], "yt_video_id": row["youtube_video_id"], "publish_at": iso(row["publish_at"])}})
        elif payload.get("video_pk") and payload.get("plan_index"):
            projected = generation._update_plan_entry_publish_state(
                video_pk=payload["video_pk"],
                plan_index=str(payload["plan_index"]),
                filename=payload["filename"],
                publish_status=_result(row)["status"],
                publish_at_local=payload.get("publish_at_local"),
                publish_at_iso=iso(row["publish_at"]),
                title=payload["title"],
                description=payload["description"],
                youtube_id=row.get("youtube_video_id"),
                owner_user_id=row["user_id"],
                brand_id=row["brand_id"],
            )
            if not projected:
                logger.warning(
                    "YouTube schedule projection will retry schedule_id=%s",
                    row["id"],
                )
    except Exception:
        logger.warning("YouTube schedule saved; UI projection needs refresh schedule_id=%s", row["id"])


def send_pending_notifications():
    from app.video_shorts.services.email_verification import send_resend_email
    conn = get_db_readonly()
    try:
        rows = conn.execute(f"SELECT id,user_id,brand_id,notification FROM {TABLE} WHERE created_at IS NOT NULL AND notification IS NOT NULL AND notified_at IS NULL LIMIT 10").fetchall()
        admins = conn.execute("SELECT email FROM shorts_users WHERE lower(role)='admin'").fetchall() if rows else []
    finally:
        conn.close()
    for schedule_id, owner, brand, notification in rows:
        message = f"YouTube schedule {schedule_id}: {notification}. Owner {owner}, brand {brand}."
        try:
            if not admins:
                continue
            for (email,) in admins:
                send_resend_email(to_email=email, subject=f"Minti YouTube schedule: {notification}", html=f"<p>{html.escape(message)}</p>", text=message)
            with connection() as conn:
                conn.execute(f"UPDATE {TABLE} SET notified_at=CURRENT_TIMESTAMP WHERE id=? AND notification=?", [schedule_id,notification])
        except Exception:
            logger.warning("YouTube schedule notification will retry schedule_id=%s", schedule_id)


def plan_schedules(*, user_id, brand_id, video_pk):
    if not user_id or not brand_id or not video_pk:
        return {}
    with connection() as conn:
        cur = conn.execute(f"SELECT * FROM {TABLE} WHERE user_id=? AND brand_id=? AND asset_key LIKE ?", [str(user_id),str(brand_id),f"clip:{int(video_pk)}:%"])
        names = [col[0] for col in cur.description]
        return {row["asset_key"].rsplit(":",1)[1]: row for row in (dict(zip(names, raw)) for raw in cur.fetchall())}


def overlay_plan(entries, *, user_id, brand_id, video_pk):
    schedules = plan_schedules(user_id=user_id, brand_id=brand_id, video_pk=video_pk)
    for entry in entries:
        row = schedules.get(str(entry.get("plan_index")))
        if not row:
            continue
        confirmed_published = row["status"] == "uploaded" and entry.get("publish_status") == "published" and entry.get("yt_video_id") == row.get("youtube_video_id")
        entry["publish_status"] = entry["yt_status"] = "published" if confirmed_published else _result(row)["status"]
        entry["upload_due_at"] = iso(row["upload_due_at"])
        if row["status"] == "cancelled":
            entry.pop("publish_at", None)
            entry.pop("publish_at_iso", None)
        else:
            entry["publish_at_iso"] = iso(row["publish_at"])
            entry["publish_at"] = json.loads(row["payload_json"]).get("publish_at_local")
        if row.get("youtube_video_id"):
            entry["yt_video_id"] = row["youtube_video_id"]
        entry["youtube_publish_now_eligible"] = bool(
            row["status"] == "pending"
            and not row.get("youtube_video_id")
            and not row.get("resumable_uri")
        )
        if row.get("error"):
            entry["schedule_update_warning"] = row["error"]
    return entries


def sync_plan_metadata(entries, *, user_id, brand_id, video_pk):
    if not user_id or not brand_id or not video_pk:
        return
    with connection() as conn:
        for entry in entries:
            key = asset_key(video_pk=video_pk,plan_index=entry.get("plan_index"))
            lock = " FOR UPDATE" if getattr(conn,"backend_name","") == "postgres" else ""
            row = conn.execute(f"SELECT id,payload_json FROM {TABLE} WHERE user_id=? AND brand_id=? AND asset_key=? AND status='pending' AND resumable_uri IS NULL{lock}", [str(user_id),str(brand_id),key]).fetchone()
            if not row:
                continue
            payload = json.loads(row[1])
            payload.update(title=entry.get("yt_title") or entry.get("title") or payload["title"],
                           description=entry.get("yt_description",entry.get("description",payload["description"])),
                           filename=entry.get("clip_filename") or entry.get("output_filename") or payload["filename"])
            payload["render_pending"] = bool(entry.get("edit_requires_regenerate") or str(entry.get("status") or "").lower() in {"queued","processing","rendering"})
            conn.execute(f"UPDATE {TABLE} SET payload_json=?,updated_at=now() WHERE id=? AND status='pending' AND resumable_uri IS NULL", [json.dumps(payload),row[0]])


def file_schedule_state(*, user_id, brand_id, filename):
    if not user_id or not brand_id:
        return {}
    conn = get_db_readonly()
    try:
        found = conn.execute(f"SELECT id FROM {TABLE} WHERE user_id=? AND brand_id=? AND asset_key=?", [str(user_id),str(brand_id),asset_key(filename=filename)]).fetchone()
        return _result(_row(conn,found[0])) if found else {}
    finally:
        conn.close()
