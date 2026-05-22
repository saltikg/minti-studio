from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from flask import g, has_request_context, session

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.video_metrics import ANALYTICS_ARCHIVE_TABLE, SNAPSHOT_TABLE

BRAND_TABLE = "shorts_brands"
BRAND_AWARE_TABLES: Tuple[Tuple[str, str], ...] = (
    ("youtube_channels", "owner_user_id"),
    ("youtube_videos", "owner_user_id"),
    ("shorts_storage_assets", "user_id"),
    ("shorts_categories", "user_id"),
    ("shorts_static_images", "user_id"),
    ("shorts_static_image_categories", "user_id"),
    ("image_to_video_jobs", "user_id"),
    ("shorts_instagram_queue", "user_id"),
    ("shorts_facebook_queue", "user_id"),
    ("shorts_tiktok_queue", "user_id"),
)


def _slugify_brand_name(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (name or "").strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "brand"


def ensure_brand_schema(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {BRAND_TABLE} (
            id VARCHAR PRIMARY KEY,
            owner_user_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            slug VARCHAR,
            is_default BOOLEAN DEFAULT false,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    try:
        user_cols = {row[1] for row in conn.execute("PRAGMA table_info('shorts_users')").fetchall()}
    except Exception:
        user_cols = set()
    if "last_brand_id" not in user_cols:
        try:
            conn.execute("ALTER TABLE shorts_users ADD COLUMN last_brand_id VARCHAR")
        except Exception:
            pass
    for table_name, _owner_col in BRAND_AWARE_TABLES:
        try:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()}
        except Exception:
            cols = set()
        if "brand_id" not in cols:
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN brand_id VARCHAR")
            except Exception:
                pass
    for table_name in (SNAPSHOT_TABLE, "shorts_channel_subscriber_daily", ANALYTICS_ARCHIVE_TABLE):
        try:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()}
        except Exception:
            cols = set()
        if cols and "brand_id" not in cols:
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN brand_id VARCHAR")
            except Exception:
                pass
    try:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{BRAND_TABLE}_owner ON {BRAND_TABLE}(owner_user_id)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{BRAND_TABLE}_owner_name ON {BRAND_TABLE}(owner_user_id, lower(name))"
        )
    except Exception:
        pass
    try:
        conn.commit()
    except Exception:
        pass


def list_user_brands(conn, user_id: str) -> List[Dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT id, name, slug, COALESCE(is_default, false) AS is_default, created_at, updated_at
        FROM {BRAND_TABLE}
        WHERE owner_user_id = ?
        ORDER BY COALESCE(is_default, false) DESC, lower(name), created_at
        """,
        [user_id],
    ).fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "slug": row[2],
            "is_default": bool(row[3]),
            "created_at": row[4],
            "updated_at": row[5],
        }
        for row in rows
    ]


def get_brand_for_user(conn, user_id: str, brand_id: Optional[str]) -> Optional[Dict[str, object]]:
    if not user_id or not brand_id:
        return None
    row = conn.execute(
        f"""
        SELECT id, name, slug, COALESCE(is_default, false) AS is_default, created_at, updated_at
        FROM {BRAND_TABLE}
        WHERE owner_user_id = ? AND id = ?
        LIMIT 1
        """,
        [user_id, brand_id],
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "slug": row[2],
        "is_default": bool(row[3]),
        "created_at": row[4],
        "updated_at": row[5],
    }


def create_brand(
    conn,
    *,
    user_id: str,
    name: str,
    make_default: bool = False,
) -> Dict[str, object]:
    brand_id = str(uuid4())
    brand_name = (name or "").strip() or "Default Brand"
    if make_default:
        try:
            conn.execute(
                f"UPDATE {BRAND_TABLE} SET is_default = false, updated_at = now() WHERE owner_user_id = ?",
                [user_id],
            )
        except Exception:
            pass
    conn.execute(
        f"""
        INSERT INTO {BRAND_TABLE} (id, owner_user_id, name, slug, is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, now(), now())
        """,
        [brand_id, user_id, brand_name, _slugify_brand_name(brand_name), bool(make_default)],
    )
    conn.execute(
        "UPDATE shorts_users SET last_brand_id = ?, updated_at = now() WHERE id = ?",
        [brand_id, user_id],
    )
    conn.commit()
    return get_brand_for_user(conn, user_id, brand_id) or {
        "id": brand_id,
        "name": brand_name,
        "slug": _slugify_brand_name(brand_name),
        "is_default": bool(make_default),
    }


def _default_brand_name(user_name: Optional[str]) -> str:
    if user_name:
        return f"{str(user_name).strip()}'s Brand"
    return "Default Brand"


def ensure_brand_for_user(
    conn,
    *,
    user_id: str,
    user_name: Optional[str] = None,
) -> Dict[str, object]:
    ensure_brand_schema(conn)
    brands = list_user_brands(conn, user_id)
    if not brands:
        brand = create_brand(
            conn,
            user_id=user_id,
            name=_default_brand_name(user_name),
            make_default=True,
        )
        _assign_unscoped_records_to_brand(conn, user_id=user_id, brand_id=brand["id"])
        return brand
    row = conn.execute(
        "SELECT last_brand_id FROM shorts_users WHERE id = ?",
        [user_id],
    ).fetchone()
    last_brand_id = row[0] if row else None
    chosen = None
    if last_brand_id:
        chosen = next((brand for brand in brands if brand["id"] == last_brand_id), None)
    if not chosen:
        chosen = next((brand for brand in brands if brand.get("is_default")), None) or brands[0]
    conn.execute(
        "UPDATE shorts_users SET last_brand_id = ?, updated_at = now() WHERE id = ?",
        [chosen["id"], user_id],
    )
    _assign_unscoped_records_to_brand(conn, user_id=user_id, brand_id=chosen["id"])
    conn.commit()
    return chosen


def _assign_unscoped_records_to_brand(conn, *, user_id: str, brand_id: str) -> None:
    if not user_id or not brand_id:
        return
    for table_name, owner_col in BRAND_AWARE_TABLES:
        try:
            conn.execute(
                f"""
                UPDATE {table_name}
                SET brand_id = ?
                WHERE {owner_col} = ?
                  AND brand_id IS NULL
                """,
                [brand_id, user_id],
            )
        except Exception:
            continue
    try:
        conn.execute(
            f"""
            UPDATE {SNAPSHOT_TABLE} m
            SET brand_id = c.brand_id
            FROM youtube_channels c
            WHERE m.brand_id IS NULL
              AND m.channel_type = 'youtube'
              AND CAST(m.channel_id AS VARCHAR) = CAST(c.channel_id AS VARCHAR)
              AND c.owner_user_id = ?
              AND c.brand_id IS NOT NULL
            """,
            [user_id],
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            UPDATE shorts_channel_subscriber_daily s
            SET brand_id = c.brand_id
            FROM youtube_channels c
            WHERE s.brand_id IS NULL
              AND s.channel_type = 'youtube'
              AND CAST(s.channel_id AS VARCHAR) = CAST(c.youtube_channel_id AS VARCHAR)
              AND c.owner_user_id = ?
              AND c.brand_id IS NOT NULL
            """,
            [user_id],
        )
    except Exception:
        pass


def load_brand_context(
    *,
    user_id: Optional[str],
    user_name: Optional[str] = None,
    requested_brand_id: Optional[str] = None,
) -> Tuple[Optional[Dict[str, object]], List[Dict[str, object]]]:
    if not user_id:
        return None, []
    conn = get_db()
    try:
        current_brand = ensure_brand_for_user(conn, user_id=user_id, user_name=user_name)
        brands = list_user_brands(conn, user_id)
        if requested_brand_id:
            chosen = get_brand_for_user(conn, user_id, requested_brand_id)
            if chosen:
                current_brand = chosen
                conn.execute(
                    "UPDATE shorts_users SET last_brand_id = ?, updated_at = now() WHERE id = ?",
                    [current_brand["id"], user_id],
                )
                conn.commit()
        return current_brand, brands
    finally:
        conn.close()


def set_active_brand_for_user(user_id: str, brand_id: str) -> Optional[Dict[str, object]]:
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        brand = get_brand_for_user(conn, user_id, brand_id)
        if not brand:
            return None
        conn.execute(
            "UPDATE shorts_users SET last_brand_id = ?, updated_at = now() WHERE id = ?",
            [brand_id, user_id],
        )
        conn.commit()
        return brand
    finally:
        conn.close()


def current_brand() -> Optional[Dict[str, object]]:
    if has_request_context():
        return getattr(g, "vs_current_brand", None)
    return None


def current_brand_id() -> Optional[str]:
    brand = current_brand()
    if brand:
        return str(brand.get("id") or "").strip() or None
    if has_request_context():
        value = session.get("vs_brand_id")
        return str(value).strip() if value else None
    return None


def brand_scoped_user_id(user_id: Optional[str], brand_id: Optional[str] = None) -> Optional[str]:
    if user_id is None:
        return None
    text = str(user_id).strip()
    if not text:
        return None
    if "::" in text:
        return text
    scoped_brand_id = brand_id or current_brand_id()
    if scoped_brand_id:
        return f"{text}::{scoped_brand_id}"
    return text


def scoped_brand_clause(
    conn,
    table_name: str,
    *,
    column_name: str = "brand_id",
    table_alias: Optional[str] = None,
) -> Tuple[str, List[object]]:
    brand_id = current_brand_id()
    if not brand_id:
        return "", []
    try:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()}
    except Exception:
        cols = set()
    if column_name not in cols:
        return "", []
    prefix = f"{table_alias}." if table_alias else ""
    return f" AND {prefix}{column_name} = ?", [brand_id]
