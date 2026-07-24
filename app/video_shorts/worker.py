from __future__ import annotations

import errno
import os
import socket
import time
import tempfile
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from pathlib import Path

from flask import g

from app import create_app
from app.video_shorts.config import (
    DISK_GUARD_PCT,
    JOB_POLL_INTERVAL_SECONDS,
    MAX_GLOBAL_CONCURRENT_JOBS,
    STALE_JOB_TIMEOUT_SECONDS,
    WORKER_CONCURRENCY,
)
from app.video_shorts.routes import generation
from app.video_shorts.routes import quick_short as quick_short_routes
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.media_utils import _resolve_source_video, _cleanup_resolved_source_video
from app.video_shorts.services.media_utils import MediaSubprocessTimeoutError
from app.video_shorts.services.quick_short_flow import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INGESTING,
    STATUS_PUBLISHING,
    STATUS_RENDERING,
    STATUS_REVIEW,
    get_session,
    update_session,
)
from app.video_shorts.services.render_jobs import (
    JOB_TYPE_INGEST_YOUTUBE,
    JOB_TYPE_INSTAGRAM_COMMENT_WEBHOOK,
    JOB_TYPE_PUBLISH_SHORT,
    JOB_TYPE_RENDER_SHORT,
    JOB_TYPE_TRANSCRIBE_UPLOAD,
    claim_next_job,
    count_processing_jobs,
    finalize_job_success,
    get_job,
    mark_job_done,
    mark_job_failed,
    requeue_job,
    requeue_timed_out_jobs,
    update_job_result,
)
from app.video_shorts.services.instagram_comment_webhook import process_instagram_comment_webhook_job
from app.video_shorts.services.disk_guard import disk_guard_triggered
from app.video_shorts.services.storage import get_media_storage, build_storage_reference
from app.video_shorts.services.transcript_service import _transcribe_with_whisper
from app.video_shorts.services.usage_metering import add_transcription_minutes
from app.video_shorts.services.usage_metering import check_transcription_quota
from app.video_shorts.services.user_events import prepare_transcript_completed_transition, track_event
from app.video_shorts.services.youtube_oauth import has_refresh_token, upload_video_with_refresh_token
from app.video_shorts.services.instagram_queue import enqueue_instagram_clip
from app.video_shorts.services.db import (
    _ensure_transcript_schema,
    _ensure_video_crop_schema,
    ensure_postgres_youtube_transcripts_id_default,
    get_db,
)
from src.trends.instagram_tokens import InstagramTokenStoreError, get_instagram_credentials

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None


class PermanentRenderJobError(RuntimeError):
    pass


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _load_user_context(user_id: str, brand_id: Optional[str]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT id, name, email, username, role, plan_id
            FROM shorts_users
            WHERE id = ?
            """,
            [user_id],
        ).fetchone()
        brand = None
        if brand_id:
            brand_row = conn.execute(
                """
                SELECT id, name, slug, is_default
                FROM shorts_brands
                WHERE id = ? AND owner_user_id = ?
                """,
                [brand_id, user_id],
            ).fetchone()
            if brand_row:
                brand = {
                    "id": brand_row[0],
                    "name": brand_row[1],
                    "slug": brand_row[2],
                    "is_default": bool(brand_row[3]),
                }
    finally:
        conn.close()
    if not row:
        raise PermanentRenderJobError("User not found for queued render job.")
    user = {
        "id": str(row[0]),
        "name": row[1],
        "email": row[2],
        "username": row[3],
        "role": row[4],
        "plan_id": row[5],
    }
    return user, brand


def _normalize_response(result: Any) -> Tuple[int, Dict[str, Any]]:
    response = result
    status_code = getattr(response, "status_code", None)
    if isinstance(result, tuple):
        response = result[0]
        if len(result) > 1 and isinstance(result[1], int):
            status_code = result[1]
    payload = {}
    if hasattr(response, "get_json"):
        try:
            payload = response.get_json(silent=True) or {}
        except Exception:
            payload = {}
    if status_code is None:
        status_code = 200
    return int(status_code), payload


def _mark_plan_failure(job: Dict[str, Any], error_message: str) -> None:
    payload = job.get("payload") or {}
    source_video_id = str(payload.get("source_video_id") or "").strip()
    plan_index = payload.get("plan_index")
    if not source_video_id or plan_index is None:
        return
    try:
        entries = generation._load_plan_entries(source_video_id)
        generation._update_plan_entry_job_state(
            source_video_id,
            entries,
            plan_index=int(plan_index),
            status="failed",
            error_message=error_message,
        )
    except Exception:
        pass


def _update_plan_status(job: Dict[str, Any], status: str) -> None:
    payload = job.get("payload") or {}
    source_video_id = str(payload.get("source_video_id") or "").strip()
    plan_index = payload.get("plan_index")
    if not source_video_id or plan_index is None:
        return
    try:
        entries = generation._load_plan_entries(source_video_id)
        generation._update_plan_entry_job_state(
            source_video_id,
            entries,
            plan_index=int(plan_index),
            status=status,
            render_job_id=job.get("id"),
        )
    except Exception:
        pass


def _set_quick_session_state(session_id: Optional[str], **updates: Any) -> None:
    if not session_id:
        return
    try:
        update_session(session_id, **updates)
    except Exception:
        pass


def _mark_job_terminal_failure(job: Dict[str, Any], message: str) -> None:
    if job.get("type") == JOB_TYPE_RENDER_SHORT:
        _mark_plan_failure(job, message)
    mark_job_failed(
        job["id"],
        message,
        release_reservation=job.get("type") == JOB_TYPE_RENDER_SHORT,
    )
    quick_session_id = str((job.get("payload") or {}).get("quick_session_id") or "").strip()
    if quick_session_id:
        quick_session = get_session(quick_session_id, user_id=job.get("user_id")) or {}
        existing_result = quick_session.get("result") or {}
        _set_quick_session_state(
            quick_session_id,
            status=STATUS_DONE if job.get("type") == JOB_TYPE_PUBLISH_SHORT else STATUS_FAILED,
            result=(
                {**existing_result, "publish": {"status": "failed", "message": message}}
                if job.get("type") == JOB_TYPE_PUBLISH_SHORT
                else {"message": message}
            ),
        )


def _user_facing_timeout_message(job: Dict[str, Any]) -> str:
    if job.get("type") == JOB_TYPE_RENDER_SHORT:
        return "This video took too long to process. Please try again. If it keeps happening, contact support."
    return "This job took too long to finish. Please try again. If it keeps happening, contact support."


def _user_facing_terminal_media_message(job: Dict[str, Any], exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        if job.get("type") == JOB_TYPE_RENDER_SHORT:
            return "This source video could not be found. Please upload or download it again, then try again."
        return "This source file could not be found. Please try again."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return "The system is busy right now. Please try again in a few minutes."
    if job.get("type") == JOB_TYPE_RENDER_SHORT:
        return "This video could not be processed right now. Please try again. If it keeps happening, contact support."
    return "This job could not be completed right now. Please try again. If it keeps happening, contact support."


def _set_job_progress(job_id: str, *, stage: str, message: str, status: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> None:
    payload = {"stage": stage, "message": message}
    if status:
        payload["status"] = status
    if extra:
        payload.update(extra)
    update_job_result(job_id, payload)


def _worker_should_wait_before_claim(app) -> bool:
    processing_jobs = count_processing_jobs()
    if processing_jobs >= int(MAX_GLOBAL_CONCURRENT_JOBS):
        app.logger.debug(
            "Worker claim paused by global concurrency cap processing_jobs=%s cap=%s",
            processing_jobs,
            MAX_GLOBAL_CONCURRENT_JOBS,
        )
        return True
    if disk_guard_triggered(operation="worker_claim", log=app.logger):
        app.logger.warning(
            "Worker claim paused by disk guard threshold_pct=%s",
            DISK_GUARD_PCT,
        )
        return True
    return False


def _duration_minutes(duration_seconds: Any) -> float:
    try:
        seconds = float(duration_seconds or 0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0.0
    return round(seconds / 60.0, 2)


def _format_transcription_minutes_label(minutes: Any) -> str:
    try:
        rounded = int(round(float(minutes or 0)))
    except Exception:
        rounded = 0
    if rounded == 1:
        return "1 minute"
    return f"{rounded} minutes"


def _transcription_quota_message(duration_seconds: Any, remaining_minutes: Any) -> str:
    needed_minutes = _duration_minutes(duration_seconds)
    return (
        f"This video is {_format_transcription_minutes_label(needed_minutes)}, "
        f"but you have {_format_transcription_minutes_label(remaining_minutes)} of transcription left this month."
    )


def _download_youtube_video(video_url: str, video_id: str) -> Path:
    if yt_dlp is None:
        raise PermanentRenderJobError("yt_dlp is not installed on the worker.")
    work_dir = Path(tempfile.mkdtemp(prefix=f"vs_quick_{video_id}_"))
    target = work_dir / f"{video_id}.mp4"
    opts = {
        "outtmpl": str(work_dir / f"{video_id}.%(ext)s"),
        "merge_output_format": "mp4",
        "format": "bestvideo*+bestaudio/best",
        "quiet": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([video_url])
    if target.exists():
        return target
    candidates = sorted(work_dir.glob(f"{video_id}.*"))
    if not candidates:
        raise FileNotFoundError(f"download output missing for {video_id}")
    candidates[0].rename(target)
    return target


def _save_transcript(video_id: str, *, full_text: str, segments: list[dict], owner_user_id: Optional[str], duration_seconds: Any) -> None:
    segments_json = generation.json.dumps(segments, ensure_ascii=False)
    conn = get_db()
    try:
        _ensure_video_crop_schema(conn)
        _ensure_transcript_schema(conn)
        ensure_postgres_youtube_transcripts_id_default(conn)
        event_video_id, should_emit_transcript_completed = prepare_transcript_completed_transition(
            conn,
            video_id=video_id,
        )
        existing = conn.execute("SELECT 1 FROM youtube_transcripts WHERE video_id = ?", [video_id]).fetchone()
        if existing:
            conn.execute(
                "UPDATE youtube_transcripts SET full_text = ?, segments_json = ?, whisper_segments_json = ? WHERE video_id = ?",
                [full_text, segments_json, segments_json, video_id],
            )
        else:
            conn.execute(
                "INSERT INTO youtube_transcripts (video_id, full_text, segments_json, whisper_segments_json) VALUES (?, ?, ?, ?)",
                [video_id, full_text, segments_json, segments_json],
            )
        conn.execute(
            """
            UPDATE youtube_videos
            SET transcript_status = 'done',
                fetch_transcript = FALSE,
                last_checked_at = CURRENT_TIMESTAMP
            WHERE video_id = ?
            """,
            [video_id],
        )
        conn.commit()
    finally:
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
            video_title = None
            try:
                conn = get_db()
                try:
                    row = conn.execute(
                        "SELECT title FROM youtube_videos WHERE video_id = ?",
                        [video_id],
                    ).fetchone()
                    if row:
                        video_title = row[0]
                finally:
                    conn.close()
            except Exception:
                video_title = None
            add_transcription_minutes(
                str(owner_user_id),
                minutes,
                video_id=video_id,
                video_title=video_title,
            )


def _suggest_clip(segments: list[dict], duration_seconds: Any) -> Tuple[float, float, str, str]:
    if segments:
        first = segments[0] or {}
        start = float(first.get("start") or 0.0)
        end = float(first.get("end") or 0.0)
        if end <= start:
            end = start + 20.0
        end = min(end, start + 30.0)
        title = str((first.get("tr_text") or first.get("text") or "First short").strip())[:80] or "First short"
        excerpt = str((first.get("tr_text") or first.get("text") or "").strip())
        return round(start, 3), round(end, 3), title, excerpt
    duration = float(duration_seconds or 30.0)
    end = min(duration, 30.0)
    return 0.0, round(end, 3), "First short", ""


def _execute_ingest_youtube_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload") or {}
    session_id = str(payload.get("quick_session_id") or "").strip()
    video_url = str(payload.get("video_url") or "").strip()
    video_id = str(payload.get("video_id") or "").strip()
    video_pk = int(payload.get("video_pk"))
    owner_user_id = str(job["user_id"])
    duration_seconds = payload.get("duration_seconds")
    _set_quick_session_state(session_id, status=STATUS_INGESTING)
    _set_job_progress(job["id"], stage="queued", message="Queued for ingest.", status="queued")
    _set_job_progress(job["id"], stage="downloading", message="Downloading the source video.", status="processing")
    local_path = _download_youtube_video(video_url, video_id)
    try:
        storage = get_media_storage()
        source_key = f"videos/{video_id}{local_path.suffix or '.mp4'}"
        storage.put_file(local_path, source_key)
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'downloaded',
                    downloaded_at = CURRENT_TIMESTAMP,
                    last_checked_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [video_pk],
            )
            conn.commit()
        finally:
            conn.close()
        _set_job_progress(job["id"], stage="transcribing", message="Transcribing with Whisper.", status="processing")
        transcript_text, segments = _transcribe_with_whisper(local_path)
        _save_transcript(video_id, full_text=transcript_text, segments=segments, owner_user_id=owner_user_id, duration_seconds=duration_seconds)
        clip_start, clip_end, clip_title, excerpt = _suggest_clip(segments, duration_seconds)
        result = {
            "stage": "ready",
            "message": "Ready to review.",
            "video_pk": video_pk,
            "video_id": video_id,
            "clip_start_seconds": clip_start,
            "clip_end_seconds": clip_end,
            "clip_title": clip_title,
            "excerpt": excerpt,
            "source_url": build_storage_reference(source_key),
        }
        _set_quick_session_state(
            session_id,
            status=STATUS_REVIEW,
            video_pk=video_pk,
            video_id=video_id,
            clip_start_seconds=clip_start,
            clip_end_seconds=clip_end,
            clip_title=clip_title,
            result=result,
        )
        return result
    finally:
        try:
            local_path.unlink(missing_ok=True)
            local_path.parent.rmdir()
        except Exception:
            pass


def _execute_transcribe_upload_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload") or {}
    session_id = str(payload.get("quick_session_id") or "").strip()
    video_id = str(payload.get("video_id") or "").strip()
    video_pk = int(payload.get("video_pk"))
    owner_user_id = str(job["user_id"])
    duration_seconds = payload.get("duration_seconds")
    needed_minutes = _duration_minutes(duration_seconds)
    if needed_minutes > 0:
        quota = check_transcription_quota(owner_user_id, needed_minutes)
        if not quota.get("allowed", False):
            raise PermanentRenderJobError(
                _transcription_quota_message(duration_seconds, quota.get("remaining_minutes"))
            )
    _set_quick_session_state(session_id, status=STATUS_INGESTING)
    _set_job_progress(job["id"], stage="uploaded", message="Upload complete. Preparing transcription.", status="processing")
    source_path, is_temp = _resolve_source_video(video_id)
    if not source_path or not source_path.exists():
        raise PermanentRenderJobError("Uploaded source file could not be found.")
    try:
        _set_job_progress(job["id"], stage="transcribing", message="Transcribing with Whisper.", status="processing")
        transcript_text, segments = _transcribe_with_whisper(source_path)
        _save_transcript(video_id, full_text=transcript_text, segments=segments, owner_user_id=owner_user_id, duration_seconds=duration_seconds)
        clip_start, clip_end, clip_title, excerpt = _suggest_clip(segments, duration_seconds)
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'downloaded',
                    downloaded_at = CURRENT_TIMESTAMP,
                    last_checked_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [video_pk],
            )
            conn.commit()
        finally:
            conn.close()
        result = {
            "stage": "ready",
            "message": "Ready to review.",
            "video_pk": video_pk,
            "video_id": video_id,
            "clip_start_seconds": clip_start,
            "clip_end_seconds": clip_end,
            "clip_title": clip_title,
            "excerpt": excerpt,
        }
        _set_quick_session_state(
            session_id,
            status=STATUS_REVIEW,
            video_pk=video_pk,
            video_id=video_id,
            clip_start_seconds=clip_start,
            clip_end_seconds=clip_end,
            clip_title=clip_title,
            result=result,
        )
        return result
    finally:
        _cleanup_resolved_source_video(source_path, is_temp)


def _execute_render_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload") or {}
    video_pk = int(payload.get("video_pk"))
    plan_index = int(payload.get("plan_index"))
    title = str(payload.get("title") or "").strip()
    user, brand = _load_user_context(job["user_id"], payload.get("brand_id"))
    with app.app_context():
        with app.test_request_context(
            f"/video_shorts/generate/{video_pk}/autoclip",
            method="POST",
            data={
                "plan_index": str(plan_index),
                "title": title,
                "_queued_job": "1",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        ):
            g.vs_current_user = user
            if brand:
                g.vs_current_brand = brand
            result = generation.autoclip_video(video_pk)
            status_code, response_payload = _normalize_response(result)
    if status_code >= 500:
        raise RuntimeError(response_payload.get("message") or "Render job failed.")
    if status_code >= 400:
        raise PermanentRenderJobError(response_payload.get("message") or "Render job failed.")
    if not response_payload.get("success"):
        raise RuntimeError(response_payload.get("message") or "Render job returned unsuccessful response.")
    return response_payload


def _execute_publish_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload") or {}
    session_id = str(payload.get("quick_session_id") or "").strip()
    video_pk = int(payload.get("video_pk"))
    video_id = str(payload.get("video_id") or "").strip()
    clip_filename = str(payload.get("clip_filename") or "").strip()
    title = str(payload.get("title") or "Short").strip()[:100] or "Short"
    description = str(payload.get("description") or title).strip()[:5000]
    target = str(payload.get("target") or "").strip().lower()
    publish_at_iso = payload.get("publish_at_iso")
    publish_at_local = payload.get("publish_at_local")
    user, brand = _load_user_context(job["user_id"], payload.get("brand_id"))
    brand_id = (brand or {}).get("id")
    _set_quick_session_state(session_id, status=STATUS_PUBLISHING)
    _set_job_progress(job["id"], stage="queued", message="Publish queued.", status="queued")

    clip_path = generation._resolve_short_path_for_processing(clip_filename)
    if not clip_path or not clip_path.exists():
        raise PermanentRenderJobError("Short file could not be found for publishing.")

    if target == "youtube":
        if not has_refresh_token(user.get("id"), brand_id=brand_id):
            raise PermanentRenderJobError("Connect YouTube first.")
        _set_job_progress(job["id"], stage="uploading", message="Uploading to YouTube.", status="processing")
        try:
            response = upload_video_with_refresh_token(
                video_path=str(clip_path),
                title=title,
                description=description,
                publish_at=publish_at_iso,
                privacy_status="private",
                user_id=user.get("id"),
                brand_id=brand_id,
            ) or {}
        except Exception as exc:
            if "invalid_grant" in str(exc):
                raise PermanentRenderJobError("YouTube connection is invalid. Reconnect and try again.") from exc
            raise
        youtube_id = response.get("id")
        publish_status = "scheduled" if publish_at_iso else "uploaded"
        with app.app_context():
            with app.test_request_context("/video_shorts/shorts/quick", method="POST"):
                g.vs_current_user = user
                if brand:
                    g.vs_current_brand = brand
                generation._update_plan_entry_publish_state(
                    video_pk=video_pk,
                    plan_index="1",
                    filename=clip_filename,
                    publish_status=publish_status,
                    publish_at_local=publish_at_local,
                    publish_at_iso=publish_at_iso,
                    title=title,
                    description=description,
                    youtube_id=youtube_id,
                )
        return {
            "target": "youtube",
            "status": publish_status,
            "message": "YouTube publish submitted.",
            "youtube_id": youtube_id,
            "publish_at": publish_at_iso,
        }

    if target == "instagram":
        try:
            instagram_creds = get_instagram_credentials(user.get("id")) or {}
        except InstagramTokenStoreError as exc:
            raise PermanentRenderJobError("Connect Instagram first.") from exc
        if not instagram_creds or not generation._validate_instagram_connection(instagram_creds):
            raise PermanentRenderJobError("Connect Instagram first.")
        _set_job_progress(job["id"], stage="queueing", message="Queueing Instagram publish.", status="processing")
        plan_entries = generation._load_plan_entries(video_id) or []
        target_entry = plan_entries[0] if plan_entries else {}
        queue_id = enqueue_instagram_clip(
            user_id=user.get("id"),
            video_id=video_id,
            plan_index=str(target_entry.get("plan_index") or 1),
            clip_filename=clip_filename,
            caption_text=(target_entry.get("ig_caption") or target_entry.get("yt_description") or description or title),
            publish_at_iso=publish_at_iso or (datetime.utcnow().isoformat() + "Z"),
            instagram_business_account_id=instagram_creds.get("instagram_business_account_id"),
            instagram_username=instagram_creds.get("instagram_username"),
            youtube_video_id=video_id,
            youtube_short_id=target_entry.get("yt_video_id"),
            plan_title=target_entry.get("title") or target_entry.get("yt_title") or title,
            media_type="reel",
            force_requeue=False,
        )
        return {
            "target": "instagram",
            "status": "scheduled" if publish_at_iso else "queued",
            "message": "Instagram publish queued.",
            "queue_id": queue_id,
            "publish_at": publish_at_iso,
            "media_type": "reel",
        }

    raise PermanentRenderJobError("Unsupported publish target.")


def _execute_instagram_comment_webhook_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    return process_instagram_comment_webhook_job(job.get("payload") or {})


def process_next_job(app, worker_id: str) -> bool:
    if _worker_should_wait_before_claim(app):
        return False
    job = claim_next_job(worker_id)
    if not job:
        return False
    if job.get("type") == JOB_TYPE_RENDER_SHORT:
        _update_plan_status(job, "processing")
    try:
        if job.get("type") == JOB_TYPE_INGEST_YOUTUBE:
            result = _execute_ingest_youtube_job(app, job)
        elif job.get("type") == JOB_TYPE_TRANSCRIBE_UPLOAD:
            result = _execute_transcribe_upload_job(app, job)
        elif job.get("type") == JOB_TYPE_PUBLISH_SHORT:
            result = _execute_publish_job(app, job)
        elif job.get("type") == JOB_TYPE_INSTAGRAM_COMMENT_WEBHOOK:
            result = _execute_instagram_comment_webhook_job(app, job)
        else:
            result = _execute_render_job(app, job)
        mark_job_done(job["id"], result)
        if job.get("type") == JOB_TYPE_RENDER_SHORT:
            finalize_job_success(job["id"])
            quick_session_id = str((job.get("payload") or {}).get("quick_session_id") or "").strip()
            if quick_session_id:
                _set_quick_session_state(
                    quick_session_id,
                    status=STATUS_DONE,
                    render_job_id=job.get("id"),
                    result=result,
                )
        elif job.get("type") == JOB_TYPE_PUBLISH_SHORT:
            quick_session_id = str((job.get("payload") or {}).get("quick_session_id") or "").strip()
            if quick_session_id:
                quick_session = get_session(quick_session_id, user_id=job.get("user_id")) or {}
                existing_result = quick_session.get("result") or {}
                _set_quick_session_state(
                    quick_session_id,
                    status=STATUS_DONE,
                    publish_job_id=job.get("id"),
                    result={**existing_result, "publish": result},
                )
        return True
    except PermanentRenderJobError as exc:
        _mark_job_terminal_failure(job, str(exc))
        return True
    except MediaSubprocessTimeoutError as exc:
        _mark_job_terminal_failure(job, _user_facing_timeout_message(job))
        return True
    except Exception as exc:
        if isinstance(exc, FileNotFoundError) or (isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC):
            app.logger.exception(
                "Job failed without retry job_id=%s type=%s error=%s",
                job.get("id"),
                job.get("type"),
                exc,
            )
            _mark_job_terminal_failure(job, _user_facing_terminal_media_message(job, exc))
            return True
        latest = get_job(job["id"]) or job
        if int(latest.get("attempts") or 0) >= int(latest.get("max_attempts") or 1):
            app.logger.exception(
                "Job failed after max attempts job_id=%s type=%s error=%s",
                latest.get("id"),
                latest.get("type"),
                exc,
            )
            _mark_job_terminal_failure(latest, _user_facing_terminal_media_message(latest, exc))
        else:
            if latest.get("type") == JOB_TYPE_RENDER_SHORT:
                _update_plan_status(latest, "queued")
            requeue_job(job["id"], str(exc))
        return True


def run_worker_loop() -> None:
    worker_id = _worker_id()
    app = create_app()
    with app.app_context():
        while True:
            requeue_timed_out_jobs(timeout_seconds=STALE_JOB_TIMEOUT_SECONDS)
            processed_any = False
            for _ in range(max(1, int(WORKER_CONCURRENCY or 1))):
                if process_next_job(app, worker_id):
                    processed_any = True
                else:
                    break
            if not processed_any:
                time.sleep(JOB_POLL_INTERVAL_SECONDS)


def main() -> None:
    run_worker_loop()


if __name__ == "__main__":
    main()
