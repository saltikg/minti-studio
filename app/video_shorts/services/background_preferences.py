from typing import Optional
from uuid import uuid4

from app.video_shorts.services.db import ensure_background_preferences_schema, get_db, get_db_readonly


def load_background_preference(user_id: Optional[str], brand_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    conn = get_db_readonly()
    try:
        ensure_background_preferences_schema(conn)
        row = conn.execute(
            """
            SELECT background_key
            FROM shorts_background_preferences
            WHERE user_id = ? AND coalesce(brand_id, '') = coalesce(?, '')
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            [user_id, brand_id],
        ).fetchone()
        return str(row[0]).strip() if row and row[0] else None
    finally:
        conn.close()


def save_background_preference(user_id: str, brand_id: Optional[str], background_key: Optional[str]) -> None:
    conn = get_db()
    try:
        ensure_background_preferences_schema(conn)
        conn.execute(
            """
            DELETE FROM shorts_background_preferences
            WHERE user_id = ? AND coalesce(brand_id, '') = coalesce(?, '')
            """,
            [user_id, brand_id],
        )
        if background_key:
            conn.execute(
                """
                INSERT INTO shorts_background_preferences (id, user_id, brand_id, background_key, updated_at)
                VALUES (?, ?, ?, ?, now())
                """,
                [str(uuid4()), user_id, brand_id, background_key],
            )
        conn.commit()
    finally:
        conn.close()
