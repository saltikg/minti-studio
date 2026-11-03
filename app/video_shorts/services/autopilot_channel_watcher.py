from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable
from urllib import parse, request
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from app.video_shorts.config import YT_DLP_COOKIES
from app.video_shorts.services.db import get_db
from app.video_shorts.services.email_verification import send_resend_email
from app.video_shorts.youtube_api import fetch_playlist_items_batch, fetch_video_stats


logger = logging.getLogger(__name__)
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
ADMIN_PATH = "/video_shorts/admin/autopilot-leads"
BASELINE_CUTOFF_UTC = datetime(2026, 11, 2, 22, 50, tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(
    timezone.utc
).replace(tzinfo=None)


def _utc_naive(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def fetch_channel_feed(channel_id: str) -> list[dict[str, Any]]:
    clean_channel_id = str(channel_id or "").strip()
    if not clean_channel_id:
        return []
    url = RSS_URL.format(channel_id=parse.quote(clean_channel_id, safe=""))
    req = request.Request(url, headers={"User-Agent": "MintiStudio-Autopilot-Watcher/1.0"})
    with request.urlopen(req, timeout=20) as response:
        body = response.read()
    root = ElementTree.fromstring(body)
    atom = "{http://www.w3.org/2005/Atom}"
    yt = "{http://www.youtube.com/xml/schemas/2015}"
    items: list[dict[str, Any]] = []
    for entry in root.findall(f"{atom}entry"):
        video_id = str(entry.findtext(f"{yt}videoId") or "").strip()
        if not video_id:
            continue
        items.append(
            {
                "video_id": video_id,
                "title": str(entry.findtext(f"{atom}title") or "").strip(),
                "published_at": _utc_naive(entry.findtext(f"{atom}published")),
            }
        )
    return items


def fetch_channel_data_api(channel_id: str) -> list[dict[str, Any]]:
    clean_channel_id = str(channel_id or "").strip()
    if not clean_channel_id.startswith("UC") or len(clean_channel_id) != 24:
        raise ValueError("A canonical YouTube channel id is required.")
    uploads_playlist_id = f"UU{clean_channel_id[2:]}"
    result = fetch_playlist_items_batch(
        uploads_playlist_id,
        max_results=15,
        quota_feature="autopilot_channel_watcher",
    )
    return list(result.get("videos") or [])


def _yt_dlp_published_at(entry: dict[str, Any]) -> datetime | None:
    timestamp = entry.get("timestamp") or entry.get("release_timestamp")
    if timestamp is not None:
        try:
            return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OSError):
            pass
    upload_date = str(entry.get("upload_date") or "").strip()
    if len(upload_date) == 8 and upload_date.isdigit():
        try:
            return datetime.strptime(upload_date, "%Y%m%d")
        except ValueError:
            pass
    return None


def fetch_channel_yt_dlp(channel_id: str) -> list[dict[str, Any]]:
    channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--flat-playlist",
        "--playlist-end",
        "15",
        "--skip-download",
        "--dump-single-json",
        "--no-warnings",
    ]
    if YT_DLP_COOKIES and YT_DLP_COOKIES.exists():
        command.extend(["--cookies", str(YT_DLP_COOKIES)])
    command.append(channel_url)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"yt-dlp exited with status {completed.returncode}")
    payload = json.loads(completed.stdout or "{}")
    items: list[dict[str, Any]] = []
    for entry in list(payload.get("entries") or [])[:15]:
        video_id = str(entry.get("id") or "").strip()
        if not video_id:
            continue
        items.append(
            {
                "video_id": video_id,
                "title": str(entry.get("title") or video_id),
                "published_at": _yt_dlp_published_at(entry),
                "duration_seconds": entry.get("duration"),
                "live_broadcast_content": str(entry.get("live_status") or "none").lower(),
            }
        )
    return items


def _merge_batched_metadata(
    items: list[dict[str, Any]],
    *,
    allow_flat_metadata: bool,
) -> list[dict[str, Any]]:
    video_ids = [str(item.get("video_id") or "").strip() for item in items]
    video_ids = [video_id for video_id in video_ids if video_id]
    if not video_ids:
        return []
    try:
        metadata_by_id = fetch_video_stats(video_ids)
    except Exception:
        if not allow_flat_metadata:
            raise
        logger.warning("Autopilot watcher videos.list failed; using yt-dlp flat metadata")
        metadata_by_id = {}
    if not metadata_by_id and not allow_flat_metadata:
        raise RuntimeError("videos.list returned no metadata")
    merged: list[dict[str, Any]] = []
    for item in items:
        video_id = str(item.get("video_id") or "").strip()
        metadata = metadata_by_id.get(video_id) or {}
        merged.append(
            {
                **item,
                **metadata,
                "video_id": video_id,
                "title": str(metadata.get("title") or item.get("title") or video_id),
                "published_at": _utc_naive(metadata.get("published_at"))
                or _utc_naive(item.get("published_at")),
                "duration_seconds": metadata.get("duration_seconds") or item.get("duration_seconds"),
                "live_broadcast_content": str(
                    metadata.get("live_broadcast_content")
                    or item.get("live_broadcast_content")
                    or "none"
                ).lower(),
            }
        )
    return merged


def fetch_channel_videos(channel_id: str) -> tuple[list[dict[str, Any]], str]:
    attempts = (
        ("rss", fetch_channel_feed, False),
        ("data_api", fetch_channel_data_api, False),
        ("yt_dlp", fetch_channel_yt_dlp, True),
    )
    errors: list[str] = []
    for source_name, loader, allow_flat_metadata in attempts:
        try:
            items = loader(channel_id)
            enriched = _merge_batched_metadata(items, allow_flat_metadata=allow_flat_metadata)
            logger.info(
                "Autopilot channel source channel_id=%s source=%s videos=%s",
                channel_id,
                source_name,
                len(enriched),
            )
            return enriched, source_name
        except Exception as exc:
            errors.append(f"{source_name}:{type(exc).__name__}")
            logger.warning(
                "Autopilot channel source failed channel_id=%s source=%s error_type=%s",
                channel_id,
                source_name,
                type(exc).__name__,
            )
    raise RuntimeError(f"All channel sources failed ({', '.join(errors)})")


def _load_customers(conn) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        WITH ranked_leads AS (
            SELECT l.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY CAST(l.user_id AS VARCHAR)
                       ORDER BY l.converted_at DESC NULLS LAST, l.id DESC
                   ) AS rn
            FROM autopilot_leads l
            WHERE l.converted_at IS NOT NULL
        )
        SELECT CAST(u.id AS VARCHAR),
               COALESCE(NULLIF(u.email, ''), NULLIF(u.username, ''), ''),
               CAST(l.brand_id AS VARCHAR),
               CASE
                   WHEN t.youtube_channel_id LIKE 'UC%' AND LENGTH(t.youtube_channel_id) = 24
                       THEN t.youtube_channel_id
                   WHEN l.youtube_channel_id LIKE 'UC%' AND LENGTH(l.youtube_channel_id) = 24
                       THEN l.youtube_channel_id
                   ELSE ''
               END,
               CASE
                   WHEN t.youtube_channel_id LIKE 'UC%' AND LENGTH(t.youtube_channel_id) = 24
                       THEN 'a:brand_connection'
                   WHEN l.youtube_channel_id LIKE 'UC%' AND LENGTH(l.youtube_channel_id) = 24
                       THEN 'b:lead_record'
                   ELSE 'none'
               END
        FROM shorts_users u
        LEFT JOIN ranked_leads l
          ON CAST(l.user_id AS VARCHAR) = CAST(u.id AS VARCHAR) AND l.rn = 1
        LEFT JOIN youtube_oauth_tokens_v2 t
          ON t.user_id = CAST(u.id AS VARCHAR) || '::' || CAST(l.brand_id AS VARCHAR)
        WHERE lower(COALESCE(u.service_mode, '')) = 'autopilot'
        ORDER BY COALESCE(u.email, u.username), u.id
        """
    ).fetchall()
    return [
        {
            "user_id": str(row[0] or "").strip(),
            "email": str(row[1] or "").strip(),
            "brand_id": str(row[2] or "").strip(),
            "channel_id": str(row[3] or "").strip(),
            "channel_source": str(row[4] or "").strip(),
        }
        for row in rows
    ]


def _minti_video_ids(conn, brand_id: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT youtube_video_id
        FROM shorts_generated_videos
        WHERE CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND COALESCE(youtube_video_id, '') <> ''
        UNION
        SELECT youtube_video_id
        FROM shorts_youtube_schedules
        WHERE CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND COALESCE(youtube_video_id, '') <> ''
        """,
        [brand_id, brand_id],
    ).fetchall()
    return {str(row[0]).strip() for row in rows if row and row[0]}


def _send_digest(items: list[dict[str, Any]]) -> None:
    admin_email = (os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip()
    base_url = (os.getenv("BASE_URL") or "https://mintistudio.com").rstrip("/")
    admin_url = f"{base_url}{ADMIN_PATH}"
    text_lines = ["New long-form uploads were detected for autopilot customers:", ""]
    html_rows: list[str] = []
    for item in items:
        duration = int(item.get("duration_s") or 0)
        duration_label = f"{duration // 60}:{duration % 60:02d}"
        video_url = f"https://www.youtube.com/watch?v={item['youtube_video_id']}"
        text_lines.extend(
            [
                str(item["email"]),
                f"{item['title']} ({duration_label})",
                video_url,
                "",
            ]
        )
        html_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['email']))}</td>"
            f"<td><a href=\"{html.escape(video_url)}\">{html.escape(str(item['title']))}</a></td>"
            f"<td>{duration_label}</td>"
            "</tr>"
        )
    text_lines.append(f"Admin: {admin_url}")
    html_body = (
        "<p>New long-form uploads were detected for autopilot customers.</p>"
        "<table><thead><tr><th>Customer</th><th>Video</th><th>Duration</th></tr></thead>"
        f"<tbody>{''.join(html_rows)}</tbody></table>"
        f"<p><a href=\"{html.escape(admin_url)}\">Open Autopilot Customers</a></p>"
    )
    send_resend_email(
        to_email=admin_email,
        subject=f"Minti Autopilot: {len(items)} new long-form upload{'s' if len(items) != 1 else ''}",
        html=html_body,
        text="\n".join(text_lines),
        error_message="Autopilot channel digest could not be sent.",
    )


def run_watcher(
    *,
    channel_loader: Callable[[str], tuple[list[dict[str, Any]], str]] = fetch_channel_videos,
    email_sender: Callable[[list[dict[str, Any]]], None] = _send_digest,
) -> dict[str, Any]:
    conn = get_db()
    reports: list[dict[str, Any]] = []
    new_items: list[dict[str, Any]] = []
    try:
        customers = _load_customers(conn)
        for customer in customers:
            report: dict[str, Any] = {
                "user_id": customer["user_id"],
                "brand_id": customer["brand_id"],
                "email": customer["email"],
                "channel_id": customer["channel_id"],
                "channel_resolution_source": customer["channel_source"],
                "fetch_source": "none",
                "videos_found": 0,
                "baseline": False,
                "inserted": 0,
                "new": 0,
                "seen": 0,
                "skipped": 0,
            }
            reports.append(report)
            if not customer["brand_id"] or not customer["channel_id"]:
                report["result"] = "No channel"
                continue
            baseline = not bool(
                conn.execute(
                    "SELECT 1 FROM autopilot_channel_videos WHERE brand_id = ? LIMIT 1",
                    [customer["brand_id"]],
                ).fetchone()
            )
            report["baseline"] = baseline
            minti_ids = _minti_video_ids(conn, customer["brand_id"])
            try:
                feed_items, fetch_source = channel_loader(customer["channel_id"])
            except Exception as exc:
                logger.warning(
                    "Autopilot channel fetch failed user_id=%s brand_id=%s channel_id=%s error_type=%s",
                    customer["user_id"],
                    customer["brand_id"],
                    customer["channel_id"],
                    type(exc).__name__,
                )
                report["result"] = f"Fetch error ({type(exc).__name__})"
                continue
            report["fetch_source"] = fetch_source
            report["videos_found"] = len(feed_items)
            for feed_item in feed_items:
                video_id = str(feed_item.get("video_id") or "").strip()
                if not video_id:
                    continue
                metadata = feed_item
                duration_s = int(metadata.get("duration_seconds") or 0)
                live_state = str(metadata.get("live_broadcast_content") or "none").strip().lower()
                skip = duration_s <= 180 or live_state in {"live", "upcoming"} or video_id in minti_ids
                published_at = _utc_naive(metadata.get("published_at"))
                status = "skipped"
                if not skip:
                    status = (
                        "seen"
                        if baseline and published_at is not None and published_at < BASELINE_CUTOFF_UTC
                        else "new"
                    )
                row = conn.execute(
                    """
                    INSERT INTO autopilot_channel_videos (
                        user_id, brand_id, channel_id, youtube_video_id, title,
                        published_at, duration_s, detected_at, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    ON CONFLICT (brand_id, youtube_video_id) DO NOTHING
                    RETURNING id
                    """,
                    [
                        customer["user_id"],
                        customer["brand_id"],
                        customer["channel_id"],
                        video_id,
                        str(metadata.get("title") or feed_item.get("title") or video_id),
                        published_at,
                        duration_s or None,
                        status,
                    ],
                ).fetchone()
                if not row:
                    continue
                report["inserted"] += 1
                report[status] += 1
                if status == "new":
                    new_items.append(
                        {
                            **customer,
                            "id": int(row[0]),
                            "youtube_video_id": video_id,
                            "title": str(metadata.get("title") or feed_item.get("title") or video_id),
                            "duration_s": duration_s,
                        }
                    )
            report["result"] = "ok"
        if new_items:
            email_sender(new_items)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"customers": reports, "new_videos": len(new_items), "email_sent": bool(new_items)}
