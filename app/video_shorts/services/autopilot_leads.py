"""Tenant-safe persistence for pre-conversion autopilot leads."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional
from uuid import uuid4

from app.video_shorts.config import (
    DEFAULT_SUB_FONT_KEY,
    DEFAULT_SUB_FONT_SIZE,
    DEFAULT_SUBTITLE_BG_ALPHA,
    DEFAULT_SUBTITLE_BG_COLOR,
    DEFAULT_SUBTITLE_TEXT_ALPHA,
    DEFAULT_SUBTITLE_TEXT_COLOR,
    DEFAULT_TITLE_FONT_SIZE,
    DEFAULT_TITLE_MARGIN,
    DEFAULT_USER_PLAN_ID,
    DEFAULT_VIDEO_OVERLAY_OFFSET,
    STYLE_TEMPLATES,
    SUB_MARGIN_DEFAULT,
)
from app.video_shorts.services.brands import create_brand, ensure_brand_schema
from app.video_shorts.services.db import (
    _schema_management_enabled,
    ensure_auth_user_schema,
    ensure_channel_owner_schema,
    ensure_storage_user_schema,
    ensure_user_preferences_schema,
    table_columns,
)

AUTOPILOT_LEADS_TABLE = "autopilot_leads"
LOCAL_UPLOADS_CHANNEL_NAME = "Local uploads"
SHORT_EDITOR_DEFAULTS_PREFERENCE_KEY = "short_editor_defaults"
BOLD_POP_STYLE_TEMPLATE_KEY = "opus"
BOLD_POP_SUBTITLE_PRESET = "opus"


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
            recipient_name VARCHAR,
            subscriber_count BIGINT,
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
    cols = table_columns(conn, AUTOPILOT_LEADS_TABLE)
    if "recipient_name" not in cols:
        try:
            conn.execute(f"ALTER TABLE {AUTOPILOT_LEADS_TABLE} ADD COLUMN recipient_name VARCHAR")
        except Exception:
            pass
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
        "recipient_name",
        "subscriber_count",
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


def _bold_pop_template() -> Dict[str, Any]:
    return next(
        (
            template
            for template in STYLE_TEMPLATES
            if str(template.get("key") or "").strip() == BOLD_POP_STYLE_TEMPLATE_KEY
        ),
        {},
    )


def _bold_pop_video_style_values() -> Dict[str, Any]:
    template = _bold_pop_template()
    return {
        "title_font_key": str(template.get("title_font_key") or "montserrat_black"),
        "title_font_size": int(template.get("title_font_size") or DEFAULT_TITLE_FONT_SIZE),
        "subtitle_font_key": DEFAULT_SUB_FONT_KEY,
        "subtitle_font_size": DEFAULT_SUB_FONT_SIZE,
        "subtitle_margin": SUB_MARGIN_DEFAULT,
        "subtitle_style": "karaoke",
        "subtitle_preset": BOLD_POP_SUBTITLE_PRESET,
        "title_margin": DEFAULT_TITLE_MARGIN,
        "title_line_spacing": -4,
        "title_bg_color": str(template.get("title_bg_color") or "#14532D"),
        "title_bg_alpha": int(template.get("title_bg_alpha") if template.get("title_bg_alpha") is not None else 0),
        "title_text_color": str(template.get("title_text_color") or "#FFFFFF"),
        "subtitle_text_color": DEFAULT_SUBTITLE_TEXT_COLOR,
        "subtitle_bg_color": DEFAULT_SUBTITLE_BG_COLOR,
        "subtitle_bg_alpha": DEFAULT_SUBTITLE_BG_ALPHA,
        "subtitle_text_alpha": DEFAULT_SUBTITLE_TEXT_ALPHA,
        "show_title": True,
        "show_subtitle": True,
        "subscribe_overlay_enabled": True,
        "visual_mode": "video",
        "video_overlay_offset": DEFAULT_VIDEO_OVERLAY_OFFSET,
    }


def _bold_pop_short_editor_defaults() -> Dict[str, Any]:
    style = _bold_pop_video_style_values()
    return {
        "font": style["title_font_key"],
        "sub_font": style["subtitle_font_key"],
        "title_font_size": style["title_font_size"],
        "sub_font_size": style["subtitle_font_size"],
        "sub_margin": style["subtitle_margin"],
        "subtitle_style": style["subtitle_style"],
        "subtitle_preset": style["subtitle_preset"],
        "title_margin": style["title_margin"],
        "title_line_spacing": style["title_line_spacing"],
        "title_bg_color": style["title_bg_color"],
        "title_bg_alpha": style["title_bg_alpha"],
        "title_text_color": style["title_text_color"],
        "subtitle_text_color": style["subtitle_text_color"],
        "subtitle_text_alpha": style["subtitle_text_alpha"],
        "subtitle_bg_color": style["subtitle_bg_color"],
        "subtitle_bg_alpha": style["subtitle_bg_alpha"],
        "video_overlay_offset": style["video_overlay_offset"],
        "enable_subscribe_overlay": style["subscribe_overlay_enabled"],
        "show_title": style["show_title"],
        "show_subtitle": style["show_subtitle"],
        "visual_mode": style["visual_mode"],
    }


def _stamp_bold_pop_video_style(conn, video_pk: Any) -> None:
    if video_pk in (None, ""):
        return
    video_columns = table_columns(conn, "youtube_videos")
    assignments = []
    params = []
    for column, value in _bold_pop_video_style_values().items():
        if column in video_columns:
            assignments.append(f"{column} = ?")
            params.append(value)
    if not assignments:
        return
    params.append(video_pk)
    conn.execute(
        f"""
        UPDATE youtube_videos
        SET {", ".join(assignments)}
        WHERE id = ?
        """,
        params,
    )


def _save_bold_pop_short_editor_defaults(conn, owner_user_id: str) -> None:
    clean_owner = str(owner_user_id or "").strip()
    if not clean_owner:
        return
    ensure_user_preferences_schema(conn)
    conn.execute(
        """
        DELETE FROM shorts_user_preferences
        WHERE user_id = ? AND preference_key = ?
        """,
        [clean_owner, SHORT_EDITOR_DEFAULTS_PREFERENCE_KEY],
    )
    conn.execute(
        """
        INSERT INTO shorts_user_preferences (id, user_id, preference_key, preference_value, updated_at)
        VALUES (?, ?, ?, ?, now())
        """,
        [
            str(uuid4()),
            clean_owner,
            SHORT_EDITOR_DEFAULTS_PREFERENCE_KEY,
            json.dumps(_bold_pop_short_editor_defaults(), ensure_ascii=False, sort_keys=True),
        ],
    )


def autopilot_leads_table_ready(conn) -> bool:
    try:
        _require_autopilot_leads_table(conn)
    except AutopilotLeadSchemaUnavailable:
        return False
    return True


def ensure_converted_autopilot_lead_for_activation(
    conn,
    *,
    user_id: str,
    user_email: str,
    user_name: str = "",
    source: str = "manual_autopilot_activation",
) -> Dict[str, str]:
    """Ensure direct Autopilot activations have a converted lead workspace anchor.

    Lead-funnel conversions already have an ``autopilot_leads`` row. Direct customer
    activations do not, but the admin customer workspace intentionally requires a
    converted lead record so operations stay tied to a revalidated user/brand pair.
    This creates that anchor without adding outreach/share attribution.
    """
    _require_autopilot_leads_table(conn)
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    ensure_brand_schema(conn)

    clean_user_id = str(user_id or "").strip()
    clean_email = _normalize_email(user_email)
    clean_name = str(user_name or "").strip()
    clean_source = str(source or "manual_autopilot_activation").strip() or "manual_autopilot_activation"
    if not clean_user_id:
        raise ValueError("User id is required.")

    brand_row = conn.execute(
        """
        SELECT CAST(id AS VARCHAR), name
        FROM shorts_brands
        WHERE CAST(owner_user_id AS VARCHAR) = ?
        ORDER BY COALESCE(is_default, FALSE) DESC, created_at ASC, id ASC
        LIMIT 1
        """,
        [clean_user_id],
    ).fetchone()
    if brand_row:
        brand_id = str(brand_row[0] or "").strip()
        brand_name = str(brand_row[1] or "").strip() or "Autopilot customer"
    else:
        brand_name = clean_name or clean_email or "Autopilot customer"
        brand = create_brand(conn, user_id=clean_user_id, name=f"{brand_name}'s Brand", make_default=True, commit=False)
        brand_id = str(brand["id"])
        brand_name = str(brand.get("name") or brand_name).strip() or "Autopilot customer"

    if not brand_id:
        raise ValueError("A customer brand is required.")

    existing = conn.execute(
        f"""
        SELECT id, converted_at
        FROM {AUTOPILOT_LEADS_TABLE}
        WHERE CAST(user_id AS VARCHAR) = ?
          AND CAST(brand_id AS VARCHAR) = ?
        ORDER BY converted_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        [clean_user_id, brand_id],
    ).fetchone()
    if existing:
        lead_id = str(existing[0] or "").strip()
        conn.execute(
            f"""
            UPDATE {AUTOPILOT_LEADS_TABLE}
            SET converted_at = COALESCE(converted_at, now()),
                creator_email = COALESCE(NULLIF(creator_email, ''), ?),
                creator_name = COALESCE(NULLIF(creator_name, ''), ?)
            WHERE id = ?
            """,
            [clean_email or None, clean_name or brand_name, lead_id],
        )
        created = False
    else:
        lead_id = str(uuid4())
        manual_channel_id = f"manual_activation:{clean_user_id}:{brand_id}"
        lead_columns = table_columns(conn, AUTOPILOT_LEADS_TABLE)
        columns = [
            "id",
            "creator_email",
            "creator_name",
            "subscriber_count",
            "youtube_channel_id",
            "channel_id",
            "first_video_id",
            "user_id",
            "brand_id",
            "created_at",
            "converted_at",
        ]
        values = [
            lead_id,
            clean_email or None,
            clean_name or brand_name,
            None,
            manual_channel_id,
            None,
            None,
            clean_user_id,
            brand_id,
        ]
        placeholders = ["?", "?", "?", "?", "?", "?", "?", "?", "?", "now()", "now()"]
        if "recipient_name" in lead_columns:
            columns.insert(3, "recipient_name")
            values.insert(3, clean_name or None)
            placeholders.insert(3, "?")
        if "pipeline_state" in lead_columns:
            columns.append("pipeline_state")
            values.append("new")
            placeholders.append("?")
        conn.execute(
            f"""
            INSERT INTO {AUTOPILOT_LEADS_TABLE} ({", ".join(columns)})
            VALUES ({", ".join(placeholders)})
            """,
            values,
        )
        created = True

    _record_manual_activation_event(
        conn,
        lead_id=lead_id,
        source=clean_source,
        created=created,
    )
    return {
        "lead_id": lead_id,
        "owner_user_id": clean_user_id,
        "brand_id": brand_id,
        "created": "true" if created else "false",
    }


def _record_manual_activation_event(conn, *, lead_id: str, source: str, created: bool) -> None:
    event_columns = table_columns(conn, "lead_pipeline_events")
    if not {"lead_id", "event_type", "detail"}.issubset(event_columns):
        return
    detail = json.dumps(
        {
            "source": source,
            "created_converted_lead": bool(created),
            "attribution": "manual_activation_no_outreach_share",
        },
        sort_keys=True,
    )
    columns = ["lead_id", "event_type", "detail"]
    placeholders = ["?", "?", "CAST(? AS JSONB)" if getattr(conn, "backend_name", "") == "postgres" else "?"]
    values: list[Any] = [lead_id, "manual_autopilot_activation", detail]
    if "id" in event_columns:
        columns.insert(0, "id")
        placeholders.insert(0, "?")
        values.insert(0, str(uuid4()))
    if "to_state" in event_columns:
        columns.append("to_state")
        placeholders.append("?")
        values.append("new")
    conn.execute(
        f"""
        INSERT INTO lead_pipeline_events ({", ".join(columns)})
        VALUES ({", ".join(placeholders)})
        """,
        values,
    )


def _normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


_LEAD_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _source_video_ready_for_planning(conn, video_pk: Any) -> bool:
    try:
        parsed_video_pk = int(video_pk)
    except (TypeError, ValueError):
        return False
    row = conn.execute(
        """
        SELECT video_id, COALESCE(download_status, ''), COALESCE(transcript_status, '')
        FROM youtube_videos
        WHERE id = ?
        LIMIT 1
        """,
        [parsed_video_pk],
    ).fetchone()
    if not row:
        return False
    video_id = str(row[0] or "").strip()
    download_status = str(row[1] or "").strip().lower()
    transcript_status = str(row[2] or "").strip().lower()
    if download_status != "downloaded" or transcript_status != "done" or not video_id:
        return False
    transcript_row = conn.execute(
        """
        SELECT 1
        FROM youtube_transcripts
        WHERE video_id = ?
          AND (
              COALESCE(full_text, '') <> ''
              OR segments_json IS NOT NULL
              OR whisper_segments_json IS NOT NULL
          )
        LIMIT 1
        """,
        [video_id],
    ).fetchone()
    return bool(transcript_row)


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
    try:
        channel_cols = table_columns(conn, "youtube_channels")
    except Exception:
        channel_cols = set()
    has_channel_description = "channel_description" in channel_cols
    channel_description = str(meta.get("channel_description") or "").strip() or None
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
        if has_channel_description and channel_description:
            conn.execute(
                """
                UPDATE youtube_channels
                SET channel_description = ?
                WHERE channel_id = ?
                  AND NULLIF(COALESCE(channel_description, ''), '') IS NULL
                """,
                [channel_description, row[0]],
            )
        return int(row[0])
    next_channel_id = conn.execute(
        "SELECT COALESCE(MAX(channel_id), 0) + 1 FROM youtube_channels"
    ).fetchone()[0]
    channel_name = str(meta.get("channel_title") or "YouTube Channel").strip() or "YouTube Channel"
    columns = [
        "channel_id",
        "channel_name",
        "channel_url",
        "notes",
        "owner_user_id",
        "youtube_channel_id",
        "is_active",
        "brand_id",
    ]
    values = [
        next_channel_id,
        channel_name,
        f"https://www.youtube.com/channel/{youtube_channel_id}",
        notes,
        owner_user_id,
        youtube_channel_id,
        True,
        brand_id,
    ]
    if has_channel_description:
        columns.append("channel_description")
        values.append(channel_description)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"""
        INSERT INTO youtube_channels ({", ".join(columns)})
        VALUES ({placeholders})
        """,
        values,
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


def provision_discovery_lead_email(
    conn,
    *,
    lead_id: str,
    creator_email: str,
) -> Dict[str, str]:
    """Convert one discovery lead into its own placeholder owner and brand.

    The lead ID is the only client-supplied identity. All channel and source-video
    records are resolved from that row before the existing source is re-homed.
    """
    _require_autopilot_leads_table(conn)
    ensure_storage_user_schema(conn)
    ensure_auth_user_schema(conn)
    ensure_brand_schema(conn)
    ensure_channel_owner_schema(conn)

    lead_id = str(lead_id or "").strip()
    email = _normalize_email(creator_email)
    if not lead_id:
        raise ValueError("Lead not found.")
    if not _LEAD_EMAIL_RE.fullmatch(email):
        raise ValueError("Enter a valid email address.")

    lead = conn.execute(
        f"""
        SELECT
            l.creator_name,
            l.creator_email,
            l.youtube_channel_id,
            l.channel_id,
            l.first_video_id,
            l.user_id,
            l.brand_id,
            l.converted_at,
            c.channel_name,
            v.id
        FROM {AUTOPILOT_LEADS_TABLE} l
        LEFT JOIN youtube_channels c ON c.channel_id = l.channel_id
        LEFT JOIN youtube_videos v ON v.id = l.first_video_id
        WHERE l.id = ?
        LIMIT 1
        """,
        [lead_id],
    ).fetchone()
    if not lead:
        raise ValueError("Lead not found.")

    existing_email = _normalize_email(lead[1])
    existing_owner_id = str(lead[5] or "").strip()
    existing_brand_id = str(lead[6] or "").strip()
    if lead[7]:
        raise ValueError("Converted customers cannot be changed from the Leads page.")
    if existing_owner_id or existing_brand_id:
        if existing_email == email and existing_owner_id and existing_brand_id:
            return {
                "lead_id": lead_id,
                "owner_user_id": existing_owner_id,
                "brand_id": existing_brand_id,
                "creator_email": email,
            }
        raise ValueError("This lead is already provisioned with a different email.")

    youtube_channel_id = str(lead[2] or "").strip()
    source_video_id = lead[9]
    channel_name = str(lead[8] or lead[0] or "YouTube Channel").strip() or "YouTube Channel"
    creator_name = str(lead[0] or channel_name).strip() or channel_name
    if not youtube_channel_id or source_video_id is None:
        raise ValueError("This discovery lead is missing its source video or YouTube channel.")

    # Do not attach a discovery lead to a real or separately provisioned account.
    existing_account = conn.execute(
        """
        SELECT 1
        FROM shorts_users
        WHERE lower(coalesce(email, '')) = ? OR lower(coalesce(username, '')) = ?
        LIMIT 1
        """,
        [email, email],
    ).fetchone()
    if existing_account:
        raise ValueError("An account already exists for this email. No changes were made.")

    owner = _provision_or_reuse_lead_owner(conn, email=email, channel_name=channel_name)
    owner_user_id = owner["user_id"]
    brand_id = owner["brand_id"]
    _save_bold_pop_short_editor_defaults(conn, owner_user_id)
    local_bucket_channel_id = _get_or_create_local_uploads_channel(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    channel_id = get_or_create_scoped_youtube_channel(
        conn,
        meta={"channel_id": youtube_channel_id, "channel_title": channel_name},
        owner_user_id=owner_user_id,
        brand_id=brand_id,
    )
    if channel_id is None:
        raise ValueError("The creator channel could not be prepared.")

    # Move the existing source row so transcript, plan, and generated-short links
    # remain attached to the same video primary key.
    conn.execute(
        """
        UPDATE youtube_videos
        SET channel_id = ?, local_bucket_channel_id = ?, owner_user_id = ?, brand_id = ?,
            creator_name = COALESCE(NULLIF(creator_name, ''), ?), creator_email = ?
        WHERE id = ?
        """,
        [
            channel_id,
            local_bucket_channel_id,
            owner_user_id,
            brand_id,
            creator_name,
            email,
            source_video_id,
        ],
    )
    _stamp_bold_pop_video_style(conn, source_video_id)
    lead_columns = table_columns(conn, AUTOPILOT_LEADS_TABLE)
    pipeline_state = "downloaded" if _source_video_ready_for_planning(conn, source_video_id) else "new"
    pipeline_assignment = ", pipeline_state = ?" if "pipeline_state" in lead_columns else ""
    conn.execute(
        f"""
        UPDATE {AUTOPILOT_LEADS_TABLE}
        SET creator_email = ?, user_id = ?, brand_id = ?, channel_id = ?{pipeline_assignment}
        WHERE id = ?
        """,
        [email, owner_user_id, brand_id, channel_id, *([pipeline_state] if pipeline_assignment else []), lead_id],
    )
    return {
        "lead_id": lead_id,
        "owner_user_id": owner_user_id,
        "brand_id": brand_id,
        "creator_email": email,
        "pipeline_state": pipeline_state,
    }


def create_autopilot_lead_from_video(
    conn,
    *,
    meta: Dict[str, Any],
    video_id: str,
    canonical_url: str,
    creator_name: str | None,
    creator_email: str | None,
    subscriber_count: int | None,
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
    try:
        subscriber_count = int(subscriber_count) if subscriber_count is not None else None
    except (TypeError, ValueError):
        subscriber_count = None
    if subscriber_count is not None and subscriber_count < 0:
        subscriber_count = None
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
        _save_bold_pop_short_editor_defaults(conn, owner_user_id)

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
        _stamp_bold_pop_video_style(conn, existing_lead[4])

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
        if email:
            _stamp_bold_pop_video_style(conn, video_pk)
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
        if email:
            _stamp_bold_pop_video_style(conn, video_pk)

    if existing_lead:
        lead_id = str(existing_lead[0])
        conn.execute(
            f"""
            UPDATE {AUTOPILOT_LEADS_TABLE}
            SET creator_email = COALESCE(NULLIF(creator_email, ''), ?),
                creator_name = COALESCE(NULLIF(creator_name, ''), ?),
                subscriber_count = COALESCE(?, subscriber_count),
                channel_id = ?, first_video_id = COALESCE(first_video_id, ?),
                user_id = CASE WHEN ? THEN user_id ELSE ? END,
                brand_id = CASE WHEN ? THEN brand_id ELSE ? END
            WHERE id = ?
            """,
            [email or None, creator_name, subscriber_count, channel_id, video_pk, is_discovery, owner_user_id, is_discovery, brand_id, lead_id],
        )
    else:
        lead_id = str(uuid4())
        conn.execute(
            f"""
            INSERT INTO {AUTOPILOT_LEADS_TABLE} (
                id, creator_email, creator_name, subscriber_count, youtube_channel_id, channel_id,
                first_video_id, user_id, brand_id, created_at, converted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now(), NULL)
            """,
            [lead_id, email or None, creator_name, subscriber_count, youtube_channel_id, channel_id, video_pk, None if is_discovery else owner_user_id, None if is_discovery else brand_id],
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
        "subscriber_count": subscriber_count,
        "discovery_only": is_discovery,
        "already_exists": already_exists,
    }
