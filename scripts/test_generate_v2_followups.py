#!/usr/bin/env python
"""Fast checks for generate v2 follow-up helpers; no renders or planner calls."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.video_shorts.routes import generation


def _check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed {detail}".strip())
    print(f"PASS {name}{(' ' + detail) if detail else ''}")


def test_auto_selection_two_eligible() -> None:
    entries = [
        {"plan_index": 1, "origin": "ai", "score": 78, "status": "pending"},
        {"plan_index": 2, "origin": "ai", "score": 82, "status": "pending"},
        {"plan_index": 3, "origin": "ai", "score": 65, "status": "pending"},
    ]
    selected = generation._select_auto_render_entries(entries, min_score=70, limit=3)
    _check("auto_selection_78_82_65", [entry["plan_index"] for entry in selected] == [2, 1])


def test_sentence_snapping() -> None:
    segments = [
        {
            "start": 0.0,
            "end": 12.0,
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.2},
                {"word": "world.", "start": 0.3, "end": 0.8},
                {"word": "Second", "start": 1.7, "end": 2.0},
                {"word": "idea", "start": 2.1, "end": 2.4},
                {"word": "lands", "start": 2.5, "end": 2.9},
                {"word": "here.", "start": 3.0, "end": 3.4},
                {"word": "Next", "start": 4.2, "end": 4.5},
                {"word": "point", "start": 4.6, "end": 5.0},
            ],
        }
    ]
    entry = {"plan_index": 7, "start": 1.3, "end": 3.8}
    with create_app().app_context():
        result = generation.snap_clip_entry_to_sentence_boundaries(entry, segments, video_id="unit")
    _check("sentence_snap_changed", result["changed"])
    _check("sentence_snap_start", abs(entry["start"] - 1.7) < 0.001, str(entry))
    _check("sentence_snap_end", abs(entry["end"] - 3.4) < 0.001, str(entry))


def test_edit_validation_and_lock() -> None:
    ok, message, payload = generation.validate_clip_title_time_edit(
        title="A useful clip",
        start="0:10.0",
        end="0:30.0",
        video_duration=120,
    )
    _check("edit_validation_ok", ok and payload["duration"] == 20.0, message)
    ok, message, _ = generation.validate_clip_title_time_edit(
        title="Too short",
        start="0:10.0",
        end="0:12.0",
        video_duration=120,
    )
    _check("edit_validation_duration_refused", not ok and "at least 5" in message, message)
    lock_reason = generation.clip_entry_publish_lock_reason({"publish_status": "scheduled"})
    _check("edit_scheduled_refused", lock_reason == "Unschedule first.", lock_reason)
    lock_reason = generation.clip_entry_publish_lock_reason({"publish_status": "not_ready"}, social_statuses={"published"})
    _check("edit_published_refused", lock_reason == "Already published.", lock_reason)


if __name__ == "__main__":
    test_auto_selection_two_eligible()
    test_sentence_snapping()
    test_edit_validation_and_lock()
