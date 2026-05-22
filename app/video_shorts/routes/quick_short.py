import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from flask import flash, redirect, render_template, request, url_for, g

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
from app.video_shorts.services.storage import get_media_storage
from app.video_shorts.youtube_api import extract_video_id, fetch_video_metadata, YoutubeApiError

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


@video_shorts_bp.route("/shorts/quick", methods=["GET", "POST"])
def quick_short():
    current_user = getattr(g, "vs_current_user", None)
    brand_id = current_brand_id()
    if not current_user:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    schema_conn = get_db()
    ensure_brand_schema(schema_conn)
    ensure_channel_owner_schema(schema_conn)
    ensure_storage_user_schema(schema_conn)
    _ensure_postgres_youtube_videos_id_default(schema_conn)
    schema_conn.close()

    flow = (request.form.get("flow") or request.args.get("flow") or "youtube").strip().lower()
    upload_kind = (request.form.get("upload_kind") or request.args.get("upload_kind") or "video").strip().lower()
    if upload_kind not in {"video", "music", "podcast"}:
        upload_kind = "video"
    step = 1
    video_url = ""
    video_id = ""
    meta = None
    upload_step = "upload"
    upload_meta = None
    uploaded_video_pk = None

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if flow == "upload":
            if action == "upload":
                upload_file = request.files.get("video_file")
                if not upload_file or not upload_file.filename:
                    flash("Bir video dosyasi secin.", "danger")
                else:
                    original_name = Path(upload_file.filename)
                    ext = original_name.suffix.lower()
                    if ext not in ALLOWED_UPLOAD_EXTS:
                        flash("Gecersiz format. Video: mp4/mov/mkv, ses: mp3/wav/m4a/aac/ogg/flac.", "danger")
                    else:
                        if request.content_length and request.content_length > MAX_UPLOAD_BYTES:
                            flash("Dosya boyutu 500MB limitini asiyor.", "danger")
                        else:
                            VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
                            temp_name = f"upload_{uuid.uuid4().hex}{ext}"
                            temp_path = VIDEOS_DIR / temp_name
                            upload_file.save(temp_path)
                            size_bytes = temp_path.stat().st_size
                            if size_bytes > MAX_UPLOAD_BYTES:
                                temp_path.unlink(missing_ok=True)
                                flash("Dosya boyutu 500MB limitini asiyor.", "danger")
                            else:
                                conn_ro = get_db_readonly()
                                try:
                                    usage = _get_user_storage_usage(conn_ro, current_user["id"])
                                finally:
                                    conn_ro.close()
                                limit_bytes = usage.get("limit_bytes") or 0
                                if limit_bytes and usage["used_bytes"] + size_bytes > limit_bytes:
                                    temp_path.unlink(missing_ok=True)
                                    flash("Storage limit dolu. Planinizi yukseltin veya dosya silin.", "danger")
                                else:
                                    video_id = f"local_{uuid.uuid4().hex}"
                                    final_path = VIDEOS_DIR / f"{video_id}{ext}"
                                    temp_path.rename(final_path)
                                    duration_seconds = _probe_duration_seconds(final_path)
                                    storage = get_media_storage()
                                    s3_source_key = f"videos/{final_path.name}"
                                    if getattr(storage, "backend_name", "local") == "s3":
                                        try:
                                            storage.put_file(final_path, s3_source_key)
                                            final_path.unlink(missing_ok=True)
                                        except Exception:
                                            final_path.unlink(missing_ok=True)
                                            flash("Dosya S3'e yuklenemedi. Lutfen tekrar deneyin.", "danger")
                                            return render_template(
                                                "quick_short_wizard.html",
                                                flow=flow,
                                                upload_kind=upload_kind,
                                                step=step,
                                                video_url=video_url,
                                                video_id=video_id,
                                                meta=meta,
                                                upload_step=upload_step,
                                                upload_meta=upload_meta,
                                                uploaded_video_pk=uploaded_video_pk,
                                            )
                                    default_title = original_name.stem or (
                                        (
                                            "Uploaded music video"
                                            if upload_kind == "music"
                                            else ("Uploaded podcast video" if upload_kind == "podcast" else "Uploaded media")
                                        )
                                    )
                                    conn = get_db()
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
                                        conn.close()
                                        final_path.unlink(missing_ok=True)
                                        flash("Local kanal olusturulamadi.", "danger")
                                    else:
                                        try:
                                            conn.execute(
                                                """
                                                INSERT INTO youtube_videos
                                                    (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                                                     duration_seconds, view_count, like_count, comment_count, video_url, owner_user_id,
                                                     brand_id, download_status, downloaded_at, is_music_only)
                                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'downloaded', now(), ?)
                                                """,
                                                [
                                                    channel_id,
                                                    video_id,
                                                    default_title,
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
                                            # Fallback for environments where new column migration isn't visible yet.
                                            try:
                                                conn.rollback()
                                            except Exception:
                                                pass
                                            conn.execute(
                                                """
                                                INSERT INTO youtube_videos
                                                    (channel_id, video_id, title, published_at, thumbnail_url, fetch_transcript,
                                                     duration_seconds, view_count, like_count, comment_count, video_url, owner_user_id,
                                                     brand_id, download_status, downloaded_at)
                                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'downloaded', now())
                                                """,
                                                [
                                                    channel_id,
                                                    video_id,
                                                    default_title,
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
                                        row = conn.execute(
                                            "SELECT id FROM youtube_videos WHERE video_id = ?",
                                            [video_id],
                                        ).fetchone()
                                        conn.commit()
                                        conn.close()
                                        video_pk = row[0] if row else None
                                        if not video_pk:
                                            final_path.unlink(missing_ok=True)
                                            flash("Video kaydi olusturulamadi.", "danger")
                                        else:
                                            _upsert_storage_asset(
                                                f"downloaded:{final_path.name}",
                                                f"s3://{s3_source_key}"
                                                if getattr(storage, "backend_name", "local") == "s3"
                                                else str(final_path),
                                                size_bytes,
                                                current_user.get("id"),
                                            )
                                            upload_step = "metadata"
                                            upload_meta = {
                                                "video_id": video_id,
                                                "filename": final_path.name,
                                                "size_bytes": size_bytes,
                                                "size_mb": round(size_bytes / (1024 * 1024), 2),
                                                "duration_seconds": duration_seconds,
                                                "title": default_title,
                                            }
                                            uploaded_video_pk = str(video_pk)
            elif action == "metadata":
                video_pk = (request.form.get("video_pk") or "").strip()
                title = (request.form.get("title") or "").strip()
                if not video_pk or not title:
                    flash("Baslik zorunlu.", "danger")
                    upload_step = "metadata"
                    uploaded_video_pk = video_pk
                    upload_meta = {"title": title}
                else:
                    conn = get_db()
                    conn.execute(
                        "UPDATE youtube_videos SET title = ? WHERE id = ?",
                        [title, video_pk],
                    )
                    conn.commit()
                    conn.close()
                    return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))
        else:
            video_url = (request.form.get("video_url") or "").strip()
            if action in {"validate", "confirm"}:
                step = 2 if action == "validate" else 3
                video_id = extract_video_id(video_url)
                if not video_id:
                    flash("Geçerli bir YouTube video URL girin.", "danger")
                    step = 1
                else:
                    try:
                        meta = fetch_video_metadata(video_id)
                    except YoutubeApiError as exc:
                        flash(str(exc), "danger")
                        step = 1
                    except Exception:
                        flash("YouTube videosu okunamadı. API anahtarını kontrol edin.", "danger")
                        step = 1

                    if meta:
                        duration = meta.get("duration_seconds")
                        if duration is not None and duration > MAX_QUICK_SHORT_SECONDS:
                            flash("Video 25 dakikadan uzun. Lütfen daha kısa bir video seçin.", "warning")
                            step = 1
            elif action == "create":
                video_id = (request.form.get("video_id") or "").strip()
                if not video_id:
                    flash("Video ID bulunamadı. Lütfen tekrar deneyin.", "danger")
                    step = 1
                else:
                    try:
                        meta = fetch_video_metadata(video_id)
                    except YoutubeApiError as exc:
                        flash(str(exc), "danger")
                        step = 1
                        meta = None
                    except Exception:
                        flash("YouTube videosu okunamadı. API anahtarını kontrol edin.", "danger")
                        step = 1
                        meta = None

                    if meta:
                        duration = meta.get("duration_seconds")
                        if duration is not None and duration > MAX_QUICK_SHORT_SECONDS:
                            flash("Video 25 dakikadan uzun. Lütfen daha kısa bir video seçin.", "warning")
                            step = 1
                        else:
                            conn = get_db()
                            ensure_brand_schema(conn)
                            channel_id = _get_or_create_channel(conn, meta, current_user.get("id"), brand_id)
                            if not channel_id:
                                conn.close()
                                flash("Video kanal bilgisi bulunamadı.", "danger")
                                return render_template(
                                    "quick_short_wizard.html",
                                    flow=flow,
                                    step=1,
                                    video_url=video_url,
                                )
                            video_pk = _upsert_video(conn, meta, channel_id, current_user.get("id"), brand_id)
                            conn.commit()
                            conn.close()
                            if not video_pk:
                                flash("Video kaydı oluşturulamadı.", "danger")
                                step = 1
                            else:
                                flash("Video indirme kuyruğa alındı. Local downloader saatlik çalışıyor.", "info")
                                return redirect(url_for("video_shorts_bp.generate_short", video_pk=video_pk))

    return render_template(
        "quick_short_wizard.html",
        flow=flow,
        upload_kind=upload_kind,
        step=step,
        video_url=video_url,
        video_id=video_id,
        meta=meta,
        upload_step=upload_step,
        upload_meta=upload_meta,
        uploaded_video_pk=uploaded_video_pk,
    )
