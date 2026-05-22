from __future__ import annotations

from datetime import date
from typing import Dict, Optional

from app.video_shorts.services.youtube_oauth import build_authenticated_youtube_analytics


def fetch_daily_subscriber_metrics(
    target_date: date,
    refresh_token: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[Dict[str, int]]:
    analytics = build_authenticated_youtube_analytics(
        refresh_token=refresh_token,
        user_id=user_id,
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
