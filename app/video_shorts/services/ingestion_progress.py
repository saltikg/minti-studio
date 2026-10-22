from __future__ import annotations

import json
from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, Iterable, Optional

from app.video_shorts.services.db import get_db_readonly


_SOURCE_JOB_TYPES = {"ingest_youtube", "transcribe_upload", "normalize_upload"}
_ACTIVE_STATUSES = {"queued", "processing", "running", "claimed"}
_TERMINAL_STATUSES = {"done", "failed", "cancelled"}

_AUTO_RENDER_SKIP_MESSAGES = {
    "daily_auto_render_cap": "Daily auto-clip limit reached",
    "storage_full": "Storage is full",
    "account_suspended": "Account is suspended",
    "suggestions_only_import": "Bulk import: suggestions only",
    "gate_disabled": "Auto-clip is not enabled for this account",
    "discovery_or_lead_video": "Suggestions only for this import",
    "ai_entries_exist": "Suggestions already existed",
    "already_ran": "Auto-clip was already processed",
}


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


def _as_positive_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clip_duration_from_payload(payload: Dict[str, Any]) -> Optional[float]:
    ranges = payload.get("edit_keep_ranges") or (payload.get("options") or {}).get("edit_keep_ranges")
    if isinstance(ranges, list):
        total = 0.0
        for item in ranges:
            if not isinstance(item, dict):
                continue
            start = _as_positive_float(item.get("start")) or 0.0
            end = _as_positive_float(item.get("end"))
            if end is not None and end > start:
                total += end - start
        if total > 0:
            return total
    start = _as_positive_float(payload.get("start")) or 0.0
    end = _as_positive_float(payload.get("end"))
    if end is not None and end > start:
        return end - start
    return None


def _render_history_sample(row: Any) -> Optional[tuple[float, float]]:
    timing = _json_object(row[0])
    clip_duration = _as_positive_float(timing.get("clip_duration_s"))
    if clip_duration is None:
        return None
    stages = timing.get("stages_ms") if isinstance(timing.get("stages_ms"), dict) else {}
    stage_total_ms = sum(
        value
        for value in (_as_positive_float(raw_value) for raw_value in stages.values())
        if value is not None
    )
    total_seconds = stage_total_ms / 1000.0 if stage_total_ms > 0 else None
    if total_seconds is None and isinstance(row[1], datetime) and isinstance(row[2], datetime):
        total_seconds = max(0.0, (row[2] - row[1]).total_seconds())
    if not total_seconds:
        return None
    return clip_duration, total_seconds


def _estimated_render_seconds(
    active_render: Optional[Dict[str, Any]],
    render_history: list[tuple[float, float]],
) -> Optional[int]:
    if not active_render:
        return None
    target_duration = _clip_duration_from_payload(active_render.get("payload") or {})
    if target_duration is None:
        return None
    lower = max(1.0, target_duration * (2.0 / 3.0))
    upper = target_duration * 1.5
    similar_totals = [
        total_seconds
        for clip_duration, total_seconds in render_history
        if lower <= clip_duration <= upper
    ]
    if len(similar_totals) < 3:
        return None
    return max(1, int(round(median(similar_totals))))


def _skip_reason_message(reason: str) -> str:
    clean = str(reason or "").strip()
    if not clean:
        return ""
    if clean.startswith("no_clips_above_") or clean == "no_strong_moments":
        return "No suggestion met the auto-clip threshold"
    return _AUTO_RENDER_SKIP_MESSAGES.get(clean, "Auto-clip was skipped")


def _count_label(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _ready_text(rendered_clip_count: int, suggestion_count: int) -> str:
    parts = []
    if rendered_clip_count:
        parts.append(_count_label(rendered_clip_count, "clip", "clips"))
    if suggestion_count:
        parts.append(_count_label(suggestion_count, "suggestion", "suggestions"))
    return "Ready" if not parts else f"Ready · {' + '.join(parts)}"


def _status_payload(
    video: Dict[str, Any],
    jobs: list[Dict[str, Any]],
    render_history: Optional[list[tuple[float, float]]] = None,
) -> Dict[str, Any]:
    source_jobs = [job for job in jobs if job["type"] in _SOURCE_JOB_TYPES]
    suggest_jobs = [job for job in jobs if job["type"] == "auto_suggest_render"]
    render_jobs = [job for job in jobs if _is_auto_render(job)]
    active_source = next((job for job in source_jobs if _active(job)), None)
    active_suggest = next((job for job in suggest_jobs if _active(job)), None)
    active_render = next((job for job in render_jobs if _active(job)), None)
    latest_source = source_jobs[0] if source_jobs else None
    latest_suggest = suggest_jobs[0] if suggest_jobs else None
    latest_render = render_jobs[0] if render_jobs else None
    rendered_clip_count = int(video.get("rendered_clip_count") or 0)
    rendered_clip_filenames = {
        str(value or "").strip()
        for value in (video.get("rendered_clip_filenames") or [])
        if str(value or "").strip()
    }
    auto_render_clip_filename = str((latest_render or {}).get("result", {}).get("clip_filename") or "").strip()
    auto_render_clip_exists = bool(
        auto_render_clip_filename and auto_render_clip_filename in rendered_clip_filenames
    )
    suggest_result = (latest_suggest or {}).get("result") or {}
    try:
        entries_generated = max(0, int(suggest_result.get("entries_generated") or 0))
    except (TypeError, ValueError):
        entries_generated = 0
    suggestion_count = max(0, entries_generated - rendered_clip_count)
    skip_reason = str(suggest_result.get("skip_reason") or "").strip()
    auto_render_status = ""
    auto_render_reason = ""
    auto_render_error = ""
    auto_render_plan_index = (latest_render or {}).get("payload", {}).get("plan_index")
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
        auto_render_status = "active"
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

    if transcript_ready and latest_suggest and latest_suggest.get("status") == "done":
        if latest_render and latest_render.get("status") == "done" and auto_render_clip_exists:
            auto_render_status = "done"
        elif latest_render and latest_render.get("status") == "failed":
            auto_render_status = "failed"
            auto_render_error = latest_render.get("error") or "The first clip could not be created."
        elif latest_render and latest_render.get("status") == "done":
            auto_render_status = "failed"
            auto_render_error = "The render finished without a saved clip."
        elif skip_reason:
            auto_render_status = "skipped"
            auto_render_reason = _skip_reason_message(skip_reason)
        elif rendered_clip_count > 0:
            auto_render_status = "skipped"
            auto_render_reason = "No auto-created clip was recorded"
        elif entries_generated > 0 and not active_render:
            failed_enqueue = next(
                (
                    item for item in (suggest_result.get("enqueued") or [])
                    if isinstance(item, dict) and not item.get("ok", True)
                ),
                None,
            )
            if failed_enqueue:
                auto_render_status = "failed"
                auto_render_error = str(failed_enqueue.get("message") or "The first clip could not be queued.")
                auto_render_plan_index = failed_enqueue.get("plan_index") or auto_render_plan_index
            else:
                auto_render_status = "skipped"
                auto_render_reason = "Auto-clip was not created"
    elif transcript_ready and not active_suggest and not active_render and not auto_render_status:
        auto_render_status = "skipped"
        auto_render_reason = "Auto-clip was not requested"

    ready = stage == "ready" and not active
    ready_label = _ready_text(rendered_clip_count, suggestion_count) if ready else ""
    detail_message = ""
    if ready and auto_render_status == "skipped":
        suggested_label = _count_label(entries_generated, "clip suggested", "clips suggested") if entries_generated else "Suggestions ready"
        detail_message = " · ".join(part for part in [suggested_label, auto_render_reason] if part)
    elif ready and auto_render_status == "failed":
        detail_message = " · ".join(part for part in ["First clip failed", auto_render_error] if part)
    if ready_label:
        message = ready_label

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
        "ready": ready,
        "ready_text": ready_label,
        "detail_message": detail_message,
        "rendered_clip_count": rendered_clip_count,
        "suggestion_count": suggestion_count,
        "entries_generated": entries_generated,
        "auto_render_status": auto_render_status,
        "auto_render_skip_reason": skip_reason,
        "auto_render_reason": auto_render_reason,
        "auto_render_error": auto_render_error,
        "auto_render_plan_index": auto_render_plan_index,
        "render_eta_seconds": _estimated_render_seconds(active_render, render_history or []),
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
            SELECT v.id, v.video_id, v.transcript_status, v.download_status
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
                "video_id": row[1],
                "transcript_status": row[2],
                "download_status": row[3],
            }
            for row in video_rows
        }
        if not videos:
            return {}
        scoped_ids = sorted(videos)
        job_placeholders = ", ".join("?" for _ in scoped_ids)
        clip_rows = conn.execute(
            f"""
            SELECT source_video.id, gv.clip_filename
            FROM shorts_generated_videos gv
            JOIN shorts_storage_assets render_asset
              ON render_asset.file_key = gv.storage_file_key
             AND CAST(render_asset.user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
             AND render_asset.brand_id IS NOT DISTINCT FROM gv.brand_id
             AND lower(coalesce(render_asset.status, 'active')) = 'active'
             AND render_asset.size_bytes > 0
            JOIN youtube_videos source_video
              ON source_video.video_id = gv.source_video_id
             AND CAST(source_video.owner_user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
             AND source_video.brand_id IS NOT DISTINCT FROM gv.brand_id
             AND source_video.deleted_at IS NULL
            WHERE source_video.id IN ({job_placeholders})
              AND CAST(gv.user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND gv.brand_id IS NOT DISTINCT FROM ?
              AND NULLIF(trim(coalesce(gv.clip_filename, '')), '') IS NOT NULL
              AND NULLIF(trim(coalesce(gv.storage_file_key, '')), '') IS NOT NULL
              AND lower(coalesce(gv.generation_status, '')) NOT IN
                  ('pending', 'queued', 'processing', 'rendering')
            """,
            [*[int(value) for value in scoped_ids], str(owner_user_id), brand_id],
        ).fetchall()
        for row in clip_rows:
            video_pk = int(row[0])
            if video_pk not in videos:
                continue
            filenames = videos[video_pk].setdefault("rendered_clip_filenames", [])
            filename = str(row[1] or "").strip()
            if filename and filename not in filenames:
                filenames.append(filename)
        for video in videos.values():
            video["rendered_clip_count"] = len(video.get("rendered_clip_filenames") or [])
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
        render_history_rows = conn.execute(
            """
            SELECT timing_json, started_at, finished_at
            FROM shorts_render_jobs
            WHERE type = 'render_short'
              AND status = 'done'
              AND timing_json IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT 50
            """
        ).fetchall()
    finally:
        conn.close()

    render_history = [
        sample
        for sample in (_render_history_sample(row) for row in render_history_rows)
        if sample is not None
    ]

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
        str(video_pk): _status_payload(video, jobs_by_video.get(video_pk, []), render_history)
        for video_pk, video in videos.items()
    }
