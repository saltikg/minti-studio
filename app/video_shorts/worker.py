from __future__ import annotations
from app.video_shorts.services import tenant_media
from app.video_shorts.services import render_timing
from app.video_shorts.services import render_jobs

import errno
import json
import os
import re
import socket
import time
import tempfile
import shutil
import secrets
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import logging, urllib.request
logger = logging.getLogger(__name__)


from flask import g

from app import create_app
from app.video_shorts.config import (
    DISCOVERY_IDLE_LOAD_1M_MAX,
    DISCOVERY_IDLE_LOG_INTERVAL_SECONDS,
    DISCOVERY_IDLE_MEM_AVAILABLE_MB_MIN,
    DISCOVERY_IDLE_WAIT_SECONDS,
    DISCOVERY_IDLE_WEB_WINDOW_SECONDS,
    DISCOVERY_IDLE_DISK_USAGE_PCT_MAX,
    DISCOVERY_TEMP_JANITOR_INTERVAL_SECONDS,
    DISCOVERY_TEMP_JANITOR_MAX_AGE_SECONDS,
    DISCOVERY_NIGHT_END_HOUR_PT,
    DISCOVERY_NIGHT_START_HOUR_PT,
    DISK_GUARD_PCT,
    JOB_POLL_INTERVAL_SECONDS,
    MAX_GLOBAL_CONCURRENT_JOBS,
    STALE_JOB_TIMEOUT_SECONDS,
    WORKER_CONCURRENCY,
    WORKER_MODE,
    VIDEO_SHORTS_TMP_DIR,
)
from app.video_shorts.routes import generation
from app.video_shorts.routes import quick_short as quick_short_routes
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.media_utils import (
    _cleanup_resolved_source_video,
    _resolve_source_video,
    normalize_s3_source_video_for_upload,
    purge_source_cache_for_video,
    resolve_s3_source_video_cached,
)
from app.video_shorts.services.media_utils import MediaSubprocessTimeoutError
from app.video_shorts.services.media_duration import (
    get_or_backfill_media_duration_seconds,
    probe_exact_media_duration_seconds,
)
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
    JOB_TYPE_AUTO_SUGGEST_RENDER,
    JOB_TYPE_ADMIN_PROXY_TRANSCRIPT,
    JOB_TYPE_ENRICH_AUTOPILOT_DISCOVERY_EMAILS,
    JOB_TYPE_ENRICH_DISCOVERY_EMAILS,
    GUEST_JOB_ORIGIN,
    JOB_TYPE_INGEST_YOUTUBE,
    JOB_TYPE_INSTAGRAM_COMMENT_WEBHOOK,
    JOB_TYPE_NORMALIZE_UPLOAD,
    JOB_TYPE_PREVIEW_FRAME,
    JOB_TYPE_PUBLISH_SHORT,
    JOB_TYPE_PURGE_VIDEO,
    JOB_TYPE_RENDER_SHORT,
    JOB_TYPE_TRANSCRIBE_UPLOAD,
    claim_next_job,
    count_processing_jobs,
    enqueue_preview_frame_job,
    finalize_job_success,
    get_job,
    mark_job_done,
    mark_job_failed,
    queued_customer_render_exists,
    recover_processing_discovery_jobs_on_startup,
    requeue_dead_local_worker_jobs,
    requeue_job,
    requeue_timed_out_jobs,
    render_job_heartbeat,
    update_job_result,
)
from app.video_shorts.services.free_tool_cache import load_text_cache, write_transcript_cache
from app.video_shorts.services.storage import put_file_for_owner
from app.video_shorts.services.autopilot_email_enrichment import enrich_autopilot_discovery_email_batch
from app.video_shorts.services.discovery_email_enrichment import enrich_discovery_email_batch
from app.video_shorts.services.discovery_automation import process_due_discovery_automation_cycle
from app.video_shorts.services.discovery_promote_queue import customer_job_waiting, process_next_discovery_promote_request
from app.video_shorts.services.instagram_comment_webhook import process_instagram_comment_webhook_job
from app.video_shorts.services.lead_pipeline import record_lead_pipeline_event_for_scope
from app.video_shorts.services.outreach_email_send import process_due_scheduled_outreach_email
from app.video_shorts.services.disk_guard import disk_guard_triggered
from app.video_shorts.services.storage import (
    build_storage_reference,
    get_media_storage,
    is_storage_reference,
    storage_reference_key,
)
from app.video_shorts.services.transcript_service import _transcribe_with_whisper
from app.video_shorts.services.usage_metering import add_transcription_minutes
from app.video_shorts.services.usage_metering import (
    check_source_video_quota,
    is_transcription_quota_exempt_user,
)
from app.video_shorts.services.user_events import prepare_transcript_completed_transition, track_event
from app.video_shorts.services.youtube_oauth import has_refresh_token
from app.video_shorts.services import youtube_scheduling
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


ACCESS_LOG_PATH = Path(os.getenv("GUNICORN_ACCESS_LOG_PATH", "/home/ubuntu/apps/minti_studio/logs/gunicorn-access.log"))
ACCESS_LOG_TIME_RE = re.compile(r"\[(?P<ts>[^\]]+)\]")
BOT_UA_RE = re.compile(r"(bot|crawl|spider|slurp|facebookexternalhit|preview|uptime|monitor|health)", re.I)
ACCESS_LOG_REQUEST_RE = re.compile(r'"(?:GET|POST|HEAD|OPTIONS)\s+(?P<target>\S+)')
_PT = ZoneInfo("America/Los_Angeles")
_last_idle_skip_log: Dict[str, float] = {}
_last_discovery_temp_janitor_run = 0.0


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _worker_mode() -> str:
    mode = str(WORKER_MODE or "all").strip().lower()
    if mode not in {"all", "customer", "discovery"}:
        logger.warning("Invalid WORKER_MODE=%s; using all", mode)
        return "all"
    return mode


def _tail_lines(path: Path, *, max_lines: int = 2000, chunk_size: int = 8192) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        lines: deque[bytes] = deque()
        while position > 0 and len(lines) <= max_lines:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            parts = buffer.splitlines()
            if position > 0:
                buffer = parts[0]
                parts = parts[1:]
            else:
                buffer = b""
            for part in reversed(parts):
                lines.appendleft(part)
                if len(lines) > max_lines:
                    lines.popleft()
        if buffer:
            lines.appendleft(buffer)
        return [line.decode("utf-8", errors="replace") for line in lines]


def _parse_access_log_time(line: str) -> Optional[datetime]:
    match = ACCESS_LOG_TIME_RE.search(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("ts"), "%d/%b/%Y:%H:%M:%S %z").astimezone(timezone.utc)
    except ValueError:
        return None


def _access_log_path(line: str) -> str:
    match = ACCESS_LOG_REQUEST_RE.search(line)
    if not match:
        return ""
    target = str(match.group("target") or "")
    parsed = urlsplit(target)
    return parsed.path or target.split("?", 1)[0]


def _is_ignored_web_hit(line: str) -> bool:
    lowered = line.lower()
    path = _access_log_path(line).lower()
    if BOT_UA_RE.search(line):
        return True
    if path.startswith(
        (
            "/video_shorts/api/admin",
            "/video_shorts/admin",
            "/video_shorts/static",
            "/admin",
            "/static",
            "/assets",
            "/favicon",
            "/health",
        )
    ):
        return True
    if "/api/client-error" in path:
        return True
    if (
        path.endswith("/status")
        or "/render-jobs/" in path
        or "/api/jobs/" in path
        or "/notifications" in path
        or "/heartbeat" in path
        or path.endswith("/auto-suggest-render-status")
    ):
        return True
    if "gokhansaltik" in lowered:
        return True
    return False


def _recent_real_web_activity(now_utc: datetime) -> int:
    cutoff = now_utc - timedelta(seconds=int(DISCOVERY_IDLE_WEB_WINDOW_SECONDS))
    hits = 0
    for line in reversed(_tail_lines(ACCESS_LOG_PATH)):
        parsed = _parse_access_log_time(line)
        if parsed is None:
            continue
        if parsed < cutoff:
            break
        if not _is_ignored_web_hit(line):
            hits += 1
    return hits


def _mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                return int(int(parts[1]) / 1024)
    except Exception:
        logger.exception("Could not read MemAvailable")
    return 0


def _root_disk_usage_pct() -> int:
    try:
        usage = shutil.disk_usage("/")
        return int(round((usage.used / usage.total) * 100))
    except Exception:
        logger.exception("Could not read root disk usage")
        return 100


def _payload_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            values.extend(_payload_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_payload_values(item))
    elif value is not None:
        text = str(value).strip()
        if text:
            values.append(text)
    return values


def _active_job_temp_refs() -> set[str]:
    conn = get_db_readonly()
    refs: set[str] = set()
    try:
        rows = conn.execute(
            """
            SELECT status, payload_json
            FROM shorts_render_jobs
            WHERE status IN ('queued', 'processing')
            """
        ).fetchall()
    except Exception:
        logger.exception("Discovery temp janitor could not read active job refs")
        return refs
    finally:
        conn.close()
    for row in rows:
        if str(row[0] or "").lower() == "processing":
            refs.add("__processing_job__")
        payload = row[1]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except Exception:
                payload = {}
        for value in _payload_values(payload):
            if value.startswith(("/tmp/", str(VIDEO_SHORTS_TMP_DIR))):
                refs.add(value)
            if re.fullmatch(r"[A-Za-z0-9_-]{6,}", value):
                refs.add(value)
    return refs


def _path_is_referenced(path: Path, refs: set[str]) -> bool:
    path_text = str(path)
    name = path.name
    for ref in refs:
        if not ref:
            continue
        if path_text.startswith(ref) or ref.startswith(path_text):
            return True
        if len(ref) >= 6 and ref in name:
            return True
    return False


def _delete_path(path: Path) -> int:
    try:
        if path.is_dir():
            size = 0
            for child in path.rglob("*"):
                try:
                    if child.is_file() or child.is_symlink():
                        size += child.stat().st_size
                except OSError:
                    pass
            shutil.rmtree(path, ignore_errors=True)
            return size
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        return size
    except Exception:
        logger.exception("Discovery temp janitor could not delete path=%s", path)
        return 0


def run_discovery_temp_janitor(*, force: bool = False) -> Dict[str, Any]:
    global _last_discovery_temp_janitor_run
    now = time.time()
    if not force and now - _last_discovery_temp_janitor_run < float(DISCOVERY_TEMP_JANITOR_INTERVAL_SECONDS):
        return {"deleted": [], "bytes_freed": 0, "skipped": "interval"}
    _last_discovery_temp_janitor_run = now
    cutoff = now - float(DISCOVERY_TEMP_JANITOR_MAX_AGE_SECONDS)
    refs = _active_job_temp_refs()
    candidates: list[Path] = []
    for pattern in ("vs_quick_*", "word_highlight_overlay_*", "track_*"):
        candidates.extend(Path("/tmp").glob(pattern))
    tmp_dir = Path(VIDEO_SHORTS_TMP_DIR)
    if tmp_dir.exists():
        if "__processing_job__" not in refs:
            candidates.extend(path for path in tmp_dir.iterdir() if path.is_file())
        else:
            logger.info("Discovery temp janitor skipped %s files because a job is processing", tmp_dir)

    deleted: list[str] = []
    bytes_freed = 0
    for path in sorted(set(candidates), key=lambda item: str(item)):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            continue
        if _path_is_referenced(path, refs):
            continue
        freed = _delete_path(path)
        if freed or not path.exists():
            bytes_freed += freed
            deleted.append(str(path))
    if deleted:
        logger.info(
            "Discovery temp janitor deleted %s path(s), freed %s bytes: %s",
            len(deleted),
            bytes_freed,
            deleted,
        )
    else:
        logger.info("Discovery temp janitor deleted 0 paths, freed 0 bytes")
    return {"deleted": deleted, "bytes_freed": bytes_freed}


def _in_discovery_night_window(now_utc: Optional[datetime] = None) -> bool:
    now_pt = (now_utc or datetime.now(timezone.utc)).astimezone(_PT)
    hour = now_pt.hour
    start = int(DISCOVERY_NIGHT_START_HOUR_PT)
    end = int(DISCOVERY_NIGHT_END_HOUR_PT)
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _log_idle_skip(reason: str) -> None:
    now = time.time()
    last = _last_idle_skip_log.get(reason, 0.0)
    if now - last >= float(DISCOVERY_IDLE_LOG_INTERVAL_SECONDS):
        logger.info("discovery worker skipped: %s", reason)
        _last_idle_skip_log[reason] = now


def discovery_idle_skip_reason(app, *, include_disk_gate: bool = False) -> Optional[str]:
    conn = get_db()
    try:
        if customer_job_waiting(conn):
            return "customer job queued/processing"
    finally:
        conn.close()

    mem_available = _mem_available_mb()
    if mem_available < int(DISCOVERY_IDLE_MEM_AVAILABLE_MB_MIN):
        return f"MemAvailable {mem_available}MB below {DISCOVERY_IDLE_MEM_AVAILABLE_MB_MIN}MB"

    if include_disk_gate:
        usage_pct = _root_disk_usage_pct()
        if usage_pct > int(DISCOVERY_IDLE_DISK_USAGE_PCT_MAX):
            return f"root disk usage {usage_pct}% > {DISCOVERY_IDLE_DISK_USAGE_PCT_MAX}%"

    if _in_discovery_night_window():
        return None

    load_1m = os.getloadavg()[0]
    if load_1m >= float(DISCOVERY_IDLE_LOAD_1M_MAX):
        return f"load1 {load_1m:.2f} >= {DISCOVERY_IDLE_LOAD_1M_MAX:.2f}"

    recent_hits = _recent_real_web_activity(datetime.now(timezone.utc))
    if recent_hits:
        return f"{recent_hits} real web request(s) in last {DISCOVERY_IDLE_WEB_WINDOW_SECONDS}s"
    return None


def _discovery_worker_should_wait(app, *, include_disk_gate: bool = False) -> bool:
    reason = discovery_idle_skip_reason(app, include_disk_gate=include_disk_gate)
    if not reason:
        return False
    _log_idle_skip(reason)
    time.sleep(float(DISCOVERY_IDLE_WAIT_SECONDS))
    return True


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
        entries = generation._load_plan_entries(source_video_id, video_pk=payload.get("video_pk"))
        generation._update_plan_entry_job_state(
            source_video_id,
            entries,
            plan_index=int(plan_index),
            status="failed",
            error_message=error_message,
         video_pk=payload.get("video_pk"))
        generation._evaluate_lead_generation_approval_gate(
            owner_user_id=job.get("user_id"),
            brand_id=payload.get("brand_id"),
            video_pk=payload.get("video_pk"),
            video_id=source_video_id,
            failed_plan_index=int(plan_index),
            failure_message=error_message,
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
        entries = generation._load_plan_entries(source_video_id, video_pk=payload.get("video_pk"))
        generation._update_plan_entry_job_state(
            source_video_id,
            entries,
            plan_index=int(plan_index),
            status=status,
            render_job_id=job.get("id"),
         video_pk=payload.get("video_pk"))
    except Exception:
        pass


def _set_quick_session_state(session_id: Optional[str], **updates: Any) -> None:
    if not session_id:
        return
    try:
        update_session(session_id, **updates)
    except Exception:
        pass


def _session_result_status_done(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"completed", "ready", "skipped", "fallback"}


def _merge_session_nested(existing: Dict[str, Any], key: str, value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not value:
        return existing
    current = existing.get(key)
    merged_child = dict(current) if isinstance(current, dict) else {}
    merged_child.update(value)
    existing[key] = merged_child
    return existing


def _processing_message_from_result(result_payload: Dict[str, Any]) -> str:
    normalize_status = ((result_payload.get("normalize") or {}).get("status"))
    transcript_status = ((result_payload.get("transcript") or {}).get("status"))
    if not _session_result_status_done(normalize_status):
        return "Preparing your video on the server..."
    if not _session_result_status_done(transcript_status):
        return "Preparing your transcript..."
    return "Ready to review."


def _normalize_percent_payload(percent: Any) -> int:
    try:
        return max(0, min(100, int(round(float(percent)))))
    except (TypeError, ValueError):
        return 0


def _update_upload_session_progress(
    session_id: Optional[str],
    *,
    user_id: Optional[str],
    normalize: Optional[Dict[str, Any]] = None,
    transcript: Optional[Dict[str, Any]] = None,
    clip_updates: Optional[Dict[str, Any]] = None,
) -> None:
    if not session_id:
        return
    try:
        quick_session = get_session(session_id, user_id=user_id) or {}
        existing_result = dict((quick_session.get("result") or {}))
        _merge_session_nested(existing_result, "normalize", normalize)
        _merge_session_nested(existing_result, "transcript", transcript)
        normalize_done = _session_result_status_done(((existing_result.get("normalize") or {}).get("status")))
        transcript_done = _session_result_status_done(((existing_result.get("transcript") or {}).get("status")))
        updates: Dict[str, Any] = {"result": existing_result}
        if clip_updates:
            updates.update(clip_updates)
        if normalize_done and transcript_done:
            existing_result["stage"] = "ready"
            existing_result["message"] = "Ready to review."
            updates["status"] = STATUS_REVIEW
        else:
            existing_result["stage"] = "preparing"
            existing_result["message"] = _processing_message_from_result(existing_result)
            updates["status"] = STATUS_INGESTING
        update_session(session_id, **updates)
    except Exception:
        pass


def _mark_job_terminal_failure(job: Dict[str, Any], message: str) -> None:
    is_render = job.get("type") == JOB_TYPE_RENDER_SHORT
    if is_render:
        _mark_plan_failure(job, message)
    timing_json = render_timing.terminal_payload() if is_render else None
    try:
        mark_job_failed(
            job["id"],
            message,
            release_reservation=is_render and not (job.get("payload") or {}).get("regenerate"),
            timing_json=timing_json,
        )
    finally:
        if is_render:
            render_timing.clear()
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


def _transcription_progress_callback(
    job_id: str,
    *,
    session_id: str = "",
    user_id: str = "",
):
    stage_percent = {
        "start": 2,
        "language_probe": 5,
        "language_mode": 8,
        "prepare_audio": 10,
        "prepare_audio_done": 15,
        "prepare_chunks": 10,
        "prepare_chunks_done": 15,
        "whisper_start": 20,
        "language_passthrough": 96,
        "tagging": 97,
        "db_write": 99,
        "cleanup": 99,
        "done": 100,
    }

    def _progress(stage: str, message: str, extra: Dict[str, Any]) -> None:
        percent = stage_percent.get(str(stage or "").strip().lower())
        chunk_count = int((extra or {}).get("chunk_count") or 0)
        chunk_index = int((extra or {}).get("chunk_index") or 0)
        if chunk_count > 0 and chunk_index > 0:
            completed = chunk_index if str(stage).endswith("_done") else max(0, chunk_index - 1)
            percent = 20 + int(round(75 * min(completed, chunk_count) / chunk_count))
        progress = {
            "percent": percent,
            "transcription_stage": stage,
            **(extra or {}),
        }
        _set_job_progress(
            job_id,
            stage="transcribing",
            message=message or "Transcribing source audio.",
            status="processing",
            extra=progress,
        )
        if session_id:
            _update_upload_session_progress(
                session_id,
                user_id=user_id,
                transcript={
                    "status": "processing",
                    "message": message or "Preparing your transcript...",
                    "percent": _normalize_percent_payload(percent),
                },
            )

    return _progress


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


def _source_video_quota_message(duration_seconds: Any, quota: Dict[str, Any]) -> str:
    if quota.get("reason") == "video_duration":
        return quick_short_routes._duration_limit_message(
            duration_seconds=int(float(duration_seconds or 0)),
            plan_label=str(quota.get("plan_label") or "Current"),
            limit_seconds=int(quota.get("max_duration_seconds") or 0),
        )
    return _transcription_quota_message(duration_seconds, quota.get("remaining_minutes"))


def _enforce_source_video_quota(owner_user_id: str, duration_seconds: Any) -> None:
    if _duration_minutes(duration_seconds) <= 0:
        return
    quota = check_source_video_quota(owner_user_id, duration_seconds)
    if not quota.get("allowed", False):
        raise PermanentRenderJobError(_source_video_quota_message(duration_seconds, quota))

def _inject_sessid(proxy_url: str, sessid: str) -> str:
    """DataImpulse proxy URL'ine ;sessid ekle — username sonuna, şifreden önce.

    Girdi:  http://<user>__cr.us:<pass>@gw.dataimpulse.com:823
    Çıktı:  http://<user>__cr.us;sessid.<id>:<pass>@gw.dataimpulse.com:823

    sessid, o indirme boyunca aynı IP'yi tutar (~30 dk) → uzun video kesilmez.
    Her yeni çağrı yeni sessid → yeni IP → retry'da kötü IP'den kaçış.
    Parse edilemezse orijinal URL döner (proxy yine çalışır, sadece sessid'siz).
    """
    try:
        scheme, rest = proxy_url.split("://", 1)
        creds, hostpart = rest.split("@", 1)
        user, password = creds.split(":", 1)  # ilk ':' şifreyi ayırır
        return f"{scheme}://{user};sessid.{sessid}:{password}@{hostpart}"
    except Exception:
        return proxy_url

def _download_youtube_video(
    video_url: str,
    video_id: str,
    *,
    progress_cb=None,
) -> Path:
    if yt_dlp is None:
        raise PermanentRenderJobError("yt_dlp is not installed on the worker.")
    work_dir = Path(tempfile.mkdtemp(prefix=f"vs_quick_{video_id}_"))
    target = work_dir / f"{video_id}.mp4"
    proxy_url = str(os.getenv("INGEST_PROXY_URL") or "").strip()
    cookies_path = str(os.getenv("INGEST_PROXY_COOKIES") or "").strip()
    download_parts: Dict[str, Dict[str, float]] = {}
    last_progress_write = 0.0

    def _download_progress(update: Dict[str, Any]) -> None:
        """Collapse yt-dlp's separate video/audio transfers into one progress value."""
        nonlocal last_progress_write
        if not progress_cb or not isinstance(update, dict):
            return
        info = update.get("info_dict") or {}
        for requested in info.get("requested_formats") or []:
            if not isinstance(requested, dict):
                continue
            part_id = str(requested.get("format_id") or requested.get("url") or len(download_parts))
            known_total = requested.get("filesize") or requested.get("filesize_approx") or 0
            part = download_parts.setdefault(part_id, {"downloaded": 0.0, "total": 0.0})
            if known_total:
                part["total"] = max(part["total"], float(known_total))

        part_id = str(info.get("format_id") or update.get("filename") or "download")
        part = download_parts.setdefault(part_id, {"downloaded": 0.0, "total": 0.0})
        downloaded = float(update.get("downloaded_bytes") or 0)
        total = float(update.get("total_bytes") or update.get("total_bytes_estimate") or 0)
        part["downloaded"] = max(part["downloaded"], downloaded)
        if total:
            part["total"] = max(part["total"], total)
        if update.get("status") == "finished" and part["total"]:
            part["downloaded"] = max(part["downloaded"], part["total"])

        now = time.monotonic()
        if (now - last_progress_write) < 2.0:
            return
        known_parts = [value for value in download_parts.values() if value["total"] > 0]
        total_bytes = sum(value["total"] for value in known_parts)
        downloaded_bytes = sum(min(value["downloaded"], value["total"]) for value in known_parts)
        percent = round((downloaded_bytes / total_bytes) * 100, 1) if total_bytes else None
        speed = update.get("speed")
        eta = update.get("eta")
        if speed and total_bytes and downloaded_bytes < total_bytes:
            eta = max(0, int(round((total_bytes - downloaded_bytes) / float(speed))))
        last_progress_write = now
        progress_cb(
            {
                "percent": percent,
                "downloaded_bytes": int(downloaded_bytes or downloaded),
                "total_bytes": int(total_bytes or total),
                "speed_bps": int(float(speed)) if speed else None,
                "eta_seconds": int(float(eta)) if eta is not None else None,
                "transfer_status": str(update.get("status") or "downloading"),
            }
        )

    opts = {
        "outtmpl": str(work_dir / f"{video_id}.%(ext)s"),
        "merge_output_format": "mp4",
        # Keep resolution ahead of frame rate. Slash-separated 30/60 fps
        # alternatives would accept a low-resolution 30 fps format before yt-dlp
        # ever considers a 60 fps HD format. Bounding both dimensions at 1920
        # admits 1920x1080 and its 1080x1920 vertical equivalent, but not 1440p/4K.
        "format": (
            "bestvideo[vcodec^=avc1][width<=1920][height<=1920][fps<=60]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1][width<=1920][height<=1920][fps<=60]+bestaudio/"
            "bestvideo[width<=1920][height<=1920][fps<=60]+bestaudio[ext=m4a]/"
            "bestvideo[width<=1920][height<=1920][fps<=60]+bestaudio/"
            "best[width<=1920][height<=1920]"
        ),
        "format_sort": ["res:1080", "fps:30"],
        "quiet": True,
        "noprogress": True,
        "legacy_server_connect": True,
        "nocheckcertificate": True,
        "retries": 5,
        "fragment_retries": 5,
        "progress_hooks": [_download_progress],

        "extractor_args": {
            "youtube": {
                "player_client": ["mweb", "default"]
            }
        },
        "js_runtimes": {"node": {}},
    }

    if proxy_url:
        session_id = secrets.token_hex(4)          # her indirmede yeni IP
        proxy_url = _inject_sessid(proxy_url, session_id)
        opts["proxy"] = proxy_url
        _log_exit_ip(proxy_url, video_id)

    if cookies_path:
        opts["cookiefile"] = cookies_path
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
        if target.exists():
            return target
        candidates = sorted(work_dir.glob(f"{video_id}.*"))
        if not candidates:
            raise FileNotFoundError(f"download output missing for {video_id}")
        candidates[0].rename(target)
        return target
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _log_exit_ip(proxy_url: str, video_id: str) -> None:
    """Proxy üstünden çıkış IP'sini logla (şifre loglanmaz)."""
    try:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler)
        ip = opener.open("https://api.ipify.org", timeout=10).read().decode().strip()
        logger.info("INGEST proxy exit IP for %s: %s", video_id, ip)
    except Exception as e:
        logger.warning("INGEST exit IP check failed for %s: %s", video_id, e)

def _download_youtube_audio_with_proxy(video_url: str, video_id: str) -> tuple[Path, bool, bool]:
    """Download only the source audio into a disposable worker directory."""
    if yt_dlp is None:
        raise PermanentRenderJobError("yt_dlp is not installed on the worker.")
    work_dir = Path(tempfile.mkdtemp(prefix=f"vs_proxy_audio_{video_id}_"))
    proxy_url = str(os.getenv("INGEST_PROXY_URL") or "").strip()
    cookies_path = str(os.getenv("INGEST_PROXY_COOKIES") or "").strip()
    opts = {
        "outtmpl": str(work_dir / f"{video_id}.%(ext)s"),
        "format": "bestaudio/best",
        "quiet": True,
        "noprogress": True,
    }
    if proxy_url:
        opts["proxy"] = proxy_url
    if cookies_path:
        opts["cookiefile"] = cookies_path
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
        candidates = sorted(path for path in work_dir.glob(f"{video_id}.*") if path.is_file())
        if not candidates:
            raise FileNotFoundError(f"audio download output missing for {video_id}")
        return candidates[0], bool(proxy_url), bool(cookies_path)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _is_unconverted_discovery_demo_transcription(
    *,
    owner_user_id: str,
    brand_id: str,
    video_pk: int,
    job_origin: str = "",
) -> bool:
    if str(job_origin or "").strip() == "discovery_demo":
        return True
    clean_owner = str(owner_user_id or "").strip()
    clean_brand = str(brand_id or "").strip()
    if not clean_owner or not clean_brand or not video_pk:
        return False
    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM autopilot_leads
            WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND first_video_id = ?
              AND converted_at IS NULL
            LIMIT 1
            """,
            [clean_owner, clean_brand, int(video_pk)],
        ).fetchone()
        return bool(row)
    except Exception:
        logger.debug(
            "Could not resolve discovery-demo transcription scope user=%s brand=%s video_pk=%s",
            clean_owner,
            clean_brand,
            video_pk,
            exc_info=True,
        )
        return False
    finally:
        conn.close()


def _save_transcript(
    video_id: str,
    *,
    full_text: str,
    segments: list[dict],
    owner_user_id: Optional[str],
    duration_seconds: Any,
    bill_usage: bool = True,
) -> None:
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
    try:
        write_transcript_cache(video_id, full_text=full_text, segments=segments)
    except Exception:
        logger.warning("free_tool_text_cache transcript hook failed video_id=%s", video_id, exc_info=True)
    if should_emit_transcript_completed and owner_user_id:
        track_event(
            str(owner_user_id),
            "transcript_completed",
            video_id=event_video_id or video_id,
            status="completed",
        )
    if owner_user_id and bill_usage:
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


def _execute_admin_proxy_transcript_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    """Transcribe a lead source through yt-dlp audio only; never write source video to S3."""
    payload = job.get("payload") or {}
    video_pk = int(payload.get("video_pk") or 0)
    video_id = str(payload.get("video_id") or "").strip()
    video_url = str(payload.get("video_url") or "").strip()
    owner_user_id = str(job.get("user_id") or "").strip()
    brand_id = str(payload.get("brand_id") or "").strip()
    job_origin = str(payload.get("job_origin") or "").strip()
    previous_status = str(payload.get("previous_download_status") or "pending").strip().lower() or "pending"
    if not video_pk or not video_id or not video_url or not owner_user_id or not brand_id:
        raise PermanentRenderJobError("Proxy transcript job is missing its scoped source.")

    audio_path: Optional[Path] = None
    result: Dict[str, Any] = {
        "proxy_used": bool(str(os.getenv("INGEST_PROXY_URL") or "").strip()),
        "cookies_configured": bool(str(os.getenv("INGEST_PROXY_COOKIES") or "").strip()),
        "audio_size_mb": None,
        "transcript_status": "processing",
        "segment_count": 0,
        "error": None,
    }
    try:
        _set_job_progress(job["id"], stage="downloading_audio", message="Downloading source audio.", status="processing", extra=result)
        audio_path, proxy_used, cookies_configured = _download_youtube_audio_with_proxy(video_url, video_id)
        result["proxy_used"] = proxy_used
        result["cookies_configured"] = cookies_configured
        result["audio_size_mb"] = round(audio_path.stat().st_size / (1024 * 1024), 2)
        _set_job_progress(job["id"], stage="transcribing", message="Transcribing source audio.", status="processing", extra=result)
        transcript_text, segments = _transcribe_with_whisper(audio_path)
        bill_usage = not _is_unconverted_discovery_demo_transcription(
            owner_user_id=owner_user_id,
            brand_id=brand_id,
            video_pk=video_pk,
            job_origin=job_origin,
        )
        _save_transcript(
            video_id,
            full_text=transcript_text,
            segments=segments,
            owner_user_id=owner_user_id,
            duration_seconds=payload.get("duration_seconds"),
            bill_usage=bill_usage,
        )
        conn = get_db()
        try:
            updated = conn.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'audio_only', transcript_status = 'done', last_checked_at = CURRENT_TIMESTAMP
                WHERE id = ? AND owner_user_id = ? AND brand_id = ?
                  AND download_status = 'audio_transcribing'
                """,
                [video_pk, owner_user_id, brand_id],
            )
            if getattr(updated, "rowcount", 1) == 0:
                raise PermanentRenderJobError("Proxy transcript source scope changed before completion.")
            conn.commit()
        finally:
            conn.close()
        result.update({"transcript_status": "done", "segment_count": len(segments or [])})
        return result
    except Exception as exc:
        result.update({"transcript_status": "failed", "error": str(exc)})
        try:
            conn = get_db()
            try:
                conn.execute(
                    """
                    UPDATE youtube_videos
                    SET download_status = ?, last_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND owner_user_id = ? AND brand_id = ?
                      AND download_status = 'audio_transcribing'
                    """,
                    [previous_status, video_pk, owner_user_id, brand_id],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            app.logger.exception("Failed to restore proxy transcript source status video_pk=%s", video_pk)
        update_job_result(job["id"], result)
        if isinstance(exc, PermanentRenderJobError):
            raise
        raise PermanentRenderJobError(f"Proxy audio transcription failed: {exc}") from exc
    finally:
        if audio_path:
            shutil.rmtree(audio_path.parent, ignore_errors=True)


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
    brand_id = str(payload.get("brand_id") or "").strip()
    duration_seconds = payload.get("duration_seconds")
    job_origin = str(payload.get("job_origin") or "").strip()
    ingest_source = str(payload.get("ingest_source") or "").strip()
    quota_exempt = bool(payload.get("quota_exempt")) or is_transcription_quota_exempt_user(owner_user_id)
    bill_usage = not quota_exempt and not _is_unconverted_discovery_demo_transcription(
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        video_pk=video_pk,
        job_origin=job_origin,
    )
    if not video_pk or not video_id or not video_url or not owner_user_id or not brand_id:
        raise PermanentRenderJobError("YouTube ingest job is missing its scoped source.")
    if bill_usage:
        _enforce_source_video_quota(owner_user_id, duration_seconds)
    _set_quick_session_state(session_id, status=STATUS_INGESTING)
    _set_job_progress(job["id"], stage="queued", message="Queued for ingest.", status="queued")
    _set_job_progress(job["id"], stage="downloading", message="Downloading the source video.", status="processing")
    storage = get_media_storage()
    source_key = next((f"videos/{video_id}{suffix}" for suffix in tenant_media.SOURCE_VIDEO_SUFFIXES
                       if storage.exists(f"videos/{video_id}{suffix}")), None)
    if source_key:
        local_path = storage.download_to_temp(source_key)
        if getattr(storage, "backend_name", "local") == "local":
            copied = Path(tempfile.mkdtemp(prefix="ingest-existing-")) / Path(source_key).name
            shutil.copyfile(local_path, copied)
            local_path = copied
    else:
        def _record_download_progress(progress: Dict[str, Any]) -> None:
            _set_job_progress(
                job["id"],
                stage="downloading",
                message="Downloading from YouTube.",
                status="processing",
                extra=progress,
            )

        local_path = _download_youtube_video(
            video_url,
            video_id,
            progress_cb=_record_download_progress,
        )
    try:
        try:
            exact_media_duration = probe_exact_media_duration_seconds(
                local_path,
                context=f"youtube_ingest video_pk={video_pk} video_id={video_id}",
            )
        except Exception:
            app.logger.warning("Exact media duration probe failed video_pk=%s", video_pk, exc_info=True)
            exact_media_duration = None
        effective_duration_seconds = exact_media_duration or duration_seconds
        if bill_usage:
            _enforce_source_video_quota(owner_user_id, effective_duration_seconds)
        if not source_key:
            source_key = f"videos/{video_id}{local_path.suffix or '.mp4'}"
            put_file_for_owner(local_path, source_key, owner_user_id=owner_user_id)
        conn = get_db()
        try:
            updated = conn.execute(
                """
                UPDATE youtube_videos
                SET download_status = 'downloaded',
                    video_url = ?,
                    media_duration_seconds = ?,
                    downloaded_at = CURRENT_TIMESTAMP,
                    last_checked_at = CURRENT_TIMESTAMP
                WHERE id = ? AND owner_user_id = ? AND brand_id = ?
                """,
                [build_storage_reference(source_key), exact_media_duration, video_pk, owner_user_id, brand_id],
            )
            if getattr(updated, "rowcount", 1) == 0:
                raise PermanentRenderJobError("YouTube ingest source scope changed before S3 upload completion.")
            conn.commit()
            try:
                record_lead_pipeline_event_for_scope(
                    owner_user_id=owner_user_id,
                    brand_id=brand_id,
                    video_pk=video_pk,
                    event_type="download_completed",
                    to_state="downloaded",
                    detail={
                        "video_pk": video_pk,
                        "video_id": video_id,
                        "job_id": job.get("id"),
                        "source_key": source_key,
                    },
                )
            except Exception as exc:
                app.logger.warning("Failed to record lead download event video_pk=%s job_id=%s: %s", video_pk, job.get("id"), exc)
        finally:
            conn.close()
        enqueue_preview_frame_job(
            owner_user_id=owner_user_id,
            brand_id=brand_id,
            video_pk=video_pk,
            source_key=source_key,
            duration_seconds=effective_duration_seconds,
        )
        text_cache = load_text_cache(video_id) if job_origin == GUEST_JOB_ORIGIN else None
        if text_cache and text_cache.get("full_text") and text_cache.get("segments"):
            transcript_text = str(text_cache.get("full_text") or "")
            segments = list(text_cache.get("segments") or [])
            _set_job_progress(job["id"], stage="transcribing", message="Transcript cache hit.", status="processing")
            _save_transcript(
                video_id,
                full_text=transcript_text,
                segments=segments,
                owner_user_id=owner_user_id,
                duration_seconds=effective_duration_seconds,
                bill_usage=False,
            )
            app.logger.info("free_tool_text_cache transcript HIT video_id=%s video_pk=%s", video_id, video_pk)
        else:
            _set_job_progress(job["id"], stage="transcribing", message="Transcribing with Whisper.", status="processing")
            transcript_text, segments = _transcribe_with_whisper(
                local_path,
                progress_cb=_transcription_progress_callback(
                    job["id"],
                    session_id=session_id,
                    user_id=owner_user_id,
                ),
            )
            _save_transcript(
                video_id,
                full_text=transcript_text,
                segments=segments,
                owner_user_id=owner_user_id,
                duration_seconds=effective_duration_seconds,
                bill_usage=bill_usage,
            )
            try:
                record_lead_pipeline_event_for_scope(
                    owner_user_id=owner_user_id,
                    brand_id=brand_id,
                    video_pk=video_pk,
                    event_type="transcript_completed",
                    to_state="downloaded",
                    detail={
                        "video_pk": video_pk,
                        "video_id": video_id,
                        "job_id": job.get("id"),
                        "source": "ingest_youtube",
                    },
                )
            except Exception as exc:
                app.logger.warning("Failed to record lead transcript event video_pk=%s job_id=%s: %s", video_pk, job.get("id"), exc)
        try:
            enqueue_auto_result = generation.enqueue_auto_suggest_render_job_after_transcript(
                owner_user_id=owner_user_id,
                brand_id=brand_id,
                video_pk=video_pk,
                video_id=video_id,
                job_origin=job_origin,
                ingest_source=ingest_source,
                admin_job_metadata=(
                    {
                        key: payload.get(key)
                        for key in ("job_origin", "made_by_admin", "quota_exempt", "acting_admin_id", "admin_operation_kind")
                        if payload.get(key) is not None
                    }
                    if quota_exempt
                    else None
                ),
            )
            if enqueue_auto_result:
                app.logger.info(
                    "auto_suggest_render_enqueue video_id=%s video_pk=%s kind=%s job_id=%s",
                    video_id,
                    video_pk,
                    enqueue_auto_result.get("kind"),
                    (enqueue_auto_result.get("job") or {}).get("id"),
                )
        except Exception as exc:
            app.logger.warning("Failed to enqueue auto_suggest_render video_pk=%s: %s", video_pk, exc)
        clip_start, clip_end, clip_title, excerpt = _suggest_clip(segments, effective_duration_seconds)
        enqueue_preview_frame_job(
            owner_user_id=owner_user_id,
            brand_id=brand_id,
            video_pk=video_pk,
            source_key=source_key,
            duration_seconds=effective_duration_seconds,
            clip_start_seconds=clip_start,
            clip_end_seconds=clip_end,
        )
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
    source_key = str(payload.get("source_key") or "").strip()
    video_pk = int(payload.get("video_pk"))
    owner_user_id = str(job["user_id"])
    brand_id = str(payload.get("brand_id") or "").strip()
    duration_seconds = payload.get("duration_seconds")
    job_origin = str(payload.get("job_origin") or "").strip()
    ingest_source = str(payload.get("ingest_source") or "").strip()
    quota_exempt = bool(payload.get("quota_exempt")) or is_transcription_quota_exempt_user(owner_user_id)
    bill_usage = not quota_exempt and not _is_unconverted_discovery_demo_transcription(
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        video_pk=video_pk,
        job_origin=job_origin,
    )
    if bill_usage:
        _enforce_source_video_quota(owner_user_id, duration_seconds)
    _set_quick_session_state(session_id, status=STATUS_INGESTING)
    _update_upload_session_progress(
        session_id,
        user_id=owner_user_id,
        transcript={"status": "processing", "message": "Preparing your transcript...", "percent": 5},
    )
    _set_job_progress(job["id"], stage="uploaded", message="Preparing your video.", status="processing")
    source_path, is_temp = _resolve_source_video(video_id, video_pk=video_pk)
    if not source_path or not source_path.exists():
        raise PermanentRenderJobError("Uploaded source file could not be found.")
    try:
        get_or_backfill_media_duration_seconds(video_pk, video_id, source_path=source_path)
        _set_job_progress(job["id"], stage="transcribing", message="Preparing your transcript.", status="processing")
        transcript_text, segments = _transcribe_with_whisper(
            source_path,
            progress_cb=_transcription_progress_callback(
                job["id"],
                session_id=session_id,
                user_id=owner_user_id,
            ),
        )
        _save_transcript(
            video_id,
            full_text=transcript_text,
            segments=segments,
            owner_user_id=owner_user_id,
            duration_seconds=duration_seconds,
            bill_usage=bill_usage,
        )
        try:
            enqueue_auto_result = generation.enqueue_auto_suggest_render_job_after_transcript(
                owner_user_id=owner_user_id,
                brand_id=brand_id,
                video_pk=video_pk,
                video_id=video_id,
                job_origin=job_origin,
                ingest_source=ingest_source,
                admin_job_metadata=(
                    {
                        key: payload.get(key)
                        for key in ("job_origin", "made_by_admin", "quota_exempt", "acting_admin_id", "admin_operation_kind")
                        if payload.get(key) is not None
                    }
                    if quota_exempt
                    else None
                ),
            )
            if enqueue_auto_result:
                app.logger.info(
                    "auto_suggest_render_enqueue video_id=%s video_pk=%s kind=%s job_id=%s",
                    video_id,
                    video_pk,
                    enqueue_auto_result.get("kind"),
                    (enqueue_auto_result.get("job") or {}).get("id"),
                )
        except Exception as exc:
            app.logger.warning("Failed to enqueue auto_suggest_render video_pk=%s: %s", video_pk, exc)
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
            try:
                record_lead_pipeline_event_for_scope(
                    owner_user_id=owner_user_id,
                    brand_id=brand_id,
                    video_pk=video_pk,
                    event_type="transcript_completed",
                    to_state="downloaded",
                    detail={
                        "video_pk": video_pk,
                        "video_id": video_id,
                        "job_id": job.get("id"),
                        "source": "transcribe_upload",
                    },
                )
            except Exception as exc:
                app.logger.warning("Failed to record lead transcript event video_pk=%s job_id=%s: %s", video_pk, job.get("id"), exc)
        finally:
            conn.close()
        if source_key and brand_id:
            enqueue_preview_frame_job(
                owner_user_id=owner_user_id,
                brand_id=brand_id,
                video_pk=video_pk,
                source_key=source_key,
                duration_seconds=duration_seconds,
                clip_start_seconds=clip_start,
                clip_end_seconds=clip_end,
            )
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
            video_pk=video_pk,
            video_id=video_id,
            clip_start_seconds=clip_start,
            clip_end_seconds=clip_end,
            clip_title=clip_title,
        )
        _update_upload_session_progress(
            session_id,
            user_id=owner_user_id,
            transcript={"status": "completed", "message": "Transcript ready.", "percent": 100},
            clip_updates={
                "video_pk": video_pk,
                "video_id": video_id,
                "clip_start_seconds": clip_start,
                "clip_end_seconds": clip_end,
                "clip_title": clip_title,
            },
        )
        return result
    finally:
        _cleanup_resolved_source_video(source_path, is_temp)


def _execute_normalize_upload_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload") or {}
    session_id = str(payload.get("quick_session_id") or "").strip()
    video_id = str(payload.get("video_id") or "").strip()
    source_key = str(payload.get("source_key") or "").strip()
    video_pk = int(payload.get("video_pk") or 0)
    owner_user_id = str(payload.get("owner_user_id") or job.get("user_id") or "").strip()
    brand_id = str(payload.get("brand_id") or "").strip()
    duration_seconds = payload.get("duration_seconds")
    if not video_id or not source_key or not video_pk or not owner_user_id or not brand_id:
        app.logger.warning(
            "normalize fallback job_id=%s video_id=%s key=%s error=missing-payload",
            job.get("id"),
            video_id,
            source_key,
        )
        return {"normalized": False, "reason": "missing-payload"}

    storage = get_media_storage()
    source_path = None
    try:
        def enqueue_preview(preview_source_key: str) -> Dict[str, Any]:
            try:
                return enqueue_preview_frame_job(
                    owner_user_id=owner_user_id,
                    brand_id=brand_id,
                    video_pk=video_pk,
                    source_key=preview_source_key,
                    duration_seconds=duration_seconds,
                )
            except Exception as preview_exc:
                app.logger.warning(
                    "preview enqueue skipped after normalize job_id=%s video_id=%s key=%s error=%s",
                    job.get("id"),
                    video_id,
                    preview_source_key,
                    preview_exc,
                )
                return {"kind": "error", "error": str(preview_exc)}

        def handle_normalize_progress(percent: int) -> None:
            _update_upload_session_progress(
                session_id,
                user_id=owner_user_id,
                normalize={
                    "status": "processing",
                    "message": "Preparing your video on the server...",
                    "percent": _normalize_percent_payload(percent),
                },
            )

        _update_upload_session_progress(
            session_id,
            user_id=owner_user_id,
            normalize={"status": "processing", "message": "Preparing your video on the server...", "percent": 0},
        )
        source_path = resolve_s3_source_video_cached(video_id, source_key, storage=storage)
        if not source_path:
            raise PermanentRenderJobError("Uploaded source file could not be found in S3.")
        new_key = normalize_s3_source_video_for_upload(
            video_id,
            source_key,
            Path(source_path),
            target_key=source_key,
            log=app.logger,
            progress_callback=handle_normalize_progress,
            owner_user_id=owner_user_id,
        )
        if not new_key:
            _update_upload_session_progress(
                session_id,
                user_id=owner_user_id,
                normalize={"status": "completed", "message": "Video preparation finished.", "percent": 100},
            )
            return {
                "normalized": False,
                "reason": "already-streamable-or-fallback",
                "db_field": "youtube_videos.video_url",
                "preview": enqueue_preview(source_key),
            }
        conn = get_db()
        try:
            updated = conn.execute(
                """
                UPDATE youtube_videos
                SET video_url = ?
                WHERE id = ? AND owner_user_id = ? AND brand_id = ?
                """,
                [build_storage_reference(new_key), video_pk, owner_user_id, brand_id],
            )
            if getattr(updated, "rowcount", 1) == 0:
                raise PermanentRenderJobError("Upload normalize source scope changed before completion.")
            conn.commit()
        finally:
            conn.close()
        _update_upload_session_progress(
            session_id,
            user_id=owner_user_id,
            normalize={"status": "completed", "message": "Video preparation finished.", "percent": 100},
        )
        return {
            "normalized": True,
            "source_key": source_key,
            "streamable_key": new_key,
            "db_field": "youtube_videos.video_url",
            "preview": enqueue_preview(new_key),
        }
    except PermanentRenderJobError:
        raise
    except Exception as exc:
        app.logger.warning(
            "normalize fallback job_id=%s video_id=%s key=%s error=%s",
            job.get("id"),
            video_id,
            source_key,
            exc,
        )
        _update_upload_session_progress(
            session_id,
            user_id=owner_user_id,
            normalize={"status": "fallback", "message": "Video preparation finished.", "percent": 100},
        )
        return {
            "normalized": False,
            "reason": "fallback",
            "source_key": source_key,
            "db_field": "youtube_videos.video_url",
            "preview": enqueue_preview(source_key),
        }
    finally:
        _cleanup_resolved_source_video(
            Path(source_path) if source_path else None,
            bool(source_path) and getattr(storage, "backend_name", "local") != "local",
        )


def _execute_preview_frame_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload") or {}
    video_pk = int(payload.get("video_pk") or 0)
    video_id = str(payload.get("video_id") or "").strip()
    source_key = str(payload.get("source_key") or "").strip().lstrip("/")
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    brand_id = str(payload.get("brand_id") or "").strip()
    duration_seconds = payload.get("duration_seconds")
    clip_start_seconds = payload.get("clip_start_seconds")
    clip_end_seconds = payload.get("clip_end_seconds")
    if not video_pk or not video_id or not source_key or not owner_user_id or not brand_id:
        raise PermanentRenderJobError("Preview frame job is missing its scoped source.")
    if str(job.get("user_id") or "").strip() != owner_user_id:
        raise PermanentRenderJobError("Preview frame job user scope does not match payload.")

    expected_prefix = f"videos/{video_id}."
    if not source_key.startswith(expected_prefix) or "/" in source_key[len("videos/") :]:
        raise PermanentRenderJobError("Preview frame source key does not match the scoped video.")

    conn = get_db_readonly()
    try:
        row = conn.execute(
            """
            SELECT video_id, video_url, duration_seconds
            FROM youtube_videos
            WHERE id = ? AND owner_user_id = ? AND brand_id = ?
            """,
            [video_pk, owner_user_id, brand_id],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise PermanentRenderJobError("Preview frame source was not found in tenant scope.")
    row_video_id = str(row[0] or "").strip()
    row_source = str(row[1] or "").strip()
    if row_video_id != video_id:
        raise PermanentRenderJobError("Preview frame source video changed before processing.")
    if is_storage_reference(row_source) and storage_reference_key(row_source) != source_key:
        raise PermanentRenderJobError("Preview frame source key changed before processing.")
    if duration_seconds is None:
        duration_seconds = row[2]

    storage = get_media_storage()
    source_path = None
    try:
        source_path = resolve_s3_source_video_cached(video_id, source_key, storage=storage)
        if not source_path:
            raise PermanentRenderJobError("Preview frame source file could not be found in S3.")
        with app.app_context():
            preview_path = generation._ensure_preview_frame(video_id, Path(source_path), duration_seconds)
            if not preview_path:
                raise RuntimeError("Preview frame was not created.")
            track_path = generation._ensure_preview_face_track(
                video_id,
                Path(source_path),
                clip_start_seconds,
                clip_end_seconds,
                duration_seconds,
            )
            applied_crop = generation._maybe_apply_face_centered_default_crop(
                video_row_id=video_pk,
                video_id=video_id,
                owner_user_id=owner_user_id,
                brand_id=brand_id,
                preview_metadata=generation._load_preview_frame_metadata(video_id),
            )
        return {
            "video_pk": video_pk,
            "video_id": video_id,
            "source_key": source_key,
            "preview_path": str(preview_path),
            "metadata_path": str(generation._preview_frame_metadata_path(video_id)),
            "track_path": str(track_path) if track_path else None,
            "track_smooth_path": (
                str(generation._preview_face_track_smooth_path(video_id, clip_start_seconds, clip_end_seconds))
                if track_path and generation._preview_face_track_smooth_path(video_id, clip_start_seconds, clip_end_seconds).exists()
                else None
            ),
            "crop_applied": bool(applied_crop),
        }
    finally:
        _cleanup_resolved_source_video(
            Path(source_path) if source_path else None,
            bool(source_path) and getattr(storage, "backend_name", "local") != "local",
        )


def _execute_render_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    from app.video_shorts.services.render_jobs import validate_render_job_scope
    validate_render_job_scope(job)
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
                "_regenerate_job": "1" if payload.get("regenerate") else "",
                "_render_settings_hash": str(payload.get("render_settings_hash") or ""),
                "_render_settings_options": json.dumps(payload.get("options") or {}, separators=(",", ":")),
                "_discovery_demo_job": "1" if str(payload.get("job_origin") or "").strip() == "discovery_demo" else "",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        ):
            g.vs_current_user = user
            if brand:
                g.vs_current_brand = brand
            try:
                completed = _render_job_completion_from_assets(job)
                if completed:
                    return completed
                guarded = generation._guard_youtube_clip_mutation(
                    video_pk, plan_index, state="rendering"
                )
                result = guarded if guarded is not None else generation.autoclip_video(video_pk)
            finally:
                generation._release_youtube_clip_mutation_lease()
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
    if payload.get("youtube_schedule_id"):
        user, brand = _load_user_context(job["user_id"], payload.get("brand_id"))
        with app.test_request_context("/video_shorts/youtube/deferred", method="POST"):
            g.vs_current_user = user
            g.vs_current_brand = brand
            return youtube_scheduling.execute_scheduled_upload(payload["youtube_schedule_id"])
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

    from app.video_shorts.services.media_access import authorize_file
    authorize_file(clip_filename, owner_user_id=user.get("id"), brand_id=brand_id, video_pk=video_pk)

    clip_path = generation._resolve_short_path_for_processing(clip_filename)
    if not clip_path or not clip_path.exists():
        raise PermanentRenderJobError("Short file could not be found for publishing.")

    if target == "youtube":
        if not has_refresh_token(user.get("id"), brand_id=brand_id):
            raise PermanentRenderJobError("Connect YouTube first.")
        _set_job_progress(job["id"], stage="uploading", message="Uploading to YouTube.", status="processing")
        try:
            response = youtube_scheduling.schedule_youtube_upload(
                video_path=str(clip_path),
                title=title,
                description=description,
                publish_at=publish_at_iso,
                privacy_status="private",
                user_id=user.get("id"),
                brand_id=brand_id,
                video_pk=video_pk, plan_index="1", publish_at_local=publish_at_local, clip_filename=clip_filename,
            ) or {}
        except Exception as exc:
            if "invalid_grant" in str(exc):
                raise PermanentRenderJobError("YouTube connection is invalid. Reconnect and try again.") from exc
            raise
        youtube_id = response.get("id")
        publish_status = response["status"]
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
        plan_entries = generation._load_plan_entries(video_id, video_pk=payload.get("video_pk")) or []
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


def process_next_job(
    app,
    worker_id: str,
    *,
    job_type: Optional[str] = None,
    customer_only: bool = False,
    discovery_only: bool = False,
) -> bool:
    if _worker_should_wait_before_claim(app):
        return False
    job = claim_next_job(
        worker_id,
        job_type=job_type,
        customer_only=customer_only,
        discovery_only=discovery_only,
    )
    if not job:
        return False
    if job.get("type") == JOB_TYPE_RENDER_SHORT:
        _update_plan_status(job, "processing")
        render_timing.begin(job)
    try:
        if job.get("type") == JOB_TYPE_INGEST_YOUTUBE:
            result = _execute_ingest_youtube_job(app, job)
        elif job.get("type") == JOB_TYPE_AUTO_SUGGEST_RENDER:
            _set_job_progress(
                job["id"],
                stage="finding_clips",
                message="Finding the best clips.",
                status="processing",
            )
            result = generation.execute_auto_suggest_render_job(job.get("payload") or {})
        elif job.get("type") == JOB_TYPE_ADMIN_PROXY_TRANSCRIPT:
            result = _execute_admin_proxy_transcript_job(app, job)
        elif job.get("type") == JOB_TYPE_NORMALIZE_UPLOAD:
            result = _execute_normalize_upload_job(app, job)
        elif job.get("type") == JOB_TYPE_PREVIEW_FRAME:
            result = _execute_preview_frame_job(app, job)
        elif job.get("type") == JOB_TYPE_TRANSCRIBE_UPLOAD:
            result = _execute_transcribe_upload_job(app, job)
        elif job.get("type") == JOB_TYPE_PUBLISH_SHORT:
            result = _execute_publish_job(app, job)
        elif job.get("type") == JOB_TYPE_INSTAGRAM_COMMENT_WEBHOOK:
            result = _execute_instagram_comment_webhook_job(app, job)
        elif job.get("type") == JOB_TYPE_ENRICH_DISCOVERY_EMAILS:
            result = enrich_discovery_email_batch(job.get("payload") or {})
        elif job.get("type") == JOB_TYPE_ENRICH_AUTOPILOT_DISCOVERY_EMAILS:
            result = enrich_autopilot_discovery_email_batch(job.get("payload") or {})
        elif job.get("type") == JOB_TYPE_PURGE_VIDEO:
            from app.video_shorts.services.video_deletion import execute_video_purge_job

            result = execute_video_purge_job(job.get("payload") or {})
        else:
            with render_job_heartbeat(job["id"], worker_id):
                result = _execute_render_job(app, job)
        try:
            timing_json = render_timing.terminal_payload() if job.get("type") == JOB_TYPE_RENDER_SHORT else None
            mark_job_done(job["id"], result, timing_json=timing_json)
            if job.get("type") == JOB_TYPE_RENDER_SHORT:
                render_timing.clear()
        except Exception:
            app.logger.exception("Failed to mark job done job_id=%s type=%s", job.get("id"), job.get("type"))
            raise
        if job.get("type") == JOB_TYPE_RENDER_SHORT:
            try:
                finalize_job_success(job["id"])
            except Exception:
                app.logger.exception("Render job finalize failed after done job_id=%s", job.get("id"))
            payload = job.get("payload") or {}
            if str(payload.get("job_origin") or "").strip() == GUEST_JOB_ORIGIN:
                source_video_id = str(payload.get("source_video_id") or "").strip()
                if source_video_id:
                    try:
                        purge_source_cache_for_video(source_video_id)
                        app.logger.info(
                            "guest source_cache purged after render job_id=%s video_id=%s",
                            job.get("id"),
                            source_video_id,
                        )
                    except Exception:
                        app.logger.warning(
                            "guest source_cache purge failed job_id=%s video_id=%s",
                            job.get("id"),
                            source_video_id,
                            exc_info=True,
                        )
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
            timing_accumulator = render_timing.retry_accumulator() if latest.get("type") == JOB_TYPE_RENDER_SHORT else None
            requeue_job(job["id"], str(exc), timing_accumulator=timing_accumulator)
            if latest.get("type") == JOB_TYPE_RENDER_SHORT:
                render_timing.clear()
        return True


def _render_job_completion_from_assets(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Recover a render only when this exact job wrote both lifecycle and asset rows."""
    payload = job.get("payload") or {}
    source_video_id = str(payload.get("source_video_id") or "").strip()
    brand_id = str(payload.get("brand_id") or "").strip()
    try:
        plan_index = int(payload.get("plan_index"))
    except (TypeError, ValueError):
        return None
    if not source_video_id or not brand_id or not job.get("user_id"):
        return None
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT gv.clip_filename,
                   gv.output_filename,
                   gv.raw_plan_entry_json,
                   gv.generation_status,
                   a.file_key,
                   a.size_bytes,
                   a.status
            FROM shorts_generated_videos gv
            LEFT JOIN shorts_storage_assets a
              ON a.file_key = gv.storage_file_key
             AND CAST(a.user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
             AND a.brand_id IS NOT DISTINCT FROM gv.brand_id
            WHERE CAST(gv.user_id AS VARCHAR) = ?
              AND gv.brand_id = ?
              AND CAST(gv.source_video_id AS VARCHAR) = ?
            ORDER BY gv.updated_at DESC
            """,
            [str(job["user_id"]), brand_id, source_video_id],
        ).fetchall()
    finally:
        conn.close()
    recovered_entry = None
    clip_filename = ""
    for row in rows:
        raw = row[2]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        raw = raw if isinstance(raw, dict) else {}
        try:
            raw_plan_index = int(raw.get("plan_index"))
        except (TypeError, ValueError):
            continue
        if raw_plan_index != plan_index or str(raw.get("render_job_id") or "") != str(job.get("id") or ""):
            continue
        if str(row[3] or "").strip().lower() not in {"created", "done"}:
            continue
        if not row[4] or int(row[5] or 0) <= 0 or str(row[6] or "active").lower() != "active":
            continue
        clip_filename = str(row[1] or row[0] or "").strip()
        if not clip_filename or not generation._short_exists(clip_filename):
            continue
        recovered_entry = raw
        break
    if not recovered_entry:
        return None

    entries = generation._load_plan_entries(source_video_id, video_pk=payload.get("video_pk")) or []
    target = next(
        (
            entry
            for entry in entries
            if str((entry or {}).get("plan_index") or "") == str(plan_index)
        ),
        None,
    )
    if target is None:
        return None
    target.update(recovered_entry)
    target["status"] = "created"
    target["render_job_id"] = str(job.get("id") or "")
    target.pop("render_error", None)
    generation._write_plan_entries(
        source_video_id, entries, video_pk=payload.get("video_pk")
    )
    logger.warning(
        "Recovered completed render from lifecycle asset job_id=%s clip=%s",
        job.get("id"),
        clip_filename,
    )
    return {
        "success": True,
        "message": f"Recovered completed render for plan index {plan_index}.",
        "plan_index": plan_index,
        "clip_filename": clip_filename,
        "status": "created",
        "recovered_completed_asset": True,
    }


def recover_completed_render_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Finalize a previously failed job whose exact output was already persisted."""
    job = get_job(job_id)
    if not job or job.get("type") != JOB_TYPE_RENDER_SHORT:
        return None
    completion = _render_job_completion_from_assets(job)
    if not completion:
        return None
    mark_job_done(job_id, completion)
    finalize_job_success(job_id)
    return completion


def _render_job_completion_from_plan(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = job.get("payload") or {}
    source_video_id = str(payload.get("source_video_id") or "").strip()
    if not source_video_id:
        return None
    try:
        plan_index = int(payload.get("plan_index"))
    except Exception:
        return None
    try:
        from app.video_shorts.services.render_jobs import validate_render_job_scope
        validate_render_job_scope(job)
        entries = generation._load_plan_entries(source_video_id, video_pk=payload.get("video_pk")) or []
    except Exception:
        logger.exception("Could not load plan entries for stuck render recovery job_id=%s", job.get("id"))
        return None
    for entry in entries:
        try:
            entry_index = int((entry or {}).get("plan_index") or 0)
        except Exception:
            continue
        if entry_index != plan_index:
            continue
        if str(entry.get("render_job_id") or "") != str(job.get("id") or ""):
            return None
        status = str((entry or {}).get("status") or "").strip().lower()
        clip_filename = str((entry or {}).get("clip_filename") or (entry or {}).get("output_filename") or "").strip()
        if status != "created" or not clip_filename:
            return None
        try:
            if not generation._short_exists(clip_filename):
                return None
        except Exception:
            logger.exception("Could not verify short output for stuck render recovery job_id=%s clip=%s", job.get("id"), clip_filename)
            return None
        return {
            "success": True,
            "message": f"Recovered completed render for plan index {plan_index}.",
            "plan_index": plan_index,
            "clip_filename": clip_filename,
            "status": "created",
            "recovered_stuck_processing": True,
        }
    return None


def _active_render_mutation_scopes() -> set[tuple[str, str, str]]:
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT user_id, payload_json
            FROM shorts_render_jobs
            WHERE type=? AND lower(status) IN ('processing', 'running', 'claimed', 'rendering')
            """,
            [JOB_TYPE_RENDER_SHORT],
        ).fetchall()
    finally:
        conn.close()
    scopes = set()
    for row in rows:
        payload = render_jobs._parse_json(row[1]) if row[1] else {}
        video_pk = payload.get("video_pk")
        plan_index = payload.get("plan_index")
        brand_id = payload.get("brand_id")
        if video_pk and plan_index and brand_id:
            scopes.add((str(row[0]), str(brand_id), youtube_scheduling.asset_key(video_pk=video_pk, plan_index=plan_index)))
    return scopes


def recover_orphaned_render_mutation_leases(app) -> int:
    released = youtube_scheduling.release_orphaned_rendering_leases(
        active_job_scopes=_active_render_mutation_scopes(),
        minimum_age_seconds=60,
    )
    for user_id, brand_id, key in released:
        app.logger.warning(
            "Released orphaned render mutation lease user_id=%s brand_id=%s asset_key=%s",
            user_id,
            brand_id,
            key,
        )
    return len(released)


def recover_stuck_render_jobs(app, *, stale_seconds: int = 15 * 60) -> Dict[str, int]:
    """Terminalize stale render jobs whose worker stopped making progress.

    If the plan entry already points at an existing rendered short, mark the job done.
    Otherwise release the stuck processing slot by requeueing while attempts remain, or
    failing once retries are exhausted.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=max(60, int(stale_seconds or 0)))
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id
            FROM shorts_render_jobs
            WHERE type = ?
              AND lower(status) IN ('processing', 'running', 'claimed', 'rendering')
              AND COALESCE(updated_at, started_at, created_at) < ?
            ORDER BY COALESCE(updated_at, started_at, created_at) ASC
            """,
            [JOB_TYPE_RENDER_SHORT, cutoff],
        ).fetchall()
        conn.commit()
    finally:
        conn.close()
    recovered = 0
    requeued = 0
    failed = 0
    for row in rows:
        job_id = str(row[0] or "").strip()
        job = get_job(job_id)
        if not job or str(job.get("status") or "").strip().lower() not in {"processing", "running", "claimed", "rendering"}:
            continue
        if not render_jobs.job_heartbeat_is_stale(job, cutoff):
            continue
        if render_jobs.worker_pid_is_alive(job, socket.gethostname()) is True:
            continue
        completion = _render_job_completion_from_assets(job) or _render_job_completion_from_plan(job)
        if completion:
            try:
                mark_job_done(job_id, completion)
                finalize_job_success(job_id)
                recovered += 1
                app.logger.warning("Recovered stuck completed render job_id=%s result=%s", job_id, completion)
            except Exception:
                app.logger.exception("Failed to recover stuck completed render job_id=%s", job_id)
            continue
        if int(job.get("attempts") or 0) >= int(job.get("max_attempts") or 1):
            mark_job_failed(job_id, "Stuck render job recovered without completed output; retries exhausted.")
            failed += 1
            app.logger.warning("Failed stuck render job without output job_id=%s", job_id)
        else:
            requeue_job(job_id, "Stuck render job recovered without completed output; requeued automatically.")
            requeued += 1
            app.logger.warning("Requeued stuck render job without output job_id=%s", job_id)
    return {"recovered": recovered, "requeued": requeued, "failed": failed}


def run_worker_loop() -> None:
    worker_id = _worker_id()
    mode = _worker_mode()
    app = create_app()
    with app.app_context():
        app.logger.info("Minti worker starting worker_id=%s mode=%s", worker_id, mode)
        if mode in {"all", "discovery"}:
            try:
                recovered = recover_processing_discovery_jobs_on_startup()
                if recovered.get("requeued") or recovered.get("failed"):
                    app.logger.warning("Recovered discovery processing jobs at startup: %s", recovered)
            except Exception:
                app.logger.exception("Discovery processing job recovery failed at startup")
            try:
                run_discovery_temp_janitor(force=True)
            except Exception:
                app.logger.exception("Discovery temp janitor failed at startup")
        while True:
            if mode in {"all", "discovery"}:
                try:
                    run_discovery_temp_janitor()
                except Exception:
                    app.logger.exception("Discovery temp janitor failed")
            requeue_dead_local_worker_jobs()
            try:
                recover_orphaned_render_mutation_leases(app)
            except Exception:
                app.logger.exception("Orphaned render mutation lease recovery failed")
            try:
                recovered_stuck = recover_stuck_render_jobs(app, stale_seconds=15 * 60)
                if any(recovered_stuck.values()):
                    app.logger.warning("Recovered stuck render jobs: %s", recovered_stuck)
            except Exception:
                app.logger.exception("Stuck render job recovery failed")
            requeue_timed_out_jobs(timeout_seconds=STALE_JOB_TIMEOUT_SECONDS)
            processed_any = False
            if mode == "discovery" and queued_customer_render_exists():
                if process_next_job(
                    app,
                    worker_id,
                    job_type=JOB_TYPE_RENDER_SHORT,
                    customer_only=True,
                ):
                    processed_any = True
                if not processed_any:
                    time.sleep(JOB_POLL_INTERVAL_SECONDS)
                continue
            if mode in {"all", "customer"}:
                try:
                    if youtube_scheduling.scheduler_tick():
                        processed_any = True
                except Exception:
                    app.logger.exception("Deferred YouTube scheduler tick failed")
                try:
                    if process_due_scheduled_outreach_email():
                        processed_any = True
                except Exception:
                    app.logger.exception("Scheduled outreach email processing failed")
                try:
                    if generation.process_new_lead_autodownload():
                        processed_any = True
                except Exception:
                    app.logger.exception("New lead auto-download polling failed")
                try:
                    if generation.process_downloaded_lead_autoplan():
                        processed_any = True
                except Exception:
                    app.logger.exception("Downloaded lead auto-plan polling failed")
                try:
                    if generation.process_planned_lead_autogenerate():
                        processed_any = True
                except Exception:
                    app.logger.exception("Planned lead auto-generate polling failed")
                try:
                    if generation.process_awaiting_approval_lead_autoapprove():
                        processed_any = True
                except Exception:
                    app.logger.exception("Awaiting approval lead auto-approve polling failed")
                try:
                    if generation.process_approved_lead_autoschedule():
                        processed_any = True
                except Exception:
                    app.logger.exception("Approved lead auto-schedule polling failed")
                try:
                    if generation.process_due_youtube_publish_reconcile():
                        processed_any = True
                except Exception:
                    app.logger.exception("YouTube publish reconcile polling failed")
            if mode in {"all", "discovery"}:
                if mode == "discovery" and _discovery_worker_should_wait(app):
                    continue
                try:
                    if process_due_discovery_automation_cycle():
                        processed_any = True
                except Exception:
                    app.logger.exception("Discovery automation polling failed")
                if mode == "discovery" and _discovery_worker_should_wait(app):
                    continue
                try:
                    while process_next_discovery_promote_request():
                        processed_any = True
                        if mode == "discovery" and _discovery_worker_should_wait(app):
                            break
                except Exception:
                    app.logger.exception("Discovery promote queue processing failed")
            for _ in range(max(1, int(WORKER_CONCURRENCY or 1))):
                if mode == "discovery" and _discovery_worker_should_wait(app, include_disk_gate=True):
                    break
                if process_next_job(
                    app,
                    worker_id,
                    customer_only=(mode == "customer"),
                    discovery_only=(mode == "discovery"),
                ):
                    processed_any = True
                else:
                    break
            if not processed_any:
                time.sleep(JOB_POLL_INTERVAL_SECONDS)


def main() -> None:
    run_worker_loop()


if __name__ == "__main__":
    main()
