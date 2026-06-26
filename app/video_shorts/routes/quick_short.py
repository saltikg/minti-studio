import shutil
import subprocess
import tempfile
import uuid
from hashlib import sha256
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import current_app, flash, g, jsonify, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import VIDEOS_DIR
from app.video_shorts.services.brands import current_brand_id, ensure_brand_schema
from app.video_shorts.services.db import (
    _ensure_video_crop_schema,
    get_db,
    get_db_readonly,
    ensure_channel_owner_schema,
    ensure_storage_user_schema,
)
from app.video_shorts.services.media_utils import _format_time_label
from app.video_shorts.services.quick_short_flow import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INGESTING,
    STATUS_INPUT,
    STATUS_PUBLISHING,
    STATUS_RENDERING,
    STATUS_REVIEW,
    create_session,
    ensure_quick_short_schema,
    get_latest_session,
    get_session,
    update_session,
)
from app.video_shorts.services.render_jobs import (
    JOB_TYPE_INGEST_YOUTUBE,
    JOB_TYPE_PUBLISH_SHORT,
    JOB_TYPE_TRANSCRIBE_UPLOAD,
    enqueue_job,
    get_job,
    update_job_payload,
)
from app.video_shorts.services.storage import get_media_storage
from app.video_shorts.services.youtube_oauth import has_refresh_token
from app.video_shorts.routes import generation
from app.video_shorts.youtube_api import extract_video_id, fetch_video_metadata, YoutubeApiError
from src.trends.instagram_tokens import InstagramTokenStoreError, get_instagram_credentials

MAX_QUICK_SHORT_SECONDS = 25 * 60
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
ALLOWED_UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
LOCAL_CHANNEL_NAME = "Local uploads"
MUSIC_CHANNEL_NAME = "Music channel"
PODCAST_CHANNEL_NAME = "Podcast channel"


def _normalize_timestamp(value):
    if not value:
        return None
    return value


def _get_or_create_channel(conn, meta, owner_id, brand_id):
    channel_key = meta.get("channel_id")
    if not channel_key:
        return None
    row = conn.execute(
        "SELECT channel_id FROM youtube_channels WHERE youtube_channel_id = ? AND brand_id = ?",
        [channel_key, brand_id],
    ).fetchone()
    if row:
        return row[0]

    channel_name = meta.get("channel_title") or "YouTube Channel"
    channel_url = f"https://www.youtube.com/channel/{channel_key}"
    notes = "Quick Short import"
    next_channel_id = _next_channel_id(conn)
    conn.execute(
        """
        INSERT INTO youtube_channels (channel_id, channel_name, channel_url, notes, owner_user_id, youtube_channel_id, is_active, brand_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [next_channel_id, channel_name, channel_url, notes, owner_id, channel_key, True, brand_id],
    )
    row = conn.execute(
        "SELECT channel_id FROM youtube_channels WHERE youtube_channel_id = ? AND brand_id = ?",
        [channel_key, brand_id],
    ).fetchone()
    return row[0] if row else None


def _get_or_create_local_channel(conn, owner_id, brand_id):
    row = conn.execute(
        """
        SELECT channel_id
        FROM youtube_channels
        WHERE owner_user_id = ?
          AND channel_name = ?
          AND brand_id = ?
        """,
        [owner_id, LOCAL_CHANNEL_NAME, brand_id],
    ).fetchone()
    if row:
        return row[0]
    next_channel_id = _next_channel_id(conn)
    conn.execute(
        """
        INSERT INTO youtube_channels (channel_id, channel_name, channel_url, notes, owner_user_id, is_active, brand_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [next_channel_id, LOCAL_CHANNEL_NAME, "local://uploads", "Local uploads", owner_id, True, brand_id],
    )
    row = conn.execute(
        """
        SELECT channel_id
        FROM youtube_channels
        WHERE owner_user_id = ?
          AND channel_name = ?
          AND brand_id = ?
        """,
        [owner_id, LOCAL_CHANNEL_NAME, brand_id],
    ).fetchone()
    return row[0] if row else None


def _get_or_create_music_channel(conn, owner_id, brand_id):
    row = conn.execute(
        """
        SELECT channel_id
        FROM youtube_channels
        WHERE owner_user_id = ?
          AND channel_name = ?
          AND brand_id = ?
        """,
        [owner_id, MUSIC_CHANNEL_NAME, brand_id],
    ).fetchone()
    if row:
        return row[0]
    next_channel_id = _next_channel_id(conn)
    conn.execute(
        """
        INSERT INTO youtube_channels (channel_id, channel_name, channel_url, notes, owner_user_id, is_active, brand_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [next_channel_id, MUSIC_CHANNEL_NAME, "local://music-uploads", "Music-only local uploads", owner_id, True, brand_id],
    )
    row = conn.execute(
        """
        SELECT channel_id
        FROM youtube_channels
        WHERE owner_user_id = ?
          AND channel_name = ?
          AND brand_id = ?
        """,
        [owner_id, MUSIC_CHANNEL_NAME, brand_id],
    ).fetchone()
    return row[0] if row else None


def _get_or_create_podcast_channel(conn, owner_id, brand_id):
    row = conn.execute(
        """
        SELECT channel_id
        FROM youtube_channels
        WHERE owner_user_id = ?
          AND channel_name = ?
          AND brand_id = ?
        """,
        [owner_id, PODCAST_CHANNEL_NAME, brand_id],
    ).fetchone()
    if row:
        return row[0]
    next_channel_id = _next_channel_id(conn)
    conn.execute(
        """
        INSERT INTO youtube_channels (channel_id, channel_name, channel_url, notes, owner_user_id, is_active, brand_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [next_channel_id, PODCAST_CHANNEL_NAME, "local://podcast-uploads", "Podcast local uploads", owner_id, True, brand_id],
    )
    row = conn.execute(
        """
        SELECT channel_id
        FROM youtube_channels
        WHERE owner_user_id = ?
          AND channel_name = ?
          AND brand_id = ?
        """,
        [owner_id, PODCAST_CHANNEL_NAME, brand_id],
    ).fetchone()
    return row[0] if row else None


def _resolve_ffprobe() -> Optional[str]:
    candidates = ["ffprobe", "/usr/bin/ffprobe"]
    for cand in candidates:
        resolved = shutil.which(cand) or cand
        if Path(resolved).is_file():
            return str(Path(resolved))
    return None


def _probe_duration_seconds(path: Path) -> Optional[int]:
    ffprobe = _resolve_ffprobe()
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except Exception:
        return None
    raw = (result.stdout or "").strip()
    try:
        return int(float(raw))
    except Exception:
        return None


def _get_user_storage_usage(conn, user_id: str) -> Dict[str, int]:
    row = conn.execute(
        """
        SELECT
            u.custom_limit_bytes,
            p.quota_bytes,
            COALESCE(SUM(a.size_bytes), 0) AS used_bytes
        FROM shorts_users u
        LEFT JOIN shorts_storage_plans p ON p.plan_id = u.plan_id
        LEFT JOIN shorts_storage_assets a
          ON a.user_id = u.id AND (a.status = 'active' OR a.status IS NULL)
        WHERE u.id = ?
        GROUP BY u.custom_limit_bytes, p.quota_bytes
        """,
        [user_id],
    ).fetchone()
    if not row:
        return {"used_bytes": 0, "limit_bytes": 0}
    limit_bytes = row[0] or row[1] or 0
    used_bytes = int(row[2] or 0)
    return {"used_bytes": used_bytes, "limit_bytes": limit_bytes}


def _ensure_postgres_youtube_videos_id_default(conn) -> None:
    if getattr(conn, "backend_name", "") != "postgres":
        return
    try:
        row = conn.execute(
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'youtube_videos'
              AND column_name = 'id'
            """
        ).fetchone()
        default_expr = str(row[0] or "") if row else ""
        if "nextval(" in default_expr:
            return
        conn.execute("CREATE SEQUENCE IF NOT EXISTS youtube_videos_id_seq")
        max_row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM youtube_videos").fetchone()
        max_id = int(max_row[0] or 0) if max_row else 0
        if max_id > 0:
            conn.execute("SELECT setval('youtube_videos_id_seq', ?, true)", [max_id])
        else:
            conn.execute("SELECT setval('youtube_videos_id_seq', 1, false)")
        conn.execute("ALTER TABLE youtube_videos ALTER COLUMN id SET DEFAULT nextval('youtube_videos_id_seq')")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _next_channel_id(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(channel_id), 0) + 1 FROM youtube_channels").fetchone()
    try:
        return int(row[0]) if row else 1
    except Exception:
        return 1


def _upsert_storage_asset(file_key: str, file_path: str, size_bytes: int, user_id: Optional[str]) -> None:
    if not file_key or not file_path:
        return
    conn = get_db()
    ensure_storage_user_schema(conn)
    try:
        conn.execute(
            """
            INSERT INTO shorts_storage_assets (file_key, file_path, file_type, size_bytes, user_id, status, updated_at)
            VALUES (?, ?, 'downloaded', ?, ?, 'active', now())
            ON CONFLICT(file_key)
            DO UPDATE SET
              file_path = excluded.file_path,
              file_type = excluded.file_type,
              size_bytes = excluded.size_bytes,
              user_id = COALESCE(shorts_storage_assets.user_id, excluded.user_id),
              status = COALESCE(shorts_storage_assets.status, 'active'),
              updated_at = now()
            """,
            [file_key, file_path, size_bytes, user_id],
        )
        conn.commit()
    finally:
        conn.close()


def _upsert_video(conn, meta, channel_id, owner_id, brand_id):
    video_id = meta.get("video_id")
    if not video_id:
        return None
    existing = conn.execute(
        "SELECT id, download_status FROM youtube_videos WHERE video_id = ?",
        [video_id],
    ).fetchone()
    if existing:
        video_pk, download_status = existing
        if (download_status or "").lower() != "downloaded":
            conn.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'pending',
                    channel_id = COALESCE(channel_id, ?),
                    owner_user_id = COALESCE(owner_user_id, ?),
                    brand_id = COALESCE(brand_id, ?)
                WHERE id = ?
                """,
                [channel_id, owner_id, brand_id, video_pk],
            )
        return video_pk

    conn.execute(
        """
        INSERT INTO youtube_videos
            (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
             duration_seconds, view_count, like_count, comment_count, video_url, owner_user_id, brand_id, download_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            channel_id,
            video_id,
            meta.get("title"),
            _normalize_timestamp(meta.get("published_at")),
            meta.get("thumbnail_url"),
            False,
            meta.get("duration_seconds"),
            meta.get("view_count"),
            meta.get("like_count"),
            meta.get("comment_count"),
            f"https://www.youtube.com/watch?v={video_id}",
            owner_id,
            brand_id,
            "pending",
        ],
    )
    row = conn.execute(
        "SELECT id FROM youtube_videos WHERE video_id = ?",
        [video_id],
    ).fetchone()
    return row[0] if row else None


def _require_quick_user():
    current_user = getattr(g, "vs_current_user", None)
    if not current_user:
        return None, redirect(url_for("video_shorts_bp.login", next=request.url))
    return current_user, None


def _ensure_quick_schema() -> None:
    schema_conn = get_db()
    ensure_brand_schema(schema_conn)
    ensure_channel_owner_schema(schema_conn)
    ensure_storage_user_schema(schema_conn)
    ensure_quick_short_schema(schema_conn)
    _ensure_postgres_youtube_videos_id_default(schema_conn)
    schema_conn.close()


def _source_video_public_url(video_id: str) -> str:
    if not video_id:
        return ""
    storage = get_media_storage()
    local_storage = get_media_storage("local")
    for suffix in (".mp4", ".mov", ".mkv", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
        key = f"videos/{video_id}{suffix}"
        local_candidate = VIDEOS_DIR / f"{video_id}{suffix}"
        if local_candidate.exists():
            return local_storage.public_url(key)
        if getattr(storage, "backend_name", "local") == "s3":
            try:
                if storage.exists(key):
                    return storage.public_url(key)
            except Exception:
                continue
    return ""


def _suggest_clip_from_segments(segments: list[dict], fallback_duration: Any) -> Dict[str, Any]:
    if segments:
        first = segments[0] or {}
        start = float(first.get("start") or 0.0)
        end = float(first.get("end") or 0.0)
        if end <= start:
            end = start + 20.0
        end = min(end, start + 30.0)
        title = str((first.get("tr_text") or first.get("text") or "First short").strip())[:80] or "First short"
        excerpt = str((first.get("tr_text") or first.get("text") or "").strip())
        return {
            "clip_start_seconds": round(start, 3),
            "clip_end_seconds": round(end, 3),
            "clip_title": title,
            "excerpt": excerpt,
        }
    duration = float(fallback_duration or 30.0)
    return {
        "clip_start_seconds": 0.0,
        "clip_end_seconds": round(min(duration, 30.0), 3),
        "clip_title": "First short",
        "excerpt": "",
    }


def _build_session_payload(session: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not session:
        return None
    payload = dict(session)
    payload["video"] = None
    payload["transcript"] = {"full_text": "", "segments": []}
    payload["job"] = None
    payload["render_job"] = None
    payload["publish_job"] = None
    payload["connections"] = {"youtube": False, "instagram": False}
    video_pk = session.get("video_pk")
    video_id = session.get("video_id")
    if video_pk or video_id:
        conn = get_db_readonly()
        try:
            row = conn.execute(
                """
                SELECT id, video_id, title, thumbnail_url, duration_seconds, download_status, transcript_status
                FROM youtube_videos
                WHERE id = ?
                """,
                [video_pk],
            ).fetchone() if video_pk else None
            if row:
                payload["video"] = {
                    "id": row[0],
                    "video_id": row[1],
                    "title": row[2],
                    "thumbnail_url": row[3],
                    "duration_seconds": row[4],
                    "duration_label": _format_time_label(row[4]) if row[4] is not None else None,
                    "download_status": row[5],
                    "transcript_status": row[6],
                    "source_url": _source_video_public_url(row[1]),
                }
            if video_id:
                transcript_text, segments = generation._fetch_transcript(conn, video_id)
                payload["transcript"] = {
                    "full_text": transcript_text or "",
                    "segments": [
                        {
                            "start": seg.get("start"),
                            "end": seg.get("end"),
                            "start_label": _format_time_label(seg.get("start")),
                            "end_label": _format_time_label(seg.get("end")) if seg.get("end") is not None else None,
                            "text": (seg.get("tr_text") or seg.get("text") or seg.get("ar_text") or "").strip(),
                        }
                        for seg in (segments or [])
                    ],
                }
                if not payload.get("clip_start_seconds") and payload["transcript"]["segments"]:
                    payload.update(_suggest_clip_from_segments(segments, payload["video"]["duration_seconds"] if payload["video"] else None))
        except Exception:
            current_app.logger.exception("Failed to build quick short session payload for session=%s", session.get("id"))
        finally:
            conn.close()
    ingest_job_id = str(session.get("ingest_job_id") or "").strip()
    if ingest_job_id:
        payload["job"] = get_job(ingest_job_id, user_id=session.get("user_id"))
    render_job_id = str(session.get("render_job_id") or "").strip()
    if render_job_id:
        payload["render_job"] = get_job(render_job_id, user_id=session.get("user_id"))
    publish_job_id = str(session.get("publish_job_id") or "").strip()
    if publish_job_id:
        payload["publish_job"] = get_job(publish_job_id, user_id=session.get("user_id"))
    try:
        brand_id = session.get("brand_id")
        user_id = str(session.get("user_id") or "").strip()
        payload["connections"]["youtube"] = bool(user_id and has_refresh_token(user_id, brand_id=brand_id))
        if user_id:
            try:
                payload["connections"]["instagram"] = bool(get_instagram_credentials(user_id))
            except InstagramTokenStoreError:
                payload["connections"]["instagram"] = False
    except Exception:
        current_app.logger.exception("Failed to resolve quick short connections for session=%s", session.get("id"))
    if payload.get("video_id") and (payload.get("status") == STATUS_DONE or (payload.get("render_job") or {}).get("status") == "done"):
        try:
            plan_entries = generation._load_plan_entries(payload["video_id"]) or []
            if plan_entries:
                entry = plan_entries[0]
                clip_filename = str(entry.get("output_filename") or entry.get("clip_filename") or "").strip()
                if clip_filename:
                    payload["result"] = {
                        **(payload.get("result") or {}),
                        "clip_filename": clip_filename,
                        "clip_url": generation._short_public_url(clip_filename),
                        "publish_status": entry.get("publish_status"),
                        "title": entry.get("title"),
                        "yt_video_id": entry.get("yt_video_id"),
                    }
        except Exception:
            current_app.logger.exception("Failed to resolve quick short output for session=%s", session.get("id"))
    return payload


def _json_error(message: str, status: int = 400, **extra: Any):
    payload = {"ok": False, "message": message}
    payload.update(extra)
    return jsonify(payload), status


@video_shorts_bp.route("/shorts/quick", methods=["GET"])
def quick_short():
    current_user, redirect_response = _require_quick_user()
    if redirect_response:
        return redirect_response
    _ensure_quick_schema()
    session_id = (request.args.get("session") or "").strip()
    session = get_session(session_id, user_id=current_user["id"]) if session_id else None
    return render_template(
        "quick_short_wizard.html",
        initial_session=_build_session_payload(session),
        max_minutes=int(MAX_QUICK_SHORT_SECONDS / 60),
    )


@video_shorts_bp.route("/shorts/quick/api/session", methods=["GET"])
def quick_short_session_api():
    current_user, redirect_response = _require_quick_user()
    if redirect_response:
        return jsonify({"error": "unauthorized"}), 401
    _ensure_quick_schema()
    session_id = (request.args.get("session_id") or "").strip()
    session = get_session(session_id, user_id=current_user["id"]) if session_id else None
    return jsonify({"ok": True, "session": _build_session_payload(session)})


@video_shorts_bp.route("/shorts/quick/api/ingest-youtube", methods=["POST"])
def quick_short_ingest_youtube():
    current_user, redirect_response = _require_quick_user()
    if redirect_response:
        return jsonify({"error": "unauthorized"}), 401
    _ensure_quick_schema()
    brand_id = current_brand_id()
    data = request.get_json(silent=True) if request.is_json else request.form
    video_url = str((data.get("video_url") if data else "") or "").strip()
    video_id = extract_video_id(video_url)
    if not video_id:
        return _json_error("Enter a valid YouTube video URL.")
    try:
        meta = fetch_video_metadata(video_id)
    except YoutubeApiError as exc:
        return _json_error(str(exc))
    except Exception:
        return _json_error("We couldn't read that YouTube video right now.")
    duration = meta.get("duration_seconds")
    if duration is not None and duration > MAX_QUICK_SHORT_SECONDS:
        return _json_error("The YouTube video must be 25 minutes or shorter.")
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        channel_id = _get_or_create_channel(conn, meta, current_user.get("id"), brand_id)
        if not channel_id:
            return _json_error("Channel details could not be prepared.")
        video_pk = _upsert_video(conn, meta, channel_id, current_user.get("id"), brand_id)
        conn.commit()
    finally:
        conn.close()
    if not video_pk:
        return _json_error("Video record could not be created.")
    session = create_session(
        user_id=current_user["id"],
        brand_id=brand_id,
        source_type="youtube",
        source_url=video_url,
        payload={"meta": meta},
    )
    job_input_hash = sha256(f"quick-youtube:{current_user['id']}:{brand_id}:{video_id}".encode("utf-8")).hexdigest()
    enqueue_result = enqueue_job(
        user_id=current_user["id"],
        job_type=JOB_TYPE_INGEST_YOUTUBE,
        payload={
            "quick_session_id": session["id"],
            "video_pk": int(video_pk),
            "video_id": video_id,
            "video_url": video_url,
            "duration_seconds": duration,
        },
        input_hash=job_input_hash,
    )
    job = enqueue_result.get("job") or {}
    session = update_session(
        session["id"],
        status=STATUS_INGESTING,
        video_pk=int(video_pk),
        video_id=video_id,
        ingest_job_id=job.get("id"),
        result={"stage": "queued", "message": "Queued for ingest."},
    )
    return jsonify({"ok": True, "session": _build_session_payload(session), "job_id": job.get("id"), "cached": enqueue_result.get("kind") == "cached"})


@video_shorts_bp.route("/shorts/quick/api/upload/presign", methods=["POST"])
def quick_short_upload_presign():
    current_user, redirect_response = _require_quick_user()
    if redirect_response:
        return jsonify({"error": "unauthorized"}), 401
    _ensure_quick_schema()
    brand_id = current_brand_id()
    data = request.get_json(silent=True) or {}
    filename = Path((data.get("filename") or "").strip()).name
    upload_kind = str(data.get("upload_kind") or "video").strip().lower()
    size_bytes = int(data.get("size_bytes") or 0)
    content_type = (data.get("content_type") or "").strip() or "application/octet-stream"
    if upload_kind not in {"video", "music", "podcast"}:
        upload_kind = "video"
    if not filename:
        return _json_error("Choose a file first.")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        return _json_error("Unsupported file format.")
    if size_bytes <= 0 or size_bytes > MAX_UPLOAD_BYTES:
        return _json_error("The file must be 500MB or smaller.")
    conn_ro = get_db_readonly()
    try:
        usage = _get_user_storage_usage(conn_ro, current_user["id"])
    finally:
        conn_ro.close()
    limit_bytes = usage.get("limit_bytes") or 0
    if limit_bytes and usage["used_bytes"] + size_bytes > limit_bytes:
        return _json_error("Storage limit is full. Free up space or upgrade first.", 403, code="storage_full")
    video_id = f"local_{uuid.uuid4().hex}"
    source_key = f"videos/{video_id}{ext}"
    storage = get_media_storage()
    if getattr(storage, "backend_name", "local") != "s3" or not getattr(storage, "client", None):
        return _json_error("Direct upload is only available when S3 storage is enabled.", 500)
    presigned_url = storage.client.generate_presigned_url(
        "put_object",
        Params={"Bucket": storage.bucket_name, "Key": source_key},
        ExpiresIn=3600,
        HttpMethod="PUT",
    )
    session = create_session(
        user_id=current_user["id"],
        brand_id=brand_id,
        source_type="upload",
        upload_kind=upload_kind,
        source_filename=filename,
        payload={
            "upload": {
                "video_id": video_id,
                "source_key": source_key,
                "filename": filename,
                "size_bytes": size_bytes,
                "content_type": content_type,
            }
        },
    )
    return jsonify(
        {
            "ok": True,
            "session_id": session["id"],
            "video_id": video_id,
            "upload_url": presigned_url,
            "source_key": source_key,
            "headers": {},
        }
    )


@video_shorts_bp.route("/shorts/quick/api/upload/complete", methods=["POST"])
def quick_short_upload_complete():
    current_user, redirect_response = _require_quick_user()
    if redirect_response:
        return jsonify({"error": "unauthorized"}), 401
    _ensure_quick_schema()
    brand_id = current_brand_id()
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id") or "").strip()
    duration_seconds = data.get("duration_seconds")
    session = get_session(session_id, user_id=current_user["id"])
    if not session:
        return _json_error("Upload session not found.", 404)
    upload_payload = (session.get("payload") or {}).get("upload") or {}
    video_id = str(upload_payload.get("video_id") or "").strip()
    filename = str(upload_payload.get("filename") or "").strip()
    source_key = str(upload_payload.get("source_key") or "").strip()
    if not video_id or not filename or not source_key:
        return _json_error("Upload payload is incomplete.")
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        ensure_channel_owner_schema(conn)
        _ensure_video_crop_schema(conn)
        if session.get("upload_kind") == "music":
            channel_id = _get_or_create_music_channel(conn, current_user.get("id"), brand_id)
        elif session.get("upload_kind") == "podcast":
            channel_id = _get_or_create_podcast_channel(conn, current_user.get("id"), brand_id)
        else:
            channel_id = _get_or_create_local_channel(conn, current_user.get("id"), brand_id)
        if not channel_id:
            return _json_error("Local channel could not be created.")
        title = Path(filename).stem or "Uploaded media"
        try:
            conn.execute(
                """
                INSERT INTO youtube_videos
                    (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                     duration_seconds, view_count, like_count, comment_count, video_url, owner_user_id,
                     brand_id, download_status, downloaded_at, is_music_only, transcript_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', now(), ?, 'pending')
                """,
                [
                    channel_id,
                    video_id,
                    title,
                    datetime.utcnow().isoformat(),
                    None,
                    False,
                    duration_seconds,
                    None,
                    None,
                    None,
                    None,
                    current_user.get("id"),
                    brand_id,
                    bool(session.get("upload_kind") == "music"),
                ],
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.execute(
                """
                INSERT INTO youtube_videos
                    (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                     duration_seconds, view_count, like_count, comment_count, video_url, owner_user_id,
                     brand_id, download_status, downloaded_at, transcript_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', now(), 'pending')
                """,
                [
                    channel_id,
                    video_id,
                    title,
                    datetime.utcnow().isoformat(),
                    None,
                    False,
                    duration_seconds,
                    None,
                    None,
                    None,
                    None,
                    current_user.get("id"),
                    brand_id,
                ],
            )
        row = conn.execute("SELECT id FROM youtube_videos WHERE video_id = ?", [video_id]).fetchone()
        conn.commit()
    finally:
        conn.close()
    video_pk = row[0] if row else None
    if not video_pk:
        return _json_error("Video record could not be created.")
    _upsert_storage_asset(
        f"downloaded:{Path(source_key).name}",
        f"s3://{source_key}",
        int(upload_payload.get("size_bytes") or 0),
        current_user.get("id"),
    )
    job_input_hash = sha256(f"quick-upload:{current_user['id']}:{brand_id}:{video_id}".encode("utf-8")).hexdigest()
    enqueue_result = enqueue_job(
        user_id=current_user["id"],
        job_type=JOB_TYPE_TRANSCRIBE_UPLOAD,
        payload={
            "quick_session_id": session["id"],
            "video_pk": int(video_pk),
            "video_id": video_id,
            "duration_seconds": duration_seconds,
        },
        input_hash=job_input_hash,
    )
    job = enqueue_result.get("job") or {}
    session = update_session(
        session["id"],
        status=STATUS_INGESTING,
        video_pk=int(video_pk),
        video_id=video_id,
        ingest_job_id=job.get("id"),
        result={"stage": "uploaded", "message": "Upload complete. Starting transcription."},
    )
    return jsonify({"ok": True, "session": _build_session_payload(session), "job_id": job.get("id")})


def _create_uploaded_video_session_and_enqueue(
    *,
    current_user,
    brand_id,
    filename: str,
    upload_kind: str,
    video_id: str,
    source_key: str,
    size_bytes: int,
    duration_seconds,
):
    session = create_session(
        user_id=current_user["id"],
        brand_id=brand_id,
        source_type="upload",
        upload_kind=upload_kind,
        source_filename=filename,
        payload={
            "upload": {
                "video_id": video_id,
                "source_key": source_key,
                "filename": filename,
                "size_bytes": size_bytes,
                "content_type": "",
            }
        },
    )
    conn = get_db()
    try:
        ensure_brand_schema(conn)
        ensure_channel_owner_schema(conn)
        _ensure_video_crop_schema(conn)
        if upload_kind == "music":
            channel_id = _get_or_create_music_channel(conn, current_user.get("id"), brand_id)
        elif upload_kind == "podcast":
            channel_id = _get_or_create_podcast_channel(conn, current_user.get("id"), brand_id)
        else:
            channel_id = _get_or_create_local_channel(conn, current_user.get("id"), brand_id)
        if not channel_id:
            raise ValueError("Local channel could not be created.")
        title = Path(filename).stem or "Uploaded media"
        try:
            conn.execute(
                """
                INSERT INTO youtube_videos
                    (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                     duration_seconds, view_count, like_count, comment_count, video_url, owner_user_id,
                     brand_id, download_status, downloaded_at, is_music_only, transcript_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', now(), ?, 'pending')
                """,
                [
                    channel_id,
                    video_id,
                    title,
                    datetime.utcnow().isoformat(),
                    None,
                    False,
                    duration_seconds,
                    None,
                    None,
                    None,
                    None,
                    current_user.get("id"),
                    brand_id,
                    bool(upload_kind == "music"),
                ],
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.execute(
                """
                INSERT INTO youtube_videos
                    (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                     duration_seconds, view_count, like_count, comment_count, video_url, owner_user_id,
                     brand_id, download_status, downloaded_at, transcript_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', now(), 'pending')
                """,
                [
                    channel_id,
                    video_id,
                    title,
                    datetime.utcnow().isoformat(),
                    None,
                    False,
                    duration_seconds,
                    None,
                    None,
                    None,
                    None,
                    current_user.get("id"),
                    brand_id,
                ],
            )
        row = conn.execute("SELECT id FROM youtube_videos WHERE video_id = ?", [video_id]).fetchone()
        conn.commit()
    finally:
        conn.close()
    video_pk = row[0] if row else None
    if not video_pk:
        raise ValueError("Video record could not be created.")
    _upsert_storage_asset(
        f"downloaded:{Path(source_key).name}",
        f"s3://{source_key}",
        int(size_bytes or 0),
        current_user.get("id"),
    )
    job_input_hash = sha256(f"quick-upload:{current_user['id']}:{brand_id}:{video_id}".encode("utf-8")).hexdigest()
    enqueue_result = enqueue_job(
        user_id=current_user["id"],
        job_type=JOB_TYPE_TRANSCRIBE_UPLOAD,
        payload={
            "quick_session_id": session["id"],
            "video_pk": int(video_pk),
            "video_id": video_id,
            "duration_seconds": duration_seconds,
        },
        input_hash=job_input_hash,
    )
    job = enqueue_result.get("job") or {}
    session = update_session(
        session["id"],
        status=STATUS_INGESTING,
        video_pk=int(video_pk),
        video_id=video_id,
        ingest_job_id=job.get("id"),
        result={"stage": "uploaded", "message": "Upload complete. Starting transcription."},
    )
    return session, job


@video_shorts_bp.route("/shorts/quick/api/upload/direct", methods=["POST"])
def quick_short_upload_direct():
    current_user, redirect_response = _require_quick_user()
    if redirect_response:
        return jsonify({"error": "unauthorized"}), 401
    _ensure_quick_schema()
    brand_id = current_brand_id()
    upload_kind = str(request.form.get("upload_kind") or "video").strip().lower()
    if upload_kind not in {"video", "music", "podcast"}:
        upload_kind = "video"
    file = request.files.get("file")
    if not file:
        return _json_error("Choose a file first.")
    filename = Path(file.filename or "").name
    if not filename:
        return _json_error("Choose a file first.")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        return _json_error("Unsupported file format.")
    temp_dir = VIDEOS_DIR.parent / "tmp_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir=str(temp_dir))
    temp_path = Path(handle.name)
    handle.close()
    try:
        file.save(temp_path)
        size_bytes = temp_path.stat().st_size if temp_path.exists() else 0
        if size_bytes <= 0 or size_bytes > MAX_UPLOAD_BYTES:
            return _json_error("The file must be 500MB or smaller.")
        conn_ro = get_db_readonly()
        try:
            usage = _get_user_storage_usage(conn_ro, current_user["id"])
        finally:
            conn_ro.close()
        limit_bytes = usage.get("limit_bytes") or 0
        if limit_bytes and usage["used_bytes"] + size_bytes > limit_bytes:
            return _json_error("Storage limit is full. Free up space or upgrade first.", 403, code="storage_full")
        video_id = f"local_{uuid.uuid4().hex}"
        source_key = f"videos/{video_id}{ext}"
        storage = get_media_storage()
        storage.put_file(temp_path, source_key)
        duration_raw = request.form.get("duration_seconds")
        try:
            duration_seconds = float(duration_raw) if duration_raw not in (None, "", "null") else None
        except Exception:
            duration_seconds = None
        session, job = _create_uploaded_video_session_and_enqueue(
            current_user=current_user,
            brand_id=brand_id,
            filename=filename,
            upload_kind=upload_kind,
            video_id=video_id,
            source_key=source_key,
            size_bytes=size_bytes,
            duration_seconds=duration_seconds,
        )
        return jsonify({"ok": True, "session": _build_session_payload(session), "job_id": job.get("id")})
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass


def _sync_quick_plan_entry(video_id: str, *, title: str, start: float, end: float) -> int:
    entries = generation._load_plan_entries(video_id) or []
    if entries:
        entry = entries[0]
        entry["title"] = title
        entry["start"] = round(start, 3)
        entry["end"] = round(end, 3)
        entry["status"] = "pending"
        entry["excerpt"] = entry.get("excerpt") or ""
        entry["transcript_full"] = entry.get("transcript_full") or ""
        generation._write_plan_entries(video_id, entries)
        return int(entry.get("plan_index") or 1)
    conn_transcript = get_db_readonly()
    try:
        _, segments = generation._fetch_transcript(conn_transcript, video_id)
    finally:
        conn_transcript.close()
    transcript_full = generation.build_transcript_for_range(segments, start, end, prefer_tr=True) if segments else ""
    entries = [
        {
            "plan_index": 1,
            "title": title,
            "start": round(start, 3),
            "end": round(end, 3),
            "clip_filename": f"1_{video_id}.mp4",
            "status": "pending",
            "transcript_full": transcript_full,
            "excerpt": transcript_full,
        }
    ]
    generation._write_plan_entries(video_id, entries)
    return 1


@video_shorts_bp.route("/shorts/quick/api/render", methods=["POST"])
def quick_short_render():
    current_user, redirect_response = _require_quick_user()
    if redirect_response:
        return jsonify({"error": "unauthorized"}), 401
    _ensure_quick_schema()
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id") or "").strip()
    session = get_session(session_id, user_id=current_user["id"])
    if not session or not session.get("video_pk") or not session.get("video_id"):
        return _json_error("Quick short session not found.", 404)
    start = float(data.get("clip_start_seconds") or session.get("clip_start_seconds") or 0.0)
    end = float(data.get("clip_end_seconds") or session.get("clip_end_seconds") or 0.0)
    title = str(data.get("clip_title") or session.get("clip_title") or "First short").strip()[:80] or "First short"
    plan_index = _sync_quick_plan_entry(session["video_id"], title=title, start=start, end=end)
    app_obj = current_app._get_current_object()
    with app_obj.test_request_context(
        f"/video_shorts/generate/{int(session['video_pk'])}/autoclip",
        method="POST",
        data={"plan_index": str(plan_index), "title": title},
        headers={"X-Requested-With": "XMLHttpRequest"},
    ):
        g.vs_current_user = current_user
        brand_id = current_brand_id()
        if brand_id:
            g.vs_current_brand = {"id": brand_id}
        response = generation.autoclip_video(int(session["video_pk"]))
    actual_response = response[0] if isinstance(response, tuple) else response
    status_code = response[1] if isinstance(response, tuple) and len(response) > 1 else getattr(actual_response, "status_code", 200)
    payload = actual_response.get_json(silent=True) or {}
    if status_code >= 400:
        return jsonify(payload), status_code
    job_id = payload.get("job_id")
    session = update_session(
        session["id"],
        status=STATUS_RENDERING,
        clip_start_seconds=start,
        clip_end_seconds=end,
        clip_title=title,
        render_job_id=job_id,
    )
    if job_id:
        job = get_job(job_id, user_id=current_user["id"]) or {}
        job_payload = job.get("payload") or {}
        if not job_payload.get("quick_session_id"):
            job_payload["quick_session_id"] = session["id"]
        update_job_payload(job_id, job_payload)
    return jsonify({"ok": True, "session": _build_session_payload(session), **payload}), status_code


@video_shorts_bp.route("/shorts/quick/api/publish", methods=["POST"])
def quick_short_publish():
    current_user, redirect_response = _require_quick_user()
    if redirect_response:
        return jsonify({"error": "unauthorized"}), 401
    _ensure_quick_schema()
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id") or "").strip()
    session = get_session(session_id, user_id=current_user["id"])
    if not session or not session.get("video_pk") or not session.get("video_id"):
        return _json_error("Quick short session not found.", 404)
    result = session.get("result") or {}
    clip_filename = str(result.get("clip_filename") or "").strip()
    if not clip_filename or not generation._short_exists(clip_filename):
        return _json_error("Short file could not be found.", 404)

    brand_id = session.get("brand_id")
    target = str(data.get("target") or "").strip().lower()
    mode = str(data.get("mode") or "now").strip().lower()
    if target not in {"youtube", "instagram"}:
        return _json_error("Choose where to publish first.")
    if mode not in {"now", "schedule"}:
        mode = "now"

    publish_at_value = str(data.get("publish_at") or "").strip()
    publish_at_iso = None
    if mode == "schedule":
        if not publish_at_value:
            return _json_error("Choose a schedule time first.")
        try:
            publish_at_iso = generation.local_to_utc_rfc3339(
                publish_at_value,
                (current_user or {}).get("time_zone") or generation.DEFAULT_TIME_ZONE,
            )
        except Exception:
            return _json_error("Enter a valid schedule time.")

    if target == "youtube" and not has_refresh_token(current_user["id"], brand_id=brand_id):
        return _json_error("Connect YouTube first.", 403, code="target_not_connected", target="youtube")
    if target == "instagram":
        try:
            instagram_creds = get_instagram_credentials(current_user["id"]) or {}
        except InstagramTokenStoreError:
            instagram_creds = {}
        if not instagram_creds or not generation._validate_instagram_connection(instagram_creds):
            return _json_error("Connect Instagram first.", 403, code="target_not_connected", target="instagram")

    title = (str(result.get("title") or session.get("clip_title") or "Short").strip()[:100] or "Short")
    description = str(result.get("description") or result.get("excerpt") or title).strip()[:5000]
    publish_payload = {
        "quick_session_id": session["id"],
        "video_pk": int(session["video_pk"]),
        "video_id": str(session["video_id"]),
        "brand_id": brand_id,
        "clip_filename": clip_filename,
        "title": title,
        "description": description,
        "target": target,
        "mode": mode,
        "publish_at_local": publish_at_value or None,
        "publish_at_iso": publish_at_iso,
    }
    job_input_hash = sha256(
        (
            f"quick-publish:{current_user['id']}:{session['id']}:{clip_filename}:{target}:{mode}:{publish_at_iso or ''}"
        ).encode("utf-8")
    ).hexdigest()
    enqueue_result = enqueue_job(
        user_id=current_user["id"],
        job_type=JOB_TYPE_PUBLISH_SHORT,
        payload=publish_payload,
        input_hash=job_input_hash,
    )
    if enqueue_result.get("kind") == "concurrency_limit":
        return _json_error(
            "Finish your current short first — your plan runs one at a time.",
            429,
            code="concurrency_limit",
        )

    job = enqueue_result.get("job") or {}
    if enqueue_result.get("kind") == "cached":
        publish_result = job.get("result") or {}
        merged = {**result, "publish": publish_result}
        session = update_session(
            session["id"],
            status=STATUS_DONE,
            publish_job_id=job.get("id"),
            result=merged,
        )
        return jsonify(
            {
                "ok": True,
                "session": _build_session_payload(session),
                "job_id": job.get("id"),
                "cached": True,
            }
        )

    merged = {
        **result,
        "publish": {
            "target": target,
            "mode": mode,
            "status": "queued",
            "message": "Publishing… we'll notify you.",
        },
    }
    session = update_session(
        session["id"],
        status=STATUS_PUBLISHING,
        publish_job_id=job.get("id"),
        result=merged,
    )
    return jsonify({"ok": True, "session": _build_session_payload(session), "job_id": job.get("id"), "cached": False})
