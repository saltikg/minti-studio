"""Claim and execute at most one publish job for the scheduler service."""
from __future__ import annotations

import os
import socket

from app import create_app
from app.video_shorts.services.render_jobs import JOB_TYPE_PUBLISH_SHORT
from app.video_shorts.worker import process_next_job


def main() -> None:
    app = create_app()
    worker_id = os.getenv("MINTI_SCHEDULER_WORKER_ID") or (
        f"scheduler:{socket.gethostname()}:{os.getpid()}"
    )
    process_next_job(app, worker_id, job_type=JOB_TYPE_PUBLISH_SHORT)


if __name__ == "__main__":
    main()
