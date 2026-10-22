from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from app.video_shorts.services.db import get_db_readonly


_SOURCE_JOB_TYPES = {"ingest_youtube", "transcribe_upload", "normalize_upload"}
_ACTIVE_STATUSES = {"queued", "processing", "running", "claimed"}
_TERMINAL_STATUSES = {"done", "failed", "cancelled"}


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


def _age_seconds(value: Any) -> Optional[int]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds()))


def _job_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row[0] or ""),
        "type": str(row[1] or ""),
        "status": str(row[2] or "").strip().lower(),
        "payload": _json_object(row[3]),
        "result": _json_object(row[4]),
        "error": str(row[5] or "").strip(),
        "created_at": row[6],
        "updated_at": row[7],
        "finished_at": row[8],
    }


def _active(job: Optional[Dict[str, Any]]) -> bool:
    return bool(job and job.get("status") in _ACTIVE_STATUSES)


def _failed(job: Optional[Dict[str, Any]]) -> bool:
    return bool(job and job.get("status") == "failed")


def _is_auto_render(job: Dict[str, Any]) -> bool:
    payload = job.get("payload") or {}
    origin = str(payload.get("job_origin") or "").strip().lower()
    return job.get("type") == "render_short" and (
        origin in {"auto", "auto_suggest"} or bool(payload.get("auto_suggest_default"))
    )


def _status_payload(
    video: Dict[str, Any],
    jobs: list[Dict[str, Any]],
) -> Dict[str, Any]:
    source_jobs = [job for job in jobs if job["type"] in _SOURCE_JOB_TYPES]
    suggest_jobs = [job for job in jobs if job["type"] == "auto_suggest_render"]
    render_jobs = [job for job in jobs if _is_auto_render(job)]
    active_source = next((job for job in source_jobs if _active(job)), None)
    active_suggest = next((job for job in suggest_jobs if _active(job)), None)
    active_render = next((job for job in render_jobs if _active(job)), None)
    latest_source = source_jobs[0] if source_jobs else None
    latest_suggest = suggest_jobs[0] if suggest_jobs else None
    transcript_ready = str(video.get("transcript_status") or "").strip().lower() in {
        "done", "ready", "complete", "completed",
    }

    job = active_render or active_suggest or active_source
    stage = "ready"
    message = "Ready"
    percent = None
    eta_seconds = None
    speed_bps = None
    error = ""
    active = False

    if active_render:
        stage, message, active = "creating_clip", "Creating your first clip…", True
    elif active_suggest:
        stage, message, active = "finding_clips", "Finding the best clips…", True
    elif active_source:
        result = active_source.get("result") or {}
        raw_stage = str(result.get("stage") or "").strip().lower()
        if active_source["type"] == "ingest_youtube" and raw_stage == "downloading":
            stage, message = "downloading", "Downloading from YouTube…"
            percent = result.get("percent")
            eta_seconds = result.get("eta_seconds")
            speed_bps = result.get("speed_bps")
        elif active_source["type"] == "transcribe_upload" or raw_stage == "transcribing":
            stage, message = "transcribing", "Transcribing…"
            percent = result.get("percent")
        else:
            stage = "processing"
            message = "Processing…"
            percent = result.get("percent")
        active = True
    else:
        failed_job = None
        if not transcript_ready and _failed(latest_source):
            failed_job = latest_source
        elif transcript_ready and _failed(latest_suggest):
            failed_job = latest_suggest
        if failed_job:
            job = failed_job
            stage, message = "failed", "Processing failed"
            error = failed_job.get("error") or "We couldn't finish processing this video."
        elif not transcript_ready:
            stage, message = "processing", "Processing…"
            active = True

    updated_at = (job or {}).get("updated_at")
    stale = bool(active and (job is None or (_age_seconds(updated_at) or 0) >= 60))
    if stale:
        message = "Still working…"
    return {
        "video_pk": int(video["id"]),
        "stage": stage,
        "message": message,
        "active": active,
        "terminal": stage in {"ready", "failed", "cancelled"},
        "failed": stage == "failed",
        "error": error,
        "percent": percent,
        "eta_seconds": eta_seconds,
        "speed_bps": speed_bps,
        "stale": stale,
        "job_id": (job or {}).get("id") or "",
        "updated_at": _iso(updated_at),
    }


def get_video_ingestion_statuses(
    *,
    owner_user_id: str,
    brand_id: Optional[str],
    video_pks: Iterable[int],
) -> Dict[str, Dict[str, Any]]:
    clean_ids_set = set()
    for value in video_pks:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            clean_ids_set.add(numeric)
    clean_ids = sorted(clean_ids_set)
    if not clean_ids:
        return {}
    placeholders = ", ".join("?" for _ in clean_ids)
    conn = get_db_readonly()
    try:
        brand_clause = "v.brand_id = ?" if brand_id else "v.brand_id IS NULL"
        video_params: list[Any] = [*clean_ids, str(owner_user_id)]
        if brand_id:
            video_params.append(str(brand_id))
        video_rows = conn.execute(
            f"""
            SELECT v.id, v.transcript_status, v.download_status
            FROM youtube_videos v
            WHERE v.id IN ({placeholders})
              AND CAST(v.owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND {brand_clause}
              AND v.deleted_at IS NULL
            """,
            video_params,
        ).fetchall()
        videos = {
            int(row[0]): {
                "id": int(row[0]),
                "transcript_status": row[1],
                "download_status": row[2],
            }
            for row in video_rows
        }
        if not videos:
            return {}
        scoped_ids = sorted(videos)
        job_placeholders = ", ".join("?" for _ in scoped_ids)
        job_rows = conn.execute(
            f"""
            SELECT id, type, status, payload_json, result_json, error,
                   created_at, updated_at, finished_at
            FROM shorts_render_jobs
            WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND type IN ('ingest_youtube', 'transcribe_upload', 'normalize_upload',
                           'auto_suggest_render', 'render_short')
              AND payload_json->>'video_pk' IN ({job_placeholders})
            ORDER BY created_at DESC
            """,
            [str(owner_user_id), *[str(value) for value in scoped_ids]],
        ).fetchall()
    finally:
        conn.close()

    jobs_by_video: Dict[int, list[Dict[str, Any]]] = {video_pk: [] for video_pk in videos}
    for row in job_rows:
        job = _job_dict(row)
        try:
            video_pk = int((job.get("payload") or {}).get("video_pk") or 0)
        except (TypeError, ValueError):
            continue
        if video_pk in jobs_by_video:
            jobs_by_video[video_pk].append(job)
    return {
        str(video_pk): _status_payload(video, jobs_by_video.get(video_pk, []))
        for video_pk, video in videos.items()
    }
