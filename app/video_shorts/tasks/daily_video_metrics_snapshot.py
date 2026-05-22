#!/usr/bin/env python3
"""
Capture published shorts’ YouTube + Instagram + Facebook metrics into the daily snapshot table.

Intended for cron, for example:
    30 03 * * * /path/to/venv/bin/python /path/to/app/video_shorts/tasks/daily_video_metrics_snapshot.py
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

import duckdb

from app import DB_PATH
from app.video_shorts.config import FB_API_BASE, SHORTS_DIR
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.instagram_queue import load_instagram_queue_map
from app.video_shorts.services.facebook_queue import (
    load_facebook_queue_map,
    update_facebook_queue_metrics,
)
from app.video_shorts.services.ai_video_workspace import ensure_ai_video_schema
from app.video_shorts.services.video_metrics import (
    SNAPSHOT_COLUMNS,
    SNAPSHOT_INSERT_SQL,
    SNAPSHOT_TABLE,
    ANALYTICS_ARCHIVE_TABLE,
    ensure_snapshot_table,
)
from app.video_shorts.youtube_api import YoutubeApiError, fetch_video_stats
from src.trends.facebook_page_tokens import get_facebook_page_data


def _parse_args():
    parser = argparse.ArgumentParser(description="Daily snapshot for published short metrics.")
    parser.add_argument(
        "--date",
        dest="snapshot_date",
        help="Target snapshot date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress log output except for errors.",
    )
    return parser.parse_args()


def _int_or_none(value: object) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_brand_id(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _split_scoped_user_id(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    text = str(value or "").strip()
    if not text:
        return None, None
    if "::" not in text:
        return text, None
    user_id, brand_id = text.split("::", 1)
    return user_id or None, brand_id or None


def _parse_publish_timestamp(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return None


def _collect_published_entries(target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    plan_suffix = "_plan.json"
    entries: List[Dict[str, Any]] = []
    if not SHORTS_DIR.exists():
        return entries
    for plan_path in SHORTS_DIR.glob(f"*{plan_suffix}"):
        if not plan_path.is_file():
            continue
        video_id = plan_path.name[: -len(plan_suffix)]
        if not video_id:
            continue
        try:
            raw = json.loads(plan_path.read_text())
        except Exception:
            continue
        plan_entries = raw.get("plan") or raw.get("clips") or []
        for plan_entry in plan_entries:
            publish_status = (plan_entry.get("publish_status") or "").lower()
            if publish_status != "published":
                short_video_id = (plan_entry.get("yt_video_id") or plan_entry.get("short_video_id") or "").strip()
                if publish_status != "scheduled" or not short_video_id:
                    continue
                publish_at_raw = plan_entry.get("publish_at_iso") or plan_entry.get("publish_at")
                publish_at = _parse_publish_timestamp(publish_at_raw)
                publish_date = publish_at.date() if publish_at else None
                if target_date and publish_date and publish_date > target_date:
                    continue
            plan_index = plan_entry.get("plan_index")
            plan_index_key = str(plan_index) if plan_index is not None else ""
            short_video_id = (plan_entry.get("yt_video_id") or plan_entry.get("short_video_id") or "").strip()
            entries.append(
                {
                    "video_id": video_id,
                    "plan_index": plan_index,
                    "plan_index_key": plan_index_key,
                    "plan_title": plan_entry.get("title") or plan_entry.get("plan_title"),
                    "short_video_id": short_video_id,
                }
            )
    for item in _list_all_ai_broadcast_entries():
        youtube_status = str(item.get("youtube_status") or "").strip().lower()
        youtube_short_id = str(item.get("youtube_video_id") or "").strip()
        publish_at_raw = item.get("youtube_publish_at") or item.get("youtube_published_at")
        publish_at = _parse_publish_timestamp(publish_at_raw)
        publish_date = publish_at.date() if publish_at else None
        if target_date and publish_date and publish_date > target_date:
            continue
        has_youtube = youtube_status in {"published", "scheduled"} and youtube_short_id
        if not has_youtube:
            # Keep AI entry in the set anyway so Instagram/Facebook/TikTok queue snapshots can attach to it.
            youtube_short_id = ""
        entries.append(
            {
                "video_id": item.get("video_id"),
                "plan_index": 1,
                "plan_index_key": "1",
                "plan_title": item.get("title") or "",
                "short_video_id": youtube_short_id,
                "brand_id": item.get("brand_id"),
                "scoped_user_id": item.get("user_id"),
            }
        )
    return entries


def _list_all_ai_broadcast_entries() -> List[Dict[str, Any]]:
    conn = get_db_readonly()
    try:
        ensure_ai_video_schema(conn)
        rows = conn.execute(
            """
            SELECT
                video_id,
                user_id,
                brand_id,
                title,
                COALESCE(youtube_status, ''),
                COALESCE(youtube_video_id, ''),
                COALESCE(youtube_publish_at, ''),
                COALESCE(youtube_published_at, '')
            FROM shorts_ai_videos
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "video_id": row[0] or "",
            "user_id": row[1] or "",
            "brand_id": row[2],
            "title": row[3] or "",
            "youtube_status": row[4] or "",
            "youtube_video_id": row[5] or "",
            "youtube_publish_at": row[6] or "",
            "youtube_published_at": row[7] or "",
        }
        for row in rows
    ]


def _load_youtube_meta(video_ids: List[str]) -> Dict[str, Mapping[str, Any]]:
    if not video_ids:
        return {}
    placeholders = ", ".join("?" for _ in video_ids)
    query = f"""
        SELECT
            v.video_id,
            v.channel_id,
            COALESCE(c.channel_name, '') AS channel_name,
            v.title AS video_title,
            COALESCE(v.brand_id, c.brand_id) AS brand_id
        FROM youtube_videos v
        LEFT JOIN youtube_channels c ON c.channel_id = v.channel_id
        WHERE v.video_id IN ({placeholders})
    """
    conn = get_db_readonly()
    try:
        rows = conn.execute(query, video_ids).fetchall()
    finally:
        conn.close()
    meta = {
        row[0]: {
            "channel_id": row[1],
            "channel_name": row[2],
            "video_title": row[3],
            "brand_id": row[4],
        }
        for row in rows
        if row[0]
    }
    ai_items = [item for item in _list_all_ai_broadcast_entries() if item.get("video_id") in set(video_ids)]
    for item in ai_items:
        meta[item["video_id"]] = {
            "channel_id": None,
            "channel_name": "AI Video",
            "video_title": item.get("title") or "",
            "brand_id": _normalize_brand_id(item.get("brand_id"))
            or _split_scoped_user_id(item.get("user_id"))[1],
        }
    return meta


def _fetch_short_stats(short_ids: List[str]) -> Dict[str, Mapping[str, int]]:
    if not short_ids:
        return {}
    try:
        return fetch_video_stats(short_ids)
    except YoutubeApiError as exc:
        print(f"Failed to fetch YouTube stats: {exc}")
        return {}
    except Exception as exc:  # pragma: no cover
        print(f"Unexpected error while fetching YouTube stats: {exc}")
        return {}


def _build_youtube_rows(
    entries: List[Dict[str, Any]],
    video_meta: Dict[str, Mapping[str, Any]],
    short_stats: Dict[str, Mapping[str, int]],
    target_date: date,
    now: datetime,
) -> List[Mapping[str, object]]:
    rows: List[Mapping[str, object]] = []
    for entry in entries:
        short_id = entry.get("short_video_id") or entry.get("video_id")
        if not short_id:
            continue
        stats = short_stats.get(short_id) or {}
        if not stats:
            continue
        meta = video_meta.get(entry["video_id"], {})
        entry_brand_id = _normalize_brand_id(entry.get("brand_id")) or _split_scoped_user_id(
            str(entry.get("scoped_user_id") or "")
        )[1]
        rows.append(
            {
                "snapshot_date": target_date,
                "effective_at": now,
                "brand_id": _normalize_brand_id(meta.get("brand_id")) or entry_brand_id,
                "channel_type": "youtube",
                "video_id": short_id,
                "channel_id": meta.get("channel_id"),
                "channel_name": meta.get("channel_name"),
                "scoped_user_id": entry.get("scoped_user_id"),
                "video_title": entry.get("plan_title") or meta.get("video_title"),
                "impressions": None,
                "views": _int_or_none(stats.get("view_count")),
                "comments": _int_or_none(stats.get("comment_count")),
                "likes": _int_or_none(stats.get("like_count")),
                "shares": None,
                "reach": None,
                "saved": None,
                "stats_source": "youtube_api",
            }
        )
    return rows


def _load_previous_youtube_snapshot(target_date: date) -> List[Dict[str, object]]:
    conn = get_db_readonly()
    try:
        ensure_snapshot_table(conn)
        prev_row = conn.execute(
            f"""
            SELECT MAX(snapshot_date) AS snapshot_date
            FROM {SNAPSHOT_TABLE}
            WHERE channel_type = 'youtube'
              AND snapshot_date < ?
            """,
            [target_date.isoformat()],
        ).fetchone()
        prev_date = prev_row[0] if prev_row else None
        if not prev_date:
            return []
        rows = conn.execute(
            f"""
            SELECT video_id, channel_id, channel_name, video_title, brand_id
            FROM {SNAPSHOT_TABLE}
            WHERE channel_type = 'youtube'
              AND snapshot_date = ?
            """,
            [prev_date],
        ).fetchall()
        return [
            {
                "video_id": row[0],
                "channel_id": row[1],
                "channel_name": row[2],
                "video_title": row[3],
                "brand_id": row[4],
            }
            for row in rows
            if row[0]
        ]
    finally:
        conn.close()


def _build_instagram_rows(
    entries: List[Dict[str, Any]],
    instagram_map: Dict[tuple, List[Dict[str, Any]]],
    target_date: date,
    now: datetime,
) -> List[Mapping[str, object]]:
    rows: List[Mapping[str, object]] = []
    for entry in entries:
        key = (entry["video_id"], entry["plan_index_key"])
        records = instagram_map.get(key) or []
        for record in records:
            if (record.get("status") or "").lower() != "published":
                continue
            media_id = record.get("instagram_media_id")
            if not media_id:
                continue
            rows.append(
                {
                    "snapshot_date": target_date,
                    "effective_at": now,
                    "brand_id": _normalize_brand_id(record.get("brand_id"))
                    or _split_scoped_user_id(record.get("user_id"))[1],
                    "channel_type": "instagram",
                    "video_id": media_id,
                    "channel_id": record.get("instagram_business_account_id"),
                    "channel_name": record.get("instagram_username"),
                    "scoped_user_id": record.get("user_id"),
                    "video_title": entry.get("plan_title"),
                    "impressions": _int_or_none(record.get("impressions")),
                    "views": None,
                    "comments": _int_or_none(record.get("comment_count")),
                    "likes": _int_or_none(record.get("like_count")),
                    "shares": _int_or_none(record.get("shares")),
                    "reach": _int_or_none(record.get("reach")),
                    "saved": _int_or_none(record.get("saved")),
                    "stats_source": "shorts_instagram_queue",
                }
            )
    return rows


def _fetch_facebook_metrics(video_id: str, page_token: str) -> Dict[str, Optional[int]]:
    import requests

    metrics = "total_video_impressions,total_video_impressions_unique"
    insights_url = f"{FB_API_BASE.rstrip('/')}/{video_id}/video_insights"
    view_count = None
    reach = None
    impressions = None
    reactions = None
    comment_count = None
    permalink = None
    try:
        resp = requests.get(
            insights_url,
            params={"access_token": page_token, "metric": metrics},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json().get("data") or []
            for item in data:
                name = (item.get("name") or "").lower()
                values = item.get("values") or []
                if not values:
                    continue
                value = values[-1].get("value")
                if name == "total_video_impressions":
                    impressions = value
                elif name == "total_video_impressions_unique":
                    reach = value
    except Exception:
        pass
    try:
        meta_url = f"{FB_API_BASE.rstrip('/')}/{video_id}"
        resp = requests.get(
            meta_url,
            params={
                "access_token": page_token,
                "fields": "views,likes.summary(true).limit(0),comments.summary(true).limit(0),permalink_url",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            payload = resp.json()
            view_count = payload.get("views") or view_count
            reactions = (payload.get("likes") or {}).get("summary", {}).get("total_count")
            comment_count = (payload.get("comments") or {}).get("summary", {}).get("total_count")
            permalink = payload.get("permalink_url")
    except Exception:
        pass
    return {
        "view_count": _int_or_none(view_count),
        "reach": _int_or_none(reach),
        "impressions": _int_or_none(impressions),
        "reactions": _int_or_none(reactions),
        "comment_count": _int_or_none(comment_count),
        "permalink": permalink,
    }


def _build_facebook_rows(
    entries: List[Dict[str, Any]],
    facebook_map: Dict[tuple, List[Dict[str, Any]]],
    target_date: date,
    now: datetime,
) -> List[Mapping[str, object]]:
    rows: List[Mapping[str, object]] = []
    for entry in entries:
        key = (entry["video_id"], entry["plan_index_key"])
        records = facebook_map.get(key) or []
        for record in records:
            if (record.get("status") or "").lower() != "published":
                continue
            fb_video_id = record.get("facebook_video_id")
            if not fb_video_id:
                continue
            metrics = {
                "view_count": record.get("view_count"),
                "reach": record.get("reach"),
                "impressions": record.get("impressions"),
                "reactions": record.get("reactions"),
                "comment_count": record.get("comment_count"),
                "permalink": record.get("permalink"),
            }
            user_id = record.get("user_id")
            if user_id:
                creds = get_facebook_page_data(user_id)
                if creds and creds.get("page_access_token"):
                    metrics = _fetch_facebook_metrics(fb_video_id, creds.get("page_access_token"))
                    update_facebook_queue_metrics(
                        record.get("id"),
                        facebook_video_id=fb_video_id,
                        permalink=metrics.get("permalink"),
                        view_count=metrics.get("view_count"),
                        reach=metrics.get("reach"),
                        impressions=metrics.get("impressions"),
                        reactions=metrics.get("reactions"),
                        comment_count=metrics.get("comment_count"),
                    )
            rows.append(
                {
                    "snapshot_date": target_date,
                    "effective_at": now,
                    "brand_id": _normalize_brand_id(record.get("brand_id"))
                    or _split_scoped_user_id(record.get("user_id"))[1],
                    "channel_type": "facebook",
                    "video_id": fb_video_id,
                    "channel_id": record.get("page_id"),
                    "channel_name": record.get("page_name"),
                    "scoped_user_id": record.get("user_id"),
                    "video_title": entry.get("plan_title"),
                    "impressions": _int_or_none(metrics.get("impressions")),
                    "views": _int_or_none(metrics.get("view_count")),
                    "comments": _int_or_none(metrics.get("comment_count")),
                    "likes": _int_or_none(metrics.get("reactions")),
                    "shares": None,
                    "reach": _int_or_none(metrics.get("reach")),
                    "saved": None,
                    "stats_source": "facebook_graph",
                }
            )
    return rows


def _insert_records(conn: duckdb.DuckDBPyConnection, records: Iterable[Mapping[str, object]]) -> int:
    params = [tuple(record.get(col) for col in SNAPSHOT_COLUMNS) for record in records]
    if not params:
        return 0
    conn.executemany(SNAPSHOT_INSERT_SQL, params)
    return len(params)


def _load_brand_map_for_video_ids(video_ids: List[str]) -> Dict[str, str]:
    normalized_ids = [str(video_id).strip() for video_id in video_ids if str(video_id).strip()]
    if not normalized_ids:
        return {}
    conn = get_db_readonly()
    try:
        placeholders = ", ".join("?" for _ in normalized_ids)
        rows = conn.execute(
            f"""
            SELECT video_id, brand_id
            FROM youtube_videos
            WHERE video_id IN ({placeholders})
              AND brand_id IS NOT NULL
            """,
            normalized_ids,
        ).fetchall()
        return {str(row[0]).strip(): str(row[1]).strip() for row in rows if row[0] and row[1]}
    finally:
        conn.close()


def _load_brand_map_for_channel_ids(channel_ids: List[str]) -> Dict[str, str]:
    normalized_ids = [str(channel_id).strip() for channel_id in channel_ids if str(channel_id).strip()]
    if not normalized_ids:
        return {}
    conn = get_db_readonly()
    try:
        placeholders = ", ".join("?" for _ in normalized_ids)
        rows = conn.execute(
            f"""
            SELECT channel_id, brand_id
            FROM youtube_channels
            WHERE channel_id IN ({placeholders})
              AND brand_id IS NOT NULL
            """,
            normalized_ids,
        ).fetchall()
        return {str(row[0]).strip(): str(row[1]).strip() for row in rows if row[0] and row[1]}
    finally:
        conn.close()


def _load_default_brand_id() -> Optional[str]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT id
            FROM shorts_brands
            WHERE COALESCE(is_default, FALSE) = TRUE
            ORDER BY created_at DESC
            LIMIT 2
            """
        ).fetchall()
        if len(rows) == 1 and rows[0] and rows[0][0]:
            return str(rows[0][0]).strip()
        return None
    finally:
        conn.close()


def _enforce_non_null_brand_ids(records: List[Dict[str, object]]) -> None:
    if not records:
        return
    video_brand_map = _load_brand_map_for_video_ids(
        [str(row.get("video_id") or "").strip() for row in records]
    )
    channel_brand_map = _load_brand_map_for_channel_ids(
        [str(row.get("channel_id") or "").strip() for row in records]
    )
    default_brand_id = _load_default_brand_id()
    unresolved: List[Dict[str, object]] = []
    for row in records:
        resolved_brand = _normalize_brand_id(row.get("brand_id"))
        if not resolved_brand:
            scoped_brand = _split_scoped_user_id(
                str(row.get("scoped_user_id") or "")
            )[1]
            resolved_brand = _normalize_brand_id(scoped_brand)
        if not resolved_brand:
            resolved_brand = _normalize_brand_id(
                video_brand_map.get(str(row.get("video_id") or "").strip())
            )
        if not resolved_brand:
            resolved_brand = _normalize_brand_id(
                channel_brand_map.get(str(row.get("channel_id") or "").strip())
            )
        if resolved_brand:
            row["brand_id"] = resolved_brand
        elif default_brand_id:
            row["brand_id"] = default_brand_id
        else:
            unresolved.append(row)

    if unresolved:
        by_channel: Dict[str, int] = {}
        for item in unresolved:
            channel_key = str(item.get("channel_type") or "unknown").strip().lower() or "unknown"
            by_channel[channel_key] = by_channel.get(channel_key, 0) + 1
        sample = ", ".join(
            f"{item.get('channel_type')}:{item.get('video_id')}"
            for item in unresolved[:10]
        )
        raise RuntimeError(
            "Refusing to write snapshot rows with NULL brand_id. "
            f"unresolved={len(unresolved)} by_channel={by_channel} sample=[{sample}]"
        )


def capture_daily_snapshot(
    target_date: Optional[Union[str, date]] = None,
    *,
    quiet: bool = False,
) -> int:
    if target_date and isinstance(target_date, str):
        try:
            target = date.fromisoformat(target_date)
        except ValueError as exc:
            raise ValueError(f"Invalid snapshot date: {exc}") from exc
    else:
        target = target_date if isinstance(target_date, date) else date.today()
    if not quiet:
        print(f"Taking daily snapshot for {target.isoformat()}", flush=True)

    plan_entries = _collect_published_entries(target)
    if not plan_entries and not quiet:
        print("No published short entries found; refreshing snapshot rows from existing sources.", flush=True)

    video_ids = sorted({entry["video_id"] for entry in plan_entries if entry.get("video_id")})
    video_meta = _load_youtube_meta(video_ids)
    short_ids = sorted({entry["short_video_id"] for entry in plan_entries if entry.get("short_video_id")})
    short_stats = _fetch_short_stats(short_ids)
    instagram_map = load_instagram_queue_map(video_ids)
    facebook_map = load_facebook_queue_map(video_ids)

    now = datetime.utcnow()
    youtube_rows = _build_youtube_rows(plan_entries, video_meta, short_stats, target, now)
    previous_rows = _load_previous_youtube_snapshot(target)
    current_ids = {row.get("video_id") for row in youtube_rows if row.get("video_id")}
    missing_ids = [
        row["video_id"]
        for row in previous_rows
        if row.get("video_id") and row["video_id"] not in current_ids
    ]
    if missing_ids:
        missing_stats = _fetch_short_stats(missing_ids)
        for row in previous_rows:
            video_id = row.get("video_id")
            if not video_id or video_id not in missing_stats:
                continue
            stats = missing_stats.get(video_id) or {}
            if not stats:
                continue
            youtube_rows.append(
                {
                    "snapshot_date": target,
                    "effective_at": now,
                    "brand_id": row.get("brand_id"),
                    "channel_type": "youtube",
                    "video_id": video_id,
                    "channel_id": row.get("channel_id"),
                    "channel_name": row.get("channel_name"),
                    "video_title": row.get("video_title"),
                    "impressions": None,
                    "views": _int_or_none(stats.get("view_count")),
                    "comments": _int_or_none(stats.get("comment_count")),
                    "likes": _int_or_none(stats.get("like_count")),
                    "shares": None,
                    "reach": None,
                    "saved": None,
                    "stats_source": "youtube_api_backfill",
                }
            )
    instagram_rows = _build_instagram_rows(plan_entries, instagram_map, target, now)
    facebook_rows = _build_facebook_rows(plan_entries, facebook_map, target, now)
    total_rows = youtube_rows + instagram_rows + facebook_rows
    _enforce_non_null_brand_ids(total_rows)  # Hard guard: never insert NULL brand_id again.

    metrics_conn = get_db()
    try:
        ensure_snapshot_table(metrics_conn)
        if not quiet:
            print(f" - {len(youtube_rows)} YouTube short rows", flush=True)
            if missing_ids:
                print(f" - backfilled {len(missing_ids)} YouTube rows from previous snapshot", flush=True)
            print(f" - {len(instagram_rows)} Instagram rows", flush=True)
            print(f" - {len(facebook_rows)} Facebook rows", flush=True)

        inserted = _insert_records(metrics_conn, total_rows)
        metrics_conn.commit()
        if not quiet:
            print(f"Inserted/updated {inserted} rows into {SNAPSHOT_TABLE}", flush=True)
            print(f"Legacy analytics archive table: {ANALYTICS_ARCHIVE_TABLE}", flush=True)
    finally:
        metrics_conn.close()

    return inserted


def main() -> int:
    args = _parse_args()
    try:
        capture_daily_snapshot(target_date=args.snapshot_date, quiet=args.quiet)
    except ValueError as exc:
        raise SystemExit(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
