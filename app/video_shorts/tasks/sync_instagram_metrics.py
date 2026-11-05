#!/usr/bin/env python3
"""
Fetches latest Instagram media insights (likes/comments) and comment samples
for published queue entries.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import html
import logging
import os
import sys
from typing import Dict, Optional

from app.video_shorts.services.instagram_api import (
    InstagramActionError,
    refresh_instagram_media,
)
from app.video_shorts.config import FB_API_BASE
from app.video_shorts.services.instagram_queue import fetch_instagram_media_jobs
from app.video_shorts.services.email_verification import send_resend_email
from app.video_shorts.services.facebook_queue import (
    fetch_facebook_media_jobs,
    update_facebook_queue_metrics,
)
from src.trends.facebook_page_tokens import get_facebook_page_data

import requests

logger = logging.getLogger(__name__)


def _send_failure_alert(summary: Dict[str, object]) -> None:
    failures = summary.get("failures") or []
    lines = [
        "Instagram media insight sync exceeded the failure threshold.",
        f"Checked: {summary['total']}",
        f"Failed: {summary['failed']}",
        "",
    ]
    lines.extend(
        f"media_id={item.get('media_id')} brand_id={item.get('brand_id')} error={item.get('error')}"
        for item in failures[:20]
    )
    text = "\n".join(lines)
    send_resend_email(
        to_email=(os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip(),
        subject=f"Minti Instagram metrics alert: {summary['failed']}/{summary['total']} failed",
        html="<p>" + html.escape(text).replace("\n", "<br>") + "</p>",
        text=text,
        error_message="Instagram metrics alert could not be sent.",
    )


def sync_instagram_metrics(
    max_items: Optional[int],
    comments_limit: int,
    *,
    published_since: Optional[str] = None,
) -> Dict[str, object]:
    jobs = fetch_instagram_media_jobs(max_items, published_since=published_since)
    summary: Dict[str, object] = {
        "total": len(jobs),
        "succeeded": 0,
        "failed": 0,
        "failures": [],
        "by_brand": {},
    }
    if not jobs:
        print("Instagram: no media with instagram_media_id stored.")
        print("Instagram summary: checked=0 succeeded=0 failed=0")
        return summary
    print(f"Instagram: checking {len(jobs)} media entries.")
    for job in jobs:
        brand_id = str(job.get("brand_id") or "unknown")
        brand_counts = summary["by_brand"].setdefault(
            brand_id, {"checked": 0, "succeeded": 0, "failed": 0}
        )
        brand_counts["checked"] += 1
        try:
            metrics = refresh_instagram_media(job["id"], comments_limit=comments_limit)
            summary["succeeded"] += 1
            brand_counts["succeeded"] += 1
            print(
                f" - refreshed media_id={job.get('instagram_media_id')} "
                f"views={metrics.get('views')} reach={metrics.get('reach')}"
            )
        except InstagramActionError as exc:
            summary["failed"] += 1
            brand_counts["failed"] += 1
            summary["failures"].append(
                {
                    "media_id": job.get("instagram_media_id"),
                    "brand_id": brand_id,
                    "error": str(exc),
                }
            )
            logger.error(
                "Instagram media insight sync failed media_id=%s brand_id=%s error=%s",
                job.get("instagram_media_id"), brand_id, exc,
            )
            print(f" - failed media_id={job.get('instagram_media_id')}: {exc}")
    print(
        f"Instagram summary: checked={summary['total']} "
        f"succeeded={summary['succeeded']} failed={summary['failed']}"
    )
    for brand_id, counts in sorted(summary["by_brand"].items()):
        print(
            f" - brand={brand_id} checked={counts['checked']} "
            f"succeeded={counts['succeeded']} failed={counts['failed']}"
        )
    if summary["total"] and (summary["failed"] / summary["total"]) > 0.20:
        try:
            _send_failure_alert(summary)
        except Exception:
            logger.exception("Instagram media insight failure alert could not be sent")
    return summary


def _fetch_facebook_metrics(video_id: str, page_token: str):
    insights_url = f"{FB_API_BASE.rstrip('/')}/{video_id}/video_insights"
    metrics = "total_video_impressions,total_video_impressions_unique"
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
    except Exception as exc:
        print(f" - facebook insights fetch failed: {exc}")
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
    except Exception as exc:
        print(f" - facebook meta fetch failed: {exc}")
    return {
        "view_count": view_count,
        "reach": reach,
        "impressions": impressions,
        "reactions": reactions,
        "comment_count": comment_count,
        "permalink": permalink,
    }


def sync_facebook_metrics(max_items: Optional[int]) -> None:
    jobs = fetch_facebook_media_jobs(max_items)
    if not jobs:
        print("Facebook: no media with facebook_video_id stored.")
        return
    print(f"Facebook: checking {len(jobs)} media entries.")
    for job in jobs:
        video_id = job.get("facebook_video_id")
        if not video_id:
            continue
        page_info = get_facebook_page_data(job.get("user_id"))
        if not page_info or not page_info.get("page_access_token"):
            print(f" - missing page token for fb_video_id={video_id}")
            continue
        metrics = _fetch_facebook_metrics(video_id, page_info.get("page_access_token"))
        update_facebook_queue_metrics(
            job.get("id"),
            facebook_video_id=video_id,
            permalink=metrics.get("permalink"),
            view_count=metrics.get("view_count"),
            reach=metrics.get("reach"),
            impressions=metrics.get("impressions"),
            reactions=metrics.get("reactions"),
            comment_count=metrics.get("comment_count"),
        )
        print(f" - refreshed fb_video_id={video_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Instagram metrics/comments.")
    parser.add_argument("--max", type=int, default=None, help="Max media rows to process")
    parser.add_argument(
        "--comments-limit",
        type=int,
        default=25,
        help="Max comments per media to store (0 to disable comment fetch)",
    )
    parser.add_argument(
        "--published-since-days",
        type=int,
        default=None,
        help="Only refresh media published within this many days.",
    )
    args = parser.parse_args()
    published_since = None
    if args.published_since_days is not None:
        published_since = (
            datetime.now(timezone.utc) - timedelta(days=max(0, args.published_since_days))
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sync_instagram_metrics(
        args.max,
        max(0, args.comments_limit),
        published_since=published_since,
    )
    sync_facebook_metrics(args.max)
    return 0


if __name__ == "__main__":
    sys.exit(main())
