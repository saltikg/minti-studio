import datetime
import json
from typing import Optional, Dict, List
import requests

from dotenv import load_dotenv

from app.video_shorts.config import (
    IG_API_BASE,
    IG_APP_ID,
    IG_APP_SECRET,
    IG_GRAPH_API_BASE,
    IG_TOKEN_REFRESH_BUFFER_DAYS,
)
from app.video_shorts.services.brands import current_brand_id
from src.trends.token_store_db import connect_store, has_columns, relation_missing

load_dotenv()


class InstagramTokenStoreError(Exception):
    """Raised when the token store cannot be accessed."""


def _connect(read_only: bool = True, retries: int = 2):
    return connect_store(
        read_only=read_only,
        retries=retries,
        error_cls=InstagramTokenStoreError,
    )


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_oauth_tokens (
            user_id VARCHAR PRIMARY KEY,
            page_access_token VARCHAR,
            instagram_business_account_id VARCHAR,
            instagram_username VARCHAR,
            facebook_page_id VARCHAR,
            facebook_page_name VARCHAR,
            meta_fb_user_id VARCHAR,
            selected_page_id VARCHAR,
            instagram_user_id VARCHAR,
            instagram_account_type VARCHAR,
            token_created_at VARCHAR,
            token_refreshed_at VARCHAR,
            expires_at VARCHAR,
            scopes VARCHAR,
            updated_at VARCHAR
        )
        """
    )
    cols = {
        col for col in has_columns(conn, "instagram_oauth_tokens")
    }
    if "user_id" not in cols and "id" in cols:
        raise InstagramTokenStoreError("instagram_oauth_tokens table uses legacy id column")
    if "instagram_username" not in cols:
        conn.execute("ALTER TABLE instagram_oauth_tokens ADD COLUMN instagram_username VARCHAR")
    if "meta_fb_user_id" not in cols:
        conn.execute("ALTER TABLE instagram_oauth_tokens ADD COLUMN meta_fb_user_id VARCHAR")
    if "selected_page_id" not in cols:
        conn.execute("ALTER TABLE instagram_oauth_tokens ADD COLUMN selected_page_id VARCHAR")
    if "instagram_user_id" not in cols:
        conn.execute("ALTER TABLE instagram_oauth_tokens ADD COLUMN instagram_user_id VARCHAR")
    if "instagram_account_type" not in cols:
        conn.execute("ALTER TABLE instagram_oauth_tokens ADD COLUMN instagram_account_type VARCHAR")
    if "token_created_at" not in cols:
        conn.execute("ALTER TABLE instagram_oauth_tokens ADD COLUMN token_created_at VARCHAR")
    if "token_refreshed_at" not in cols:
        conn.execute("ALTER TABLE instagram_oauth_tokens ADD COLUMN token_refreshed_at VARCHAR")


def _ensure_pending_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS instagram_oauth_pending (
            user_id VARCHAR PRIMARY KEY,
            pages_json VARCHAR,
            expires_at VARCHAR,
            scopes VARCHAR,
            updated_at VARCHAR
        )
        """
    )


def _scope_owner(user_id: Optional[str]) -> str:
    owner = user_id or "global"
    brand_id = current_brand_id()
    if brand_id and "::" not in owner:
        return f"{owner}::{brand_id}"
    return owner


def store_instagram_token(
    user_id: Optional[str],
    page_access_token: str,
    instagram_business_account_id: str,
    instagram_username: Optional[str],
    facebook_page_id: str,
    facebook_page_name: str,
    meta_fb_user_id: Optional[str] = None,
    selected_page_id: Optional[str] = None,
    instagram_user_id: Optional[str] = None,
    instagram_account_type: Optional[str] = None,
    expires_at: Optional[str] = None,
    scopes: str = "",
):
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        token_owner = _scope_owner(user_id)
        now = datetime.datetime.utcnow().isoformat()
        conn.execute(
            """
            INSERT INTO instagram_oauth_tokens
            (user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, instagram_user_id, instagram_account_type, token_created_at, token_refreshed_at, expires_at, scopes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
              page_access_token = excluded.page_access_token,
              instagram_business_account_id = excluded.instagram_business_account_id,
              instagram_username = excluded.instagram_username,
              facebook_page_id = excluded.facebook_page_id,
              facebook_page_name = excluded.facebook_page_name,
              meta_fb_user_id = excluded.meta_fb_user_id,
              selected_page_id = excluded.selected_page_id,
              instagram_user_id = excluded.instagram_user_id,
              instagram_account_type = excluded.instagram_account_type,
              token_created_at = excluded.token_created_at,
              token_refreshed_at = excluded.token_refreshed_at,
              expires_at = excluded.expires_at,
              scopes = excluded.scopes,
              updated_at = excluded.updated_at
            """,
            [
                token_owner,
                page_access_token,
                instagram_business_account_id,
                instagram_username,
                facebook_page_id,
                facebook_page_name,
                meta_fb_user_id,
                selected_page_id,
                instagram_user_id,
                instagram_account_type,
                now,
                now,
                expires_at,
                scopes,
                now,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_instagram_data(user_id: Optional[str] = None) -> Optional[Dict[str, Optional[str]]]:
    try:
        conn = _connect(read_only=True)
    except InstagramTokenStoreError:
        return None
    try:
        if user_id:
            row = conn.execute(
                """
                SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, instagram_user_id, instagram_account_type, token_created_at, token_refreshed_at, expires_at, scopes, updated_at
                FROM instagram_oauth_tokens
                WHERE user_id = ?
                LIMIT 1
                """,
                [_scope_owner(user_id)],
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, instagram_user_id, instagram_account_type, token_created_at, token_refreshed_at, expires_at, scopes, updated_at
                FROM instagram_oauth_tokens
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception as exc:
        if relation_missing(exc, "instagram_oauth_tokens"):
            row = None
        else:
            raise
    finally:
        conn.close()
    if row is None:
        try:
            conn = _connect(read_only=False)
        except InstagramTokenStoreError:
            return None
        try:
            _ensure_table(conn)
            if user_id:
                row = conn.execute(
                    """
                    SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, instagram_user_id, instagram_account_type, token_created_at, token_refreshed_at, expires_at, scopes, updated_at
                    FROM instagram_oauth_tokens
                    WHERE user_id = ?
                    LIMIT 1
                    """,
                    [_scope_owner(user_id)],
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, instagram_user_id, instagram_account_type, token_created_at, token_refreshed_at, expires_at, scopes, updated_at
                    FROM instagram_oauth_tokens
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ).fetchone()
        except Exception as exc:
            if relation_missing(exc, "instagram_oauth_tokens"):
                return None
            raise
        finally:
            conn.close()
    if not row:
        return None
    return {
        "user_id": row[0],
        "page_access_token": row[1],
        "instagram_business_account_id": row[2],
        "instagram_username": row[3],
        "facebook_page_id": row[4],
        "facebook_page_name": row[5],
        "meta_fb_user_id": row[6],
        "selected_page_id": row[7],
        "instagram_user_id": row[8],
        "instagram_account_type": row[9],
        "token_created_at": row[10],
        "token_refreshed_at": row[11],
        "expires_at": row[12],
        "scopes": row[13],
        "updated_at": row[14],
    }


def get_instagram_credentials(user_id: Optional[str] = None) -> Optional[Dict[str, Optional[str]]]:
    data = get_instagram_data(user_id=user_id)
    if data and data.get("page_access_token") and data.get("instagram_business_account_id"):
        try:
            refreshed = refresh_instagram_token_if_needed(user_id=user_id, current=data)
            return refreshed or data
        except Exception:
            return data
    return None


def list_instagram_credentials() -> List[Dict[str, Optional[str]]]:
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, updated_at
            FROM instagram_oauth_tokens
            ORDER BY updated_at DESC
            """
        ).fetchall()
    except Exception as exc:
        if relation_missing(exc, "instagram_oauth_tokens"):
            return []
        raise
    finally:
        conn.close()
    results: List[Dict[str, Optional[str]]] = []
    for row in rows:
        results.append(
            {
                "user_id": row[0],
                "page_access_token": row[1],
                "instagram_business_account_id": row[2],
                "instagram_username": row[3],
                "facebook_page_id": row[4],
                "facebook_page_name": row[5],
                "updated_at": row[6],
            }
        )
    return results


def clear_instagram_token(user_id: Optional[str] = None):
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        if user_id:
            conn.execute(
                "DELETE FROM instagram_oauth_tokens WHERE user_id = ?",
                [_scope_owner(user_id)],
            )
        else:
            conn.execute("DELETE FROM instagram_oauth_tokens")
        conn.commit()
    finally:
        conn.close()


def store_pending_instagram_choices(
    user_id: str,
    pages: List[Dict[str, Optional[str]]],
    expires_at: Optional[str],
    scopes: str,
):
    conn = _connect(read_only=False)
    try:
        _ensure_pending_table(conn)
        now = datetime.datetime.utcnow().isoformat()
        conn.execute(
            """
            INSERT INTO instagram_oauth_pending (user_id, pages_json, expires_at, scopes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
              pages_json = excluded.pages_json,
              expires_at = excluded.expires_at,
              scopes = excluded.scopes,
              updated_at = excluded.updated_at
            """,
            [
                _scope_owner(user_id),
                json.dumps(pages),
                expires_at,
                scopes,
                now,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_instagram_choices(user_id: str) -> Optional[Dict[str, Optional[str]]]:
    conn = _connect(read_only=True)
    try:
        row = conn.execute(
            """
            SELECT pages_json, expires_at, scopes
            FROM instagram_oauth_pending
            WHERE user_id = ?
            LIMIT 1
            """,
            [_scope_owner(user_id)],
        ).fetchone()
    except Exception as exc:
        if relation_missing(exc, "instagram_oauth_pending"):
            return None
        raise
    finally:
        conn.close()
    if not row:
        return None
    try:
        pages = json.loads(row[0] or "[]")
    except Exception:
        pages = []
    return {
        "pages": pages,
        "expires_at": row[1],
        "scopes": row[2],
    }


def clear_pending_instagram_choices(user_id: str):
    conn = _connect(read_only=False)
    try:
        _ensure_pending_table(conn)
        conn.execute(
            "DELETE FROM instagram_oauth_pending WHERE user_id = ?",
            [_scope_owner(user_id)],
        )
        conn.commit()
    finally:
        conn.close()


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        if str(value).endswith("Z"):
            return datetime.datetime.fromisoformat(str(value)[:-1] + "+00:00")
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _fetch_instagram_account_profile(access_token: str, instagram_user_id: Optional[str] = None) -> Dict[str, Optional[str]]:
    params = {"fields": "user_id,username"}
    resp = requests.get(
        f"{IG_GRAPH_API_BASE.rstrip('/')}/me",
        params={**params, "access_token": access_token},
        timeout=12,
    )
    resp.raise_for_status()
    me_payload = resp.json() or {}
    normalized_user_id = str(me_payload.get("user_id") or instagram_user_id or "").strip()
    profile = {
        "instagram_user_id": normalized_user_id,
        "instagram_username": me_payload.get("username"),
        "instagram_account_type": None,
    }
    if not normalized_user_id:
        return profile
    detail_resp = requests.get(
        f"{IG_API_BASE.rstrip('/')}/{normalized_user_id}",
        params={"fields": "id,username,account_type", "access_token": access_token},
        timeout=12,
    )
    detail_resp.raise_for_status()
    detail_payload = detail_resp.json() or {}
    profile["instagram_user_id"] = str(detail_payload.get("id") or normalized_user_id)
    profile["instagram_username"] = detail_payload.get("username") or profile["instagram_username"]
    profile["instagram_account_type"] = detail_payload.get("account_type")
    return profile


def refresh_instagram_token_if_needed(
    *,
    user_id: Optional[str],
    current: Optional[Dict[str, Optional[str]]] = None,
    force: bool = False,
) -> Optional[Dict[str, Optional[str]]]:
    data = current or get_instagram_data(user_id=user_id)
    if not data or not data.get("page_access_token"):
        return data
    if not (IG_APP_ID and IG_APP_SECRET):
        return data
    expires_at = _parse_iso_datetime(data.get("expires_at"))
    now = datetime.datetime.now(datetime.timezone.utc)
    should_refresh = force
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        threshold = now + datetime.timedelta(days=max(1, int(IG_TOKEN_REFRESH_BUFFER_DAYS or 14)))
        should_refresh = should_refresh or expires_at <= threshold
    if not should_refresh:
        return data
    resp = requests.get(
        f"{IG_GRAPH_API_BASE.rstrip('/')}/refresh_access_token",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": data.get("page_access_token"),
        },
        timeout=12,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    refreshed_token = payload.get("access_token") or data.get("page_access_token")
    expires_in = payload.get("expires_in")
    refreshed_expires_at = data.get("expires_at")
    if expires_in:
        try:
            refreshed_expires_at = (
                now + datetime.timedelta(seconds=int(expires_in))
            ).replace(microsecond=0).isoformat()
        except Exception:
            refreshed_expires_at = data.get("expires_at")
    profile = _fetch_instagram_account_profile(
        refreshed_token,
        instagram_user_id=data.get("instagram_user_id") or data.get("instagram_business_account_id"),
    )
    store_instagram_token(
        user_id=user_id,
        page_access_token=refreshed_token,
        instagram_business_account_id=profile.get("instagram_user_id") or data.get("instagram_business_account_id") or "",
        instagram_username=profile.get("instagram_username") or data.get("instagram_username"),
        facebook_page_id=data.get("facebook_page_id") or "",
        facebook_page_name=data.get("facebook_page_name") or "",
        meta_fb_user_id=data.get("meta_fb_user_id"),
        selected_page_id=data.get("selected_page_id"),
        instagram_user_id=profile.get("instagram_user_id") or data.get("instagram_user_id"),
        instagram_account_type=profile.get("instagram_account_type") or data.get("instagram_account_type"),
        expires_at=refreshed_expires_at,
        scopes=data.get("scopes") or "",
    )
    return get_instagram_data(user_id=user_id)
