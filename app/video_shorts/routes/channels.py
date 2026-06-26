import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import flash, g, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.brands import current_brand_id, ensure_brand_schema
from app.video_shorts.services.db import (
    _ensure_video_crop_schema,
    get_db,
    get_db_readonly,
    ensure_channel_owner_schema,
)
from app.video_shorts.services.generated_video_lifecycle import ensure_generated_videos_schema

DEFAULT_TIME_ZONE = "America/Los_Angeles"
MUSIC_CHANNEL_NAME = "Music channel"
PODCAST_CHANNEL_NAME = "Podcast channel"
SOURCES_OWNER_EMAIL = "gokhansaltik@gmail.com"


def _pseudo_channel_id(kind: str, user_id: str, brand_id: str | None) -> int:
    seed = f"{kind}:{user_id}:{brand_id or 'default'}"
    # Deterministic bigint-safe synthetic id for local pseudo channels.
    value = int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:15], 16)
    return 8_000_000_000_000_000 + (value % 999_999_999_999_999)


def _next_channel_id(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(channel_id), 0) + 1 FROM youtube_channels").fetchone()
    try:
        return int(row[0]) if row else 1
    except Exception:
        return 1


def _format_channel_timestamp(value, tz_name: str) -> str:
    if not value:
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIME_ZONE)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local_dt = value.astimezone(tz)
    return local_dt.strftime("%Y-%m-%d %H:%M")


def _is_sources_owner(current_user) -> bool:
    email = str((current_user or {}).get("email") or "").strip().lower()
    return email == SOURCES_OWNER_EMAIL


def _format_duration_label(seconds) -> str:
    try:
        total = int(round(float(seconds or 0)))
    except Exception:
        total = 0
    if total <= 0:
        return "—"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_video_timestamp(value, tz_name: str) -> str:
    if not value:
        return "Unknown date"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIME_ZONE)
    dt_value = value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            dt_value = datetime.fromisoformat(normalized)
        except Exception:
            return value[:16]
    if getattr(dt_value, "tzinfo", None) is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(tz).strftime("%b %-d, %Y")


def _video_status_payload(download_status: str | None, transcript_status: str | None, short_count: int) -> dict:
    download_value = str(download_status or "").strip().lower()
    transcript_value = str(transcript_status or "").strip().lower()
    transcript_ready = transcript_value in {"done", "ready", "completed", "ok"}
    transcript_pending = transcript_value == "pending"
    download_pending_without_transcript = download_value == "pending" and not transcript_ready and not transcript_value
    if short_count > 0:
        label = "Has shorts"
        tone = "success"
        filter_key = "has_shorts"
    elif transcript_ready:
        label = "Ready"
        tone = "ready"
        filter_key = "ready"
    elif transcript_pending or download_pending_without_transcript:
        label = "Transcribing"
        tone = "info"
        filter_key = "transcribing"
    else:
        label = "No shorts"
        tone = "muted"
        filter_key = "no_shorts"
    return {
        "label": label,
        "tone": tone,
        "filter_key": filter_key,
        "is_transcribing": transcript_pending or download_pending_without_transcript,
        "transcript_ready": transcript_ready,
    }


@video_shorts_bp.route("/my-videos", methods=["GET"])
def my_videos_page():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))

    brand_id = current_brand_id()
    user_tz = current_user.get("time_zone") or DEFAULT_TIME_ZONE
    search_query = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "all").strip().lower()
    if status_filter not in {"all", "ready", "transcribing", "has_shorts", "no_shorts"}:
        status_filter = "all"

    schema_conn = get_db()
    ensure_brand_schema(schema_conn)
    ensure_channel_owner_schema(schema_conn)
    _ensure_video_crop_schema(schema_conn)
    ensure_generated_videos_schema(schema_conn)
    schema_conn.close()

    conn = get_db_readonly()
    params = [current_user["id"]]
    where_clauses = ["v.owner_user_id = ?"]
    if brand_id:
        where_clauses.append("v.brand_id = ?")
        params.append(brand_id)
    else:
        where_clauses.append("v.brand_id IS NULL")
    if search_query:
        where_clauses.append("lower(coalesce(v.title, '')) LIKE ?")
        params.append(f"%{search_query.lower()}%")
    where_sql = " AND ".join(where_clauses)
    rows = conn.execute(
        f"""
        SELECT
            v.id,
            v.video_id,
            v.title,
            v.thumbnail_url,
            v.duration_seconds,
            v.download_status,
            v.transcript_status,
            COALESCE(v.downloaded_at, v.published_at) AS added_at,
            c.channel_name,
            COALESCE(g.short_count, 0) AS short_count
        FROM youtube_videos v
        LEFT JOIN youtube_channels c ON c.channel_id = v.channel_id
        LEFT JOIN (
            SELECT source_video_id, COUNT(*) AS short_count
            FROM shorts_generated_videos
            GROUP BY source_video_id
        ) g ON CAST(g.source_video_id AS VARCHAR) = CAST(v.video_id AS VARCHAR)
        WHERE {where_sql}
        ORDER BY COALESCE(v.downloaded_at, v.published_at) DESC NULLS LAST, v.id DESC
        """,
        params,
    ).fetchall()
    conn.close()

    videos = []
    for row in rows:
        item = {
            "id": row[0],
            "video_id": row[1],
            "title": row[2] or "Untitled video",
            "thumbnail_url": row[3] or "",
            "duration_label": _format_duration_label(row[4]),
            "download_status": row[5] or "",
            "transcript_status": row[6] or "",
            "added_at_label": _format_video_timestamp(row[7], user_tz),
            "channel_name": row[8] or "",
            "short_count": int(row[9] or 0),
        }
        status = _video_status_payload(item["download_status"], item["transcript_status"], item["short_count"])
        item.update(status)
        item["thumb_fallback"] = (item["title"][:1] or "V").upper()
        if status_filter != "all":
            if status_filter == "no_shorts":
                if item["short_count"] != 0:
                    continue
            elif status_filter == "has_shorts":
                if item["short_count"] <= 0:
                    continue
            elif item["filter_key"] != status_filter:
                continue
        videos.append(item)

    return render_template(
        "my_videos.html",
        videos=videos,
        video_count=len(videos),
        search_query=search_query,
        status_filter=status_filter,
    )


@video_shorts_bp.route("/channels", methods=["GET", "POST"])
def channels_page():
    current_user = getattr(g, "vs_current_user", None)
    if current_user and not _is_sources_owner(current_user):
        return redirect(url_for("video_shorts_bp.my_videos_page"))
    brand_id = current_brand_id()
    user_tz = (current_user or {}).get("time_zone") or DEFAULT_TIME_ZONE
    schema_conn = get_db()
    ensure_brand_schema(schema_conn)
    ensure_channel_owner_schema(schema_conn)
    _ensure_video_crop_schema(schema_conn)
    if current_user:
        music_channel_row = schema_conn.execute(
            """
            SELECT channel_id
            FROM youtube_channels
            WHERE owner_user_id = ?
              AND channel_name = ?
              AND brand_id = ?
            """,
            [current_user["id"], MUSIC_CHANNEL_NAME, brand_id],
        ).fetchone()
        if not music_channel_row:
            has_music_only = schema_conn.execute(
                """
                SELECT 1
                FROM youtube_videos
                WHERE owner_user_id = ?
                  AND COALESCE(is_music_only, false) = true
                LIMIT 1
                """,
                [current_user["id"]],
            ).fetchone()
            if has_music_only:
                schema_conn.execute(
                    """
                    INSERT INTO youtube_channels (channel_id, channel_name, channel_url, notes, owner_user_id, is_active, brand_id)
                    VALUES (?, ?, ?, ?, ?, true, ?)
                    """,
                    [
                        _pseudo_channel_id("music", current_user["id"], brand_id),
                        MUSIC_CHANNEL_NAME,
                        "local://music-uploads",
                        "Music-only local uploads",
                        current_user["id"],
                        brand_id,
                    ],
                )
                music_channel_row = schema_conn.execute(
                    """
                    SELECT channel_id
                    FROM youtube_channels
                    WHERE owner_user_id = ?
                      AND channel_name = ?
                      AND brand_id = ?
                    """,
                    [current_user["id"], MUSIC_CHANNEL_NAME, brand_id],
                ).fetchone()
        if music_channel_row:
            schema_conn.execute(
                """
                UPDATE youtube_videos
                SET channel_id = ?
                WHERE owner_user_id = ?
                  AND COALESCE(is_music_only, false) = true
                  AND (channel_id IS NULL OR CAST(channel_id AS VARCHAR) <> CAST(? AS VARCHAR))
                """,
                [music_channel_row[0], current_user["id"], music_channel_row[0]],
            )
        podcast_channel_row = schema_conn.execute(
            """
            SELECT channel_id
            FROM youtube_channels
            WHERE owner_user_id = ?
              AND channel_name = ?
              AND brand_id = ?
            """,
            [current_user["id"], PODCAST_CHANNEL_NAME, brand_id],
        ).fetchone()
        if not podcast_channel_row:
            has_podcast = schema_conn.execute(
                """
                SELECT 1
                FROM youtube_videos
                WHERE owner_user_id = ?
                  AND COALESCE(podcast_audio_filename, '') <> ''
                LIMIT 1
                """,
                [current_user["id"]],
            ).fetchone()
            if has_podcast:
                schema_conn.execute(
                    """
                    INSERT INTO youtube_channels (channel_id, channel_name, channel_url, notes, owner_user_id, is_active, brand_id)
                    VALUES (?, ?, ?, ?, ?, true, ?)
                    """,
                    [
                        _pseudo_channel_id("podcast", current_user["id"], brand_id),
                        PODCAST_CHANNEL_NAME,
                        "local://podcast-uploads",
                        "Podcast local uploads",
                        current_user["id"],
                        brand_id,
                    ],
                )
                podcast_channel_row = schema_conn.execute(
                    """
                    SELECT channel_id
                    FROM youtube_channels
                    WHERE owner_user_id = ?
                      AND channel_name = ?
                      AND brand_id = ?
                    """,
                    [current_user["id"], PODCAST_CHANNEL_NAME, brand_id],
                ).fetchone()
        if podcast_channel_row:
            schema_conn.execute(
                """
                UPDATE youtube_videos
                SET channel_id = ?
                WHERE owner_user_id = ?
                  AND COALESCE(podcast_audio_filename, '') <> ''
                  AND (channel_id IS NULL OR CAST(channel_id AS VARCHAR) <> CAST(? AS VARCHAR))
                """,
                [podcast_channel_row[0], current_user["id"], podcast_channel_row[0]],
            )
    schema_conn.close()

    # ----- POST: add channel -----
    if request.method == "POST":
        channel_name = (request.form.get("channel_name") or "").strip()
        channel_url = (request.form.get("channel_url") or "").strip()
        notes = (request.form.get("notes") or "").strip() or None

        if not channel_name or not channel_url:
            flash("Channel name and URL are required.", "danger")
            return redirect(url_for("video_shorts_bp.channels_page"))

        conn = get_db()
        owner_id = current_user["id"] if current_user else None
        existing = conn.execute(
            """
            SELECT channel_id
            FROM youtube_channels
            WHERE owner_user_id IS NOT DISTINCT FROM ?
              AND brand_id IS NOT DISTINCT FROM ?
              AND (
                lower(channel_url) = lower(?)
                OR lower(channel_name) = lower(?)
              )
            LIMIT 1
            """,
            [owner_id, brand_id, channel_url, channel_name],
        ).fetchone()
        if existing:
            conn.close()
            flash("This channel already exists.", "warning")
            return redirect(url_for("video_shorts_bp.channels_page"))

        next_channel_id = _next_channel_id(conn)
        conn.execute(
            """
            INSERT INTO youtube_channels (channel_id, channel_name, channel_url, notes, owner_user_id, brand_id, is_active)
            VALUES (?, ?, ?, ?, ?, ?, true)
            """,
            [next_channel_id, channel_name, channel_url, notes, owner_id, brand_id],
        )
        conn.commit()
        conn.close()

        flash("Channel added successfully!", "success")
        return redirect(url_for("video_shorts_bp.channels_page"))

    # ----- GET: list channels with progress -----
    conn = get_db_readonly()

    params = []
    where_clause = ""
    if current_user:
        where_clause = "WHERE c.owner_user_id = ?"
        params.append(current_user["id"])
        if brand_id:
            where_clause += " AND c.brand_id = ?"
            params.append(brand_id)
        else:
            where_clause += " AND c.brand_id IS NULL"
    rows = conn.execute(
        f"""
        SELECT
            c.channel_id,
            c.channel_name,
            c.channel_url,
            c.added_at,
            c.youtube_channel_id,
            c.uploads_playlist_id,
            c.total_videos,
            c.is_active,
            c.next_page_token,
            c.owner_user_id,
            COALESCE(v.pulled_videos, 0) AS pulled_videos,
            COALESCE(p.pending_videos, 0) AS pending_videos,
            COALESCE(p.downloaded_videos, 0) AS downloaded_videos
        FROM youtube_channels c
        LEFT JOIN (
            SELECT channel_id, COUNT(*) AS pulled_videos
            FROM youtube_videos
            GROUP BY channel_id
        ) v
          ON v.channel_id = c.channel_id
        LEFT JOIN (
            SELECT channel_id,
                   COUNT(*) FILTER (WHERE lower(coalesce(download_status,'')) = 'pending') AS pending_videos,
                   COUNT(*) FILTER (WHERE lower(coalesce(download_status,'')) = 'downloaded') AS downloaded_videos
            FROM youtube_videos
            GROUP BY channel_id
        ) p
          ON p.channel_id = c.channel_id
        {where_clause}
        ORDER BY c.channel_id DESC
        """
        ,
        params,
    ).fetchall()

    cols = [d[0] for d in conn.description]
    channels = [dict(zip(cols, r)) for r in rows]
    conn.close()

    # progress hesapla
    for ch in channels:
        if ch.get("is_pseudo"):
            continue
        pulled = ch.get("pulled_videos") or 0
        total = ch.get("total_videos")
        if total and total > 0:
            completion_pct = int(pulled * 100 / total)
            downloaded = min(ch.get("downloaded_videos") or 0, total)
            download_pct = int(downloaded * 100 / total)
            ch["completion_pct"] = completion_pct
            ch["download_pct"] = min(completion_pct, download_pct)
        else:
            ch["completion_pct"] = None
            ch["download_pct"] = 0
        ch["added_at_display"] = _format_channel_timestamp(ch.get("added_at"), user_tz)

    real_source_count = sum(
        1 for ch in channels if not str(ch.get("channel_url") or "").startswith("local://")
    )

    return render_template(
        "channels.html",
        channels=channels,
        real_source_count=real_source_count,
    )


@video_shorts_bp.route("/channels/<int:channel_id>/set_active", methods=["POST"])
def set_channel_active(channel_id):
    current_user = getattr(g, "vs_current_user", None)
    if not _is_sources_owner(current_user):
        return redirect(url_for("video_shorts_bp.my_videos_page"))
    brand_id = current_brand_id()

    is_active_str = request.form.get("is_active", "0")
    is_active = 1 if is_active_str == "1" else 0

    conn = get_db()
    ensure_channel_owner_schema(conn)
    owner_row = conn.execute(
        """
        SELECT owner_user_id
        FROM youtube_channels
        WHERE channel_id = ?
          AND owner_user_id = ?
          AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
        """,
        [channel_id, current_user["id"] if current_user else None, brand_id, brand_id],
    ).fetchone()
    owner_id = owner_row[0] if owner_row else None
    if not current_user or owner_id != current_user["id"]:
        conn.close()
        flash("You do not have permission to update this channel.", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    conn.execute(
        """
        UPDATE youtube_channels
        SET is_active = ?
        WHERE channel_id = ?
          AND owner_user_id = ?
          AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
        """,
        [is_active, channel_id, current_user["id"], brand_id, brand_id],
    )
    conn.close()

    if is_active:
        flash("Channel marked as active.", "success")
    else:
        flash("Channel marked as inactive.", "secondary")

    return redirect(url_for("video_shorts_bp.channels_page"))


@video_shorts_bp.route("/channels/delete/<int:channel_id>", methods=["POST"])
def delete_channel(channel_id):
    current_user = getattr(g, "vs_current_user", None)
    if not _is_sources_owner(current_user):
        return redirect(url_for("video_shorts_bp.my_videos_page"))
    conn = get_db()
    ensure_channel_owner_schema(conn)
    brand_id = current_brand_id()
    owner_row = conn.execute(
        """
        SELECT owner_user_id
        FROM youtube_channels
        WHERE channel_id = ?
          AND owner_user_id = ?
          AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
        """,
        [channel_id, current_user["id"] if current_user else None, brand_id, brand_id],
    ).fetchone()
    owner_id = owner_row[0] if owner_row else None
    if not current_user or owner_id != current_user["id"]:
        conn.close()
        flash("You do not have permission to delete this channel.", "danger")
        return redirect(url_for("video_shorts_bp.channels_page"))
    conn.execute(
        """
        DELETE FROM youtube_channels
        WHERE channel_id = ?
          AND owner_user_id = ?
          AND ((? IS NULL AND brand_id IS NULL) OR brand_id = ?)
        """,
        [channel_id, current_user["id"], brand_id, brand_id],
    )
    conn.close()

    flash("Channel deleted.", "warning")
    return redirect(url_for("video_shorts_bp.channels_page"))
