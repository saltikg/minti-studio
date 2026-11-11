"""Dedicated scheduler heartbeat and single-lane publish worker."""
from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from app.video_shorts.config import (
    DISCOVERY_IDLE_MEM_AVAILABLE_MB_MIN,
    JOB_POLL_INTERVAL_SECONDS,
)
from app.video_shorts.services import scheduler_reachability, youtube_scheduling
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.render_jobs import JOB_TYPE_PUBLISH_SHORT


logger = logging.getLogger(__name__)
SCHEDULER_ADVISORY_LOCK_ID = 746192442
TICK_INTERVAL_SECONDS = max(2.0, min(5.0, float(os.getenv("SCHEDULER_TICK_SECONDS", "3") or "3")))


def _mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(int(line.split()[1]) / 1024)
    except Exception:
        logger.exception("Scheduler could not read MemAvailable")
    return 0


def _rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        logger.exception("Scheduler could not read RSS")
    return 0.0


def _worker_id() -> str:
    return f"scheduler:{socket.gethostname()}:{os.getpid()}"


def _publish_job_waiting() -> bool:
    conn = get_db_readonly()
    try:
        row = conn.execute(
            "SELECT 1 FROM shorts_render_jobs WHERE status = 'queued' AND type = ? LIMIT 1",
            [JOB_TYPE_PUBLISH_SHORT],
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def _acquire_singleton_lock():
    conn = get_db()
    try:
        if getattr(conn, "backend_name", "") == "postgres":
            row = conn.execute(
                "SELECT pg_try_advisory_lock(?)",
                [SCHEDULER_ADVISORY_LOCK_ID],
            ).fetchone()
            conn.commit()
            if not row or not bool(row[0]):
                raise RuntimeError("Another minti scheduler instance owns the advisory lock")
        return conn
    except Exception:
        conn.close()
        raise


def _publish_loop(app, stop_event: threading.Event, worker_id: str) -> None:
    while not stop_event.is_set():
        available_mb = _mem_available_mb()
        if available_mb < int(DISCOVERY_IDLE_MEM_AVAILABLE_MB_MIN):
            app.logger.warning(
                "Scheduler publish claim paused MemAvailable=%sMB threshold=%sMB",
                available_mb,
                DISCOVERY_IDLE_MEM_AVAILABLE_MB_MIN,
            )
            stop_event.wait(JOB_POLL_INTERVAL_SECONDS)
            continue
        try:
            waiting = _publish_job_waiting()
        except Exception:
            app.logger.exception("Scheduler publish queue check failed")
            waiting = False
        if not waiting:
            stop_event.wait(JOB_POLL_INTERVAL_SECONDS)
            continue
        env = os.environ.copy()
        env["MINTI_SCHEDULER_WORKER_ID"] = worker_id
        result = subprocess.run(
            [sys.executable, "-m", "app.video_shorts.publish_worker_once"],
            env=env,
            check=False,
        )
        if result.returncode != 0:
            app.logger.error("Scheduler publish child exited code=%s", result.returncode)
            stop_event.wait(JOB_POLL_INTERVAL_SECONDS)


def run_tick_loop(
    app,
    stop_event: threading.Event,
    *,
    interval_seconds: float = TICK_INTERVAL_SECONDS,
    singleton_conn=None,
) -> None:
    next_tick = time.monotonic()
    while not stop_event.is_set():
        if singleton_conn is not None:
            # A broken session has released the PostgreSQL session lock. Exit so
            # systemd can restart instead of ticking without singleton ownership.
            singleton_conn.execute("SELECT 1").fetchone()
            singleton_conn.commit()
        try:
            youtube_scheduling.scheduler_tick()
            scheduler_reachability.process_monitors()
        except Exception:
            app.logger.exception("Dedicated scheduler tick failed")
        next_tick += interval_seconds
        delay = max(0.0, next_tick - time.monotonic())
        if delay == 0.0:
            next_tick = time.monotonic()
        stop_event.wait(delay)


def run_scheduler_service(*, stop_event: Optional[threading.Event] = None) -> None:
    logging.basicConfig(level=logging.INFO)
    app = SimpleNamespace(logger=logger)
    stop_event = stop_event or threading.Event()
    lock_conn = _acquire_singleton_lock()
    worker_id = _worker_id()

    def _request_stop(_signum, _frame):
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)

    app.logger.info(
        "Minti scheduler starting worker_id=%s tick_seconds=%s rss_mb=%s mem_available_mb=%s",
        worker_id,
        TICK_INTERVAL_SECONDS,
        _rss_mb(),
        _mem_available_mb(),
    )
    publish_thread = None
    try:
        try:
            scheduler_reachability.run_startup_check(trigger="scheduler_start")
        except Exception:
            app.logger.exception("Scheduler reachability startup check failed")
        publish_thread = threading.Thread(
            target=_publish_loop,
            args=(app, stop_event, worker_id),
            name="minti-publish-worker",
            daemon=False,
        )
        publish_thread.start()
        run_tick_loop(app, stop_event, singleton_conn=lock_conn)
    finally:
        stop_event.set()
        if publish_thread is not None:
            publish_thread.join()
        try:
            if getattr(lock_conn, "backend_name", "") == "postgres":
                lock_conn.execute(
                    "SELECT pg_advisory_unlock(?)",
                    [SCHEDULER_ADVISORY_LOCK_ID],
                )
                lock_conn.commit()
        finally:
            lock_conn.close()


def main() -> None:
    run_scheduler_service()


if __name__ == "__main__":
    main()
