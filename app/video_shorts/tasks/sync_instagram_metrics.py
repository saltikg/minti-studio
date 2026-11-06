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
    InstagramUnsyncableError,
    refresh_instagram_media,
)
from app.video_shorts.services.instagram_queue import fetch_instagram_media_jobs
from app.video_shorts.services.instagram_metrics_state import (
    claim_instagram_metrics_alert,
    fetch_instagram_unsyncable_counts,
    mark_instagram_insights_unsyncable,
)
from app.video_shorts.services.email_verification import send_resend_email
from app.video_shorts.services.facebook_queue import (
    fetch_facebook_media_jobs,
    update_facebook_queue_metrics,
)
from app.video_shorts.services.facebook_metrics import (
    FacebookMetricsError,
    fetch_facebook_metrics,
)
from app.video_shorts.services.social_queue_identity import queue_identity, queue_token_identity
from src.trends.facebook_page_tokens import (
    get_facebook_page_data,
    mark_facebook_insights_status,
)

logger = logging.getLogger(__name__)
ACTIVE_INSTAGRAM_METRICS_DAYS = 60
ACTIVE_FACEBOOK_METRICS_DAYS = 60


def _send_failure_alert(summary: Dict[str, object]) -> None:
    failures = summary.get("failures") or []
    lines = [
        "Instagram media insight sync exceeded the failure threshold.",
        f"Checked: {summary['total']}",
        f"Attempted: {summary['attempted']}",
        f"Failed: {summary['failed']}",
        f"Unsyncable (excluded): {sum((summary.get('unsyncable_by_brand') or {}).values())}",
        "",
    ]
    lines.extend(
        f"media_id={item.get('media_id')} brand_id={item.get('brand_id')} error={item.get('error')}"
        for item in failures[:20]
    )
    text = "\n".join(lines)
    send_resend_email(
        to_email=(os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip(),
        subject=f"Minti Instagram metrics alert: {summary['failed']}/{summary['attempted']} failed",
        html="<p>" + html.escape(text).replace("\n", "<br>") + "</p>",
        text=text,
        error_message="Instagram metrics alert could not be sent.",
    )


def _send_social_failure_alert(
    instagram_summary: Dict[str, object],
    facebook_summary: Dict[str, object],
) -> None:
    instagram_failures = int(instagram_summary.get("failed") or 0)
    facebook_failures = int(facebook_summary.get("failed") or 0)
    lines = [
        "Social media insight sync needs attention.",
        (
            "Instagram: "
            f"attempted={instagram_summary.get('attempted', 0)} "
            f"failed={instagram_failures}"
        ),
        (
            "Facebook: "
            f"attempted={facebook_summary.get('attempted', 0)} "
            f"failed={facebook_failures} "
            f"needs_reconnect={facebook_summary.get('needs_reconnect', 0)}"
        ),
        "",
    ]
    lines.extend(
        f"Instagram media_id={item.get('media_id')} brand_id={item.get('brand_id')} error={item.get('error')}"
        for item in (instagram_summary.get("failures") or [])[:10]
    )
    lines.extend(
        f"Facebook video_id={item.get('video_id')} brand_id={item.get('brand_id')} error={item.get('error')}"
        for item in (facebook_summary.get("failures") or [])[:10]
    )
    text = "\n".join(lines)
    send_resend_email(
        to_email=(os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip(),
        subject=(
            "Minti social metrics alert: "
            f"{instagram_failures + facebook_failures} failure(s)"
        ),
        html="<p>" + html.escape(text).replace("\n", "<br>") + "</p>",
        text=text,
        error_message="Social metrics alert could not be sent.",
    )


def sync_instagram_metrics(
    max_items: Optional[int],
    comments_limit: int,
    *,
    published_since: Optional[str] = None,
    send_alert: bool = True,
) -> Dict[str, object]:
    if published_since is None:
        published_since = (
            datetime.now(timezone.utc) - timedelta(days=ACTIVE_INSTAGRAM_METRICS_DAYS)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    jobs = fetch_instagram_media_jobs(max_items, published_since=published_since)
    try:
        unsyncable_by_brand = fetch_instagram_unsyncable_counts()
    except Exception:
        logger.exception("Instagram unsyncable counts could not be loaded")
        unsyncable_by_brand = {}
    summary: Dict[str, object] = {
        "total": len(jobs),
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "unsyncable": 0,
        "failures": [],
        "by_brand": {},
        "unsyncable_by_brand": unsyncable_by_brand,
        "published_since": published_since,
    }
    if not jobs:
        print(f"Instagram: no active media published since {published_since}.")
        print("Instagram summary: checked=0 attempted=0 succeeded=0 failed=0 unsyncable=0")
        for brand_id, count in sorted(unsyncable_by_brand.items()):
            print(f" - brand={brand_id} unsyncable_total={count}")
        return summary
    print(
        f"Instagram: checking {len(jobs)} active media entries "
        f"published since {published_since}."
    )
    for job in jobs:
        brand_id = str(job.get("brand_id") or "unknown")
        brand_counts = summary["by_brand"].setdefault(
            brand_id,
            {"checked": 0, "attempted": 0, "succeeded": 0, "failed": 0, "unsyncable": 0},
        )
        brand_counts["checked"] += 1
        if brand_id == "unknown":
            reason_code = "missing_brand_id"
            reason = "Instagram queue row has no stored brand_id."
            mark_instagram_insights_unsyncable(
                job["id"],
                reason_code=reason_code,
                reason=reason,
            )
            summary["unsyncable"] += 1
            brand_counts["unsyncable"] += 1
            unsyncable_by_brand[brand_id] = unsyncable_by_brand.get(brand_id, 0) + 1
            logger.warning(
                "Instagram media marked unsyncable media_id=%s brand_id=%s reason=%s",
                job.get("instagram_media_id"), brand_id, reason_code,
            )
            print(
                f" - unsyncable media_id={job.get('instagram_media_id')} "
                f"reason={reason_code}"
            )
            continue
        try:
            metrics = refresh_instagram_media(job["id"], comments_limit=comments_limit)
            summary["attempted"] += 1
            summary["succeeded"] += 1
            brand_counts["attempted"] += 1
            brand_counts["succeeded"] += 1
            print(
                f" - refreshed media_id={job.get('instagram_media_id')} "
                f"views={metrics.get('views')} reach={metrics.get('reach')}"
            )
        except InstagramUnsyncableError as exc:
            mark_instagram_insights_unsyncable(
                job["id"],
                reason_code=exc.reason_code,
                reason=str(exc),
            )
            summary["unsyncable"] += 1
            brand_counts["unsyncable"] += 1
            unsyncable_by_brand[brand_id] = unsyncable_by_brand.get(brand_id, 0) + 1
            logger.warning(
                "Instagram media marked unsyncable media_id=%s brand_id=%s reason=%s",
                job.get("instagram_media_id"), brand_id, exc.reason_code,
            )
            print(
                f" - unsyncable media_id={job.get('instagram_media_id')} "
                f"reason={exc.reason_code}"
            )
        except InstagramActionError as exc:
            summary["attempted"] += 1
            summary["failed"] += 1
            brand_counts["attempted"] += 1
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
        f"attempted={summary['attempted']} succeeded={summary['succeeded']} "
        f"failed={summary['failed']} unsyncable={summary['unsyncable']}"
    )
    for brand_id, counts in sorted(summary["by_brand"].items()):
        print(
            f" - brand={brand_id} checked={counts['checked']} "
            f"attempted={counts['attempted']} succeeded={counts['succeeded']} "
            f"failed={counts['failed']} unsyncable={counts['unsyncable']}"
        )
    for brand_id, count in sorted(unsyncable_by_brand.items()):
        print(f" - brand={brand_id} unsyncable_total={count}")
    if send_alert and summary["attempted"] and (summary["failed"] / summary["attempted"]) > 0.20:
        try:
            if claim_instagram_metrics_alert(summary, cooldown_hours=24):
                _send_failure_alert(summary)
            else:
                print("Instagram alert suppressed: 24-hour cooldown active.")
        except Exception:
            logger.exception("Instagram media insight failure alert could not be sent")
    return summary


def sync_facebook_metrics(
    max_items: Optional[int],
    *,
    published_since: Optional[str] = None,
    send_alert: bool = True,
) -> Dict[str, object]:
    if published_since is None:
        published_since = (
            datetime.now(timezone.utc) - timedelta(days=ACTIVE_FACEBOOK_METRICS_DAYS)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    jobs = fetch_facebook_media_jobs(max_items, published_since=published_since)
    summary: Dict[str, object] = {
        "total": len(jobs),
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "needs_reconnect": 0,
        "failures": [],
        "by_brand": {},
        "published_since": published_since,
    }
    if not jobs:
        print(f"Facebook: no active media published since {published_since}.")
        return summary
    print(
        f"Facebook: checking {len(jobs)} media entries published since "
        f"{published_since}."
    )
    blocked_connections: Dict[str, str] = {}
    for job in jobs:
        video_id = job.get("facebook_video_id")
        if not video_id:
            continue
        brand_id = str(job.get("brand_id") or "unknown")
        brand_counts = summary["by_brand"].setdefault(
            brand_id,
            {"checked": 0, "attempted": 0, "succeeded": 0, "failed": 0},
        )
        brand_counts["checked"] += 1
        try:
            owner_user_id, scoped_brand_id = queue_identity(
                job.get("user_id"), job.get("brand_id")
            )
            credential_key = queue_token_identity(job)
        except ValueError as exc:
            summary["failed"] += 1
            brand_counts["failed"] += 1
            summary["failures"].append(
                {"video_id": video_id, "brand_id": brand_id, "error": str(exc)}
            )
            logger.error("Facebook media tenant identity invalid video_id=%s error=%s", video_id, exc)
            continue
        if credential_key in blocked_connections:
            continue
        page_info = get_facebook_page_data(credential_key)
        credential_page_id = str((page_info or {}).get("page_id") or "")
        queue_page_id = str(job.get("page_id") or "")
        if (
            not page_info
            or not page_info.get("page_access_token")
            or not credential_page_id
            or credential_page_id != queue_page_id
        ):
            error = (
                "Facebook credential is missing or does not match the queue Page "
                f"(queue={queue_page_id or 'missing'}, credential={credential_page_id or 'missing'})."
            )
            summary["failed"] += 1
            brand_counts["failed"] += 1
            summary["failures"].append(
                {"video_id": video_id, "brand_id": brand_id, "error": error}
            )
            blocked_connections[credential_key] = error
            logger.error("Facebook media credential mismatch video_id=%s brand_id=%s", video_id, brand_id)
            continue
        summary["attempted"] += 1
        brand_counts["attempted"] += 1
        try:
            metrics = fetch_facebook_metrics(
                str(video_id), str(page_info.get("page_access_token"))
            )
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
            mark_facebook_insights_status(
                owner_user_id,
                brand_id=str(scoped_brand_id),
                status="active",
            )
            summary["succeeded"] += 1
            brand_counts["succeeded"] += 1
            print(f" - refreshed fb_video_id={video_id}")
        except FacebookMetricsError as exc:
            status = "needs_reconnect" if exc.needs_reconnect else "error"
            mark_facebook_insights_status(
                owner_user_id,
                brand_id=str(scoped_brand_id),
                status=status,
                error=str(exc),
            )
            summary["failed"] += 1
            brand_counts["failed"] += 1
            if exc.needs_reconnect:
                summary["needs_reconnect"] += 1
                blocked_connections[credential_key] = str(exc)
            summary["failures"].append(
                {"video_id": video_id, "brand_id": brand_id, "error": str(exc)}
            )
            logger.error(
                "Facebook media insight sync failed video_id=%s brand_id=%s error=%s",
                video_id, brand_id, exc,
            )
    print(
        f"Facebook summary: checked={summary['total']} attempted={summary['attempted']} "
        f"succeeded={summary['succeeded']} failed={summary['failed']} "
        f"needs_reconnect={summary['needs_reconnect']}"
    )
    should_alert = bool(summary["needs_reconnect"]) or (
        bool(summary["attempted"])
        and (int(summary["failed"]) / int(summary["attempted"])) > 0.20
    )
    if send_alert and should_alert:
        alert_summary = {"instagram": {}, "facebook": summary}
        if claim_instagram_metrics_alert(
            alert_summary,
            cooldown_hours=24,
            alert_key="social_metrics_failures",
        ):
            _send_social_failure_alert({}, summary)
        else:
            print("Social metrics alert suppressed: 24-hour cooldown active.")
    return summary


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
    instagram_summary = sync_instagram_metrics(
        args.max,
        max(0, args.comments_limit),
        published_since=published_since,
        send_alert=False,
    )
    facebook_summary = sync_facebook_metrics(
        args.max,
        published_since=published_since,
        send_alert=False,
    )
    instagram_alert = bool(instagram_summary["attempted"]) and (
        int(instagram_summary["failed"]) / int(instagram_summary["attempted"])
    ) > 0.20
    facebook_alert = bool(facebook_summary["needs_reconnect"]) or (
        bool(facebook_summary["attempted"])
        and (int(facebook_summary["failed"]) / int(facebook_summary["attempted"])) > 0.20
    )
    if instagram_alert or facebook_alert:
        combined = {"instagram": instagram_summary, "facebook": facebook_summary}
        if claim_instagram_metrics_alert(
            combined,
            cooldown_hours=24,
            alert_key="social_metrics_failures",
        ):
            _send_social_failure_alert(instagram_summary, facebook_summary)
        else:
            print("Social metrics alert suppressed: 24-hour cooldown active.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
