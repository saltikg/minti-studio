"""Best-effort, per-render timing capture with no stage-level database writes."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional


logger = logging.getLogger(__name__)
TIMING_VERSION = 2
STAGE_KEYS = (
    "download",
    "face_detect",
    "transcribe_captions",
    "compose",
    "encode",
    "upload",
)

_active: ContextVar[Optional[Dict[str, Any]]] = ContextVar("render_timing", default=None)


def _log_capture_error(action: str) -> None:
    logger.warning("Render timing capture failed action=%s; continuing render", action, exc_info=True)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _process_tree_rss_bytes(root_pid: int) -> int:
    """Return current RSS for Python and all live descendants (including ffmpeg)."""
    total = 0
    pending = [int(root_pid)]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid <= 0 or pid in seen:
            continue
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/status", "r", encoding="utf-8") as handle:
                status = handle.readlines()
            for line in status:
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
            pass
        try:
            with open(
                f"/proc/{pid}/task/{pid}/children",
                "r",
                encoding="utf-8",
            ) as handle:
                children = handle.read().split()
            pending.extend(int(child) for child in children)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
            pass
    return total


def _start_rss_monitor(state: Dict[str, Any]) -> None:
    stop_event = threading.Event()

    def _monitor() -> None:
        while not stop_event.is_set():
            try:
                rss_mb = round(_process_tree_rss_bytes(os.getpid()) / (1024 * 1024), 2)
                state["peak_rss_mb"] = max(float(state.get("peak_rss_mb") or 0.0), rss_mb)
            except Exception:
                _log_capture_error("peak_rss_sample")
            stop_event.wait(0.25)

    thread = threading.Thread(target=_monitor, name="render-rss-monitor", daemon=True)
    state["_rss_stop_event"] = stop_event
    state["_rss_thread"] = thread
    thread.start()


def _stop_rss_monitor(state: Optional[Dict[str, Any]]) -> None:
    if not state:
        return
    try:
        stop_event = state.get("_rss_stop_event")
        if stop_event:
            stop_event.set()
        thread = state.get("_rss_thread")
        if thread and thread is not threading.current_thread():
            thread.join(timeout=0.5)
    except Exception:
        _log_capture_error("peak_rss_stop")


def begin(job: Dict[str, Any]) -> None:
    """Begin timing a claimed render job. Never raises into the render path."""
    try:
        prior = ((job.get("result") or {}).get("_timing_accumulator") or {})
        prior_stages = prior.get("stages_ms") if isinstance(prior.get("stages_ms"), dict) else {}
        stages = {key: int(prior_stages.get(key) or 0) for key in STAGE_KEYS}
        ran_stages = set(prior.get("ran_stages") or [])
        started_at = _parse_timestamp(job.get("started_at"))
        wait_from = _parse_timestamp(prior.get("last_requeued_at")) or _parse_timestamp(job.get("created_at"))
        current_wait_ms = 0
        if started_at and wait_from:
            current_wait_ms = max(0, int(round((started_at - wait_from).total_seconds() * 1000)))
        state = {
            "job_id": str(job.get("id") or ""),
            "attempt_started": time.monotonic(),
            "queue_wait_ms": int(prior.get("queue_wait_ms") or 0) + current_wait_ms,
            "stages_ms": stages,
            "ran_stages": ran_stages,
            "clip_duration_s": prior.get("clip_duration_s"),
            "source_resolution": prior.get("source_resolution"),
            "face_found": prior.get("face_found"),
            "layout": prior.get("layout"),
            "concurrent_jobs": int(job.get("_timing_concurrent_jobs") or prior.get("concurrent_jobs") or 0),
            "traffic_guard_wait_ms": prior.get("traffic_guard_wait_ms"),
            "source_cache_hit": prior.get("source_cache_hit"),
            "attempt_count": int(job.get("attempts") or prior.get("attempt_count") or 1),
            "encode_settings": dict(prior.get("encode_settings") or {}),
            "peak_rss_mb": float(prior.get("peak_rss_mb") or 0.0),
        }
        _active.set(state)
        _start_rss_monitor(state)
    except Exception:
        _active.set(None)
        _log_capture_error("begin")


def clear() -> None:
    try:
        _stop_rss_monitor(_active.get())
        _active.set(None)
    except Exception:
        _log_capture_error("clear")


def _add_elapsed(stage_name: str, started: float, *, subtract_ms: int = 0) -> None:
    try:
        state = _active.get()
        if not state or stage_name not in STAGE_KEYS:
            return
        elapsed_ms = max(0, int(round((time.monotonic() - started) * 1000)) - max(0, int(subtract_ms)))
        state["stages_ms"][stage_name] = int(state["stages_ms"].get(stage_name) or 0) + elapsed_ms
        state["ran_stages"].add(stage_name)
    except Exception:
        _log_capture_error(f"stage:{stage_name}")


@contextmanager
def stage(stage_name: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        _add_elapsed(stage_name, started)


@contextmanager
def exclusive_stage(stage_name: str, *, excluding: str) -> Iterator[None]:
    state = _active.get()
    before = int(((state or {}).get("stages_ms") or {}).get(excluding) or 0)
    started = time.monotonic()
    try:
        yield
    finally:
        state = _active.get()
        after = int(((state or {}).get("stages_ms") or {}).get(excluding) or 0)
        _add_elapsed(stage_name, started, subtract_ms=max(0, after - before))


def record_elapsed(stage_name: str, started: float) -> None:
    _add_elapsed(stage_name, started)


def set_metadata(**values: Any) -> None:
    try:
        state = _active.get()
        if not state:
            return
        for key, value in values.items():
            if key in state and value is not None:
                state[key] = value
    except Exception:
        _log_capture_error("set_metadata")


def record_source_cache_hit(hit: bool) -> None:
    try:
        state = _active.get()
        if not state:
            return
        # Any download miss means this attempt was not served wholly from cache.
        if state.get("source_cache_hit") is False:
            return
        state["source_cache_hit"] = bool(hit)
    except Exception:
        _log_capture_error("source_cache_hit")


def record_source_resolution(width: Any, height: Any) -> None:
    try:
        state = _active.get()
        if not state or state.get("source_resolution"):
            return
        clean_width = int(width or 0)
        clean_height = int(height or 0)
        if clean_width > 0 and clean_height > 0:
            state["source_resolution"] = {"width": clean_width, "height": clean_height}
    except Exception:
        _log_capture_error("source_resolution")


def _command_value(cmd: list[str], *flags: str) -> Optional[str]:
    for flag in flags:
        try:
            index = len(cmd) - 1 - cmd[::-1].index(flag)
        except ValueError:
            continue
        if index + 1 < len(cmd):
            return str(cmd[index + 1])
    return None


def record_encode_command(cmd: Any) -> None:
    try:
        state = _active.get()
        if not state:
            return
        parts = [str(part) for part in (cmd or [])]
        if not parts:
            return
        bitrate = _command_value(parts, "-b:v", "-b")
        fps = _command_value(parts, "-r")
        if not fps:
            for part in parts:
                if "fps=" in part:
                    fps = part.split("fps=", 1)[1].split(",", 1)[0].split(":", 1)[0]
                    break
        settings = state["encode_settings"]
        settings.update(
            {
                "preset": _command_value(parts, "-preset"),
                "crf": _command_value(parts, "-crf"),
                "bitrate": bitrate,
                "threads": _command_value(parts, "-threads") or "auto",
                "fps": fps or "source",
            }
        )
    except Exception:
        _log_capture_error("encode_settings")


def record_output_resolution(width: Any, height: Any) -> None:
    try:
        state = _active.get()
        if not state:
            return
        clean_width = int(width or 0)
        clean_height = int(height or 0)
        if clean_width > 0 and clean_height > 0:
            state["encode_settings"]["output_resolution"] = {
                "width": clean_width,
                "height": clean_height,
            }
    except Exception:
        _log_capture_error("output_resolution")


def retry_accumulator() -> Optional[Dict[str, Any]]:
    try:
        state = _active.get()
        if not state:
            return None
        return {
            "queue_wait_ms": int(state.get("queue_wait_ms") or 0),
            "stages_ms": dict(state.get("stages_ms") or {}),
            "ran_stages": sorted(state.get("ran_stages") or []),
            "clip_duration_s": state.get("clip_duration_s"),
            "source_resolution": state.get("source_resolution"),
            "face_found": state.get("face_found"),
            "layout": state.get("layout"),
            "concurrent_jobs": state.get("concurrent_jobs"),
            "traffic_guard_wait_ms": state.get("traffic_guard_wait_ms"),
            "source_cache_hit": state.get("source_cache_hit"),
            "attempt_count": state.get("attempt_count"),
            "encode_settings": dict(state.get("encode_settings") or {}),
            "peak_rss_mb": state.get("peak_rss_mb"),
            "last_requeued_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        _log_capture_error("retry_accumulator")
        return None


def terminal_payload() -> Optional[Dict[str, Any]]:
    try:
        state = _active.get()
        if not state:
            return None
        ran = state.get("ran_stages") or set()
        stages = {
            key: int(state["stages_ms"].get(key) or 0) if key in ran else None
            for key in STAGE_KEYS
        }
        settings = dict(state.get("encode_settings") or {})
        for key, default in (
            ("preset", None),
            ("crf", None),
            ("bitrate", None),
            ("threads", None),
            ("output_resolution", None),
            ("fps", None),
        ):
            settings.setdefault(key, default)
        return {
            "version": TIMING_VERSION,
            "queue_wait_ms": int(state.get("queue_wait_ms") or 0),
            "stages_ms": stages,
            "clip_duration_s": state.get("clip_duration_s"),
            "source_resolution": state.get("source_resolution"),
            "face_found": state.get("face_found"),
            "layout": state.get("layout"),
            "concurrent_jobs": state.get("concurrent_jobs"),
            "traffic_guard_wait_ms": state.get("traffic_guard_wait_ms"),
            "source_cache_hit": state.get("source_cache_hit"),
            "attempt_count": int(state.get("attempt_count") or 1),
            "peak_rss_mb": round(float(state.get("peak_rss_mb") or 0.0), 2) or None,
            "encode_settings": settings,
        }
    except Exception:
        _log_capture_error("terminal_payload")
        return None
