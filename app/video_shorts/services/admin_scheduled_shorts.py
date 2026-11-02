"""Read-only aggregation for the admin Scheduled Shorts page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from app.video_shorts.services.db import table_columns


PT = ZoneInfo("America/Los_Angeles")
SCHEDULED_STATUSES = {"pending", "queued", "uploading", "retry", "scheduled"}
FAILED_STATUSES = {"failed", "missed", "uncertain"}
PUBLISHED_STATUSES = {"published", "uploaded"}
BLOCKED_STATUS = "blocked_target_mismatch"


def _utc(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pt_label(value: Optional[datetime]) -> str:
    if not value:
        return "—"
    local = value.astimezone(PT)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a %b')} {local.day}, {hour}:{local.strftime('%M %p')}"


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _scope(user_id: Any, brand_id: Any) -> tuple[str, str, str]:
    raw_user = str(user_id or "").strip()
    raw_brand = str(brand_id or "").strip()
    owner = raw_user
    embedded_brand = ""
    if "::" in raw_user:
        owner, embedded_brand = raw_user.split("::", 1)
        owner = owner.strip()
        embedded_brand = embedded_brand.strip()
    brand = raw_brand or embedded_brand
    token_key = f"{owner}::{brand}" if owner and brand else ""
    return owner, brand, token_key


def _rows(conn, sql: str, params: Optional[list[Any]] = None) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params or [])
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _table_rows(conn, table_name: str, sql: str) -> list[dict[str, Any]]:
    if not table_columns(conn, table_name):
        return []
    return _rows(conn, sql)


def _status(raw_status: Any) -> tuple[str, str]:
    normalized = str(raw_status or "").strip().lower()
    if normalized in PUBLISHED_STATUSES:
        return "published", "Published ✓"
    if normalized == BLOCKED_STATUS:
        return "blocked", "Blocked"
    if normalized in FAILED_STATUSES:
        return "failed", "Failed"
    return "scheduled", "Scheduled"


def _load_reference_data(conn):
    users = {
        str(row["id"]): {
            "service_mode": str(row.get("service_mode") or "").strip().lower(),
            "email": str(row.get("email") or "").strip(),
        }
        for row in _table_rows(
            conn,
            "shorts_users",
            "SELECT CAST(id AS VARCHAR) AS id, service_mode, email FROM shorts_users",
        )
    }
    brands = {
        str(row["id"]): {
            "name": str(row.get("name") or "").strip() or "Untitled brand",
            "owner_user_id": str(row.get("owner_user_id") or "").strip(),
        }
        for row in _table_rows(
            conn,
            "shorts_brands",
            "SELECT CAST(id AS VARCHAR) AS id, name, CAST(owner_user_id AS VARCHAR) AS owner_user_id FROM shorts_brands",
        )
    }
    customer_scopes: set[tuple[str, str]] = set()
    if table_columns(conn, "autopilot_leads"):
        for row in _rows(
            conn,
            """
            SELECT DISTINCT CAST(user_id AS VARCHAR) AS user_id,
                            CAST(brand_id AS VARCHAR) AS brand_id
            FROM autopilot_leads
            WHERE converted_at IS NOT NULL
              AND user_id IS NOT NULL
              AND brand_id IS NOT NULL
            """,
        ):
            customer_scopes.add((str(row["user_id"]), str(row["brand_id"])))

    videos_by_pk: dict[int, dict[str, Any]] = {}
    videos_by_scope_source: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _table_rows(
        conn,
        "youtube_videos",
        """
        SELECT id, CAST(owner_user_id AS VARCHAR) AS owner_user_id,
               CAST(brand_id AS VARCHAR) AS brand_id, CAST(video_id AS VARCHAR) AS video_id,
               title, thumbnail_url
        FROM youtube_videos
        """,
    ):
        video = {
            "id": int(row["id"]),
            "owner_user_id": str(row.get("owner_user_id") or "").strip(),
            "brand_id": str(row.get("brand_id") or "").strip(),
            "video_id": str(row.get("video_id") or "").strip(),
            "title": str(row.get("title") or "").strip(),
            "thumbnail_url": str(row.get("thumbnail_url") or "").strip(),
        }
        videos_by_pk[video["id"]] = video
        videos_by_scope_source[(video["owner_user_id"], video["brand_id"], video["video_id"])] = video
    return users, brands, customer_scopes, videos_by_pk, videos_by_scope_source


def _load_connections(conn):
    youtube: dict[str, dict[str, str]] = {}
    youtube_titles_by_id: dict[str, str] = {}
    for row in _table_rows(
        conn,
        "youtube_oauth_tokens_v2",
        "SELECT user_id, youtube_channel_id, channel_title FROM youtube_oauth_tokens_v2",
    ):
        key = str(row.get("user_id") or "").strip()
        channel_id = str(row.get("youtube_channel_id") or "").strip()
        title = str(row.get("channel_title") or "").strip()
        youtube[key] = {"id": channel_id, "name": title}
        if channel_id and title:
            youtube_titles_by_id[channel_id] = title
    instagram = {
        str(row.get("user_id") or "").strip(): {
            "id": str(row.get("instagram_business_account_id") or "").strip(),
            "name": str(row.get("instagram_username") or "").strip(),
        }
        for row in _table_rows(
            conn,
            "instagram_oauth_tokens",
            "SELECT user_id, instagram_business_account_id, instagram_username FROM instagram_oauth_tokens",
        )
    }
    facebook = {
        str(row.get("user_id") or "").strip(): {
            "id": str(row.get("page_id") or "").strip(),
            "name": str(row.get("page_name") or "").strip(),
        }
        for row in _table_rows(
            conn,
            "facebook_page_tokens",
            "SELECT user_id, page_id, page_name FROM facebook_page_tokens",
        )
    }
    return youtube, youtube_titles_by_id, instagram, facebook


def _live_url(platform: str, row: dict[str, Any]) -> str:
    if platform == "youtube" and row.get("youtube_video_id"):
        return f"https://www.youtube.com/watch?v={row['youtube_video_id']}"
    permalink = str(row.get("permalink") or "").strip()
    if permalink:
        return permalink
    if platform == "facebook" and row.get("facebook_video_id") and row.get("page_id"):
        return f"https://www.facebook.com/{row['page_id']}/videos/{row['facebook_video_id']}"
    return ""


def _build_item(
    *,
    platform: str,
    row: dict[str, Any],
    owner_user_id: str,
    brand_id: str,
    token_key: str,
    expected_target_id: str,
    snapshot_target_name: str,
    current_connection: dict[str, str],
    video: Optional[dict[str, Any]],
    title: str,
    publish_at: Any,
    published_at: Any,
    updated_at: Any,
    users: dict[str, dict[str, str]],
    brands: dict[str, dict[str, str]],
    customer_scopes: set[tuple[str, str]],
) -> dict[str, Any]:
    current_target_id = str((current_connection or {}).get("id") or "").strip()
    expected_target_id = str(expected_target_id or "").strip()
    mismatch = not expected_target_id or not current_target_id or expected_target_id != current_target_id
    status_key, status_label = _status(row.get("status"))
    publish_dt = _utc(publish_at)
    event_dt = _utc(published_at) or _utc(updated_at) or publish_dt
    brand = brands.get(brand_id) or {}
    user = users.get(owner_user_id) or {}
    is_customer = user.get("service_mode") == "autopilot"
    channel_name = str(snapshot_target_name or "").strip() or expected_target_id or "Missing target"
    return {
        "id": str(row.get("id") or ""),
        "platform": platform,
        "platform_label": platform.title(),
        "owner_user_id": owner_user_id,
        "brand_id": brand_id,
        "brand_name": str(brand.get("name") or "").strip() or "Unknown brand",
        "is_autopilot_customer": is_customer,
        "has_customer_workspace": (owner_user_id, brand_id) in customer_scopes,
        "video_pk": (video or {}).get("id"),
        "short_title": str(title or "").strip() or (video or {}).get("title") or "Untitled short",
        "thumbnail_url": str((video or {}).get("thumbnail_url") or "").strip(),
        "channel_name": channel_name,
        "expected_target_id": expected_target_id,
        "current_target_id": current_target_id,
        "is_mismatch": mismatch,
        "status_key": status_key,
        "status_label": status_label,
        "is_problem": mismatch or status_key in {"blocked", "failed"},
        "publish_at": publish_dt,
        "publish_time_label": _pt_label(publish_dt),
        "event_at": event_dt,
        "live_url": _live_url(platform, row) if status_key == "published" else "",
        "raw_status": str(row.get("status") or "").strip(),
        "token_key": token_key,
    }


def load_scheduled_shorts(conn, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Return upcoming schedules plus published rows from the last seven days."""
    now_utc = _utc(now) or datetime.now(timezone.utc)
    published_cutoff = now_utc - timedelta(days=7)
    users, brands, customer_scopes, videos_by_pk, videos_by_scope_source = _load_reference_data(conn)
    youtube_connections, youtube_titles_by_id, instagram_connections, facebook_connections = _load_connections(conn)
    items: list[dict[str, Any]] = []

    youtube_rows = _table_rows(
        conn,
        "shorts_youtube_schedules",
        """
        SELECT id, user_id, brand_id, expected_youtube_channel_id, payload_json,
               status, publish_at, youtube_video_id, updated_at
        FROM shorts_youtube_schedules
        WHERE status NOT IN ('cancelled')
        """,
    )
    for row in youtube_rows:
        owner, brand, token_key = _scope(row.get("user_id"), row.get("brand_id"))
        payload = _json_object(row.get("payload_json"))
        video_pk = payload.get("video_pk")
        try:
            video = videos_by_pk.get(int(video_pk)) if video_pk is not None else None
        except (TypeError, ValueError):
            video = None
        expected = str(row.get("expected_youtube_channel_id") or "").strip()
        item = _build_item(
            platform="youtube",
            row=row,
            owner_user_id=owner,
            brand_id=brand,
            token_key=token_key,
            expected_target_id=expected,
            snapshot_target_name=youtube_titles_by_id.get(expected, ""),
            current_connection=youtube_connections.get(token_key) or {},
            video=video,
            title=str(payload.get("title") or ""),
            publish_at=row.get("publish_at"),
            published_at=None,
            updated_at=row.get("updated_at"),
            users=users,
            brands=brands,
            customer_scopes=customer_scopes,
        )
        items.append(item)

    for platform, table_name, connections, query, expected_field, name_field in (
        (
            "instagram",
            "shorts_instagram_queue",
            instagram_connections,
            """
            SELECT id, user_id, brand_id, video_id, plan_index, clip_filename, plan_title,
                   instagram_business_account_id, instagram_username, status, publish_at,
                   published_at, instagram_media_id, permalink, updated_at
            FROM shorts_instagram_queue
            WHERE status NOT IN ('cancelled')
            """,
            "instagram_business_account_id",
            "instagram_username",
        ),
        (
            "facebook",
            "shorts_facebook_queue",
            facebook_connections,
            """
            SELECT id, user_id, brand_id, video_id, plan_index, clip_filename, plan_title,
                   page_id, page_name, status, publish_at, published_at,
                   facebook_video_id, permalink, updated_at
            FROM shorts_facebook_queue
            WHERE status NOT IN ('cancelled')
            """,
            "page_id",
            "page_name",
        ),
    ):
        for row in _table_rows(conn, table_name, query):
            owner, brand, token_key = _scope(row.get("user_id"), row.get("brand_id"))
            source_video_id = str(row.get("video_id") or "").strip()
            video = videos_by_scope_source.get((owner, brand, source_video_id))
            item = _build_item(
                platform=platform,
                row=row,
                owner_user_id=owner,
                brand_id=brand,
                token_key=token_key,
                expected_target_id=str(row.get(expected_field) or ""),
                snapshot_target_name=str(row.get(name_field) or ""),
                current_connection=connections.get(token_key) or {},
                video=video,
                title=str(row.get("plan_title") or ""),
                publish_at=row.get("publish_at"),
                published_at=row.get("published_at"),
                updated_at=row.get("updated_at"),
                users=users,
                brands=brands,
                customer_scopes=customer_scopes,
            )
            items.append(item)

    visible: list[dict[str, Any]] = []
    for item in items:
        if item["status_key"] == "published":
            if item["event_at"] and item["event_at"] >= published_cutoff:
                item["section"] = "published"
                visible.append(item)
            continue
        if item["publish_at"] and item["publish_at"] >= now_utc:
            item["section"] = "upcoming"
            visible.append(item)
    return visible


def filter_scheduled_shorts(
    items: Iterable[dict[str, Any]],
    *,
    include_all_brands: bool,
    brand_id: str = "",
    platform: str = "",
) -> dict[str, Any]:
    platform = str(platform or "").strip().lower()
    if platform not in {"youtube", "instagram", "facebook"}:
        platform = ""
    brand_id = str(brand_id or "").strip()
    scope_items = [item for item in items if include_all_brands or item["is_autopilot_customer"]]
    brand_options = sorted(
        {
            (item["brand_id"], item["brand_name"])
            for item in scope_items
            if item.get("brand_id")
        },
        key=lambda value: value[1].lower(),
    )
    filtered = [
        item
        for item in scope_items
        if (not brand_id or item["brand_id"] == brand_id)
        and (not platform or item["platform"] == platform)
    ]
    upcoming = sorted(
        (item for item in filtered if item["section"] == "upcoming"),
        key=lambda item: item["publish_at"] or datetime.max.replace(tzinfo=timezone.utc),
    )
    published = sorted(
        (item for item in filtered if item["section"] == "published"),
        key=lambda item: item["event_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {
        "upcoming": upcoming,
        "published": published,
        "brand_options": [{"id": item[0], "name": item[1]} for item in brand_options],
        "upcoming_count": len(upcoming),
        "published_count": len(published),
        "problem_count": sum(1 for item in filtered if item["is_problem"]),
        "platform": platform,
        "brand_id": brand_id,
        "include_all_brands": include_all_brands,
    }
