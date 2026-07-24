from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from app.video_shorts.config import DISK_GUARD_PCT, VIDEO_SHORTS_TMP_DIR


USER_FACING_DISK_GUARD_MESSAGE = "The system is busy right now. Please try again in a few minutes."


def _stat_target(path: Optional[Path] = None) -> Path:
    candidate = Path(path or VIDEO_SHORTS_TMP_DIR)
    for target in (candidate, *candidate.parents):
        if target.exists():
            return target
    return candidate.parent


def disk_usage_percent(path: Optional[Path] = None) -> float:
    target = _stat_target(path)
    stats = os.statvfs(target)
    total_blocks = int(stats.f_blocks or 0)
    if total_blocks <= 0:
        return 0.0
    available_blocks = int(stats.f_bavail or 0)
    used_blocks = max(0, total_blocks - available_blocks)
    return round((used_blocks / total_blocks) * 100.0, 1)


def disk_guard_triggered(*, operation: str, log=None, path: Optional[Path] = None) -> bool:
    usage_pct = disk_usage_percent(path)
    if usage_pct < float(DISK_GUARD_PCT):
        return False
    if log is not None:
        log.warning(
            "Disk guard blocked operation=%s usage_pct=%.1f threshold_pct=%s path=%s",
            operation,
            usage_pct,
            DISK_GUARD_PCT,
            _stat_target(path),
        )
    return True
