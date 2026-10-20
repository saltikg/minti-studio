#!/usr/bin/env python3
"""Focused checks for interactive render origin and caption pair resolution."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.video_shorts.routes import generation
from app.video_shorts.services.render_jobs import (
    AUTO_RENDER_JOB_ORIGIN,
    AUTO_RENDER_JOB_PRIORITY,
    CUSTOMER_JOB_ORIGIN,
    DISCOVERY_JOB_ORIGIN,
    DISCOVERY_JOB_PRIORITY,
)
from app.video_shorts import worker


def main() -> None:
    original_scope_check = generation._is_discovery_demo_scope
    generation._is_discovery_demo_scope = lambda *_args, **_kwargs: True
    try:
        assert generation._interactive_render_origin(
            owner_user_id="owner",
            brand_id="brand",
            video_pk=1,
            auto_render_job=False,
            discovery_pipeline_job=False,
        ) == (CUSTOMER_JOB_ORIGIN, None)
        assert generation._interactive_render_origin(
            owner_user_id="owner",
            brand_id="brand",
            video_pk=1,
            auto_render_job=False,
            discovery_pipeline_job=True,
        ) == (DISCOVERY_JOB_ORIGIN, DISCOVERY_JOB_PRIORITY)
        assert generation._interactive_render_origin(
            owner_user_id="owner",
            brand_id="brand",
            video_pk=1,
            auto_render_job=True,
            discovery_pipeline_job=False,
        ) == (AUTO_RENDER_JOB_ORIGIN, AUTO_RENDER_JOB_PRIORITY)
    finally:
        generation._is_discovery_demo_scope = original_scope_check

    defaults = {"caption_anchor": "bottom", "sub_margin": 270}
    assert generation._resolve_caption_position(
        stored_anchor="top",
        stored_margin=772,
        margin_custom=False,
        user_defaults=defaults,
    ) == ("top", 520)
    assert generation._resolve_caption_position(
        stored_anchor=None,
        stored_margin=None,
        margin_custom=False,
        user_defaults=defaults,
    ) == ("bottom", 270)
    assert generation._resolve_caption_position(
        stored_anchor="top",
        stored_margin=772,
        margin_custom=True,
        user_defaults=defaults,
    ) == ("top", 772)

    for path in (
        "/video_shorts/generate/7681/render-jobs/job/status",
        "/video_shorts/api/jobs/job-id",
        "/video_shorts/generate/7681/auto-suggest-render-status",
        "/video_shorts/admin/notifications",
        "/video_shorts/api/worker/heartbeat",
    ):
        line = f'127.0.0.1 - - [20/Oct/2026:05:00:00 +0000] "GET {path} HTTP/1.1" 200 10 "-" "Mozilla/5.0"'
        assert worker._is_ignored_web_hit(line), path

    real_page = '127.0.0.1 - - [20/Oct/2026:05:00:00 +0000] "GET /video_shorts/generate/7681 HTTP/1.1" 200 10 "-" "Mozilla/5.0"'
    assert not worker._is_ignored_web_hit(real_page)
    print("RENDER_ORIGIN_CAPTION_RESOLUTION_OK")


if __name__ == "__main__":
    main()
