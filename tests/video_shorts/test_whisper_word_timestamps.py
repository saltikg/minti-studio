from types import SimpleNamespace

from app.video_shorts.routes.generation import (
    _flatten_transcript_words_for_snapping,
    _segments_for_indexed_keep_ranges,
)
from app.video_shorts.services.transcript_service import (
    _display_timed_words,
    _unicode_alignment_key,
    _whisper_response_to_segments,
    build_transcript_for_range,
    join_display_timed_words,
    replace_display_word_text,
)


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


def test_display_words_restore_punctuation_without_changing_alignment_words():
    segment = {
        "start": 0.0,
        "end": 3.0,
        "text": "Oran 2.1%, sonra 0.3%.",
        "words": [
            {"word": "Oran", "start": 0.0, "end": 0.5},
            {"word": "2", "start": 0.5, "end": 0.8},
            {"word": "1", "start": 0.8, "end": 1.1},
            {"word": "sonra", "start": 1.2, "end": 1.8},
            {"word": "0", "start": 1.9, "end": 2.2},
            {"word": "3", "start": 2.2, "end": 2.6},
        ],
    }

    display = _display_timed_words(segment)
    merged = _display_timed_words(segment, merge_joined=True)

    assert [word["word"] for word in segment["words"]] == ["Oran", "2", "1", "sonra", "0", "3"]
    assert [word["display_word"] for word in display] == ["Oran", "2.", "1%,", "sonra", "0.", "3%."]
    assert [word["word"] for word in merged] == ["Oran", "2.1%,", "sonra", "0.3%."]


def test_display_words_interpolate_raw_token_missing_from_whisper_timings():
    segment = {
        "start": 0.0,
        "end": 2.0,
        "text": "Ben Amerika'dayım. yok. Buradayım.",
        "words": [
            {"word": "Ben", "start": 0.0, "end": 0.4},
            {"word": "Amerika'dayım", "start": 0.4, "end": 0.9},
            {"word": "Buradayım", "start": 1.3, "end": 1.8},
        ],
    }

    display = _display_timed_words(segment, merge_joined=True)

    assert [word["word"] for word in display] == ["Ben", "Amerika'dayım.", "yok.", "Buradayım."]
    assert display[2]["start"] == 0.9
    assert display[2]["end"] == 1.3
    assert display[2]["_interpolated_display_timing"] is True
    # The timing layer stays untouched and therefore keeps the exact same cut indexes.
    flattened = _flatten_transcript_words_for_snapping([segment])
    assert [(word["start"], word["end"]) for word in flattened] == [
        (0.0, 0.4),
        (0.4, 0.9),
        (1.3, 1.8),
    ]


def test_boundary_transcript_uses_punctuated_text_not_alignment_tokens():
    segment = {
        "start": 0.0,
        "end": 3.0,
        "text": "Oran 2.1%, sonra 0.3%.",
        "words": [
            {"word": "Oran", "start": 0.0, "end": 0.5},
            {"word": "2", "start": 0.5, "end": 0.8},
            {"word": "1", "start": 0.8, "end": 1.1},
            {"word": "sonra", "start": 1.2, "end": 1.8},
            {"word": "0", "start": 1.9, "end": 2.2},
            {"word": "3", "start": 2.2, "end": 2.6},
        ],
    }

    assert build_transcript_for_range([segment], 0.4, 2.7) == "2.1%, sonra 0.3%."


def test_alignment_normalization_is_unicode_aware_for_turkish_letters():
    assert _unicode_alignment_key("Ç Ğ I İ Ö Ş Ü ç ğ ı i ö ş ü") == "çğiiöşüçğıiöşü"

    segment = {
        "start": 0.0,
        "end": 2.0,
        "text": "Çığ, ışığı ölçüşü.",
        "words": [
            {"word": "Çığ", "start": 0.0, "end": 0.5},
            {"word": "ışığı", "start": 0.6, "end": 1.1},
            {"word": "ölçüşü", "start": 1.2, "end": 1.8},
        ],
    }
    assert [word["word"] for word in _display_timed_words(segment, merge_joined=True)] == [
        "Çığ,",
        "ışığı",
        "ölçüşü.",
    ]


def test_adjacent_turkish_tokens_split_by_whisper_keep_raw_spelling():
    segment = {
        "start": 0.0,
        "end": 2.0,
        "text": "%80'i 90'ı Wi-Fi.",
        "words": [
            {"word": "80", "start": 0.0, "end": 0.3},
            {"word": "i", "start": 0.3, "end": 0.5},
            {"word": "90", "start": 0.6, "end": 0.9},
            {"word": "ı", "start": 0.9, "end": 1.1},
            {"word": "Wi", "start": 1.2, "end": 1.5},
            {"word": "Fi", "start": 1.5, "end": 1.8},
        ],
    }

    assert [word["word"] for word in _display_timed_words(segment, merge_joined=True)] == [
        "%80'i",
        "90'ı",
        "Wi-Fi.",
    ]


def test_word_edit_preserves_segment_punctuation_and_percent_signs():
    segment = {
        "text": "Oran 2.1%, tamam.",
        "words": [
            {"word": "Oran", "start": 0.0, "end": 0.5},
            {"word": "2", "start": 0.5, "end": 0.8},
            {"word": "1", "start": 0.8, "end": 1.1},
            {"word": "tamam", "start": 1.2, "end": 1.8},
        ],
    }
    display = _display_timed_words(segment)
    display[2]["display_word"] = replace_display_word_text(
        display[2]["display_word"],
        segment["words"][2]["word"],
        "4",
    )

    assert join_display_timed_words(display) == "Oran 2.4%, tamam."


def test_indexed_clip_transcript_preserves_joined_decimal_tokens():
    segment = {
        "start": 0.0,
        "end": 3.0,
        "text": "Oran 2.1%, sonra 0.3%.",
        "words": [
            {"word": "Oran", "start": 0.0, "end": 0.5},
            {"word": "2", "start": 0.5, "end": 0.8},
            {"word": "1", "start": 0.8, "end": 1.1},
            {"word": "sonra", "start": 1.2, "end": 1.8},
            {"word": "0", "start": 1.9, "end": 2.2},
            {"word": "3", "start": 2.2, "end": 2.6},
        ],
    }
    selected = _segments_for_indexed_keep_ranges(
        [segment],
        [{"start": 0.5, "end": 2.6, "start_word_index": 1, "end_word_index": 5}],
    )

    assert selected[0]["text"] == "2.1%, sonra 0.3%."
