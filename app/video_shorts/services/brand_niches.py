from __future__ import annotations

from typing import Any

from app.video_shorts.services.db import table_columns
from app.video_shorts.services.niches import (
    final_niche_pool,
    list_niches,
    load_niche_snapshot,
    near_miss_niche_rows,
)


BRAND_TABLE = "shorts_brands"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _ranked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -int(row.get("subscriber_count") or 0),
            _clean(row.get("title")).lower(),
        ),
    )


def build_niche_suggestion_index(
    niches_with_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> dict[str, dict[str, Any]]:
    suggestions: dict[str, dict[str, Any]] = {}
    for niche, rows in niches_with_rows:
        for group, ranked_rows in (
            ("pool", _ranked(final_niche_pool(rows, clinicians_only=False, niche=niche))),
            ("near_miss", _ranked(near_miss_niche_rows(rows, clinicians_only=False, niche=niche))),
        ):
            total = len(ranked_rows)
            for rank, row in enumerate(ranked_rows, start=1):
                channel_id = _clean(row.get("youtube_channel_id"))
                if not channel_id or channel_id in suggestions:
                    continue
                suggestions[channel_id] = {
                    "niche_id": _clean(niche.get("id")),
                    "niche_name": _clean(niche.get("name")),
                    "rank": rank,
                    "total": total,
                    "group": group,
                }
    return suggestions


def brand_publishing_channel_id(conn, owner_user_id: str, brand_id: str) -> str:
    """Return the brand's upload destination, never one of its content sources."""
    token_columns = table_columns(conn, "youtube_oauth_tokens_v2")
    if {"user_id", "youtube_channel_id"}.issubset(token_columns):
        token_order = "updated_at DESC" if "updated_at" in token_columns else "user_id"
        row = conn.execute(
            f"""
            SELECT youtube_channel_id
            FROM youtube_oauth_tokens_v2
            WHERE user_id = ?
              AND youtube_channel_id LIKE 'UC%%'
            ORDER BY {token_order}
            LIMIT 1
            """,
            [f"{owner_user_id}::{brand_id}"],
        ).fetchone()
        channel_id = _clean(row[0] if row else "")
        if channel_id.startswith("UC"):
            return channel_id

    lead_columns = table_columns(conn, "autopilot_leads")
    explicit_columns = {
        "user_id", "brand_id", "youtube_channel_id", "youtube_channel_connected_at",
    }
    if explicit_columns.issubset(lead_columns):
        lead_order = "youtube_channel_connected_at DESC"
        if "converted_at" in lead_columns:
            lead_order += ", converted_at DESC NULLS LAST"
        row = conn.execute(
            f"""
            SELECT youtube_channel_id
            FROM autopilot_leads
            WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND youtube_channel_connected_at IS NOT NULL
              AND youtube_channel_id LIKE 'UC%%'
            ORDER BY {lead_order}
            LIMIT 1
            """,
            [owner_user_id, brand_id],
        ).fetchone()
        channel_id = _clean(row[0] if row else "")
        if channel_id.startswith("UC"):
            return channel_id
    return ""


def brand_youtube_channel_ids(conn, owner_user_id: str, brand_id: str) -> set[str]:
    """Compatibility wrapper for code that expects a collection."""
    channel_id = brand_publishing_channel_id(conn, owner_user_id, brand_id)
    return {channel_id} if channel_id else set()


def load_admin_brand_niche_settings(conn, owner_user_id: str) -> dict[str, Any]:
    brand_columns = table_columns(conn, BRAND_TABLE)
    if not {"id", "owner_user_id", "name"}.issubset(brand_columns):
        return {"brands": [], "niches": []}

    niches = list_niches(conn)
    niche_rows = []
    for niche in niches:
        selected, rows = load_niche_snapshot(conn, niche["slug"])
        if selected:
            niche_rows.append((selected, rows))
    suggestion_index = build_niche_suggestion_index(niche_rows)

    niche_id_sql = "CAST(niche_id AS VARCHAR)" if "niche_id" in brand_columns else "NULL"
    brand_rows = conn.execute(
        f"""
        SELECT CAST(id AS VARCHAR), name, {niche_id_sql} AS niche_id
        FROM {BRAND_TABLE}
        WHERE CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
        ORDER BY COALESCE(is_default, false) DESC, lower(name), created_at
        """,
        [owner_user_id],
    ).fetchall()
    brands = []
    for row in brand_rows:
        brand_id = _clean(row[0])
        channel_id = brand_publishing_channel_id(conn, owner_user_id, brand_id)
        suggestion = suggestion_index.get(channel_id)
        brands.append(
            {
                "id": brand_id,
                "name": _clean(row[1]) or "Unnamed brand",
                "niche_id": _clean(row[2]),
                "suggestion": suggestion,
            }
        )
    return {
        "brands": brands,
        "niches": [
            {"id": _clean(niche["id"]), "name": _clean(niche["name"])}
            for niche in niches if _clean(niche.get("status")) != "draft"
        ],
    }


def update_brand_niche(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
    niche_id: str,
) -> str:
    if "niche_id" not in table_columns(conn, BRAND_TABLE):
        raise RuntimeError("Brand niche migration has not been applied.")
    brand = conn.execute(
        f"""
        SELECT CAST(b.id AS VARCHAR)
        FROM {BRAND_TABLE} b
        JOIN shorts_users u
          ON CAST(u.id AS VARCHAR) = CAST(b.owner_user_id AS VARCHAR)
        WHERE CAST(b.id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(b.owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
        LIMIT 1
        """,
        [brand_id, owner_user_id],
    ).fetchone()
    if not brand:
        raise LookupError("Brand not found for user.")
    selected_niche_id = _clean(niche_id)
    if selected_niche_id:
        niche = conn.execute(
            "SELECT CAST(id AS VARCHAR) FROM niches WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR) LIMIT 1",
            [selected_niche_id],
        ).fetchone()
        if not niche:
            raise ValueError("Choose a valid niche.")
    conn.execute(
        f"""
        UPDATE {BRAND_TABLE}
        SET niche_id = ?, updated_at = CURRENT_TIMESTAMP
        WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
        """,
        [selected_niche_id or None, brand_id, owner_user_id],
    )
    return selected_niche_id
