#!/usr/bin/env python3
"""
Capture daily subscriber/follower counts per channel (YouTube + Instagram + Facebook + TikTok).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
import requests

from app.video_shorts.config import FB_API_BASE, TIKTOK_API_BASE
from app.video_shorts.services.db import get_db
from app.video_shorts.services.instagram_api import InstagramActionError, fetch_instagram_follower_count
from app.video_shorts.services.subscriber_metrics import (
    ensure_subscriber_snapshot_table,
    insert_subscriber_snapshot,
)
from app.video_shorts.services.youtube_analytics import fetch_daily_subscriber_metrics
from app.video_shorts.services.youtube_oauth import (
    build_authenticated_youtube,
    list_stored_refresh_tokens,
)
from src.trends.facebook_page_tokens import list_facebook_page_credentials
from src.trends.instagram_tokens import list_instagram_credentials
from src.trends.tiktok_tokens import list_tiktok_credentials


def _split_scoped_user_id(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    text = str(value or "").strip()
    if not text:
        return None, None
    if "::" not in text:
        return text, None
    user_id, brand_id = text.split("::", 1)
    return user_id or None, brand_id or None


def _fetch_connected_youtube_channel(
    user_id: Optional[str] = None,
    refresh_token: Optional[str] = None,
) -> Optional[Dict[str, object]]:
    try:
        youtube = build_authenticated_youtube(refresh_token=refresh_token, user_id=user_id)
    except Exception as exc:
        print(f"YouTube client init failed for {user_id}: {exc}")
        return None
    if not youtube:
        return None
    try:
        response = youtube.channels().list(part="snippet,statistics", mine=True, maxResults=1).execute()
    except Exception as exc:
        print(f"YouTube subscriber sync failed: {exc}")
        return None
    items = response.get("items", [])
    if not items:
        return None
    item = items[0]
    channel_id = item.get("id")
    if not channel_id:
        return None
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    try:
        subscriber_count = int(stats.get("subscriberCount"))
    except (TypeError, ValueError):
        subscriber_count = None
    return {
        "channel_id": channel_id,
        "channel_name": snippet.get("title"),
        "subscriber_count": subscriber_count,
    }


def _build_instagram_records(snapshot_date: date, now: datetime) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    for creds in list_instagram_credentials():
        token = creds.get("page_access_token")
        business_id = creds.get("instagram_business_account_id")
        if not token or not business_id:
            continue
        try:
            payload = fetch_instagram_follower_count(business_id, token)
        except InstagramActionError as exc:
            print(f"Instagram follower sync failed for {business_id}: {exc}")
            continue
        followers = payload.get("followers_count")
        if followers is None:
            continue
        _, brand_id = _split_scoped_user_id(creds.get("user_id"))
        records.append(
            {
                "snapshot_date": snapshot_date,
                "effective_at": now,
                "brand_id": brand_id,
                "channel_type": "instagram",
                "channel_id": business_id,
                "channel_name": payload.get("username") or creds.get("instagram_username"),
                "subscriber_count": followers,
                "subscriber_count_exact": followers,
                "stats_source": "instagram_graph",
            }
        )
    return records


def _facebook_api_url(path: str) -> str:
    base = (FB_API_BASE or "https://graph.facebook.com/v24.0").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _build_facebook_records(snapshot_date: date, now: datetime) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    for info in list_facebook_page_credentials():
        if not info.get("page_access_token") or not info.get("page_id"):
            continue
        try:
            resp = requests.get(
                _facebook_api_url(info["page_id"]),
                params={
                    "access_token": info["page_access_token"],
                    "fields": "name,fan_count,followers_count",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                continue
            payload = resp.json() or {}
            count = payload.get("followers_count")
            if count is None:
                count = payload.get("fan_count")
            if count is None:
                continue
            _, brand_id = _split_scoped_user_id(info.get("user_id"))
            records.append(
                {
                    "snapshot_date": snapshot_date,
                    "effective_at": now,
                    "brand_id": brand_id,
                    "channel_type": "facebook",
                    "channel_id": info.get("page_id"),
                    "channel_name": payload.get("name") or info.get("page_name"),
                    "subscriber_count": count,
                    "subscriber_count_exact": count,
                    "stats_source": "facebook_graph",
                }
            )
        except Exception as exc:
            print(f"Facebook follower sync failed for {info.get('page_id')}: {exc}")
    return records


def _tiktok_v2_url(path: str) -> str:
    base = (TIKTOK_API_BASE or "https://open.tiktokapis.com").rstrip("/")
    if base.endswith("/v2"):
        base = base[: -len("/v2")]
    return f"{base}/v2/{path.lstrip('/')}"


def _build_tiktok_records(snapshot_date: date, now: datetime) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    for info in list_tiktok_credentials():
        token = info.get("access_token")
        open_id = info.get("open_id")
        if not token or not open_id:
            continue
        try:
            resp = requests.get(
                _tiktok_v2_url("user/info/"),
                headers={"Authorization": f"Bearer {token}"},
                params={"fields": "open_id,display_name,username,follower_count"},
                timeout=30,
            )
            if resp.status_code != 200:
                continue
            payload = resp.json() or {}
            user = (payload.get("data") or {}).get("user") or {}
            follower_count = user.get("follower_count")
            if follower_count is None:
                continue
            _, brand_id = _split_scoped_user_id(info.get("user_id"))
            records.append(
                {
                    "snapshot_date": snapshot_date,
                    "effective_at": now,
                    "brand_id": brand_id,
                    "channel_type": "tiktok",
                    "channel_id": user.get("open_id") or open_id,
                    "channel_name": user.get("username") or user.get("display_name"),
                    "subscriber_count": follower_count,
                    "subscriber_count_exact": follower_count,
                    "stats_source": "tiktok_api",
                }
            )
        except Exception as exc:
            print(f"TikTok follower sync failed for {open_id}: {exc}")
    return records


def _fetch_previous_youtube_subscriber_count(
    conn,
    *,
    channel_id: str,
    snapshot_date: date,
    brand_id: Optional[str] = None,
) -> Optional[int]:
    brand_filter_sql = "brand_id IS NULL"
    brand_filter_params: List[object] = []
    if brand_id is not None:
        brand_filter_sql = "brand_id = ?"
        brand_filter_params = [brand_id]
    row = conn.execute(
        f"""
        SELECT COALESCE(subscriber_count_exact, subscriber_count, subscriber_count_api_rounded)
        FROM shorts_channel_subscriber_daily
        WHERE channel_type = 'youtube'
          AND channel_id = ?
          AND {brand_filter_sql}
          AND snapshot_date < ?
        ORDER BY snapshot_date DESC
        LIMIT 1
        """,
        [channel_id, *brand_filter_params, snapshot_date.isoformat()],
    ).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def capture_daily_subscriber_snapshot(target_date: Optional[date] = None) -> int:
    snapshot_date = target_date or date.today()
    now = datetime.utcnow()
    youtube_records: List[Dict[str, object]] = []
    youtube_channels_for_backfill: List[Tuple[Optional[str], Optional[str], Optional[Dict[str, object]]]] = []
    for token_info in list_stored_refresh_tokens():
        if token_info.get("reauth_required"):
            continue
        scoped_user_id = token_info.get("user_id")
        refresh_token = token_info.get("refresh_token")
        _, brand_id = _split_scoped_user_id(scoped_user_id)
        youtube_channel = _fetch_connected_youtube_channel(
            user_id=scoped_user_id,
            refresh_token=refresh_token,
        )
        print(f"YouTube channel[{scoped_user_id}]: {youtube_channel}", flush=True)
        if not youtube_channel:
            continue
        youtube_channels_for_backfill.append((scoped_user_id, brand_id, youtube_channel))
        api_subscriber_count = youtube_channel.get("subscriber_count")
        daily_metrics = fetch_daily_subscriber_metrics(
            snapshot_date,
            refresh_token=refresh_token,
            user_id=scoped_user_id,
            brand_id=brand_id,
            expected_channel_id=str(youtube_channel.get("channel_id") or "").strip() or None,
        )
        gained = None
        lost = None
        net = None
        if daily_metrics:
            gained = daily_metrics.get("subscribers_gained")
            lost = daily_metrics.get("subscribers_lost")
            if gained is not None or lost is not None:
                net = (gained or 0) - (lost or 0)
        subscriber_count = api_subscriber_count
        channel_id = youtube_channel.get("channel_id")
        if subscriber_count is None:
            continue
        youtube_records.append(
            {
                "snapshot_date": snapshot_date,
                "effective_at": now,
                "brand_id": brand_id,
                "channel_type": "youtube",
                "channel_id": channel_id,
                "channel_name": youtube_channel.get("channel_name"),
                "subscriber_count": subscriber_count,
                "subscriber_count_exact": subscriber_count,
                "subscribers_gained": gained,
                "subscribers_lost": lost,
                "subscribers_net": net,
                "subscriber_count_api_rounded": api_subscriber_count,
                "stats_source": "youtube_data_api",
            }
        )

    instagram_records = _build_instagram_records(snapshot_date, now)
    facebook_records = _build_facebook_records(snapshot_date, now)
    tiktok_records = _build_tiktok_records(snapshot_date, now)
    all_records = youtube_records + instagram_records + facebook_records + tiktok_records

    conn = get_db()
    try:
        ensure_subscriber_snapshot_table(conn)
        all_records = youtube_records + instagram_records + facebook_records + tiktok_records
        if not all_records:
            return 0
        inserted = insert_subscriber_snapshot(conn, all_records)
        for scoped_user_id, brand_id, youtube_channel in youtube_channels_for_backfill:
            _backfill_youtube_daily_metrics(
                conn,
                youtube_channel=youtube_channel,
                days=10,
                now=now,
                refresh_token=None,
                user_id=scoped_user_id,
                brand_id=brand_id,
            )
        conn.commit()
        return inserted
    finally:
        conn.close()


def _values_differ(existing: object, incoming: object) -> bool:
    if existing is None and incoming is None:
        return False
    return existing != incoming


def _backfill_youtube_daily_metrics(
    conn,
    youtube_channel: Optional[Dict[str, object]],
    days: int,
    now: datetime,
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> int:
    if not youtube_channel or not youtube_channel.get("channel_id"):
        return 0
    channel_id = youtube_channel["channel_id"]
    end_date = date.today()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    updated = 0
    current = start_date
    brand_filter_sql = "brand_id IS NULL"
    brand_filter_params: List[object] = []
    if brand_id is not None:
        brand_filter_sql = "brand_id = ?"
        brand_filter_params = [brand_id]
    while current <= end_date:
        metrics = fetch_daily_subscriber_metrics(
            current,
            refresh_token=refresh_token,
            user_id=user_id,
            brand_id=brand_id,
            expected_channel_id=str(channel_id),
        )
        if not metrics:
            current += timedelta(days=1)
            continue
        gained = metrics.get("subscribers_gained")
        lost = metrics.get("subscribers_lost")
        net = None
        if gained is not None or lost is not None:
            net = (gained or 0) - (lost or 0)
        row = conn.execute(
            f"""
            SELECT subscribers_gained, subscribers_lost, subscribers_net
            FROM shorts_channel_subscriber_daily
            WHERE channel_type = 'youtube'
              AND channel_id = ?
              AND {brand_filter_sql}
              AND snapshot_date = ?
            LIMIT 1
            """,
            [channel_id, *brand_filter_params, current.isoformat()],
        ).fetchone()
        if row:
            if (
                _values_differ(row[0], gained)
                or _values_differ(row[1], lost)
                or _values_differ(row[2], net)
            ):
                conn.execute(
                    f"""
                    UPDATE shorts_channel_subscriber_daily
                       SET subscribers_gained = ?,
                           subscribers_lost = ?,
                           subscribers_net = ?,
                           brand_id = COALESCE(brand_id, ?),
                           effective_at = ?
                     WHERE channel_type = 'youtube'
                       AND channel_id = ?
                       AND {brand_filter_sql}
                       AND snapshot_date = ?
                    """,
                    [gained, lost, net, brand_id, now, channel_id, *brand_filter_params, current.isoformat()],
                )
                updated += 1
        current += timedelta(days=1)
    return updated


def main() -> int:
    print("daily_subscriber_snapshot starting", flush=True)
    try:
        inserted = capture_daily_subscriber_snapshot()
        print(f"daily_subscriber_snapshot inserted={inserted}", flush=True)
        return 0
    except Exception as exc:
        print(f"daily_subscriber_snapshot failed: {exc}", flush=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
