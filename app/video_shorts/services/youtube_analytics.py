from __future__ import annotations

from datetime import date
from typing import Dict, Optional

from app.video_shorts.services.youtube_oauth import (
    build_authenticated_youtube,
    build_authenticated_youtube_analytics,
    resolve_token_lookup_user_id,
)


def _fetch_authenticated_channel_id(
    *,
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> Optional[str]:
    youtube = build_authenticated_youtube(
        refresh_token=refresh_token,
        user_id=user_id,
        brand_id=brand_id,
    )
    if not youtube:
        return None
    try:
        response = youtube.channels().list(part="id", mine=True, maxResults=1).execute()
    except Exception:
        return None
    items = response.get("items") or []
    if not items:
        return None
    return str(items[0].get("id") or "").strip() or None


def fetch_daily_subscriber_metrics(
    target_date: date,
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
    brand_id: Optional[str] = None,
    expected_channel_id: Optional[str] = None,
) -> Optional[Dict[str, int]]:
    resolved_user_id, _ = resolve_token_lookup_user_id(user_id, brand_id=brand_id)
    if not resolved_user_id or "::" not in resolved_user_id:
        return None
    if brand_id and not resolved_user_id.endswith(f"::{brand_id}"):
        return None
    authenticated_channel_id = _fetch_authenticated_channel_id(
        refresh_token=refresh_token,
        user_id=user_id,
        brand_id=brand_id,
    )
    if expected_channel_id and authenticated_channel_id != str(expected_channel_id).strip():
        return None
    analytics = build_authenticated_youtube_analytics(
        refresh_token=refresh_token,
        user_id=user_id,
        brand_id=brand_id,
    )
    if not analytics:
        return None
    day = target_date.isoformat()
    try:
        response = (
            analytics.reports()
            .query(
                ids="channel==MINE",
                startDate=day,
                endDate=day,
                metrics="subscribersGained,subscribersLost",
            )
            .execute()
        )
    except Exception as exc:
        print(f"YouTube Analytics subscriber metrics failed: {exc}")
        return None
    rows = response.get("rows") or []
    if not rows:
        return None
    gained, lost = rows[0][:2]
    try:
        gained_value = int(gained)
    except (TypeError, ValueError):
        gained_value = 0
    try:
        lost_value = int(lost)
    except (TypeError, ValueError):
        lost_value = 0
    return {"subscribers_gained": gained_value, "subscribers_lost": lost_value}
