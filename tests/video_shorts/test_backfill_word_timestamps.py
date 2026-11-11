from scripts.backfill_word_timestamps import align_word_timings


def test_alignment_preserves_words_and_interpolates_unmatched_word():
    segments = [
        {
            "start": 0.0,
            "end": 4.0,
            "text": "Well estrogen really changes",
            "words": [
                {"word": "Well", "start": 0.0, "end": 1.0},
                {"word": "estrogen", "start": 1.0, "end": 2.0},
                {"word": "really", "start": 2.0, "end": 3.0},
                {"word": "changes", "start": 3.0, "end": 4.0},
            ],
        }
    ]
    whisper_words = [
        {"text": "Well,", "normalized": "well", "start": 0.2, "end": 0.5},
        {"text": "estrogen", "normalized": "estrogen", "start": 0.6, "end": 1.4},
        {"text": "changes.", "normalized": "changes", "start": 2.4, "end": 3.2},
    ]

    updated, report = align_word_timings(segments, whisper_words)

    assert [word["word"] for word in updated[0]["words"]] == [
        "Well",
        "estrogen",
        "really",
        "changes",
    ]
    assert updated[0]["words"][0]["start"] == 0.2
    assert updated[0]["words"][2]["start"] == 1.4
    assert updated[0]["words"][2]["end"] == 2.4
    assert all(word["timing_source"] == "whisper_word" for word in updated[0]["words"])
    assert report["matched_words"] == 3
    assert report["match_rate"] == 0.75


def test_unmatched_edge_word_remains_unresolved():
    segments = [
        {
            "words": [
                {"word": "extra", "start": 0.0, "end": 0.3},
                {"word": "matched", "start": 0.3, "end": 1.0},
            ]
        }
    ]
    whisper_words = [
        {"text": "matched", "normalized": "matched", "start": 0.4, "end": 0.9},
    ]

    _, report = align_word_timings(segments, whisper_words)

    assert report["match_rate"] == 0.5
    assert report["unresolved_indexes"] == [0]
