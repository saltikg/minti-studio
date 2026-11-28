from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from app.video_shorts.services.brand_niches import brand_publishing_channel_id
from app.video_shorts.services.db import table_columns
from app.video_shorts.services.niche_outliers import (
    customer_visible_outlier_rows,
    list_niche_cohorts,
    load_latest_niche_outliers,
    _whole_percentile,
)
from app.video_shorts.services.niche_weekly_ideas import load_brand_weekly_ideas
from app.video_shorts.services.niche_rising_topics import load_rising_topics  # legacy test/extension hook
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
    early: bool = False,
) -> list[dict[str, Any]]:
    eligible = customer_visible_outlier_rows(
        [
            row for row in rows
            if str(row.get("youtube_channel_id") or "") in pool_by_channel
        ], score_key="speed_ratio" if early else "outlier_ratio"
    )
    creators_by_theme: dict[str, set[str]] = defaultdict(set)
    for row in eligible:
        theme = str(row.get("theme_label") or "").strip()
        if theme:
            creators_by_theme[theme].add(str(row.get("youtube_channel_id") or ""))
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
                "speed_ratio": float(row.get("speed_ratio") or 0),
                "age_days": float(row.get("age_days") or 0),
                "duration_s": int(row.get("duration_s") or 0),
                "thumbnail_url": f"https://i.ytimg.com/vi/{row.get('video_id')}/hqdefault.jpg",
                "theme_label": str(row.get("theme_label") or ""),
                "format_label": str(row.get("format_label") or ""),
                "badges": [badge for badge in (
                    (
                        f"Worked for {len(creators_by_theme[str(row.get('theme_label') or '')])} creators"
                        if len(creators_by_theme[str(row.get("theme_label") or "")]) >= 2 else ""
                    ),
                    (
                        "High engagement" if float(row.get("baseline_engagement") or 0) > 0
                        and float(row.get("engagement") or 0) >= 2 * float(row.get("baseline_engagement") or 0)
                        else ""
                    ),
                    "Story" if row.get("format_label") == "story" else "",
                ) if badge],
            }
        )
    return cards


def _pattern_line(cards: list[dict[str, Any]]) -> str:
    if not cards:
        return ""
    themes = Counter(card["theme_label"] for card in cards if card.get("theme_label"))
    theme, count = themes.most_common(1)[0] if themes else ("the same topic", 0)
    stories = sum(card.get("format_label") == "story" for card in cards)
    durations = [int(card["duration_s"]) for card in cards]
    return (
        f"Last week's pattern: {count} of {len(cards)} winners were about {theme}; "
        f"{stories} were stories. Most ran {_whole_percentile(durations, .25)}–"
        f"{_whole_percentile(durations, .75)}s."
    )


def load_customer_niche_page(
    conn,
    *,
    owner_user_id: str,
    brand_id: str,
    cohort_start: str = "",
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

    as_of, outlier_rows = load_latest_niche_outliers(
        conn, niche["id"], cohort_start=cohort_start, signal_type="weekly"
    )
    cohorts = list_niche_cohorts(conn, niche["id"]) if as_of else []
    selected_cohort = cohort_start if cohort_start in cohorts else (cohorts[0] if cohorts else "")
    if selected_cohort and selected_cohort != cohort_start:
        as_of, outlier_rows = load_latest_niche_outliers(
            conn, niche["id"], cohort_start=selected_cohort, signal_type="weekly"
        )
    cards = _top_outlier_cards(
        outlier_rows,
        pool_by_channel=pool_by_channel,
        own_channel_ids=own_channel_ids,
    )
    pattern = _pattern_line(cards)
    early_as_of, early_rows = load_latest_niche_outliers(conn, niche["id"], signal_type="early")
    early_cards = _top_outlier_cards(
        early_rows, pool_by_channel=pool_by_channel, own_channel_ids=own_channel_ids, early=True,
    )
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
        "early_cards": early_cards,
        "early_as_of": early_as_of,
        "cohorts": cohorts,
        "selected_cohort": selected_cohort,
        "cohort_start_display": date.fromisoformat(selected_cohort).strftime("%b %-d") if selected_cohort else "",
        "cohort_end_display": (date.fromisoformat(selected_cohort) + timedelta(days=6)).strftime("%b %-d") if selected_cohort else "",
        "next_update_display": (
            date.fromisoformat(as_of) + timedelta(days=7 - date.fromisoformat(as_of).weekday())
        ).strftime("%b %-d") if selected_cohort else "",
        "pattern": pattern,
        "ideas_as_of": ideas_as_of,
        "weekly_ideas": weekly_ideas[:3],
    }
