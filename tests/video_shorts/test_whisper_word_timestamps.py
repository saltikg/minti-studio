from types import SimpleNamespace

from app.video_shorts.routes.generation import _flatten_transcript_words_for_snapping
from app.video_shorts.services.transcript_service import _whisper_response_to_segments


def test_top_level_whisper_words_are_assigned_to_segments_without_synthesis():
    response = SimpleNamespace(
        text="Well estrogen. So this.",
        segments=[
            SimpleNamespace(start=10.0, end=11.2, duration=1.2, text="Well estrogen."),
            SimpleNamespace(start=11.2, end=12.0, duration=0.8, text="So this."),
        ],
        words=[
            SimpleNamespace(word="Well", start=10.08, end=10.31),
            SimpleNamespace(word="estrogen.", start=10.35, end=11.05),
            SimpleNamespace(word="So", start=11.28, end=11.45),
            SimpleNamespace(word="this.", start=11.49, end=11.92),
        ],
    )

    full_text, segments = _whisper_response_to_segments(response)

    assert full_text == response.text
    assert [[word["word"] for word in segment["words"]] for segment in segments] == [
        ["Well", "estrogen."],
        ["So", "this."],
    ]
    assert segments[0]["words"][0]["start"] == 10.08
    assert segments[1]["words"][1]["end"] == 11.92
    assert all(segment["word_timing_source"] == "whisper_word" for segment in segments)


def test_missing_whisper_words_are_marked_synthetic():
    response = SimpleNamespace(
        text="One two three",
        segments=[SimpleNamespace(start=2.0, end=5.0, duration=3.0, text="One two three")],
        words=[],
    )

    _, segments = _whisper_response_to_segments(response)

    assert segments[0]["word_timing_source"] == "synthetic"
    assert [word["start"] for word in segments[0]["words"]] == [2.0, 3.0, 4.0]
    assert all(word["timing_source"] == "synthetic" for word in segments[0]["words"])


def test_legacy_uniform_word_timings_are_recognized_as_synthetic():
    words = _flatten_transcript_words_for_snapping(
        [
            {
                "start": 10.0,
                "end": 11.2,
                "text": "Well estrogen changes",
                "words": [
                    {"word": "Well", "start": 10.0, "end": 10.4},
                    {"word": "estrogen", "start": 10.4, "end": 10.8},
                    {"word": "changes", "start": 10.8, "end": 11.2},
                ],
            }
        ]
    )

    assert [word["timing_source"] for word in words] == ["synthetic"] * 3
