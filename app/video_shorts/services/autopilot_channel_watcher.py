from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable
from urllib import parse, request
from xml.etree import ElementTree

from app.video_shorts.services.db import get_db
from app.video_shorts.services.email_verification import send_resend_email
from app.video_shorts.youtube_api import fetch_video_metadata


logger = logging.getLogger(__name__)
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
ADMIN_PATH = "/video_shorts/admin/autopilot-leads"


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
                   WHEN t.youtube_channel_id LIKE 'UC%' AND LENGTH(t.youtube_channel_id) = 24 THEN 'connected'
                   WHEN l.youtube_channel_id LIKE 'UC%' AND LENGTH(l.youtube_channel_id) = 24 THEN 'lead'
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
    feed_loader: Callable[[str], list[dict[str, Any]]] = fetch_channel_feed,
    metadata_loader: Callable[[str], dict[str, Any]] = fetch_video_metadata,
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
                "channel_source": customer["channel_source"],
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
                feed_items = feed_loader(customer["channel_id"])
            except Exception as exc:
                logger.warning(
                    "Autopilot channel RSS failed user_id=%s brand_id=%s channel_id=%s error_type=%s",
                    customer["user_id"],
                    customer["brand_id"],
                    customer["channel_id"],
                    type(exc).__name__,
                )
                report["result"] = f"Feed error ({type(exc).__name__})"
                continue
            report["metadata_errors"] = 0
            for feed_item in feed_items:
                video_id = str(feed_item.get("video_id") or "").strip()
                if not video_id:
                    continue
                try:
                    metadata = metadata_loader(video_id) or {}
                except Exception as exc:
                    report["metadata_errors"] += 1
                    logger.warning(
                        "Autopilot video metadata failed brand_id=%s video_id=%s error_type=%s",
                        customer["brand_id"],
                        video_id,
                        type(exc).__name__,
                    )
                    continue
                duration_s = int(metadata.get("duration_seconds") or 0)
                live_state = str(metadata.get("live_broadcast_content") or "none").strip().lower()
                skip = duration_s <= 180 or live_state in {"live", "upcoming"} or video_id in minti_ids
                status = "skipped" if skip else ("seen" if baseline else "new")
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
                        _utc_naive(metadata.get("published_at")) or feed_item.get("published_at"),
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
