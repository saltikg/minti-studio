#!/usr/bin/env python3
"""Fast unit checks for the transcript-ready auto suggest/render workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.routes import generation
from app.video_shorts.services import render_jobs


@dataclass
class _Row:
    value: Any

    def fetchone(self):
        return self.value


class _FakeConn:
    def execute(self, sql: str, params: list[Any] | None = None):
        if "SELECT role FROM shorts_users" in sql:
            return _Row(["member"])
        return _Row(None)

    def close(self):
        pass


def _patch_common(monkey: dict[str, Any]) -> None:
    for name, value in monkey.items():
        setattr(generation, name, value)


def _restore_common(originals: dict[str, Any]) -> None:
    for name, value in originals.items():
        setattr(generation, name, value)


def main() -> int:
    originals = {
        name: getattr(generation, name)
        for name in (
            "AUTO_SUGGEST_RENDER_ENABLED",
            "AUTO_SUGGEST_RENDER_USER_IDS",
            "AUTO_RENDER_TOP_N",
            "AUTO_RENDER_MIN_SCORE",
            "_is_discovery_demo_scope",
            "_owner_is_guest_user",
            "enqueue_worker_job",
            "_fetch_scoped_video_row_with_scope",
            "get_db_readonly",
            "_load_auto_suggest_render_meta",
            "_write_auto_suggest_render_meta",
            "_load_plan_entries",
            "_generate_clip_plan_for_video",
            "apply_autopilot_design_to_video",
            "is_autopilot_quota_exempt",
            "_enqueue_auto_render_for_plan_entry",
        )
    }
    try:
        owner = "user-1"
        brand = "brand-1"
        video_pk = 42
        video_id = "abc123"
        enqueued_jobs: list[dict[str, Any]] = []

        def fake_enqueue_worker_job(**kwargs):
            enqueued_jobs.append(kwargs)
            return {"kind": "queued", "job": {"id": "auto-job-1"}}

        _patch_common(
            {
                "AUTO_SUGGEST_RENDER_ENABLED": False,
                "AUTO_SUGGEST_RENDER_USER_IDS": {owner},
                "_is_discovery_demo_scope": lambda *args, **kwargs: False,
                "_owner_is_guest_user": lambda *args, **kwargs: False,
                "enqueue_worker_job": fake_enqueue_worker_job,
            }
        )
        result = generation.enqueue_auto_suggest_render_job_after_transcript(
            owner_user_id=owner,
            brand_id=brand,
            video_pk=video_pk,
            video_id=video_id,
        )
        assert result and result["kind"] == "queued"
        assert enqueued_jobs[-1]["payload"]["auto_suggest_mode"] == "suggestions_only"

        generation.AUTO_SUGGEST_RENDER_ENABLED = True
        result = generation.enqueue_auto_suggest_render_job_after_transcript(
            owner_user_id=owner,
            brand_id=brand,
            video_pk=video_pk,
            video_id=video_id,
        )
        assert result and result["kind"] == "queued"
        assert enqueued_jobs[-1]["job_type"] == render_jobs.JOB_TYPE_AUTO_SUGGEST_RENDER
        assert enqueued_jobs[-1]["payload"]["auto_suggest_mode"] == "legacy"

        generation._is_discovery_demo_scope = lambda *args, **kwargs: True
        assert generation.enqueue_auto_suggest_render_job_after_transcript(
            owner_user_id=owner,
            brand_id=brand,
            video_pk=video_pk,
            video_id=video_id,
        ) is None

        generation._is_discovery_demo_scope = lambda *args, **kwargs: False
        entries = [
            {"plan_index": 1, "origin": "ai", "score": 99, "status": "pending"},
            {"plan_index": 2, "origin": "ai", "score": 60, "status": "pending"},
            {"plan_index": 3, "origin": "ai", "score": 90, "status": "created"},
            {"plan_index": 4, "origin": "ai", "score": 88, "status": "pending"},
            {"plan_index": 5, "origin": "manual", "score": 100, "status": "pending"},
            {"plan_index": 6, "origin": "ai", "score": 70, "status": "pending"},
            {"plan_index": 7, "origin": "ai", "score": 75, "status": "pending", "render_job_id": "busy"},
        ]
        selected = generation._select_auto_render_entries(entries, min_score=70, limit=3)
        assert [item["plan_index"] for item in selected] == [1, 4, 6]

        plan_entries: list[dict[str, Any]] = []
        planner_calls = {"count": 0}
        defaults_calls = {"count": 0}
        render_calls: list[int] = []
        meta_payloads: list[dict[str, Any]] = []

        def fake_planner(*args, **kwargs):
            planner_calls["count"] += 1
            plan_entries[:] = [
                {"plan_index": 1, "origin": "ai", "score": 85, "status": "pending", "planner": "v1"},
                {"plan_index": 2, "origin": "ai", "score": 91, "status": "pending", "planner": "v1"},
                {"plan_index": 3, "origin": "ai", "score": 72, "status": "pending", "planner": "v1"},
                {"plan_index": 4, "origin": "ai", "score": 65, "status": "pending", "planner": "v1"},
            ]
            return {"clip_count": 4}

        _patch_common(
            {
                "AUTO_RENDER_TOP_N": 3,
                "AUTO_RENDER_MIN_SCORE": 70,
                "_fetch_scoped_video_row_with_scope": lambda *args, **kwargs: [
                    video_pk,
                    video_id,
                    "Title",
                    120,
                    "done",
                    "downloaded",
                ],
                "get_db_readonly": lambda: _FakeConn(),
                "_load_auto_suggest_render_meta": lambda _video_id, **_kwargs: {},
                "_write_auto_suggest_render_meta": lambda _video_id, payload, **_kwargs: meta_payloads.append(payload),
                "_load_plan_entries": lambda _video_id, **_kwargs: list(plan_entries),
                "_generate_clip_plan_for_video": fake_planner,
                "apply_autopilot_design_to_video": lambda **kwargs: defaults_calls.__setitem__("count", defaults_calls["count"] + 1) or {"updated": True},
                "is_autopilot_quota_exempt": lambda *args, **kwargs: True,
                "_enqueue_auto_render_for_plan_entry": lambda **kwargs: render_calls.append(int(kwargs["plan_index"])) or {"ok": True, "job_id": f"job-{kwargs['plan_index']}"},
            }
        )
        app = Flask(__name__)
        with app.app_context():
            payload = {"owner_user_id": owner, "brand_id": brand, "video_pk": video_pk, "video_id": video_id}
            run_result = generation.execute_auto_suggest_render_job(payload)
        assert planner_calls["count"] == 1
        assert defaults_calls["count"] == 1
        assert render_calls == [2, 1, 3]
        assert [item["plan_index"] for item in run_result["selected"]] == [2, 1, 3]
        assert meta_payloads[-1]["status"] == "completed"

        marker_count = len(meta_payloads)
        generation._load_auto_suggest_render_meta = lambda _video_id, **_kwargs: {"auto_run_at": "2026-01-01T00:00:00Z"}
        with app.app_context():
            second = generation.execute_auto_suggest_render_job(payload)
        assert second["skip_reason"] == "already_ran"
        assert planner_calls["count"] == 1
        assert len(meta_payloads) == marker_count

        generation._load_auto_suggest_render_meta = lambda _video_id, **_kwargs: {}
        generation._load_plan_entries = lambda _video_id, **_kwargs: []
        generation._generate_clip_plan_for_video = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("planner crashed"))
        try:
            with app.app_context():
                generation.execute_auto_suggest_render_job(payload)
        except RuntimeError as exc:
            assert str(exc) == "planner crashed"
        else:
            raise AssertionError("planner crash should propagate to the worker")
        assert len(meta_payloads) == marker_count

        assert 0 > render_jobs.AUTO_RENDER_JOB_PRIORITY > render_jobs.DISCOVERY_JOB_PRIORITY

        print("PASS gate_off_no_job")
        print("PASS gate_on_job_enqueued")
        print("PASS discovery_video_no_job")
        print("PASS selection_top3_min70_skip_created")
        print("PASS second_run_noop")
        print("PASS crashed_run_does_not_write_marker")
        print("PASS null_design_defaults_applied")
        print(
            "PASS claim_order manual_priority=0 auto_priority=%s discovery_priority=%s"
            % (render_jobs.AUTO_RENDER_JOB_PRIORITY, render_jobs.DISCOVERY_JOB_PRIORITY)
        )
        return 0
    finally:
        _restore_common(originals)


if __name__ == "__main__":
    raise SystemExit(main())
