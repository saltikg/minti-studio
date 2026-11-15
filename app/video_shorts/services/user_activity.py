from __future__ import annotations

import logging
from typing import Mapping

from flask import request

from app.video_shorts.services.db import get_db, get_db_readonly


logger = logging.getLogger(__name__)

LOGIN_METHODS = {"password", "google", "magic_link"}
ACTIVITY_THROTTLE_MINUTES = 5

_EXCLUDED_ACTIVITY_ENDPOINTS = {
    "public_email_unsubscribe_alias",
    "public_nurture_click_alias",
    "video_shorts_bp.customer_notification_click",
    "video_shorts_bp.monthly_report_click",
    "video_shorts_bp.stats_report_render",
    "video_shorts_bp.static",
}


def load_activity_user_context(user_id: str) -> dict[str, object] | None:
    """Load only fields needed by the activity guard, without touching full profile state."""
    clean_user_id = str(user_id or "").strip()
    if not clean_user_id:
        return None
    conn = None
    try:
        conn = get_db_readonly()
        row = conn.execute(
            """
            SELECT CAST(id AS VARCHAR), COALESCE(role, 'member'), COALESCE(is_guest, FALSE)
            FROM shorts_users
            WHERE CAST(id AS VARCHAR) = ?
            LIMIT 1
            """,
            [clean_user_id],
        ).fetchone()
        if not row:
            return None
        return {"id": str(row[0]), "role": str(row[1] or "member"), "is_guest": bool(row[2])}
    except Exception:
        logger.exception("Activity user lookup failed; request will continue user_id=%s", clean_user_id)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def record_successful_login(user_id: str, method: str) -> None:
    """Best-effort audit for a completed customer authentication."""
    clean_user_id = str(user_id or "").strip()
    clean_method = str(method or "").strip().lower()
    if not clean_user_id or clean_method not in LOGIN_METHODS:
        logger.warning("Skipped invalid successful-login audit user_id=%s method=%s", clean_user_id, clean_method)
        return

    conn = None
    try:
        conn = get_db()
        conn.execute(
            "UPDATE shorts_users SET last_login_at = CURRENT_TIMESTAMP WHERE CAST(id AS VARCHAR) = ?",
            [clean_user_id],
        )
        conn.execute(
            """
            INSERT INTO user_login_events (user_id, logged_in_at, method)
            VALUES (?, CURRENT_TIMESTAMP, ?)
            """,
            [clean_user_id, clean_method],
        )
        conn.commit()
    except Exception:
        logger.exception(
            "Successful-login tracking failed; authentication will continue user_id=%s method=%s",
            clean_user_id,
            clean_method,
        )
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def should_track_customer_activity(current_user: Mapping[str, object] | None) -> bool:
    user = current_user or {}
    if not str(user.get("id") or "").strip():
        return False
    if str(user.get("role") or "member").strip().lower() == "admin":
        return False
    if bool(user.get("is_guest")):
        return False
    if request.endpoint in _EXCLUDED_ACTIVITY_ENDPOINTS:
        return False

    path = str(request.path or "")
    if path.startswith(("/static/", "/video_shorts/static/")):
        return False
    if path in {"/health", "/healthz", "/video_shorts/health", "/video_shorts/healthz"}:
        return False
    return True


def record_authenticated_activity(current_user: Mapping[str, object] | None) -> None:
    """Throttle customer activity writes and never interfere with the request."""
    if not should_track_customer_activity(current_user):
        return

    user_id = str((current_user or {}).get("id") or "").strip()
    conn = None
    try:
        conn = get_db()
        conn.execute(
            f"""
            UPDATE shorts_users
            SET last_active_at = CURRENT_TIMESTAMP
            WHERE CAST(id AS VARCHAR) = ?
              AND (
                    last_active_at IS NULL
                    OR last_active_at < CURRENT_TIMESTAMP - INTERVAL '{ACTIVITY_THROTTLE_MINUTES} minutes'
                  )
            """,
            [user_id],
        )
        conn.commit()
    except Exception:
        logger.exception(
            "Authenticated activity tracking failed; request will continue user_id=%s endpoint=%s",
            user_id,
            request.endpoint,
        )
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
