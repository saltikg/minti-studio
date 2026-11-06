from app.video_shorts.routes import generation
from app.video_shorts.services import compositor


def test_unselected_word_guards_clamp_shelley_cut_on_both_edges():
    words = [
        {"index": 1695, "start": 661.232, "end": 661.511, "text": "here."},
        {"index": 1696, "start": 661.511, "end": 661.790, "text": "So"},
        {"index": 1765, "start": 686.286, "end": 686.565, "text": "labs."},
        {"index": 1766, "start": 686.565, "end": 686.844, "text": "So"},
        {"index": 1767, "start": 686.844, "end": 687.122, "text": "work"},
    ]
    ranges = [{
        "start": 661.436,
        "end": 686.955,
        "start_word_index": 1696,
        "end_word_index": 1765,
    }]

    guarded = compositor._guard_keep_ranges_from_unselected_words(ranges, words)

    assert guarded == [{
        "start": 661.591,
        "end": 686.485,
        "start_word_index": 1696,
        "end_word_index": 1765,
    }]


def test_unselected_word_guards_cover_every_multi_range_edge():
    words = [
        {"index": 0, "start": 0.0, "end": 0.4},
        {"index": 1, "start": 0.4, "end": 0.8},
        {"index": 2, "start": 0.8, "end": 1.2},
        {"index": 3, "start": 1.2, "end": 1.6},
        {"index": 4, "start": 1.6, "end": 2.0},
        {"index": 5, "start": 2.0, "end": 2.4},
    ]
    ranges = [
        {"start": 0.3, "end": 0.9, "start_word_index": 1, "end_word_index": 1},
        {"start": 1.1, "end": 2.1, "start_word_index": 3, "end_word_index": 4},
    ]

    guarded = compositor._guard_keep_ranges_from_unselected_words(ranges, words)

    assert guarded[0]["start"] == 0.48
    assert guarded[0]["end"] == 0.72
    assert guarded[1]["start"] == 1.28
    assert guarded[1]["end"] == 1.92


def test_audio_snap_cannot_exceed_selected_word_padding_without_neighbors():
    words = [
        {"index": 0, "start": 2.0, "end": 2.4},
        {"index": 1, "start": 2.4, "end": 2.8},
    ]
    ranges = [{
        "start": 1.5,
        "end": 3.4,
        "start_word_index": 0,
        "end_word_index": 1,
    }]

    guarded = compositor._guard_keep_ranges_from_unselected_words(ranges, words)

    assert guarded[0]["start"] == 1.95
    assert guarded[0]["end"] == 2.88


def test_new_word_selection_ranges_never_pad_into_neighbors():
    words = [
        {"start": 0.0, "end": 1.0},
        {"start": 1.0, "end": 2.0},
        {"start": 2.0, "end": 3.0},
    ]

    selected = generation._range_from_word_indexes(
        words,
        1,
        1,
        start_padding=0.04,
        end_padding=0.08,
    )

    assert selected == {
        "start": 1.08,
        "end": 1.92,
        "start_word_index": 1,
        "end_word_index": 1,
    }


def test_indexed_caption_text_excludes_unselected_neighbors():
    segments = [{
        "start": 0.0,
        "end": 2.0,
        "text": "before selected after",
        "words": [
            {"start": 0.0, "end": 0.5, "word": "before"},
            {"start": 0.5, "end": 1.0, "word": "selected"},
            {"start": 1.0, "end": 1.5, "word": "after"},
        ],
    }]
    ranges = [{
        "start": 0.58,
        "end": 0.92,
        "start_word_index": 1,
        "end_word_index": 1,
    }]

    selected_segments = generation._segments_for_indexed_keep_ranges(segments, ranges)
    remapped = generation._remap_transcript_segments_to_keep_ranges(selected_segments, ranges)

    assert [word["word"] for word in remapped[0]["words"]] == ["selected"]
    assert remapped[0]["text"] == "selected"
