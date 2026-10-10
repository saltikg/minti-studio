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
    selected_min50 = generation._select_auto_render_entries(entries, min_score=50, limit=3)
    _check("auto_selection_min50_includes_65", [entry["plan_index"] for entry in selected_min50] == [2, 1, 3])


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


def test_text_trim_selection_ranges() -> None:
    words = [
        {"text": "Before", "start": 0.0, "end": 0.4},
        {"text": "clip", "start": 0.5, "end": 0.9},
        {"text": "starts", "start": 1.0, "end": 1.4},
        {"text": "kept", "start": 1.5, "end": 2.0},
        {"text": "middle", "start": 2.1, "end": 2.7},
        {"text": "ends", "start": 2.8, "end": 3.2},
        {"text": "after", "start": 3.3, "end": 3.7},
    ]
    current = [{"start": 1.5, "end": 3.3}]
    result = generation.derive_contiguous_clip_keep_range_from_word_selection(words, current, 0, 1, "restore")
    _check("text_trim_restore_before", result["ok"] and result["ranges"][0]["start"] == 0.0 and result["ranges"][0]["end"] == 3.3, str(result))
    result = generation.derive_contiguous_clip_keep_range_from_word_selection(words, current, 6, 6, "restore")
    _check("text_trim_restore_after", result["ok"] and result["ranges"][0]["start"] == 1.5 and result["ranges"][0]["end"] == 3.8, str(result))
    result = generation.derive_contiguous_clip_keep_range_from_word_selection(words, current, 0, 6, "restore")
    _check("text_trim_restore_gap", result["ok"] and result["ranges"][0]["start"] == 0.0 and result["ranges"][0]["end"] == 3.8, str(result))
    result = generation.derive_contiguous_clip_keep_range_from_word_selection(words, current, 3, 4, "keep-only")
    _check("text_trim_keep_only_inside", result["ok"] and result["ranges"][0]["start"] == 1.5 and result["ranges"][0]["end"] == 2.8, str(result))
    ok, message, _ = generation.validate_clip_title_time_edit(title="Tiny", start=result["start"], end=result["end"], video_duration=20)
    _check("text_trim_min_duration_refused", not ok and "at least 5" in message, message)
    ok, message, _ = generation.validate_clip_title_time_edit(title="Huge", start=0, end=91, video_duration=120)
    _check("text_trim_max_duration_refused", not ok and "90 seconds" in message, message)


def test_word_time_mapping() -> None:
    segments = [
        {
            "start": 10.0,
            "end": 14.0,
            "words": [
                {"word": "Relative", "start": 0.0, "end": 0.3},
                {"word": "word.", "start": 0.4, "end": 0.8},
                {"word": "Next", "start": 11.0, "end": 11.4},
            ],
        }
    ]
    words = generation._transcript_words_with_sentence_indexes(segments)
    _check("word_time_relative_start", abs(words[0]["start"] - 10.0) < 0.001, str(words))
    _check("word_time_sentence_index", words[2]["sentence"] == 1, str(words))


if __name__ == "__main__":
    test_auto_selection_two_eligible()
    test_sentence_snapping()
    test_edit_validation_and_lock()
    test_text_trim_selection_ranges()
    test_word_time_mapping()
