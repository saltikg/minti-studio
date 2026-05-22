import datetime
from typing import Optional, Dict, List

from dotenv import load_dotenv

from app.video_shorts.services.brands import current_brand_id
from src.trends.token_store_db import connect_store, has_columns, relation_missing

load_dotenv()


class FacebookTokenStoreError(Exception):
    """Raised when the token store cannot be accessed."""


def _connect(read_only: bool = True, retries: int = 4):
    return connect_store(
        read_only=read_only,
        retries=retries,
        error_cls=FacebookTokenStoreError,
    )


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facebook_page_tokens (
            user_id VARCHAR PRIMARY KEY,
            fb_user_id VARCHAR,
            page_id VARCHAR,
            page_name VARCHAR,
            page_access_token VARCHAR,
            token_created_at VARCHAR,
            expires_at VARCHAR,
            scopes VARCHAR,
            updated_at VARCHAR
        )
        """
    )
    cols = {
        col for col in has_columns(conn, "facebook_page_tokens")
    }
    if "fb_user_id" not in cols:
        conn.execute("ALTER TABLE facebook_page_tokens ADD COLUMN fb_user_id VARCHAR")
    if "page_id" not in cols:
        conn.execute("ALTER TABLE facebook_page_tokens ADD COLUMN page_id VARCHAR")
    if "page_name" not in cols:
        conn.execute("ALTER TABLE facebook_page_tokens ADD COLUMN page_name VARCHAR")
    if "page_access_token" not in cols:
        conn.execute("ALTER TABLE facebook_page_tokens ADD COLUMN page_access_token VARCHAR")
    if "token_created_at" not in cols:
        conn.execute("ALTER TABLE facebook_page_tokens ADD COLUMN token_created_at VARCHAR")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE facebook_page_tokens ADD COLUMN expires_at VARCHAR")
    if "scopes" not in cols:
        conn.execute("ALTER TABLE facebook_page_tokens ADD COLUMN scopes VARCHAR")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE facebook_page_tokens ADD COLUMN updated_at VARCHAR")


def _scope_owner(user_id: Optional[str]) -> str:
    owner = user_id or "global"
    brand_id = current_brand_id()
    if brand_id and "::" not in owner:
        return f"{owner}::{brand_id}"
    return owner


def store_facebook_page_token(
    user_id: Optional[str],
    *,
    fb_user_id: Optional[str],
    page_id: str,
    page_name: Optional[str],
    page_access_token: str,
    expires_at: Optional[str],
    scopes: str,
) -> None:
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        token_owner = _scope_owner(user_id)
        now = datetime.datetime.utcnow().isoformat()
        conn.execute(
            """
            INSERT INTO facebook_page_tokens
            (user_id, fb_user_id, page_id, page_name, page_access_token, token_created_at, expires_at, scopes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
              fb_user_id = excluded.fb_user_id,
              page_id = excluded.page_id,
              page_name = excluded.page_name,
              page_access_token = excluded.page_access_token,
              token_created_at = excluded.token_created_at,
              expires_at = excluded.expires_at,
              scopes = excluded.scopes,
              updated_at = excluded.updated_at
            """,
            [
                token_owner,
                fb_user_id,
                page_id,
                page_name,
                page_access_token,
                now,
                expires_at,
                scopes,
                now,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_facebook_page_data(user_id: Optional[str] = None) -> Optional[Dict[str, Optional[str]]]:
    conn = _connect(read_only=True)
    try:
        if user_id:
            row = conn.execute(
                """
                SELECT user_id, fb_user_id, page_id, page_name, page_access_token, token_created_at, expires_at, scopes, updated_at
                FROM facebook_page_tokens
                WHERE user_id = ?
                LIMIT 1
                """,
                [_scope_owner(user_id)],
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT user_id, fb_user_id, page_id, page_name, page_access_token, token_created_at, expires_at, scopes, updated_at
                FROM facebook_page_tokens
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception as exc:
        if relation_missing(exc, "facebook_page_tokens"):
            return None
        raise
    finally:
        conn.close()
    if not row:
        return None
    return {
        "user_id": row[0],
        "fb_user_id": row[1],
        "page_id": row[2],
        "page_name": row[3],
        "page_access_token": row[4],
        "token_created_at": row[5],
        "expires_at": row[6],
        "scopes": row[7],
        "updated_at": row[8],
    }


def list_facebook_page_credentials() -> List[Dict[str, Optional[str]]]:
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT user_id, fb_user_id, page_id, page_name, page_access_token, scopes, expires_at, updated_at
            FROM facebook_page_tokens
            ORDER BY updated_at DESC
            """
        ).fetchall()
    except Exception as exc:
        if relation_missing(exc, "facebook_page_tokens"):
            return []
        raise
    finally:
        conn.close()
    return [
        {
            "user_id": row[0],
            "fb_user_id": row[1],
            "page_id": row[2],
            "page_name": row[3],
            "page_access_token": row[4],
            "scopes": row[5],
            "expires_at": row[6],
            "updated_at": row[7],
        }
        for row in rows
    ]


def clear_facebook_page_token(user_id: Optional[str] = None) -> None:
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        if user_id:
            conn.execute("DELETE FROM facebook_page_tokens WHERE user_id = ?", [_scope_owner(user_id)])
        else:
            conn.execute("DELETE FROM facebook_page_tokens")
        conn.commit()
    finally:
        conn.close()
