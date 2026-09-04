"""Tenant-safe persistence for pre-conversion autopilot leads."""

from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import uuid4

from app.video_shorts.config import DEFAULT_USER_PLAN_ID
from app.video_shorts.services.brands import create_brand, ensure_brand_schema
from app.video_shorts.services.db import (
    _schema_management_enabled,
    ensure_auth_user_schema,
    ensure_channel_owner_schema,
    ensure_storage_user_schema,
    table_columns,
)

AUTOPILOT_LEADS_TABLE = "autopilot_leads"
LOCAL_UPLOADS_CHANNEL_NAME = "Local uploads"


class AutopilotLeadSchemaUnavailable(RuntimeError):
    pass


def ensure_autopilot_leads_schema(conn) -> None:
    """Create the lead table only in self-managed development databases.

    Production Postgres schema changes are intentionally applied by the migration script.
    """
    if not _schema_management_enabled():
        return
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {AUTOPILOT_LEADS_TABLE} (
            id VARCHAR PRIMARY KEY,
            creator_email VARCHAR,
            creator_name VARCHAR,
            youtube_channel_id VARCHAR NOT NULL,
            channel_id BIGINT,
            first_video_id BIGINT,
            user_id VARCHAR,
            brand_id VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            converted_at TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_{AUTOPILOT_LEADS_TABLE}_youtube_channel
        ON {AUTOPILOT_LEADS_TABLE}(youtube_channel_id)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{AUTOPILOT_LEADS_TABLE}_user_brand
        ON {AUTOPILOT_LEADS_TABLE}(user_id, brand_id)
        """
    )
    conn.commit()


def _require_autopilot_leads_table(conn) -> None:
    ensure_autopilot_leads_schema(conn)
    required = {
        "id",
        "creator_email",
        "creator_name",
        "youtube_channel_id",
        "channel_id",
        "first_video_id",
        "user_id",
        "brand_id",
        "created_at",
        "converted_at",
    }
    columns = table_columns(conn, AUTOPILOT_LEADS_TABLE)
    if not required.issubset(columns):
        raise AutopilotLeadSchemaUnavailable(
            "Autopilot leads are unavailable until the database migration is applied."
        )


def autopilot_leads_table_ready(conn) -> bool:
    try:
        _require_autopilot_leads_table(conn)
    except AutopilotLeadSchemaUnavailable:
        return False
    return True


def _normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _get_or_create_local_uploads_channel(conn, *, owner_user_id: str, brand_id: str) -> int:
    row = conn.execute(
        """
        SELECT channel_id
        FROM youtube_channels
        WHERE owner_user_id = ?
          AND brand_id = ?
          AND lower(coalesce(channel_url, '')) = 'local://uploads'
        LIMIT 1
        """,
        [owner_user_id, brand_id],
    ).fetchone()
    if row:
        return int(row[0])
    next_channel_id = conn.execute(
        "SELECT COALESCE(MAX(channel_id), 0) + 1 FROM youtube_channels"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO youtube_channels (
            channel_id, channel_name, channel_url, notes, owner_user_id, is_active, brand_id
        )
        VALUES (?, ?, 'local://uploads', 'Local uploads', ?, TRUE, ?)
        """,
        [next_channel_id, LOCAL_UPLOADS_CHANNEL_NAME, owner_user_id, brand_id],
    )
    return int(next_channel_id)


def get_or_create_scoped_youtube_channel(
    conn,
    *,
    meta: Dict[str, Any],
    owner_user_id: str,
    brand_id: str,
    notes: str = "Autopilot lead import",
) -> Optional[int]:
    """Resolve a public channel only within the requested owner/brand scope."""
    youtube_channel_id = str(meta.get("channel_id") or "").strip()
    if not youtube_channel_id or not owner_user_id or not brand_id:
        return None
    row = conn.execute(
        """
        SELECT channel_id
        FROM youtube_channels
        WHERE youtube_channel_id = ?
          AND owner_user_id = ?
          AND brand_id = ?
        LIMIT 1
        """,
        [youtube_channel_id, owner_user_id, brand_id],
    ).fetchone()
    if row:
        return int(row[0])
    next_channel_id = conn.execute(
        "SELECT COALESCE(MAX(channel_id), 0) + 1 FROM youtube_channels"
    ).fetchone()[0]
    channel_name = str(meta.get("channel_title") or "YouTube Channel").strip() or "YouTube Channel"
    conn.execute(
        """
        INSERT INTO youtube_channels (
            channel_id, channel_name, channel_url, notes, owner_user_id,
            youtube_channel_id, is_active, brand_id
        )
        VALUES (?, ?, ?, ?, ?, ?, TRUE, ?)
        """,
        [
            next_channel_id,
            channel_name,
            f"https://www.youtube.com/channel/{youtube_channel_id}",
            notes,
            owner_user_id,
            youtube_channel_id,
            brand_id,
        ],
    )
    return int(next_channel_id)


def _load_reusable_lead_owner(conn, *, email: str) -> Optional[Dict[str, str]]:
    row = conn.execute(
        f"""
        SELECT
            l.user_id,
            l.brand_id,
            u.email,
            u.password_hash,
            coalesce(u.email_verified, FALSE)
        FROM {AUTOPILOT_LEADS_TABLE} l
        JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(l.user_id AS VARCHAR)
        WHERE lower(coalesce(l.creator_email, '')) = ?
          AND l.user_id IS NOT NULL
          AND l.brand_id IS NOT NULL
        ORDER BY l.created_at ASC
        LIMIT 1
        """,
        [email],
    ).fetchone()
    if not row:
        return None
    # Only reuse the placeholder that this feature created. A real user must never
    # be silently repurposed as a lead because an admin entered their email.
    if bool(row[3]) or bool(row[4]) or _normalize_email(row[2]) != email:
        return None
    return {"user_id": str(row[0]), "brand_id": str(row[1])}


def _provision_or_reuse_lead_owner(conn, *, email: str, channel_name: str) -> Dict[str, str]:
    reusable = _load_reusable_lead_owner(conn, email=email)
    if reusable:
        return reusable

    existing = conn.execute(
        """
        SELECT CAST(id AS VARCHAR)
        FROM shorts_users
        WHERE lower(email) = ? OR lower(username) = ?
        LIMIT 1
        """,
        [email, email],
    ).fetchone()
    if existing:
        raise ValueError("An account already exists for this email. No changes were made.")

    user_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO shorts_users (
            id, username, password_hash, name, email, role, plan_id,
            email_verified, pending_service_intent, pending_service_tier, created_at, updated_at
        )
        VALUES (?, ?, NULL, ?, ?, 'member', ?, FALSE, 'autopilot', 15, now(), now())
        """,
        [user_id, email, channel_name, email, DEFAULT_USER_PLAN_ID],
    )
    brand = create_brand(conn, user_id=user_id, name=channel_name, make_default=True, commit=False)
    return {"user_id": user_id, "brand_id": str(brand["id"])}


def create_autopilot_lead_from_video(
    conn,
    *,
    meta: Dict[str, Any],
    video_id: str,
    canonical_url: str,
    creator_name: str | None,
    creator_email: str | None,
    discovery_owner_user_id: str,
    discovery_brand_id: str,
) -> Dict[str, Any]:
    """Create/update one lead and stamp its first source video to the correct tenant."""
    _require_autopilot_leads_table(conn)
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    ensure_brand_schema(conn)
    ensure_channel_owner_schema(conn)

    youtube_channel_id = str(meta.get("channel_id") or "").strip()
    channel_name = str(meta.get("channel_title") or creator_name or "YouTube Channel").strip() or "YouTube Channel"
    email = _normalize_email(creator_email)
    creator_name = str(creator_name or channel_name).strip() or channel_name
    if not youtube_channel_id:
        raise ValueError("The creator channel could not be resolved.")

    existing_lead = conn.execute(
        f"""
        SELECT id, creator_email, user_id, brand_id, first_video_id
        FROM {AUTOPILOT_LEADS_TABLE}
        WHERE youtube_channel_id = ?
        LIMIT 1
        """,
        [youtube_channel_id],
    ).fetchone()

    owner_user_id = str(discovery_owner_user_id or "").strip()
    brand_id = str(discovery_brand_id or "").strip()
    is_discovery = not bool(email)
    if email:
        if existing_lead and existing_lead[1] and _normalize_email(existing_lead[1]) != email:
            raise ValueError("This YouTube channel is already linked to a different lead email.")
        if existing_lead and existing_lead[2] and existing_lead[3]:
            owner_user_id = str(existing_lead[2])
            brand_id = str(existing_lead[3])
        else:
            owner = _provision_or_reuse_lead_owner(conn, email=email, channel_name=channel_name)
            owner_user_id = owner["user_id"]
            brand_id = owner["brand_id"]

    if not owner_user_id or not brand_id:
        raise ValueError("No discovery brand is available for this lead.")

    local_bucket_channel_id = _get_or_create_local_uploads_channel(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    channel_id = get_or_create_scoped_youtube_channel(
        conn,
        meta=meta,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    if channel_id is None:
        raise ValueError("The creator channel could not be prepared.")

    # A discovery lead may have received its contact email later. Move its first
    # already-downloaded source into the newly provisioned owner/brand instead of
    # leaving that customer work attached to the admin discovery bucket.
    if email and existing_lead and not existing_lead[2] and existing_lead[4]:
        conn.execute(
            """
            UPDATE youtube_videos
            SET channel_id = ?, local_bucket_channel_id = ?, owner_user_id = ?, brand_id = ?,
                creator_name = COALESCE(creator_name, ?), creator_email = ?
            WHERE id = ?
            """,
            [
                channel_id,
                local_bucket_channel_id,
                owner_user_id,
                brand_id,
                creator_name,
                email,
                existing_lead[4],
            ],
        )

    video_row = conn.execute(
        """
        SELECT id
        FROM youtube_videos
        WHERE video_id = ? AND owner_user_id = ? AND brand_id = ?
        LIMIT 1
        """,
        [video_id, owner_user_id, brand_id],
    ).fetchone()
    if video_row:
        video_pk = int(video_row[0])
        conn.execute(
            """
            UPDATE youtube_videos
            SET channel_id = ?, local_bucket_channel_id = ?, creator_name = COALESCE(creator_name, ?),
                creator_email = COALESCE(creator_email, ?)
            WHERE id = ? AND owner_user_id = ? AND brand_id = ?
            """,
            [channel_id, local_bucket_channel_id, creator_name, email or None, video_pk, owner_user_id, brand_id],
        )
        already_exists = True
    else:
        conn.execute(
            """
            INSERT INTO youtube_videos (
                channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                duration_seconds, view_count, like_count, comment_count, video_url,
                local_bucket_channel_id, owner_user_id, brand_id, download_status, subtitle_style,
                creator_name, creator_email
            )
            VALUES (?, ?, ?, ?, ?, FALSE, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'karaoke', ?, ?)
            """,
            [
                channel_id,
                video_id,
                meta.get("title") or canonical_url,
                meta.get("published_at"),
                meta.get("thumbnail_url"),
                meta.get("duration_seconds"),
                meta.get("view_count"),
                meta.get("like_count"),
                meta.get("comment_count"),
                canonical_url,
                local_bucket_channel_id,
                owner_user_id,
                brand_id,
                creator_name,
                email or None,
            ],
        )
        video_pk = int(
            conn.execute(
                """
                SELECT id FROM youtube_videos
                WHERE video_id = ? AND owner_user_id = ? AND brand_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                [video_id, owner_user_id, brand_id],
            ).fetchone()[0]
        )
        already_exists = False

    if existing_lead:
        lead_id = str(existing_lead[0])
        conn.execute(
            f"""
            UPDATE {AUTOPILOT_LEADS_TABLE}
            SET creator_email = COALESCE(NULLIF(creator_email, ''), ?),
                creator_name = COALESCE(NULLIF(creator_name, ''), ?),
                channel_id = ?, first_video_id = COALESCE(first_video_id, ?),
                user_id = CASE WHEN ? THEN user_id ELSE ? END,
                brand_id = CASE WHEN ? THEN brand_id ELSE ? END
            WHERE id = ?
            """,
            [email or None, creator_name, channel_id, video_pk, is_discovery, owner_user_id, is_discovery, brand_id, lead_id],
        )
    else:
        lead_id = str(uuid4())
        conn.execute(
            f"""
            INSERT INTO {AUTOPILOT_LEADS_TABLE} (
                id, creator_email, creator_name, youtube_channel_id, channel_id,
                first_video_id, user_id, brand_id, created_at, converted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, now(), NULL)
            """,
            [lead_id, email or None, creator_name, youtube_channel_id, channel_id, video_pk, None if is_discovery else owner_user_id, None if is_discovery else brand_id],
        )

    return {
        "lead_id": lead_id,
        "video_pk": video_pk,
        "video_id": video_id,
        "channel_id": channel_id,
        "brand_id": brand_id,
        "owner_user_id": owner_user_id,
        "creator_name": creator_name,
        "creator_email": email or None,
        "discovery_only": is_discovery,
        "already_exists": already_exists,
    }
