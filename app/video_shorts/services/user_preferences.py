from typing import Optional
from uuid import uuid4

from app.video_shorts.services.db import ensure_user_preferences_schema, get_db, get_db_readonly


def load_user_preference(user_id: Optional[str], preference_key: str) -> Optional[str]:
    if not user_id or not preference_key:
        return None
    conn = get_db_readonly()
    try:
        ensure_user_preferences_schema(conn)
        row = conn.execute(
            """
            SELECT preference_value
            FROM shorts_user_preferences
            WHERE user_id = ? AND preference_key = ?
            LIMIT 1
            """,
            [user_id, preference_key],
        ).fetchone()
        if not row or row[0] is None:
            return None
        value = str(row[0]).strip()
        return value or None
    finally:
        conn.close()


def save_user_preference(user_id: str, preference_key: str, value: Optional[str]) -> None:
    if not user_id or not preference_key:
        return
    conn = get_db()
    try:
        ensure_user_preferences_schema(conn)
        conn.execute(
            """
            DELETE FROM shorts_user_preferences
            WHERE user_id = ? AND preference_key = ?
            """,
            [user_id, preference_key],
        )
        clean_value = str(value or "").strip()
        if clean_value:
            conn.execute(
                """
                INSERT INTO shorts_user_preferences (id, user_id, preference_key, preference_value, updated_at)
                VALUES (?, ?, ?, ?, now())
                """,
                [str(uuid4()), user_id, preference_key, clean_value],
            )
        conn.commit()
    finally:
        conn.close()


def load_user_bool_preference(user_id: Optional[str], preference_key: str, default: bool = False) -> bool:
    if not user_id or not preference_key:
        return default
    conn = get_db_readonly()
    try:
        ensure_user_preferences_schema(conn)
        row = conn.execute(
            """
            SELECT preference_value
            FROM shorts_user_preferences
            WHERE user_id = ? AND preference_key = ?
            LIMIT 1
            """,
            [user_id, preference_key],
        ).fetchone()
        if not row:
            return default
        raw = str(row[0] or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return default
    finally:
        conn.close()


def save_user_bool_preference(user_id: str, preference_key: str, value: bool) -> None:
    if not user_id or not preference_key:
        return
    conn = get_db()
    try:
        ensure_user_preferences_schema(conn)
        conn.execute(
            """
            DELETE FROM shorts_user_preferences
            WHERE user_id = ? AND preference_key = ?
            """,
            [user_id, preference_key],
        )
        conn.execute(
            """
            INSERT INTO shorts_user_preferences (id, user_id, preference_key, preference_value, updated_at)
            VALUES (?, ?, ?, ?, now())
            """,
            [str(uuid4()), user_id, preference_key, "true" if value else "false"],
        )
        conn.commit()
    finally:
        conn.close()
