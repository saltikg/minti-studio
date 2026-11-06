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
PLATFORM_ORDER = {"youtube": 0, "instagram": 1, "facebook": 2}


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
    generated_by_scope_filename: dict[tuple[str, str, str], str] = {}
    for row in _table_rows(
        conn,
        "shorts_generated_videos",
        """
        SELECT id, CAST(user_id AS VARCHAR) AS user_id,
               CAST(brand_id AS VARCHAR) AS brand_id, clip_filename, output_filename
        FROM shorts_generated_videos
        """,
    ):
        for filename in (row.get("clip_filename"), row.get("output_filename")):
            clean_filename = str(filename or "").strip()
            if clean_filename:
                generated_by_scope_filename[
                    (
                        str(row.get("user_id") or "").strip(),
                        str(row.get("brand_id") or "").strip(),
                        clean_filename,
                    )
                ] = str(row.get("id") or "").strip()
    return (
        users,
        brands,
        customer_scopes,
        videos_by_pk,
        videos_by_scope_source,
        generated_by_scope_filename,
    )


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
    generated_video_id: str,
    source_video_id: str,
    plan_index: Any,
    clip_filename: str,
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
    published_dt = _utc(published_at)
    event_dt = (published_dt or publish_dt) if status_key == "published" else (_utc(updated_at) or publish_dt)
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
        "generated_video_id": str(generated_video_id or "").strip(),
        "source_video_id": str(source_video_id or (video or {}).get("video_id") or "").strip(),
        "plan_index": str(plan_index if plan_index is not None else "").strip(),
        "clip_filename": str(clip_filename or "").strip(),
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
        "platform_time": (published_dt or publish_dt) if status_key == "published" else publish_dt,
        "live_url": _live_url(platform, row) if status_key == "published" else "",
        "raw_status": str(row.get("status") or "").strip(),
        "token_key": token_key,
    }


def _short_group_key(item: dict[str, Any]) -> tuple[str, ...]:
    """Return the strongest available tenant-scoped generated-Short identity."""
    tenant = (str(item.get("owner_user_id") or ""), str(item.get("brand_id") or ""))
    generated_video_id = str(item.get("generated_video_id") or "").strip()
    if generated_video_id:
        return (*tenant, "generated", generated_video_id)
    plan_index = str(item.get("plan_index") or "").strip()
    video_pk = item.get("video_pk")
    if video_pk not in (None, "") and plan_index:
        return (*tenant, "video-plan", str(video_pk), plan_index)
    source_video_id = str(item.get("source_video_id") or "").strip()
    if source_video_id and plan_index:
        return (*tenant, "source-plan", source_video_id, plan_index)
    clip_filename = str(item.get("clip_filename") or "").strip()
    if clip_filename:
        return (*tenant, "clip", clip_filename)
    return (*tenant, "platform-row", str(item.get("platform") or ""), str(item.get("id") or ""))


def _group_platform_items(
    items: Iterable[dict[str, Any]],
    *,
    now_utc: datetime,
    published_cutoff: datetime,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(_short_group_key(item), []).append(item)

    shorts: list[dict[str, Any]] = []
    for group_key, platform_rows in grouped.items():
        # A generated Short has one active record per platform. If legacy duplicates
        # exist, keep the most recently observed record instead of rendering two chips.
        by_platform: dict[str, dict[str, Any]] = {}
        for platform_row in platform_rows:
            platform = str(platform_row.get("platform") or "")
            previous = by_platform.get(platform)
            if previous is None or (
                platform_row.get("event_at") or datetime.min.replace(tzinfo=timezone.utc)
            ) > (
                previous.get("event_at") or datetime.min.replace(tzinfo=timezone.utc)
            ):
                by_platform[platform] = platform_row
        platforms = sorted(
            by_platform.values(),
            key=lambda item: PLATFORM_ORDER.get(str(item.get("platform") or ""), 99),
        )
        if not platforms:
            continue

        preferred = next((item for item in platforms if item["platform"] == "youtube"), platforms[0])
        platform_times = [item["platform_time"] for item in platforms if item.get("platform_time")]
        earliest_time = min(platform_times) if platform_times else None
        time_mismatch = bool(
            len(platform_times) > 1
            and (max(platform_times) - min(platform_times)).total_seconds() > 5 * 60
        )
        for platform_row in platforms:
            platform_row["show_platform_time"] = time_mismatch
            platform_row["platform_time_label"] = _pt_label(platform_row.get("platform_time"))

        has_scheduled = any(item["status_key"] == "scheduled" for item in platforms)
        recent_event = max(
            (item["event_at"] for item in platforms if item.get("event_at")),
            default=None,
        )
        if has_scheduled:
            section = "upcoming"
        elif recent_event and recent_event >= published_cutoff:
            section = "published"
        else:
            continue

        is_problem = any(item["is_problem"] for item in platforms)
        shorts.append(
            {
                "id": "|".join(group_key),
                "owner_user_id": preferred["owner_user_id"],
                "brand_id": preferred["brand_id"],
                "brand_name": preferred["brand_name"],
                "is_autopilot_customer": preferred["is_autopilot_customer"],
                "has_customer_workspace": preferred["has_customer_workspace"],
                "video_pk": preferred.get("video_pk"),
                "short_title": preferred["short_title"],
                "thumbnail_url": preferred["thumbnail_url"],
                "platforms": platforms,
                "platform_names": {item["platform"] for item in platforms},
                "post_count": len(platforms),
                "problem_post_count": sum(1 for item in platforms if item["is_problem"]),
                "is_problem": is_problem,
                "is_mismatch": is_problem,
                "publish_at": earliest_time,
                "publish_time_label": _pt_label(earliest_time),
                "event_at": recent_event,
                "section": section,
                "has_time_mismatch": time_mismatch,
                "is_upcoming": section == "upcoming",
                "now": now_utc,
            }
        )
    return shorts


def load_scheduled_shorts(conn, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Return one row per generated Short with its platform posts grouped."""
    now_utc = _utc(now) or datetime.now(timezone.utc)
    published_cutoff = now_utc - timedelta(days=7)
    (
        users,
        brands,
        customer_scopes,
        videos_by_pk,
        videos_by_scope_source,
        generated_by_scope_filename,
    ) = _load_reference_data(conn)
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
        clip_filename = str(payload.get("filename") or "").strip()
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
            generated_video_id=generated_by_scope_filename.get((owner, brand, clip_filename), ""),
            source_video_id=str((video or {}).get("video_id") or ""),
            plan_index=payload.get("plan_index"),
            clip_filename=clip_filename,
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
            clip_filename = str(row.get("clip_filename") or "").strip()
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
                generated_video_id=generated_by_scope_filename.get((owner, brand, clip_filename), ""),
                source_video_id=source_video_id,
                plan_index=row.get("plan_index"),
                clip_filename=clip_filename,
                title=str(row.get("plan_title") or ""),
                publish_at=row.get("publish_at"),
                published_at=row.get("published_at"),
                updated_at=row.get("updated_at"),
                users=users,
                brands=brands,
                customer_scopes=customer_scopes,
            )
            items.append(item)

    return _group_platform_items(items, now_utc=now_utc, published_cutoff=published_cutoff)


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
        and (not platform or platform in item["platform_names"])
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
        "upcoming_post_count": sum(item["post_count"] for item in upcoming),
        "published_count": len(published),
        "published_post_count": sum(item["post_count"] for item in published),
        "problem_count": sum(1 for item in filtered if item["is_problem"]),
        "problem_post_count": sum(item["post_count"] for item in filtered if item["is_problem"]),
        "platform": platform,
        "brand_id": brand_id,
        "include_all_brands": include_all_brands,
    }
