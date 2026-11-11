#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import os
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv

try:
    import psycopg as _db_driver
except ImportError:  # pragma: no cover
    import psycopg2 as _db_driver


ROOT = Path(__file__).resolve().parents[1]
PROD_ENV_PATH = Path("/home/ubuntu/apps/minti_studio/.env")
DEFAULT_ENV_PATH = ROOT / ".env"
ACCESS_LOG_PATH = Path("/home/ubuntu/apps/minti_studio/logs/gunicorn-access.log")
RECENT_WEB_WINDOW = timedelta(minutes=5)
UPCOMING_PUBLISH_WINDOW = timedelta(minutes=20)
ACCESS_LOG_TIME_RE = re.compile(r"\[(?P<ts>[^\]]+)\]")
ACCESS_LOG_REQUEST_RE = re.compile(r'"(?:GET|POST|HEAD|OPTIONS)\s+(?P<target>\S+)')
BOT_UA_RE = re.compile(r"(bot|crawl|spider|slurp|facebookexternalhit|preview|uptime|monitor|health)", re.I)


@dataclass
class ProcessingJob:
    job_id: str
    job_type: str
    user_email: str
    started_at: datetime
    origin: str = ""


@dataclass
class ScheduledPublish:
    platform: str
    schedule_id: str
    brand_id: str
    status: str
    due_at: datetime
    due_kind: str


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


def _connect() -> Any:
    return _db_driver.connect(_database_url(), options="-c search_path=main,public")


def _fetch_processing_jobs() -> list[ProcessingJob]:
    query = """
        SELECT
            j.id,
            j.type,
            COALESCE(u.email, j.user_id) AS user_email,
            j.started_at,
            COALESCE(j.payload_json->>'job_origin', '') AS origin
        FROM shorts_render_jobs j
        LEFT JOIN shorts_users u ON u.id::text = j.user_id
        WHERE lower(j.status) IN ('processing', 'running', 'claimed', 'rendering')
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
                        origin=str(row[4] or ""),
                    )
                )
            cur.execute("SELECT to_regclass('main.discovery_promote_requests')")
            if cur.fetchone()[0]:
                cur.execute(
                    """
                    SELECT
                        r.id,
                        'discovery_promote' AS type,
                        r.discovery_lead_id::text AS user_email,
                        r.started_at,
                        'discovery_promote' AS origin
                    FROM discovery_promote_requests r
                    WHERE r.status = 'processing'
                      AND r.started_at IS NOT NULL
                    ORDER BY r.started_at ASC
                    """
                )
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
                            origin=str(row[4] or ""),
                        )
                    )
    return jobs


def _fetch_imminent_schedules(now: datetime) -> list[ScheduledPublish]:
    cutoff = now + UPCOMING_PUBLISH_WINDOW
    query = """
        WITH candidates AS (
            SELECT 'youtube' AS platform, id::text, brand_id::text, status,
                   upload_due_at AS due_at, 'upload_due_at' AS due_kind
              FROM shorts_youtube_schedules
             WHERE lower(status) IN ('pending','queued','uploading')
               AND (lower(status) = 'uploading'
                    OR upload_due_at BETWEEN %s AND %s
                    OR publish_at BETWEEN %s AND %s)
            UNION ALL
            SELECT 'instagram', id::text, brand_id::text, status,
                   COALESCE(publish_at::timestamp, CURRENT_TIMESTAMP), 'publish_at'
              FROM shorts_instagram_queue
             WHERE lower(status) IN ('pending','queued','uploading','processing','retry')
               AND (lower(status) IN ('uploading','processing')
                    OR publish_at::timestamp BETWEEN %s AND %s)
            UNION ALL
            SELECT 'facebook', id::text, brand_id::text, status,
                   COALESCE(publish_at::timestamp, CURRENT_TIMESTAMP), 'publish_at'
              FROM shorts_facebook_queue
             WHERE lower(status) IN ('pending','queued','uploading','processing','retry')
               AND (lower(status) IN ('uploading','processing')
                    OR publish_at::timestamp BETWEEN %s AND %s)
            UNION ALL
            SELECT 'tiktok', id::text, brand_id::text, status,
                   COALESCE(publish_at::timestamp, CURRENT_TIMESTAMP), 'publish_at'
              FROM shorts_tiktok_queue
             WHERE lower(status) IN ('pending','queued','uploading','processing','retry')
               AND (lower(status) IN ('uploading','processing')
                    OR publish_at::timestamp BETWEEN %s AND %s)
        )
        SELECT platform,id,brand_id,status,due_at,due_kind
          FROM candidates
         ORDER BY due_at,platform,id
    """
    values = [now, cutoff, now, cutoff, now, cutoff, now, cutoff, now, cutoff]
    schedules: list[ScheduledPublish] = []
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, values)
            for platform, schedule_id, brand_id, status, due_at, due_kind in cur.fetchall():
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
                else:
                    due_at = due_at.astimezone(timezone.utc)
                schedules.append(ScheduledPublish(
                    platform=str(platform), schedule_id=str(schedule_id),
                    brand_id=str(brand_id or ""), status=str(status or ""),
                    due_at=due_at, due_kind=str(due_kind),
                ))
    return schedules


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
    if "gokhansaltik" in lowered:
        return True
    return False


def _recent_web_hits(now: datetime) -> list[datetime]:
    cutoff = now - RECENT_WEB_WINDOW
    hits: list[datetime] = []
    for line in reversed(_tail_lines(ACCESS_LOG_PATH)):
        parsed = _parse_access_log_time(line)
        if parsed is None:
            continue
        if parsed < cutoff:
            break
        if not _is_ignored_web_hit(line):
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
        origin = f" | {job.origin}" if job.origin else ""
        print(
            f"  - {job.job_type}{origin} | {job.user_email} | started {_format_age(job.started_at, now)}"
        )


def _is_discovery_job(job: ProcessingJob) -> bool:
    origin = str(job.origin or "").strip().lower()
    job_type = str(job.job_type or "").strip().lower()
    return origin in {"discovery_demo", "discovery_promote"} or job_type.startswith("discovery_")


def main() -> int:
    _load_env_file()
    now = datetime.now(timezone.utc)

    try:
        jobs = _fetch_processing_jobs()
        schedules = _fetch_imminent_schedules(now)
    except Exception as exc:
        print(f"ERROR — could not check processing jobs: {exc}", file=sys.stderr)
        return 2

    if schedules:
        print(f"WAIT — {len(schedules)} schedule(s) are uploading or due within 20 minutes:")
        for item in schedules:
            print(
                f"  - {item.platform} | {item.schedule_id} | brand {item.brand_id or 'unknown'} | "
                f"status {item.status} | {item.due_kind} {item.due_at.isoformat()}"
            )
        if jobs:
            print(f"  Processing job(s) also active: {len(jobs)}")
            _print_processing_jobs(jobs, now)
        return 1

    if jobs:
        customer_jobs = [job for job in jobs if not _is_discovery_job(job)]
        if customer_jobs:
            print(f"WAIT — {len(customer_jobs)} customer/non-discovery job(s) in progress:")
            _print_processing_jobs(customer_jobs, now)
            discovery_jobs = [job for job in jobs if _is_discovery_job(job)]
            if discovery_jobs:
                print(f"  Discovery job(s) also running ({len(discovery_jobs)}), but customer jobs keep deploy blocked:")
                _print_processing_jobs(discovery_jobs, now)
            return 1

        recent_hits = _recent_web_hits(now)
        if recent_hits:
            print(
                "CAUTION — web request(s) in the last 5 min; a restart causes a brief (~5-7s) "
                "outage for anyone currently on the site. Safe to proceed if you accept that."
            )
            print(f"  - recent requests seen: {len(recent_hits)}")
            print(f"  - last request: {_format_age(recent_hits[-1], now)}")
            print(f"  - discovery jobs in progress: {len(jobs)}; deploy may proceed, but restart discovery worker only when idle:")
            _print_processing_jobs(jobs, now)
            return 0

        print(f"SAFE_DEFER_DISCOVERY — {len(jobs)} discovery job(s) in progress:")
        _print_processing_jobs(jobs, now)
        print("deploy now; restart the discovery worker only when idle")
        return 0

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
