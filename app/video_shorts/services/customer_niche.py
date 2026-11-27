from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

from app.video_shorts.services.brand_niches import brand_publishing_channel_id
from app.video_shorts.services.db import table_columns
from app.video_shorts.services.niche_outliers import (
    customer_visible_outlier_rows,
    load_latest_niche_outliers,
)
from app.video_shorts.services.niche_weekly_ideas import load_brand_weekly_ideas
from app.video_shorts.services.niches import final_niche_pool, load_niche_snapshot


def compact_number(value: Any) -> str:
    number = int(value or 0)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(number) >= divisor:
            compact = number / divisor
            precision = 0 if compact >= 100 else 1
            return f"{compact:.{precision}f}".rstrip("0").rstrip(".") + suffix
    return f"{number:,}"


def _ranked_pool(rows: list[dict[str, Any]], niche: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        final_niche_pool(rows, clinicians_only=False, niche=niche),
        key=lambda row: (
            -int(row.get("subscriber_count") or 0),
            str(row.get("title") or "").lower(),
        ),
    )


def _display_rank_row(row: dict[str, Any], rank: int, own_channel_ids: set[str]) -> dict[str, Any]:
    channel_id = str(row.get("youtube_channel_id") or "")
    subscribers = int(row.get("subscriber_count") or 0)
    views_90d = int(row.get("views_90d") or 0)
    return {
        "rank": rank,
        "youtube_channel_id": channel_id,
        "channel_name": str(row.get("title") or "Untitled channel"),
        "subscribers": subscribers,
        "subscribers_display": compact_number(row.get("subscriber_count")),
        "views_90d": views_90d,
        "views_90d_display": compact_number(row.get("views_90d")),
        "views_per_subscriber": (views_90d / subscribers) if subscribers > 0 else None,
        "best_views_per_subscriber": False,
        "is_you": channel_id in own_channel_ids,
    }


def display_short_title(value: Any) -> str:
    original = " ".join(str(value or "").split())
    cleaned = re.sub(r"#[\w-]+", " ", original, flags=re.UNICODE)
    cleaned = " ".join(cleaned.split()).rstrip(" .,:;!?-–—|/")
    return cleaned or original


def _top_outlier_cards(
    rows: list[dict[str, Any]],
    *,
    pool_by_channel: dict[str, dict[str, Any]],
    own_channel_ids: set[str],
) -> list[dict[str, Any]]:
    eligible = customer_visible_outlier_rows(
        [
            row for row in rows
            if str(row.get("youtube_channel_id") or "") in pool_by_channel
        ]
    )
    cards = []
    for row in eligible:
        channel_id = str(row.get("youtube_channel_id") or "")
        cards.append(
            {
                "video_id": str(row.get("video_id") or ""),
                "url": f"https://youtube.com/shorts/{row.get('video_id')}",
                "title": display_short_title(row.get("title") or "Untitled Short"),
                "channel_name": (
                    "You"
                    if channel_id in own_channel_ids
                    else str(pool_by_channel[channel_id].get("title") or "Untitled channel")
                ),
                "views": int(row.get("views") or 0),
                "views_display": compact_number(row.get("views")),
                "ratio": float(row.get("outlier_ratio") or 0),
                "duration_s": int(row.get("duration_s") or 0),
            }
        )
    return cards


def load_customer_niche_page(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
) -> dict[str, Any] | None:
    brand = conn.execute(
        """
        SELECT CAST(b.id AS VARCHAR), b.name, n.id, n.slug, n.name
        FROM shorts_brands b
        JOIN niches n ON n.id = b.niche_id
        WHERE CAST(b.id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(b.owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
        LIMIT 1
        """,
        [brand_id, owner_user_id],
    ).fetchone()
    if not brand:
        return None
    niche, snapshot_rows = load_niche_snapshot(conn, str(brand[3]))
    if not niche:
        return None
    ranked_pool = _ranked_pool(snapshot_rows, niche)
    publishing_channel_id = brand_publishing_channel_id(conn, owner_user_id, brand_id)
    own_channel_ids = {publishing_channel_id} if publishing_channel_id else set()
    pool_by_channel = {
        str(row.get("youtube_channel_id") or ""): row for row in ranked_pool
    }
    ranked_rows = [
        _display_rank_row(row, rank, own_channel_ids)
        for rank, row in enumerate(ranked_pool, start=1)
    ]
    own_index = next(
        (index for index, row in enumerate(ranked_rows) if row["is_you"]),
        None,
    )
    place = None
    if own_index is not None:
        rank = own_index + 1
        total = len(ranked_rows)
        neighbors = ranked_rows[max(0, own_index - 2) : own_index + 3]
        ratios = [
            row["views_per_subscriber"] for row in neighbors
            if row["views_per_subscriber"] is not None
        ]
        if ratios:
            best_ratio = max(ratios)
            for row in neighbors:
                row["best_views_per_subscriber"] = row["views_per_subscriber"] == best_ratio
        place = {
            "rank": rank,
            "total": total,
            "top_percent": max(1, math.ceil(rank * 100 / total)) if total else 0,
            "position_percent": (
                0.0 if total <= 1 else round((rank - 1) * 100 / (total - 1), 2)
            ),
            "neighbors": neighbors,
        }

    as_of, outlier_rows = load_latest_niche_outliers(conn, niche["id"])
    cards = _top_outlier_cards(
        outlier_rows,
        pool_by_channel=pool_by_channel,
        own_channel_ids=own_channel_ids,
    )
    pattern = ""
    if as_of and table_columns(conn, "niche_outlier_patterns"):
        row = conn.execute(
            """
            SELECT pattern_sentence
            FROM niche_outlier_patterns
            WHERE niche_id = ? AND as_of = ?
            LIMIT 1
            """,
            [niche["id"], as_of],
        ).fetchone()
        pattern = str((row[0] if row else "") or "").strip()
    ideas_as_of, weekly_ideas = load_brand_weekly_ideas(conn, niche["id"], brand_id)
    return {
        "brand_id": str(brand[0]),
        "brand_name": str(brand[1] or "Customer"),
        "publishing_channel_id": publishing_channel_id,
        "niche_id": niche["id"],
        "niche_slug": niche["slug"],
        "niche_name": niche["name"],
        "pool_size": len(ranked_rows),
        "as_of": as_of,
        "as_of_display": (
            date.fromisoformat(as_of).strftime("%b %d, %Y").replace(" 0", " ")
            if as_of
            else "pending"
        ),
        "place": place,
        "cards": cards,
        "pattern": pattern,
        "ideas_as_of": ideas_as_of,
        "weekly_ideas": weekly_ideas[:3],
    }
