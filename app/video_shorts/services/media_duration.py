from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

from app.video_shorts.config import FFMPEG_BIN, FFPROBE_TIMEOUT
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.media_utils import (
    _cleanup_resolved_source_video,
    _resolve_source_video,
    run_media_subprocess,
)


logger = logging.getLogger(__name__)
_duration_locks: dict[int, threading.Lock] = {}
_duration_locks_guard = threading.Lock()


def _resolve_ffprobe() -> str:
    configured = Path(FFMPEG_BIN).with_name("ffprobe") if FFMPEG_BIN else None
    candidates = [str(configured) if configured else "", "ffprobe", "/usr/bin/ffprobe"]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate) or candidate
        if Path(resolved).is_file():
            return str(Path(resolved))
    return "ffprobe"


def probe_exact_media_duration_seconds(target: Path | str, *, context: str = "") -> Optional[float]:
    """Return the shortest positive primary audio/video stream duration."""
    result = run_media_subprocess(
        [
            _resolve_ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,duration:format=duration",
            "-of",
            "json",
            str(target),
        ],
        operation="probe_exact_media_duration",
        context=context or f"target={Path(str(target)).name}",
        capture_output=True,
        text=True,
        check=True,
        timeout=FFPROBE_TIMEOUT,
    )
    payload = json.loads(result.stdout or "{}")
    stream_durations: list[float] = []
    for stream in payload.get("streams") or []:
        if str(stream.get("codec_type") or "").lower() not in {"video", "audio"}:
            continue
        try:
            value = float(stream.get("duration"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            stream_durations.append(value)
    if stream_durations:
        return min(stream_durations)
    try:
        format_duration = float((payload.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return format_duration if format_duration > 0 else None


def _video_duration_lock(video_pk: int) -> threading.Lock:
    with _duration_locks_guard:
        return _duration_locks.setdefault(int(video_pk), threading.Lock())


def get_or_backfill_media_duration_seconds(
    video_pk: int,
    video_id: str,
    *,
    source_path: Optional[Path] = None,
) -> Optional[float]:
    """Read the persisted exact duration, probing and storing it once if absent."""
    clean_video_pk = int(video_pk)

    def read_stored() -> Optional[float]:
        conn = get_db_readonly()
        try:
            if "media_duration_seconds" not in table_columns(conn, "youtube_videos"):
                return None
            row = conn.execute(
                "SELECT media_duration_seconds FROM youtube_videos WHERE id = ?",
                [clean_video_pk],
            ).fetchone()
            value = float(row[0]) if row and row[0] is not None else None
            return value if value and value > 0 else None
        finally:
            conn.close()

    stored = read_stored()
    if stored is not None:
        return stored
    with _video_duration_lock(clean_video_pk):
        stored = read_stored()
        if stored is not None:
            return stored
        resolved_path = source_path
        resolved_is_temp = False
        try:
            if resolved_path is None:
                resolved_path, resolved_is_temp = _resolve_source_video(video_id, video_pk=clean_video_pk)
            if not resolved_path or not Path(resolved_path).exists():
                return None
            probed = probe_exact_media_duration_seconds(
                Path(resolved_path),
                context=f"video_pk={clean_video_pk} video_id={video_id}",
            )
            if probed is None:
                return None
            conn = get_db()
            try:
                conn.execute(
                    """
                    UPDATE youtube_videos
                    SET media_duration_seconds = ?
                    WHERE id = ?
                      AND (media_duration_seconds IS NULL OR media_duration_seconds <= 0)
                    """,
                    [float(probed), clean_video_pk],
                )
                conn.commit()
            finally:
                conn.close()
            logger.info("media_duration_backfilled video_pk=%s duration=%.6f", clean_video_pk, probed)
            return float(probed)
        except Exception:
            logger.warning("media_duration_backfill_failed video_pk=%s", clean_video_pk, exc_info=True)
            return None
        finally:
            if source_path is None:
                _cleanup_resolved_source_video(resolved_path, resolved_is_temp)
