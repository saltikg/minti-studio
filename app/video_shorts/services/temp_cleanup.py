import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List

from app.video_shorts.config import VIDEO_SHORTS_TMP_DIR


logger = logging.getLogger(__name__)

_CLEANUP_LOCK = threading.Lock()
_LAST_CLEANUP_TS = 0.0
_CLEANUP_INTERVAL_SECONDS = 30 * 60
_STALE_MAX_AGE_SECONDS = 12 * 60 * 60
_PRUNE_MIN_AGE_SECONDS = 60 * 60
_MAX_TMP_BYTES = 8 * 1024 ** 3
_TARGET_TMP_BYTES = 4 * 1024 ** 3


def ensure_video_shorts_tmp_dir() -> Path:
    VIDEO_SHORTS_TMP_DIR.mkdir(parents=True, exist_ok=True)
    return VIDEO_SHORTS_TMP_DIR


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += int(child.stat().st_size)
            except Exception:
                continue
        return total
    except Exception:
        return 0


def _safe_remove(path: Path) -> int:
    try:
        if path.is_dir():
            size = _path_size(path)
            shutil.rmtree(path, ignore_errors=True)
            return size
        size = int(path.stat().st_size) if path.exists() else 0
        path.unlink(missing_ok=True)
        return size
    except Exception:
        logger.exception("Failed to remove temp path: %s", path)
        return 0


def _collect_entries(tmp_dir: Path) -> List[Dict[str, object]]:
    entries: List[Dict[str, object]] = []
    for path in tmp_dir.iterdir():
        if path.name.startswith(".cleanup"):
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        entries.append(
            {
                "path": path,
                "mtime": float(stat.st_mtime),
                "size": float(_path_size(path)),
            }
        )
    return entries


def cleanup_video_shorts_temp_dir(force: bool = False) -> None:
    global _LAST_CLEANUP_TS
    now = time.time()
    if not force and (now - _LAST_CLEANUP_TS) < _CLEANUP_INTERVAL_SECONDS:
        return
    with _CLEANUP_LOCK:
        now = time.time()
        if not force and (now - _LAST_CLEANUP_TS) < _CLEANUP_INTERVAL_SECONDS:
            return
        tmp_dir = ensure_video_shorts_tmp_dir()
        removed_count = 0
        removed_bytes = 0

        entries = _collect_entries(tmp_dir)
        stale_cutoff = now - _STALE_MAX_AGE_SECONDS
        for entry in entries:
            if float(entry["mtime"]) > stale_cutoff:
                continue
            removed_bytes += _safe_remove(entry["path"])
            removed_count += 1

        entries = _collect_entries(tmp_dir)
        total_bytes = sum(int(entry["size"]) for entry in entries)
        if total_bytes > _MAX_TMP_BYTES:
            prune_cutoff = now - _PRUNE_MIN_AGE_SECONDS
            pruneable = [entry for entry in entries if float(entry["mtime"]) <= prune_cutoff]
            pruneable.sort(key=lambda item: (float(item["mtime"]), -float(item["size"])))
            for entry in pruneable:
                if total_bytes <= _TARGET_TMP_BYTES:
                    break
                removed = _safe_remove(entry["path"])
                if removed <= 0:
                    continue
                total_bytes = max(0, total_bytes - removed)
                removed_bytes += removed
                removed_count += 1

        _LAST_CLEANUP_TS = now
        if removed_count:
            logger.info(
                "video_shorts tmp cleanup removed=%s freed=%.2fMB dir=%s",
                removed_count,
                removed_bytes / (1024 ** 2),
                tmp_dir,
            )
