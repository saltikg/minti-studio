from app.video_shorts.services.transcript_service import _normalize_word_highlight_overlay_specs


def test_caption_overlay_timeline_bridges_small_gaps_and_holds_last_event():
    specs = [
        {"path": "first.png", "start": 1.0, "end": 1.2},
        {"path": "second.png", "start": 1.45, "end": 1.7},
        {"path": "last.png", "start": 2.0, "end": 2.82},
    ]

    normalized = _normalize_word_highlight_overlay_specs(specs, clip_duration=2.89)

    assert [(item["start"], item["end"]) for item in normalized] == [
        (1.0, 1.45),
        (1.45, 1.7),
        (2.0, 2.89),
    ]


def test_caption_overlay_timeline_orders_events_and_removes_overlaps():
    specs = [
        {"path": "last.png", "start": 1.7, "end": 2.0},
        {"path": "first.png", "start": 1.0, "end": 1.8},
        {"path": "duplicate.png", "start": 1.7, "end": 1.75},
    ]

    normalized = _normalize_word_highlight_overlay_specs(specs, clip_duration=2.1)

    assert [item["path"] for item in normalized] == ["first.png", "last.png"]
    assert all(
        float(left["end"]) <= float(right["start"])
        for left, right in zip(normalized, normalized[1:])
    )
    assert normalized[-1]["end"] == 2.1
