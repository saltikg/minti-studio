from __future__ import annotations

from typing import Dict, Optional

import requests

from app.video_shorts.config import FB_API_BASE


class FacebookMetricsError(RuntimeError):
    def __init__(self, message: str, *, needs_reconnect: bool = False):
        super().__init__(message)
        self.needs_reconnect = needs_reconnect


def _error_message(response, *, operation: str) -> str:
    message = ""
    code = None
    try:
        error = (response.json() or {}).get("error") or {}
        message = str(error.get("message") or "").strip()
        code = error.get("code")
    except Exception:
        message = str(getattr(response, "text", "") or "").strip()[:300]
    detail = message or "Facebook returned an empty error response."
    return f"{operation} failed (HTTP {response.status_code}, code {code}): {detail}"


def _int_or_none(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_facebook_metrics(
    video_id: str,
    page_token: str,
    *,
    timeout: int = 30,
) -> Dict[str, object]:
    """Fetch Facebook lifetime video metrics without turning missing data into zero."""
    try:
        insights = requests.get(
            f"{FB_API_BASE.rstrip('/')}/{video_id}/video_insights",
            params={
                "access_token": page_token,
                "metric": "total_video_impressions,total_video_impressions_unique",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise FacebookMetricsError(f"Facebook video insights request failed: {exc}") from exc
    if insights.status_code != 200:
        message = _error_message(insights, operation="Facebook video insights")
        reconnect = insights.status_code in {401, 403} or "permission" in message.lower()
        raise FacebookMetricsError(message, needs_reconnect=reconnect)

    impressions = None
    reach = None
    try:
        insights_payload = insights.json() or {}
    except ValueError as exc:
        raise FacebookMetricsError("Facebook video insights returned invalid JSON.") from exc
    for item in insights_payload.get("data") or []:
        values = item.get("values") or []
        value = values[-1].get("value") if values else None
        name = str(item.get("name") or "").lower()
        if name == "total_video_impressions":
            impressions = _int_or_none(value)
        elif name == "total_video_impressions_unique":
            reach = _int_or_none(value)

    try:
        metadata = requests.get(
            f"{FB_API_BASE.rstrip('/')}/{video_id}",
            params={
                "access_token": page_token,
                "fields": (
                    "views,likes.summary(true).limit(0),"
                    "comments.summary(true).limit(0),permalink_url"
                ),
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise FacebookMetricsError(f"Facebook video metadata request failed: {exc}") from exc
    if metadata.status_code != 200:
        message = _error_message(metadata, operation="Facebook video metadata")
        reconnect = metadata.status_code in {401, 403} or "permission" in message.lower()
        raise FacebookMetricsError(message, needs_reconnect=reconnect)

    try:
        payload = metadata.json() or {}
    except ValueError as exc:
        raise FacebookMetricsError("Facebook video metadata returned invalid JSON.") from exc
    likes_summary = (payload.get("likes") or {}).get("summary")
    comments_summary = (payload.get("comments") or {}).get("summary")
    return {
        "view_count": _int_or_none(payload.get("views")) if "views" in payload else None,
        "reach": reach,
        "impressions": impressions,
        "reactions": (
            _int_or_none(likes_summary.get("total_count"))
            if isinstance(likes_summary, dict) and "total_count" in likes_summary
            else None
        ),
        "comment_count": (
            _int_or_none(comments_summary.get("total_count"))
            if isinstance(comments_summary, dict) and "total_count" in comments_summary
            else None
        ),
        "permalink": payload.get("permalink_url"),
    }
