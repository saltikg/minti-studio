import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app, g, has_app_context, has_request_context
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.video_shorts.config import (
    YOUTUBE_CLIENT_ID,
    YOUTUBE_CLIENT_SECRET,
    YOUTUBE_OAUTH_SCOPES,
    YOUTUBE_REDIRECT_URI,
)
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
IDENTITY_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]

SCOPES = [scope.strip() for scope in (YOUTUBE_OAUTH_SCOPES or "").split(",") if scope.strip()]
if not SCOPES:
    SCOPES = DEFAULT_SCOPES.copy()

for scope in DEFAULT_SCOPES:
    if scope not in SCOPES:
        SCOPES.append(scope)
for identity_scope in IDENTITY_SCOPES:
    if identity_scope not in SCOPES:
        SCOPES.append(identity_scope)
LEGACY_TOKEN_TABLE = "youtube_oauth_tokens"
TOKEN_TABLE = "youtube_oauth_tokens_v2"
TOKEN_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TOKEN_TABLE} (
    user_id VARCHAR PRIMARY KEY,
    refresh_token TEXT NOT NULL,
    scopes TEXT,
    updated_at TEXT,
    reauth_required INTEGER DEFAULT 0
)
"""
LEGACY_TOKEN_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {LEGACY_TOKEN_TABLE} (
    id INTEGER PRIMARY KEY,
    refresh_token TEXT NOT NULL,
    scopes TEXT,
    updated_at TEXT,
    reauth_required INTEGER DEFAULT 0
)
"""

logger = logging.getLogger(__name__)


def _normalize_user_id(user_id: Optional[str] = None, brand_id: Optional[str] = None) -> Optional[str]:
    value = user_id
    if value is None and has_request_context():
        current_user = getattr(g, "vs_current_user", None) or {}
        value = current_user.get("id")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    scoped_brand_id = brand_id or current_brand_id()
    if scoped_brand_id and "::" not in text:
        return f"{text}::{scoped_brand_id}"
    return text


def _ensure_token_tables(conn):
    if getattr(conn, "backend_name", "") == "postgres":
        return
    try:
        conn.execute(TOKEN_TABLE_SQL)
        columns = list(table_columns(conn, TOKEN_TABLE))
        if "reauth_required" not in columns:
            conn.execute(
                f"ALTER TABLE {TOKEN_TABLE} ADD COLUMN reauth_required INTEGER DEFAULT 0"
            )
        conn.execute(LEGACY_TOKEN_TABLE_SQL)
        columns = list(table_columns(conn, LEGACY_TOKEN_TABLE))
        if "reauth_required" not in columns:
            conn.execute(
                f"ALTER TABLE {LEGACY_TOKEN_TABLE} ADD COLUMN reauth_required INTEGER DEFAULT 0"
            )
        legacy_count = conn.execute(
            f"SELECT COUNT(*) FROM {LEGACY_TOKEN_TABLE} WHERE id = 1"
        ).fetchone()[0]
        v2_count = conn.execute(
            f"SELECT COUNT(*) FROM {TOKEN_TABLE}"
        ).fetchone()[0]
        if legacy_count and not v2_count:
            legacy_row = conn.execute(
                f"""
                SELECT refresh_token, scopes, updated_at, COALESCE(reauth_required, 0)
                FROM {LEGACY_TOKEN_TABLE}
                WHERE id = 1
                LIMIT 1
                """
            ).fetchone()
            if legacy_row and legacy_row[0]:
                try:
                    admin_row = conn.execute(
                        """
                        SELECT CAST(id AS VARCHAR)
                        FROM shorts_users
                        WHERE lower(COALESCE(role, '')) = 'admin'
                        LIMIT 1
                        """
                    ).fetchone()
                except Exception:
                    admin_row = None
                if admin_row and admin_row[0]:
                    conn.execute(
                        f"""
                        INSERT INTO {TOKEN_TABLE} (user_id, refresh_token, scopes, updated_at, reauth_required)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (user_id) DO UPDATE SET
                            refresh_token = excluded.refresh_token,
                            scopes = excluded.scopes,
                            updated_at = excluded.updated_at,
                            reauth_required = excluded.reauth_required
                        """,
                        [
                            str(admin_row[0]),
                            legacy_row[0],
                            legacy_row[1] or ",".join(SCOPES),
                            legacy_row[2] or datetime.datetime.utcnow().isoformat(),
                            int(legacy_row[3] or 0),
                        ],
                    )
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise


def store_refresh_token(
    refresh_token: str,
    scopes: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
):
    conn = get_db()
    try:
        _ensure_token_tables(conn)
        normalized_user_id = _normalize_user_id(user_id, brand_id)
        if normalized_user_id:
            conn.execute(
                f"""
                INSERT INTO {TOKEN_TABLE} (user_id, refresh_token, scopes, updated_at, reauth_required)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT (user_id) DO UPDATE SET
                    refresh_token = excluded.refresh_token,
                    scopes = excluded.scopes,
                    updated_at = excluded.updated_at,
                    reauth_required = 0
                """,
                [
                    normalized_user_id,
                    refresh_token,
                    ",".join(scopes or SCOPES),
                    datetime.datetime.utcnow().isoformat(),
                ],
            )
        else:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {LEGACY_TOKEN_TABLE} (id, refresh_token, scopes, updated_at, reauth_required)
                VALUES (1, ?, ?, ?, 0)
                """,
                [refresh_token, ",".join(scopes or SCOPES), datetime.datetime.utcnow().isoformat()],
            )
        conn.commit()
    finally:
        conn.close()


def _get_default_token_row(conn) -> Optional[Tuple[str, int]]:
    try:
        row = conn.execute(
            f"""
            SELECT
              t.refresh_token,
              COALESCE(t.reauth_required, 0)
            FROM {TOKEN_TABLE} t
            LEFT JOIN shorts_users u
              ON CAST(u.id AS VARCHAR) = t.user_id
            ORDER BY
              CASE WHEN lower(COALESCE(u.role, '')) = 'admin' THEN 0 ELSE 1 END,
              t.updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        row = conn.execute(
            f"""
            SELECT refresh_token, COALESCE(reauth_required, 0)
            FROM {TOKEN_TABLE}
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    if row and row[0]:
        return row[0], int(row[1] or 0)
    return None


def _get_token_row(user_id: Optional[str] = None, brand_id: Optional[str] = None) -> Tuple[Optional[str], int]:
    try:
        conn = get_db_readonly()
    except Exception as exc:
        if "lock" in str(exc).lower():
            return None, 0
        raise
    try:
        try:
            _ensure_token_tables(conn)
        except Exception as exc:
            if "read-only" in str(exc).lower():
                return None, 0
            raise
        normalized_user_id = _normalize_user_id(user_id, brand_id)
        try:
            if normalized_user_id:
                row = conn.execute(
                    f"""
                    SELECT refresh_token, COALESCE(reauth_required, 0)
                    FROM {TOKEN_TABLE}
                    WHERE user_id = ?
                    LIMIT 1
                    """,
                    [normalized_user_id],
                ).fetchone()
            else:
                row = _get_default_token_row(conn)
        except Exception as exc:
            if TOKEN_TABLE in str(exc).lower():
                row = None
            elif LEGACY_TOKEN_TABLE in str(exc).lower():
                row = None
            else:
                raise
        if normalized_user_id and row:
            return row[0], int(row[1] or 0)
        if not normalized_user_id and row:
            return row[0], int(row[1] or 0)
        if normalized_user_id:
            return None, 0
        if not row or not row[0]:
            try:
                legacy_row = conn.execute(
                    f"""
                    SELECT refresh_token, COALESCE(reauth_required, 0)
                    FROM {LEGACY_TOKEN_TABLE}
                    WHERE id = 1
                    """
                ).fetchone()
            except Exception as exc:
                if LEGACY_TOKEN_TABLE in str(exc).lower():
                    return None, 0
                raise
            if not legacy_row:
                return None, 0
            return legacy_row[0], int(legacy_row[1] or 0)
        return None, 0
    finally:
        conn.close()


def get_stored_refresh_token(user_id: Optional[str] = None, brand_id: Optional[str] = None) -> Optional[str]:
    token, reauth_required = _get_token_row(user_id=user_id, brand_id=brand_id)
    if reauth_required:
        return None
    return token


def list_stored_refresh_tokens() -> List[Dict[str, object]]:
    try:
        conn = get_db_readonly()
    except Exception as exc:
        if "lock" in str(exc).lower():
            return []
        raise
    try:
        try:
            _ensure_token_tables(conn)
        except Exception as exc:
            if "read-only" in str(exc).lower():
                return []
            raise
        rows = conn.execute(
            f"""
            SELECT user_id, refresh_token, scopes, updated_at, COALESCE(reauth_required, 0)
            FROM {TOKEN_TABLE}
            ORDER BY updated_at DESC
            """
        ).fetchall()
    except Exception as exc:
        if TOKEN_TABLE in str(exc).lower():
            return []
        raise
    finally:
        conn.close()
    return [
        {
            "user_id": row[0],
            "refresh_token": row[1],
            "scopes": row[2],
            "updated_at": row[3],
            "reauth_required": bool(row[4]),
        }
        for row in rows
        if row and row[0] and row[1]
    ]


def is_reauth_required(user_id: Optional[str] = None, brand_id: Optional[str] = None) -> bool:
    _, reauth_required = _get_token_row(user_id=user_id, brand_id=brand_id)
    return bool(reauth_required)


def mark_refresh_token_invalid(user_id: Optional[str] = None, brand_id: Optional[str] = None) -> None:
    conn = get_db()
    try:
        _ensure_token_tables(conn)
        normalized_user_id = _normalize_user_id(user_id, brand_id)
        target_user_id = normalized_user_id
        if not target_user_id:
            default_row = conn.execute(
                f"""
                SELECT user_id
                FROM {TOKEN_TABLE}
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            if default_row and default_row[0]:
                target_user_id = str(default_row[0])
        if target_user_id:
            conn.execute(
                f"""
                UPDATE {TOKEN_TABLE}
                   SET reauth_required = 1,
                       updated_at = ?
                 WHERE user_id = ?
                """,
                [datetime.datetime.utcnow().isoformat(), target_user_id],
            )
        else:
            conn.execute(
                f"""
                UPDATE {LEGACY_TOKEN_TABLE}
                   SET reauth_required = 1,
                       updated_at = ?
                 WHERE id = 1
                """,
                [datetime.datetime.utcnow().isoformat()],
            )
        conn.commit()
    finally:
        conn.close()


def has_refresh_token(user_id: Optional[str] = None, brand_id: Optional[str] = None) -> bool:
    return bool(get_stored_refresh_token(user_id=user_id, brand_id=brand_id))


def build_oauth_flow(state: Optional[str] = None) -> Flow:
    client_config = {
        "web": {
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [YOUTUBE_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=YOUTUBE_REDIRECT_URI,
    )
    if state:
        flow.state = state
    return flow


def upload_video_with_refresh_token(
    video_path: str,
    title: str,
    description: str,
    publish_at: Optional[str] = None,
    refresh_token: Optional[str] = None,
    privacy_status: str = "private",
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
):
    youtube = build_authenticated_youtube(refresh_token, user_id=user_id, brand_id=brand_id)
    if not youtube:
        raise ValueError("No refresh token is available for YouTube upload.")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy_status,
        },
    }
    if publish_at:
        body["status"]["publishAt"] = publish_at

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    return response


def update_video_with_refresh_token(
    video_id: str,
    title: str,
    description: str,
    publish_at: Optional[str] = None,
    privacy_status: str = "private",
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
):
    youtube = build_authenticated_youtube(user_id=user_id, brand_id=brand_id)
    if not youtube:
        raise ValueError("No refresh token is available for YouTube update.")
    body = {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy_status,
        },
    }
    if publish_at:
        body["status"]["publishAt"] = publish_at

    request = youtube.videos().update(part="snippet,status", body=body)
    response = request.execute()
    return response


def _refresh_credentials(
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Optional[Credentials]:
    token = refresh_token or get_stored_refresh_token(user_id=user_id, brand_id=brand_id)
    if not token:
        return None
    creds = Credentials(
        token=None,
        refresh_token=token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        message = str(exc).lower()
        if "invalid_grant" in message or "unauthorized_client" in message:
            log = current_app.logger if has_app_context() else logger
            log.warning(
                "YouTube refresh token rejected (%s); marking reauth required.",
                "unauthorized_client" if "unauthorized_client" in message else "invalid_grant",
            )
            mark_refresh_token_invalid(user_id=user_id, brand_id=brand_id)
            return None
        raise
    return creds


def build_authenticated_youtube(
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
):
    creds = _refresh_credentials(refresh_token, user_id=user_id, brand_id=brand_id)
    if not creds:
        return None
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def build_authenticated_youtube_analytics(
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
):
    creds = _refresh_credentials(refresh_token, user_id=user_id, brand_id=brand_id)
    if not creds:
        return None
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def get_access_token(
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Optional[str]:
    """
    Return a fresh OAuth access token for YouTube API calls.
    """
    creds = _refresh_credentials(refresh_token, user_id=user_id, brand_id=brand_id)
    if not creds:
        return None
    return creds.token


def fetch_video_statuses(
    video_ids: List[str],
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    if not video_ids:
        return {}
    youtube = build_authenticated_youtube(refresh_token, user_id=user_id, brand_id=brand_id)
    if not youtube:
        return {}
    statuses: Dict[str, Dict[str, Any]] = {}
    try:
        response = youtube.videos().list(part="status", id=",".join(video_ids)).execute()
        for item in response.get("items", []):
            vid = item.get("id")
            if not vid:
                continue
            statuses[vid] = item.get("status", {})
    except Exception:
        current_app.logger.exception("Failed to fetch YouTube video statuses")
    return statuses

def clear_refresh_token(user_id: Optional[str] = None, brand_id: Optional[str] = None) -> None:
    conn = get_db()
    try:
        _ensure_token_tables(conn)
        normalized_user_id = _normalize_user_id(user_id, brand_id)
        target_user_id = normalized_user_id
        if not target_user_id and not normalized_user_id:
            default_row = conn.execute(
                f"""
                SELECT user_id
                FROM {TOKEN_TABLE}
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            if default_row and default_row[0]:
                target_user_id = str(default_row[0])
        if target_user_id:
            conn.execute(f"DELETE FROM {TOKEN_TABLE} WHERE user_id = ?", [target_user_id])
        else:
            conn.execute(f"DELETE FROM {LEGACY_TOKEN_TABLE} WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


def get_connected_channel_info(
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Tuple[Optional[Dict[str, object]], Optional[str]]:
    token = get_stored_refresh_token(user_id=user_id, brand_id=brand_id)
    if not token:
        return None, None
    creds = Credentials(
        token=None,
        refresh_token=token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = youtube.channels().list(part="snippet,contentDetails,statistics", mine=True, maxResults=1).execute()
        items = resp.get("items", [])
        if not items:
            return None, None
        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics") or {}
        try:
            subscriber_count = int(stats.get("subscriberCount"))
        except (TypeError, ValueError):
            subscriber_count = None
        return {
            "title": snippet.get("title", "Unknown channel"),
            "description": snippet.get("description", "")[:180],
            "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url"),
            "subscriber_count": subscriber_count,
        }, None
    except RefreshError as exc:
        current_app.logger.warning("YouTube channel info fetch failed due to refresh error: %s", exc)
        return None, "invalid_grant"
    except Exception as exc:  # pragma: no cover - network failures
        current_app.logger.exception("YouTube channel info fetch failed: %s", exc)
        return None, "general_error"
