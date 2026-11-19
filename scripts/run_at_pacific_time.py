#!/usr/bin/env python3
"""Run a cron command only at the requested America/Los_Angeles wall time."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


PACIFIC = ZoneInfo("America/Los_Angeles")


def should_run(now_utc: datetime, *, hour: int, minute: int, weekdays: set[int] | None) -> tuple[bool, datetime]:
    now_pt = now_utc.astimezone(PACIFIC)
    matches = now_pt.hour == hour and now_pt.minute == minute
    if weekdays is not None:
        matches = matches and now_pt.weekday() in weekdays
    return matches, now_pt


def _safe_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")
    if not name:
        raise ValueError("--name must contain at least one letter or number")
    return name


def _claim_guard(guard_dir: Path, name: str, now_pt: datetime) -> tuple[Path | None, Path]:
    guard_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_safe_name(name)}-{now_pt.date().isoformat()}"
    done = guard_dir / f"{stem}.done"
    running = guard_dir / f"{stem}.running"
    if done.exists():
        return None, done
    try:
        fd = os.open(running, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None, done
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(now_pt.isoformat())
    return running, done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--hour", required=True, type=int, choices=range(24), metavar="0-23")
    parser.add_argument("--minute", required=True, type=int, choices=range(60), metavar="0-59")
    parser.add_argument("--weekdays", help="Comma-separated Python weekdays: Monday=0 through Sunday=6")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    weekdays = None
    if args.weekdays:
        weekdays = {int(item) for item in args.weekdays.split(",")}
        if not weekdays.issubset(set(range(7))):
            parser.error("--weekdays values must be between 0 and 6")

    matches, now_pt = should_run(
        datetime.now(timezone.utc), hour=args.hour, minute=args.minute, weekdays=weekdays
    )
    if not matches:
        return 0

    guard_dir = Path(os.environ.get("MINTI_CRON_GUARD_DIR", "/var/tmp/minti-cron-guards"))
    running, done = _claim_guard(guard_dir, args.name, now_pt)
    if running is None:
        return 0
    try:
        result = subprocess.run(command, check=False)
        if result.returncode == 0:
            running.replace(done)
        else:
            running.unlink(missing_ok=True)
        return result.returncode
    except BaseException:
        running.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
