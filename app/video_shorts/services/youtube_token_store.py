"""The sole persistence boundary for both YouTube refresh-token tables.

All callers, including migration scripts, use this module. Never log token
values, database parameters, or provider response bodies.
"""
import datetime
import logging
import os
from typing import Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from flask import g, has_request_context

from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns


TOKEN_PREFIX = "enc:v1:"


class YouTubeTokenStoreError(RuntimeError):
    pass


def _cipher() -> Fernet:
    key = os.environ.get("YOUTUBE_TOKEN_ENC_KEY", "").strip()
    if not key:
        raise YouTubeTokenStoreError("YOUTUBE_TOKEN_ENC_KEY is required to save or decrypt YouTube tokens.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise YouTubeTokenStoreError("YOUTUBE_TOKEN_ENC_KEY must be a valid Fernet key.") from None


def _decode_refresh_token(value: str) -> str:
    if value.startswith(TOKEN_PREFIX):
        cipher = _cipher()
        try:
            return cipher.decrypt(value[len(TOKEN_PREFIX):].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError):
            raise YouTubeTokenStoreError("Unable to decrypt stored YouTube token; check the encryption key.") from None
    if value.startswith("enc:"):
        raise YouTubeTokenStoreError("Unsupported YouTube token encryption version.")
    logger.warning("Reading a legacy plaintext YouTube token; run the token encryption migration.")
    return value


def _encode_refresh_token(value: str) -> str:
    cipher = _cipher()  # Fail before any write if the key is unavailable.
    if not isinstance(value, str) or not value:
        raise YouTubeTokenStoreError("A nonempty YouTube refresh token is required.")
    if value.startswith("enc:"):
        value = _decode_refresh_token(value)
    return TOKEN_PREFIX + cipher.encrypt(value.encode("utf-8")).decode("ascii")


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


def split_scoped_user_id(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    text = str(value or "").strip()
    if not text:
        return None, None
    if "::" not in text:
        return text, None
    user_id, brand_id = text.split("::", 1)
    return (str(user_id or "").strip() or None, str(brand_id or "").strip() or None)


def resolve_stored_token_owner_brand(
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    clean_user_id = str(user_id or "").strip() or None
    clean_brand_id = str(brand_id or "").strip() or None
    normalized_user_id = _normalize_user_id(clean_user_id, clean_brand_id)
    try:
        conn = get_db_readonly()
    except Exception as exc:
        if "lock" in str(exc).lower():
            return clean_user_id, clean_brand_id
        raise
    try:
        try:
            _ensure_token_tables(conn)
        except Exception as exc:
            if "read-only" in str(exc).lower():
                return clean_user_id, clean_brand_id
            raise
        token_row = None
        if normalized_user_id:
            token_row = conn.execute(
                f"""
                SELECT user_id
                FROM {TOKEN_TABLE}
                WHERE user_id = ?
                LIMIT 1
                """,
                [normalized_user_id],
            ).fetchone()
        if not token_row and clean_user_id:
            token_row = conn.execute(
                f"""
                SELECT user_id
                FROM {TOKEN_TABLE}
                WHERE user_id = ?
                   OR user_id LIKE ?
                ORDER BY
                    CASE
                        WHEN user_id = ? THEN 0
                        WHEN user_id LIKE ? THEN 1
                        ELSE 2
                    END,
                    updated_at DESC
                LIMIT 1
                """,
                [
                    clean_user_id,
                    f"{clean_user_id}::%",
                    normalized_user_id or clean_user_id,
                    f"{clean_user_id}::%",
                ],
            ).fetchone()
    except Exception as exc:
        if TOKEN_TABLE in str(exc).lower():
            return clean_user_id, clean_brand_id
        raise
    finally:
        conn.close()
    if not token_row or not token_row[0]:
        return clean_user_id, clean_brand_id
    token_owner_user_id, token_brand_id = split_scoped_user_id(token_row[0])
    return token_owner_user_id or clean_user_id, token_brand_id or clean_brand_id


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
                            _encode_refresh_token(legacy_row[0]),
                            legacy_row[1] or "",
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
    encrypted_token = _encode_refresh_token(refresh_token)
    recorded_scopes = ",".join(sorted(set(scopes or [])))
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
                    encrypted_token,
                    recorded_scopes,
                    datetime.datetime.utcnow().isoformat(),
                ],
            )
        else:
            conn.execute(
                f"""
                INSERT INTO {LEGACY_TOKEN_TABLE} (id, refresh_token, scopes, updated_at, reauth_required)
                VALUES (1, ?, ?, ?, 0)
                ON CONFLICT (id) DO UPDATE SET refresh_token = excluded.refresh_token,
                    scopes = excluded.scopes, updated_at = excluded.updated_at, reauth_required = 0
                """,
                [encrypted_token, recorded_scopes, datetime.datetime.utcnow().isoformat()],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise YouTubeTokenStoreError("Could not save the encrypted YouTube token.") from None
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
            return _decode_refresh_token(row[0]), int(row[1] or 0)
        if not normalized_user_id and row:
            return _decode_refresh_token(row[0]), int(row[1] or 0)
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
            return _decode_refresh_token(legacy_row[0]), int(legacy_row[1] or 0)
        return None, 0
    finally:
        conn.close()


def get_stored_refresh_token(user_id: Optional[str] = None, brand_id: Optional[str] = None) -> Optional[str]:
    token, reauth_required = _get_token_row(user_id=user_id, brand_id=brand_id)
    if reauth_required:
        return None
    return token


def get_refresh_token_for_revocation(user_id: str, brand_id: Optional[str] = None) -> Optional[str]:
    # A rejected/expired token must still be offered to Google's revoke endpoint.
    if not user_id:
        raise YouTubeTokenStoreError("A user is required to disconnect YouTube.")
    return _get_token_row(user_id=user_id, brand_id=brand_id)[0]


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
            "refresh_token": _decode_refresh_token(row[1]),
            "scopes": row[2],
            "updated_at": row[3],
            "reauth_required": bool(row[4]),
        }
        for row in rows
        if row and row[0] and row[1]
    ]


def resolve_token_lookup_user_id(
    user_id: Optional[str],
    brand_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    normalized_user_id = _normalize_user_id(user_id, brand_id)
    if normalized_user_id:
        return normalized_user_id, "brand_scoped" if "::" in normalized_user_id else "plain"
    plain_user_id = str(user_id or "").strip()
    if not plain_user_id:
        return None, "missing_user_id"
    conn = get_db_readonly()
    try:
        try:
            _ensure_token_tables(conn)
        except Exception as exc:
            if "read-only" in str(exc).lower():
                return None, "token_table_unavailable"
            raise
        try:
            rows = conn.execute(
                f"""
                SELECT user_id
                FROM {TOKEN_TABLE}
                WHERE user_id = ?
                   OR user_id LIKE ?
                ORDER BY updated_at DESC
                """,
                [plain_user_id, f"{plain_user_id}::%"],
            ).fetchall()
        except Exception as exc:
            if TOKEN_TABLE in str(exc).lower():
                return None, "token_table_missing"
            raise
    finally:
        conn.close()
    keys = [str(row[0]).strip() for row in rows if row and row[0]]
    exact_keys = [key for key in keys if key == plain_user_id]
    scoped_keys = [key for key in keys if key.startswith(f"{plain_user_id}::")]
    if exact_keys:
        return exact_keys[0], "plain_exact"
    unique_keys = list(dict.fromkeys(scoped_keys))
    if len(unique_keys) == 1:
        return unique_keys[0], "single_scoped_fallback"
    if len(unique_keys) > 1:
        return None, "ambiguous_scoped_tokens"
    return None, "no_token"


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


def migrate_plaintext_refresh_tokens() -> Dict[str, int]:
    """Encrypt both tables in one transaction, without changing grants or metadata."""
    _cipher()
    conn = get_db()
    counts = {TOKEN_TABLE: 0, LEGACY_TOKEN_TABLE: 0}
    try:
        if getattr(conn, "backend_name", "") != "postgres":
            conn.execute("BEGIN TRANSACTION")
        for table, key in ((TOKEN_TABLE, "user_id"), (LEGACY_TOKEN_TABLE, "id")):
            if not table_columns(conn, table):
                continue
            rows = conn.execute(
                f"SELECT {key}, refresh_token FROM {table} WHERE refresh_token NOT LIKE ?",
                [TOKEN_PREFIX + "%"],
            ).fetchall()
            for row_id, plaintext in rows:
                encrypted = _encode_refresh_token(plaintext)
                # Do not overwrite a token replaced by a concurrent OAuth callback.
                changed = conn.execute(
                    f"UPDATE {table} SET refresh_token = ? WHERE {key} = ? AND refresh_token = ? RETURNING {key}",
                    [encrypted, row_id, plaintext],
                ).fetchone()
                counts[table] += int(changed is not None)
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise YouTubeTokenStoreError("YouTube token migration failed; transaction rolled back.") from None
    finally:
        conn.close()

