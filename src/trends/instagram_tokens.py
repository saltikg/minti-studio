import datetime
import json
from typing import Optional, Dict, List

from dotenv import load_dotenv

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
            token_created_at VARCHAR,
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
    if "token_created_at" not in cols:
        conn.execute("ALTER TABLE instagram_oauth_tokens ADD COLUMN token_created_at VARCHAR")


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
            (user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, token_created_at, expires_at, scopes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
              page_access_token = excluded.page_access_token,
              instagram_business_account_id = excluded.instagram_business_account_id,
              instagram_username = excluded.instagram_username,
              facebook_page_id = excluded.facebook_page_id,
              facebook_page_name = excluded.facebook_page_name,
              meta_fb_user_id = excluded.meta_fb_user_id,
              selected_page_id = excluded.selected_page_id,
              token_created_at = excluded.token_created_at,
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
                SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, token_created_at, expires_at, scopes, updated_at
                FROM instagram_oauth_tokens
                WHERE user_id = ?
                LIMIT 1
                """,
                [_scope_owner(user_id)],
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, token_created_at, expires_at, scopes, updated_at
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
                    SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, token_created_at, expires_at, scopes, updated_at
                    FROM instagram_oauth_tokens
                    WHERE user_id = ?
                    LIMIT 1
                    """,
                    [_scope_owner(user_id)],
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT user_id, page_access_token, instagram_business_account_id, instagram_username, facebook_page_id, facebook_page_name, meta_fb_user_id, selected_page_id, token_created_at, expires_at, scopes, updated_at
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
        "token_created_at": row[8],
        "expires_at": row[9],
        "scopes": row[10],
        "updated_at": row[11],
    }


def get_instagram_credentials(user_id: Optional[str] = None) -> Optional[Dict[str, Optional[str]]]:
    data = get_instagram_data(user_id=user_id)
    if data and data.get("page_access_token") and data.get("instagram_business_account_id"):
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
