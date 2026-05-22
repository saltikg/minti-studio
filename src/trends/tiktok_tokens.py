import datetime
from typing import Optional, Dict, List

from dotenv import load_dotenv

from app.video_shorts.services.brands import current_brand_id
from src.trends.token_store_db import connect_store, has_columns, relation_missing

load_dotenv()


class TikTokTokenStoreError(Exception):
    """Raised when the token store cannot be accessed."""


def _connect(read_only: bool = True, retries: int = 2):
    return connect_store(
        read_only=read_only,
        retries=retries,
        error_cls=TikTokTokenStoreError,
    )


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tiktok_oauth_tokens (
            user_id VARCHAR PRIMARY KEY,
            access_token VARCHAR,
            refresh_token VARCHAR,
            open_id VARCHAR,
            username VARCHAR,
            display_name VARCHAR,
            scopes VARCHAR,
            expires_at VARCHAR,
            refresh_expires_at VARCHAR,
            updated_at VARCHAR
        )
        """
    )
    cols = {
        col for col in has_columns(conn, "tiktok_oauth_tokens")
    }
    if "username" not in cols:
        conn.execute("ALTER TABLE tiktok_oauth_tokens ADD COLUMN username VARCHAR")
    if "display_name" not in cols:
        conn.execute("ALTER TABLE tiktok_oauth_tokens ADD COLUMN display_name VARCHAR")
    if "refresh_expires_at" not in cols:
        conn.execute("ALTER TABLE tiktok_oauth_tokens ADD COLUMN refresh_expires_at VARCHAR")


def _scope_owner(user_id: Optional[str]) -> str:
    owner = user_id or "global"
    brand_id = current_brand_id()
    if brand_id and "::" not in owner:
        return f"{owner}::{brand_id}"
    return owner


def store_tiktok_token(
    user_id: Optional[str],
    access_token: str,
    refresh_token: Optional[str],
    open_id: Optional[str],
    username: Optional[str],
    display_name: Optional[str],
    scopes: str,
    expires_at: Optional[str],
    refresh_expires_at: Optional[str],
):
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        token_owner = _scope_owner(user_id)
        now = datetime.datetime.utcnow().isoformat()
        conn.execute(
            """
            INSERT INTO tiktok_oauth_tokens
            (user_id, access_token, refresh_token, open_id, username, display_name, scopes, expires_at, refresh_expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
              access_token = excluded.access_token,
              refresh_token = excluded.refresh_token,
              open_id = excluded.open_id,
              username = excluded.username,
              display_name = excluded.display_name,
              scopes = excluded.scopes,
              expires_at = excluded.expires_at,
              refresh_expires_at = excluded.refresh_expires_at,
              updated_at = excluded.updated_at
            """,
            [
                token_owner,
                access_token,
                refresh_token,
                open_id,
                username,
                display_name,
                scopes,
                expires_at,
                refresh_expires_at,
                now,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_tiktok_data(user_id: Optional[str] = None) -> Optional[Dict[str, Optional[str]]]:
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        if user_id:
            row = conn.execute(
                """
                SELECT user_id, access_token, refresh_token, open_id, username, display_name, scopes, expires_at, refresh_expires_at, updated_at
                FROM tiktok_oauth_tokens
                WHERE user_id = ?
                LIMIT 1
                """,
                [_scope_owner(user_id)],
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT user_id, access_token, refresh_token, open_id, username, display_name, scopes, expires_at, refresh_expires_at, updated_at
                FROM tiktok_oauth_tokens
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception as exc:
        if relation_missing(exc, "tiktok_oauth_tokens"):
            return None
        raise
    finally:
        conn.close()
    if not row:
        return None
    return {
        "user_id": row[0],
        "access_token": row[1],
        "refresh_token": row[2],
        "open_id": row[3],
        "username": row[4],
        "display_name": row[5],
        "scopes": row[6],
        "expires_at": row[7],
        "refresh_expires_at": row[8],
        "updated_at": row[9],
    }


def list_tiktok_credentials() -> List[Dict[str, Optional[str]]]:
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT user_id, access_token, refresh_token, open_id, username, display_name, scopes, expires_at, refresh_expires_at, updated_at
            FROM tiktok_oauth_tokens
            ORDER BY updated_at DESC
            """
        ).fetchall()
    except Exception as exc:
        if relation_missing(exc, "tiktok_oauth_tokens"):
            return []
        raise
    finally:
        conn.close()
    results: List[Dict[str, Optional[str]]] = []
    for row in rows:
        results.append(
            {
                "user_id": row[0],
                "access_token": row[1],
                "refresh_token": row[2],
                "open_id": row[3],
                "username": row[4],
                "display_name": row[5],
                "scopes": row[6],
                "expires_at": row[7],
                "refresh_expires_at": row[8],
                "updated_at": row[9],
            }
        )
    return results


def clear_tiktok_token(user_id: Optional[str] = None):
    conn = _connect(read_only=False)
    try:
        _ensure_table(conn)
        if user_id:
            conn.execute(
                "DELETE FROM tiktok_oauth_tokens WHERE user_id = ?",
                [_scope_owner(user_id)],
            )
        else:
            conn.execute("DELETE FROM tiktok_oauth_tokens")
        conn.commit()
    finally:
        conn.close()
