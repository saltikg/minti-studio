#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import psycopg
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
PROD_ENV_PATH = Path("/home/ubuntu/apps/minti_studio/.env")
DEFAULT_ENV_PATH = ROOT / ".env"
ACCESS_LOG_PATH = Path("/home/ubuntu/apps/minti_studio/logs/gunicorn-access.log")
RECENT_WEB_WINDOW = timedelta(minutes=5)
ACCESS_LOG_TIME_RE = re.compile(r"\[(?P<ts>[^\]]+)\]")


@dataclass
class ProcessingJob:
    job_id: str
    job_type: str
    user_email: str
    started_at: datetime


def _load_env_file() -> Path:
    env_path = PROD_ENV_PATH if PROD_ENV_PATH.exists() else DEFAULT_ENV_PATH
    load_dotenv(env_path)
    return env_path


def _database_url() -> str:
    for key in ("VIDEO_SHORTS_DATABASE_URL", "VIDEO_SHORTS_POSTGRES_URL", "DATABASE_URL"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    raise RuntimeError(
        "Database URL not found in env. Expected one of VIDEO_SHORTS_DATABASE_URL, "
        "VIDEO_SHORTS_POSTGRES_URL, or DATABASE_URL."
    )


def _connect() -> psycopg.Connection:
    return psycopg.connect(_database_url(), options="-c search_path=main,public")


def _fetch_processing_jobs() -> list[ProcessingJob]:
    query = """
        SELECT
            j.id,
            j.type,
            COALESCE(u.email, j.user_id) AS user_email,
            j.started_at
        FROM shorts_render_jobs j
        LEFT JOIN shorts_users u ON u.id = j.user_id
        WHERE j.status = 'processing'
        ORDER BY j.started_at ASC
    """
    jobs: list[ProcessingJob] = []
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            for row in cur.fetchall():
                started_at = row[3]
                if started_at is None:
                    continue
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                else:
                    started_at = started_at.astimezone(timezone.utc)
                jobs.append(
                    ProcessingJob(
                        job_id=str(row[0]),
                        job_type=str(row[1]),
                        user_email=str(row[2] or "unknown"),
                        started_at=started_at,
                    )
                )
    return jobs


def _tail_lines(path: Path, *, max_lines: int = 2000, chunk_size: int = 8192) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        position = end
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
    raw = match.group("ts")
    try:
        parsed = datetime.strptime(raw, "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _recent_web_hits(now: datetime) -> list[datetime]:
    cutoff = now - RECENT_WEB_WINDOW
    hits: list[datetime] = []
    for line in reversed(_tail_lines(ACCESS_LOG_PATH)):
        parsed = _parse_access_log_time(line)
        if parsed is None:
            continue
        if parsed < cutoff:
            break
        hits.append(parsed)
    hits.reverse()
    return hits


def _format_age(started_at: datetime, now: datetime) -> str:
    delta = max(timedelta(0), now - started_at)
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m ago"
    if minutes:
        return f"{minutes}m {seconds}s ago"
    return f"{seconds}s ago"


def _print_processing_jobs(jobs: Iterable[ProcessingJob], now: datetime) -> None:
    for job in jobs:
        print(
            f"  - {job.job_type} | {job.user_email} | started {_format_age(job.started_at, now)}"
        )


def main() -> int:
    _load_env_file()
    now = datetime.now(timezone.utc)

    try:
        jobs = _fetch_processing_jobs()
    except Exception as exc:
        print(f"ERROR — could not check processing jobs: {exc}", file=sys.stderr)
        return 2

    if jobs:
        print(f"WAIT — {len(jobs)} job(s) in progress:")
        _print_processing_jobs(jobs, now)
        return 1

    recent_hits = _recent_web_hits(now)
    if recent_hits:
        print(
            "CAUTION — web request(s) in the last 5 min; a restart causes a brief (~5-7s) "
            "outage for anyone currently on the site. Safe to proceed if you accept that."
        )
        print(f"  - recent requests seen: {len(recent_hits)}")
        print(f"  - last request: {_format_age(recent_hits[-1], now)}")
        return 0

    print("SAFE — no active jobs, no recent web activity. OK to deploy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
