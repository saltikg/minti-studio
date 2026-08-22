import json
import re
from datetime import datetime, timedelta, timezone

from flask import current_app, g, jsonify, request

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import CAPTION_API_TOKEN
from app.video_shorts.services.auth_protection import RateLimitRule, check_rate_limits
from app.video_shorts.services.brands import current_brand_id, ensure_brand_schema
from app.video_shorts.services.db import (
    _ensure_transcript_schema,
    _ensure_video_crop_schema,
    ensure_channel_owner_schema,
    ensure_postgres_youtube_transcripts_id_default,
    ensure_youtube_video_local_bucket_schema,
    get_db,
    get_db_readonly,
)
from app.video_shorts.services.error_capture import CLIENT_ERROR_MAX_BODY_BYTES, capture_client_error, current_event_user_id
from app.video_shorts.services.render_jobs import get_job
from app.video_shorts.services.transcript_service import _normalize_segments_for_use
from app.video_shorts.services.user_events import prepare_transcript_completed_transition, track_event
from app.video_shorts.services.usage_metering import add_transcription_minutes, get_usage_snapshot
from app.video_shorts.youtube_api import (
    YoutubeApiError,
    extract_channel_id,
    extract_video_id,
    fetch_channel_subscriber_counts,
    fetch_playlist_items_batch,
    fetch_video_metadata,
    fetch_video_stats,
    get_channel_metadata,
)
from app.video_shorts.routes.videos import _get_or_create_real_youtube_channel, _load_admin_global_outreach_match


CLIENT_ERROR_RATE_LIMITS = [
    RateLimitRule(limit=10, window_seconds=60),
    RateLimitRule(limit=30, window_seconds=3600),
]
LONGFORM_WINDOW_DAYS = 60
SHORTS_WINDOW_DAYS = 15
UPLOAD_SAMPLE_SIZE = 50
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
CONTACT_LINE_RE = re.compile(r"(iletisim|iletişim|contact|business)", re.IGNORECASE)


def _duration_minutes(duration_seconds) -> float:
    try:
        seconds = float(duration_seconds or 0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0.0
    return round(seconds / 60.0, 2)


def _check_caption_token(req):
    token = req.headers.get("X-Api-Token")
    return bool(token and token == CAPTION_API_TOKEN)


def _parse_yt_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_channel_context(raw_url: str):
    candidate = str(raw_url or "").strip()
    if not candidate:
        return None, None, ("missing_url", "Missing url.")

    video_id = extract_video_id(candidate)
    if video_id:
        meta = fetch_video_metadata(video_id)
        channel_id = str(meta.get("channel_id") or "").strip()
        if not channel_id:
            return None, None, ("channel_not_found", "Channel could not be resolved from video metadata.")
        return channel_id, meta, None

    channel_id = extract_channel_id(candidate)
    if not channel_id:
        return None, None, ("invalid_url", "Unsupported or invalid YouTube URL.")
    return str(channel_id).strip(), None, None


def _collect_recent_uploads(uploads_playlist_id: str, *, limit: int = UPLOAD_SAMPLE_SIZE):
    videos = []
    page_token = None
    while len(videos) < limit:
        batch = fetch_playlist_items_batch(
            playlist_id=uploads_playlist_id,
            page_token=page_token,
            max_results=min(50, limit),
        )
        batch_videos = batch.get("videos") or []
        if not batch_videos:
            break
        videos.extend(batch_videos)
        page_token = batch.get("next_page_token")
        if not page_token:
            break
    return videos[:limit]


def _subscriber_gate(subscriber_count):
    try:
        count = int(subscriber_count)
    except (TypeError, ValueError):
        return "hedef_disi"
    if count < 100_000:
        return "ideal"
    if count <= 300_000:
        return "uygun"
    return "hedef_disi"


def _youtube_env_error_message(exc: Exception) -> str | None:
    message = str(exc or "").strip()
    lowered = message.lower()
    if "youtub" in lowered and ("api key" in lowered or "oauth" in lowered):
        return "YouTube API credentials are not configured for this environment."
    return None


def _resolve_brand_local_uploads_channel(conn, brand_id: str):
    row = conn.execute(
        """
        SELECT b.owner_user_id, c.channel_id, c.owner_user_id, c.brand_id
        FROM shorts_brands b
        LEFT JOIN youtube_channels c
          ON c.brand_id = b.id
         AND c.owner_user_id = b.owner_user_id
         AND lower(COALESCE(c.channel_url, '')) = 'local://uploads'
         AND COALESCE(c.is_active, true) = true
        WHERE b.id = ?
        LIMIT 1
        """,
        [brand_id],
    ).fetchone()
    if not row:
        return None
    owner_user_id = str(row[0] or "").strip() or None
    local_channel_id = row[1]
    local_owner_user_id = str(row[2] or "").strip() or None
    local_brand_id = str(row[3] or "").strip() or None
    if not owner_user_id or local_channel_id is None:
        return None
    if local_owner_user_id != owner_user_id or local_brand_id != brand_id:
        return None
    return {
        "owner_user_id": owner_user_id,
        "channel_id": local_channel_id,
        "brand_id": brand_id,
    }


def _video_already_in_bucket(conn, video_id: str, local_bucket_channel_id) -> bool:
    if not video_id or local_bucket_channel_id is None:
        return False
    row = conn.execute(
        """
        SELECT id
        FROM youtube_videos
        WHERE video_id = ?
          AND (channel_id = ? OR local_bucket_channel_id = ?)
        LIMIT 1
        """,
        [video_id, local_bucket_channel_id, local_bucket_channel_id],
    ).fetchone()
    return bool(row)


def _count_videos_for_creator_channel(conn, channel_id: str) -> int:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM youtube_videos v
        JOIN youtube_channels c
          ON c.channel_id = v.channel_id
        WHERE COALESCE(c.youtube_channel_id, '') = ?
        """,
        [normalized_channel_id],
    ).fetchone()
    try:
        return int((row or [0])[0] or 0)
    except Exception:
        return 0


def _extract_creator_email(description: str | None) -> str | None:
    match = EMAIL_RE.search(str(description or ""))
    if not match:
        return None
    return match.group(0).strip() or None


def _looks_like_urlish_name(value: str) -> bool:
    candidate = str(value or "").strip().lower()
    if not candidate:
        return False
    blocked_fragments = ("http", "www.", ".com", ".net", ".org", "/", "@")
    if any(fragment in candidate for fragment in blocked_fragments):
        return True
    return "." in candidate and "/" in candidate


def _extract_creator_name(description: str | None, channel_title: str | None) -> str | None:
    for raw_line in str(description or "").splitlines():
        line = raw_line.strip()
        if not line or not CONTACT_LINE_RE.search(line):
            continue
        if ":" not in line:
            continue
        candidate = line.split(":", 1)[1].strip()
        candidate = EMAIL_RE.sub("", candidate).strip(" -|,;/")
        if _looks_like_urlish_name(candidate):
            continue
        parts = [part for part in re.split(r"\s+", candidate) if part]
        if 1 <= len(parts) <= 4 and all(any(ch.isalpha() for ch in part) for part in parts):
            return " ".join(parts)[:255]
    fallback = str(channel_title or "").strip()
    return fallback[:255] if fallback else None


_CREATOR_FIELD_UNSET = object()


def _normalize_manual_creator_name(value, channel_title: str | None) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    if _looks_like_urlish_name(candidate):
        fallback = str(channel_title or "").strip()
        return fallback[:255] if fallback else None
    return candidate[:255]


def _normalize_manual_creator_email(value) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    if "@" not in candidate or "." not in candidate:
        return None
    return candidate[:255]


def _update_existing_creator_fields(conn, row_id, *, creator_name=_CREATOR_FIELD_UNSET, creator_email=_CREATOR_FIELD_UNSET) -> bool:
    assignments = []
    params = []
    if creator_name is not _CREATOR_FIELD_UNSET:
        assignments.append("creator_name = ?")
        params.append(creator_name)
    if creator_email is not _CREATOR_FIELD_UNSET:
        assignments.append("creator_email = ?")
        params.append(creator_email)
    if not assignments:
        return False
    conn.execute(
        f"""
        UPDATE youtube_videos
        SET {", ".join(assignments)}
        WHERE id = ?
        """,
        params + [row_id],
    )
    return True


def _json_error(error: str, message: str, status: int):
    return jsonify({"error": error, "message": message}), status


def _coerce_video_pk(value):
    try:
        return int(value)
    except Exception:
        return value


@video_shorts_bp.route("/api/admin/youtube-channel-diagnose", methods=["POST"])
def admin_youtube_channel_diagnose():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized", "message": "Admin session required."}), 401
    if (current_user.get("role") or "").strip().lower() != "admin":
        return jsonify({"error": "forbidden", "message": "Admin access required."}), 403

    payload = request.get_json(silent=True) or {}
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return jsonify({"error": "bad_request", "message": "Request body must include a YouTube url."}), 400

    try:
        resolved_channel_id, video_meta, resolve_error = _resolve_channel_context(raw_url)
        if resolve_error:
            code, message = resolve_error
            status = 400 if code in {"missing_url", "invalid_url"} else 404
            return jsonify({"error": code, "message": message}), status

        channel_lookup_url = f"https://www.youtube.com/channel/{resolved_channel_id}"
        channel_meta = get_channel_metadata(channel_lookup_url)
        subscriber_map = fetch_channel_subscriber_counts([resolved_channel_id])
        subscriber_info = subscriber_map.get(resolved_channel_id) or {}
        recent_uploads = _collect_recent_uploads(channel_meta["uploads_playlist_id"], limit=UPLOAD_SAMPLE_SIZE)
        stats_map = fetch_video_stats([item.get("video_id") for item in recent_uploads if item.get("video_id")])

        now_utc = datetime.now(timezone.utc)
        longform_last_60d = 0
        shorts_last_15d = 0
        latest_short_dt = None
        for item in recent_uploads:
            video_id = str(item.get("video_id") or "").strip()
            published_at = _parse_yt_timestamp(item.get("published_at"))
            duration_seconds = (stats_map.get(video_id) or {}).get("duration_seconds")
            try:
                is_short = int(duration_seconds or 0) <= 60
            except (TypeError, ValueError):
                is_short = False
            if is_short:
                if published_at and (latest_short_dt is None or published_at > latest_short_dt):
                    latest_short_dt = published_at
                if published_at and published_at >= now_utc - timedelta(days=SHORTS_WINDOW_DAYS):
                    shorts_last_15d += 1
                continue
            if published_at and published_at >= now_utc - timedelta(days=LONGFORM_WINDOW_DAYS):
                longform_last_60d += 1

        subscriber_count = subscriber_info.get("subscriber_count")
        gate_subscriber = _subscriber_gate(subscriber_count)
        gate_cadence = "aktif" if longform_last_60d >= 2 else "aktif_degil"
        creator_name = None
        creator_email = None
        if video_meta:
            creator_name = _extract_creator_name(video_meta.get("description"), video_meta.get("channel_title"))
            creator_email = _extract_creator_email(video_meta.get("description"))
        outreach_detail = None
        already_added = False
        channel_video_count = 0
        try:
            conn = get_db_readonly()
        except RuntimeError as exc:
            message = str(exc or "").strip()
            return jsonify({"error": "server_config", "message": message or "Database is not configured."}), 500
        try:
            outreach_detail = _load_admin_global_outreach_match(conn, resolved_channel_id)
            channel_video_count = _count_videos_for_creator_channel(conn, resolved_channel_id)
            candidate_video_id = str((video_meta or {}).get("video_id") or "").strip()
            active_brand_id = str(current_brand_id() or "").strip() or None
            if candidate_video_id and active_brand_id:
                local_channel = _resolve_brand_local_uploads_channel(conn, active_brand_id)
                if local_channel:
                    already_added = _video_already_in_bucket(
                        conn,
                        candidate_video_id,
                        local_channel.get("channel_id"),
                    )
        finally:
            conn.close()

        channel_title = (
            subscriber_info.get("channel_title")
            or (video_meta or {}).get("channel_title")
            or None
        )
        return jsonify(
            {
                "channel_id": resolved_channel_id,
                "channel_title": channel_title,
                "subscriber_count": subscriber_count,
                "creator_name": creator_name,
                "creator_email": creator_email,
                "eligible": gate_subscriber != "hedef_disi" and gate_cadence == "aktif",
                "gate_subscriber": gate_subscriber,
                "gate_cadence": gate_cadence,
                "longform_last_60d": longform_last_60d,
                "shorts_last_15d": shorts_last_15d,
                "shorts_color": "yellow" if shorts_last_15d >= 7 else "green",
                "shorts_window": "last_15_days",
                "latest_short_date": latest_short_dt.isoformat().replace("+00:00", "Z") if latest_short_dt else None,
                "already_added": already_added,
                "already_reached": bool(outreach_detail),
                "channel_already_in_minti": channel_video_count > 0,
                "channel_video_count": channel_video_count,
                "outreach_detail": outreach_detail,
            }
        )
    except YoutubeApiError as exc:
        env_message = _youtube_env_error_message(exc)
        if env_message:
            return jsonify({"error": "server_config", "message": env_message}), 500
        message = str(exc or "").strip() or "YouTube API request failed."
        status = 404 if "not found" in message.lower() else 502
        return jsonify({"error": "youtube_api_error", "message": message}), status
    except RuntimeError as exc:
        message = str(exc or "").strip()
        if "database" in message.lower():
            return jsonify({"error": "server_config", "message": message}), 500
        current_app.logger.exception("Unexpected runtime error in admin YouTube diagnose endpoint")
        return jsonify({"error": "server_error", "message": message or "Unexpected server error."}), 500
    except Exception:
        current_app.logger.exception("Unexpected error in admin YouTube diagnose endpoint")
        return jsonify({"error": "server_error", "message": "Unexpected server error."}), 500


@video_shorts_bp.route("/api/admin/add-youtube-video", methods=["POST"])
def admin_add_youtube_video():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return _json_error("unauthorized", "Admin session required.", 401)
    if (current_user.get("role") or "").strip().lower() != "admin":
        return _json_error("forbidden", "Admin access required.", 403)

    payload = request.get_json(silent=True) or {}
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _json_error("invalid_url", "Request body must include a YouTube video url.", 400)

    brand_id = str(current_brand_id() or "").strip() or None
    if not brand_id:
        return _json_error("no_active_brand", "No active brand is selected for this session.", 400)

    video_id = extract_video_id(raw_url)
    if not video_id or len(video_id) != 11:
        return _json_error("invalid_url", "Unsupported or invalid YouTube video URL.", 400)

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        meta = fetch_video_metadata(video_id)
    except YoutubeApiError as exc:
        env_message = _youtube_env_error_message(exc)
        if env_message:
            return _json_error("server_config", env_message, 500)
        message = str(exc or "").strip() or "YouTube API request failed."
        return _json_error("youtube_api_error", message, 502)

    resolved_channel_key = str(meta.get("channel_id") or "").strip()
    if not resolved_channel_key:
        return _json_error("channel_not_found", "Channel could not be resolved from video metadata.", 404)

    manual_creator_name_present = bool(str(payload.get("creator_name") or "").strip())
    manual_creator_email_present = bool(str(payload.get("creator_email") or "").strip())
    fallback_creator_name = _extract_creator_name(meta.get("description"), meta.get("channel_title"))
    fallback_creator_email = _extract_creator_email(meta.get("description"))
    creator_name = (
        _normalize_manual_creator_name(payload.get("creator_name"), meta.get("channel_title"))
        if manual_creator_name_present
        else fallback_creator_name
    )
    creator_email = (
        _normalize_manual_creator_email(payload.get("creator_email"))
        if manual_creator_email_present
        else fallback_creator_email
    )

    conn = None
    try:
        conn = get_db()
        ensure_brand_schema(conn)
        ensure_channel_owner_schema(conn)
        ensure_youtube_video_local_bucket_schema(conn)
        _ensure_video_crop_schema(conn)

        local_channel = _resolve_brand_local_uploads_channel(conn, brand_id)
        if not local_channel:
            return _json_error("no_local_uploads_channel", "No active Local uploads channel exists for the active brand.", 400)

        local_bucket_channel_id = local_channel["channel_id"]
        owner_user_id = local_channel["owner_user_id"]

        if _video_already_in_bucket(conn, video_id, local_bucket_channel_id):
            existing_local = conn.execute(
                """
                SELECT id
                FROM youtube_videos
                WHERE video_id = ?
                  AND (channel_id = ? OR local_bucket_channel_id = ?)
                LIMIT 1
                """,
                [video_id, local_bucket_channel_id, local_bucket_channel_id],
            ).fetchone()
            row = conn.execute(
                """
                SELECT id, channel_id, brand_id, title, creator_name, creator_email
                FROM youtube_videos
                WHERE id = ?
                LIMIT 1
                """,
                [existing_local[0]],
            ).fetchone()
            updated = False
            resolved_creator_name = row[4] if row else None
            resolved_creator_email = row[5] if row else None
            if row:
                updated = _update_existing_creator_fields(
                    conn,
                    row[0],
                    creator_name=creator_name if manual_creator_name_present else _CREATOR_FIELD_UNSET,
                    creator_email=creator_email if manual_creator_email_present else _CREATOR_FIELD_UNSET,
                )
                if updated:
                    conn.commit()
                    resolved_creator_name = creator_name if manual_creator_name_present else resolved_creator_name
                    resolved_creator_email = creator_email if manual_creator_email_present else resolved_creator_email
            return jsonify(
                {
                    "ok": True,
                    "video_id": _coerce_video_pk(row[0] if row else existing_local[0]),
                    "youtube_video_id": video_id,
                    "channel_id": str((row[1] if row else meta.get("channel_id")) or "").strip() or None,
                    "brand_id": str((row[2] if row else brand_id) or "").strip() or brand_id,
                    "creator_name": resolved_creator_name,
                    "creator_email": resolved_creator_email,
                    "title": (row[3] if row else meta.get("title")),
                    "already_exists": True,
                    "updated": updated,
                }
            )

        try:
            resolved_channel_id = _get_or_create_real_youtube_channel(
                conn,
                meta,
                owner_user_id,
                brand_id,
            )
        except Exception:
            current_app.logger.exception(
                "Failed to resolve or create real YouTube channel for admin add endpoint video_id=%s",
                video_id,
            )
            return _json_error("server_error", "Failed to resolve the creator channel.", 500)
        if resolved_channel_id is None:
            return _json_error("channel_not_found", "Creator channel could not be resolved.", 404)

        existing = conn.execute(
            """
            SELECT id, owner_user_id, brand_id, local_bucket_channel_id, channel_id, title, creator_name, creator_email
            FROM youtube_videos
            WHERE video_id = ?
            LIMIT 1
            """,
            [video_id],
        ).fetchone()
        if existing:
            existing_owner_user_id = str(existing[1] or "").strip() or None
            existing_brand_id = str(existing[2] or "").strip() or None
            existing_local_bucket_channel_id = existing[3]
            same_scope = existing_owner_user_id == owner_user_id and existing_brand_id == brand_id
            if same_scope and existing_local_bucket_channel_id != local_bucket_channel_id:
                conn.execute(
                    """
                    UPDATE youtube_videos
                    SET local_bucket_channel_id = ?,
                        creator_name = COALESCE(creator_name, ?),
                        creator_email = COALESCE(creator_email, ?)
                    WHERE id = ?
                    """,
                    [local_bucket_channel_id, creator_name, creator_email, existing[0]],
                )
                updated = _update_existing_creator_fields(
                    conn,
                    existing[0],
                    creator_name=creator_name if manual_creator_name_present else _CREATOR_FIELD_UNSET,
                    creator_email=creator_email if manual_creator_email_present else _CREATOR_FIELD_UNSET,
                )
                conn.commit()
                return jsonify(
                    {
                        "ok": True,
                        "video_id": _coerce_video_pk(existing[0]),
                        "youtube_video_id": video_id,
                        "channel_id": str(existing[4] or "").strip() or resolved_channel_id,
                        "brand_id": brand_id,
                        "creator_name": creator_name if manual_creator_name_present else (existing[6] or creator_name),
                        "creator_email": creator_email if manual_creator_email_present else (existing[7] or creator_email),
                        "title": existing[5] or meta.get("title"),
                        "already_exists": True,
                        "updated": updated,
                    }
                )
            updated = _update_existing_creator_fields(
                conn,
                existing[0],
                creator_name=creator_name if manual_creator_name_present else _CREATOR_FIELD_UNSET,
                creator_email=creator_email if manual_creator_email_present else _CREATOR_FIELD_UNSET,
            )
            if updated:
                conn.commit()
            return jsonify(
                {
                    "ok": True,
                    "video_id": _coerce_video_pk(existing[0]),
                    "youtube_video_id": video_id,
                    "channel_id": str(existing[4] or "").strip() or resolved_channel_id,
                    "brand_id": existing_brand_id or brand_id,
                    "creator_name": creator_name if manual_creator_name_present else (existing[6] or creator_name),
                    "creator_email": creator_email if manual_creator_email_present else (existing[7] or creator_email),
                    "title": existing[5] or meta.get("title"),
                    "already_exists": True,
                    "updated": updated,
                }
            )

        conn.execute(
            """
            INSERT INTO youtube_videos
                (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                 duration_seconds, view_count, like_count, comment_count, video_url, local_bucket_channel_id,
                 owner_user_id, brand_id, download_status, subtitle_style, creator_name, creator_email)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                resolved_channel_id,
                video_id,
                meta.get("title") or canonical_url,
                meta.get("published_at"),
                meta.get("thumbnail_url"),
                False,
                meta.get("duration_seconds"),
                meta.get("view_count"),
                meta.get("like_count"),
                meta.get("comment_count"),
                canonical_url,
                local_bucket_channel_id,
                owner_user_id,
                brand_id,
                "pending",
                "karaoke",
                creator_name,
                creator_email,
            ],
        )
        inserted = conn.execute(
            """
            SELECT id
            FROM youtube_videos
            WHERE video_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            [video_id],
        ).fetchone()
        conn.commit()
        return jsonify(
            {
                "ok": True,
                "video_id": _coerce_video_pk(inserted[0] if inserted else None),
                "youtube_video_id": video_id,
                "channel_id": resolved_channel_id,
                "brand_id": brand_id,
                "creator_name": creator_name,
                "creator_email": creator_email,
                "title": meta.get("title") or canonical_url,
                "already_exists": False,
                "updated": False,
            }
        )
    except RuntimeError as exc:
        message = str(exc or "").strip()
        return _json_error("server_config", message or "Database is not configured.", 500)
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        current_app.logger.exception("Unexpected error in admin add YouTube video endpoint")
        return _json_error("server_error", "Unexpected server error.", 500)
    finally:
        if conn is not None:
            conn.close()


@video_shorts_bp.route("/api/caption-tasks", methods=["GET"])
def caption_tasks():
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    try:
        limit = int(request.args.get("limit", "20"))
    except Exception:
        limit = 20

    conn = get_db_readonly()
    rows = conn.execute(
        """
        SELECT id, video_id, title AS video_title, video_url
        FROM youtube_videos
        WHERE fetch_transcript = TRUE
          AND lower(transcript_status) = 'pending'
        ORDER BY published_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    tasks = [dict(zip(cols, r)) for r in rows]
    conn.close()
    return jsonify({"tasks": tasks})


@video_shorts_bp.route("/api/usage", methods=["GET"])
def usage_api():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(get_usage_snapshot(current_user["id"]))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@video_shorts_bp.route("/api/jobs/<job_id>", methods=["GET"])
def render_job_status_api(job_id: str):
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401
    try:
        job = get_job(job_id, user_id=str(current_user["id"]))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if not job:
        return jsonify({"error": "not_found"}), 404
    error_text = str(job.get("error") or "").strip()
    error_code = None
    lowered_error = error_text.lower()
    if "export limit reached" in lowered_error or "monthly export limit reached" in lowered_error:
        error_code = "export_limit_reached"
    return jsonify(
        {
            "id": job["id"],
            "status": job["status"],
            "priority": job["priority"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "result": job.get("result"),
            "error": job.get("error"),
            "error_code": error_code,
            "queue_position": job.get("queue_position"),
        }
    )


@video_shorts_bp.route("/api/client-error", methods=["POST"])
def client_error_api():
    content_length = int(request.content_length or 0)
    if content_length > CLIENT_ERROR_MAX_BODY_BYTES:
        return ("", 204)

    key_parts = [
        current_event_user_id(),
        request.headers.get("X-Forwarded-For", ""),
        request.remote_addr or "",
    ]
    allowed, _retry_after = check_rate_limits("client-error", key_parts, CLIENT_ERROR_RATE_LIMITS)
    if not allowed:
        return ("", 204)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ("", 204)

    error_type = str(payload.get("error_type") or "").strip().lower()
    if not error_type:
        return ("", 204)

    capture_client_error(
        error_type=error_type,
        message=payload.get("message"),
        source=payload.get("source") or payload.get("page"),
        user_agent=request.headers.get("User-Agent"),
    )
    return ("", 204)


@video_shorts_bp.route("/api/caption-result", methods=["POST"])
def caption_result():
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    caption_text = (data.get("caption_text") or "").strip()
    lang = (data.get("lang") or "en").strip()
    segments = data.get("segments")

    if not video_db_id or not caption_text:
        return jsonify({"error": "missing fields"}), 400

    segments_json = None
    whisper_segments_json = None
    if isinstance(segments, list):
        try:
            normalized = _normalize_segments_for_use(segments)
            whisper_segments_json = json.dumps(normalized, ensure_ascii=False)
            segments_json = whisper_segments_json
        except Exception:
            segments_json = None
            whisper_segments_json = None

    conn = get_db()
    _ensure_video_crop_schema(conn)
    _ensure_transcript_schema(conn)
    ensure_postgres_youtube_transcripts_id_default(conn)
    try:
        row = conn.execute(
            "SELECT video_id, owner_user_id, duration_seconds, title FROM youtube_videos WHERE id = ?",
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404
        video_id = row[0]
        owner_user_id = row[1]
        duration_seconds = row[2]
        video_title = row[3]
        event_video_id, should_emit_transcript_completed = prepare_transcript_completed_transition(
            conn,
            video_pk=video_db_id,
        )

        conn.execute(
            """
            INSERT INTO youtube_transcripts (video_id, full_text, segments_json, whisper_segments_json)
            VALUES (?, ?, ?, ?)
            """,
            [video_id, caption_text, segments_json, whisper_segments_json],
        )

        conn.execute(
            """
            UPDATE youtube_videos
            SET transcript_status = 'done',
                fetch_transcript = FALSE,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [video_db_id],
        )
        conn.commit()
        conn.close()
        if should_emit_transcript_completed and owner_user_id:
            track_event(
                str(owner_user_id),
                "transcript_completed",
                video_id=event_video_id or video_id,
                status="completed",
            )
        if owner_user_id:
            minutes = _duration_minutes(duration_seconds)
            if minutes > 0:
                try:
                    add_transcription_minutes(
                        str(owner_user_id),
                        minutes,
                        video_id=video_id,
                        video_title=video_title,
                    )
                except Exception:
                    current_app.logger.exception(
                        "Failed to meter caption worker transcription usage for video_db_id=%s",
                        video_db_id,
                    )
        return jsonify({"ok": True}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/caption-status", methods=["POST"])
def caption_status():
    """
    Worker can report non-success states (e.g., no transcript available or an error)
    so the same video does not keep re-appearing in the queue.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    status = (data.get("status") or "").strip().lower()
    if not video_db_id or not status:
        return jsonify({"error": "missing fields"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM youtube_videos WHERE id = ?",
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404

        conn.execute(
            """
            UPDATE youtube_videos
            SET transcript_status = ?,
                fetch_transcript = FALSE,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [status, video_db_id],
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/download-status", methods=["POST"])
def download_status():
    """
    Allow a local downloader to mark the video as downloaded (or failed) on the central DB.
    This mirrors how transcript workers report status so local/remote stay in sync.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    video_db_id = data.get("video_db_id")
    status = (data.get("status") or "").strip().lower()
    if not video_db_id or not status:
        return jsonify({"error": "missing fields"}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM youtube_videos WHERE id = ?",
            [video_db_id],
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "video not found"}), 404

        conn.execute(
            """
            UPDATE youtube_videos
            SET download_status = ?,
                downloaded_at = CASE WHEN ? = 'downloaded' THEN CURRENT_TIMESTAMP ELSE NULL END,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [status, status, video_db_id],
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True}), 200
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"error": str(e)}), 500


@video_shorts_bp.route("/api/download-tasks", methods=["GET"])
def download_tasks():
    """
    Provide a queue for downloader workers based on download_status='pending'.
    """
    if not _check_caption_token(request):
        return jsonify({"error": "forbidden"}), 403

    try:
        limit = int(request.args.get("limit", "20"))
    except Exception:
        limit = 20

    conn = get_db_readonly()
    rows = conn.execute(
        """
        SELECT
          yv.id,
          yv.channel_id,
          ch.channel_name,
          yv.video_id,
          yv.title AS video_title,
          yv.video_url,
          yv.download_status
        FROM youtube_videos yv
        LEFT JOIN youtube_channels ch ON ch.channel_id = yv.channel_id
        WHERE lower(coalesce(yv.download_status,'')) = 'pending'
        ORDER BY yv.published_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    tasks = [dict(zip(cols, r)) for r in rows]
    conn.close()
    return jsonify({"tasks": tasks})
