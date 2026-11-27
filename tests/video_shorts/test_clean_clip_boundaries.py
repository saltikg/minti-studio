from pathlib import Path

import numpy as np

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
        "start": 661.511,
        "end": 686.565,
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

    assert guarded[0]["start"] == 0.4
    assert guarded[0]["end"] == 0.8
    assert guarded[1]["start"] == 1.2
    assert guarded[1]["end"] == 2.0


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

    assert guarded[0]["start"] == 1.5
    assert guarded[0]["end"] == 3.4


def test_end_cut_stays_in_pause_before_next_removed_word(monkeypatch, tmp_path):
    words = [
        {"index": 0, "start": 0.5, "end": 1.0, "text": "cancer."},
        {"index": 1, "start": 1.3, "end": 1.6, "text": "Work"},
    ]
    ranges = [{
        "start": 0.5,
        "end": 1.0,
        "start_word_index": 0,
        "end_word_index": 0,
    }]
    monkeypatch.setattr(
        compositor,
        "_extract_mono_pcm_window",
        lambda *args, **kwargs: np.zeros(int(kwargs["duration"] * kwargs["sample_rate"]), dtype=np.float32),
    )

    snapped = compositor._snap_keep_range_boundaries_to_audio(
        tmp_path / "source.mp4",
        ranges,
        transcript_words=words,
        sample_rate=1000,
    )

    assert 1.0 <= snapped[0]["end"] < 1.3
    assert snapped[0]["audio_fade_out_seconds"] <= snapped[0]["end"] - 1.0 + 1e-9


def test_back_to_back_word_timings_use_real_audio_pause(monkeypatch, tmp_path):
    words = [
        {"index": 0, "start": 0.5, "end": 1.0, "text": "cancer."},
        {"index": 1, "start": 1.0, "end": 1.4, "text": "Work"},
    ]
    ranges = [{
        "start": 0.5,
        "end": 1.0,
        "start_word_index": 0,
        "end_word_index": 0,
    }]
    sample_rate = 1000

    def synthetic_pause(*args, **kwargs):
        window_start = kwargs["window_start"]
        duration = kwargs["duration"]
        times = window_start + np.arange(int(duration * sample_rate)) / sample_rate
        # The transcript says the words touch at 1.0s, but the waveform has a
        # real pause from 0.96s through 1.12s.
        return np.where((times >= 0.96) & (times < 1.12), 0.0, 0.6).astype(np.float32)

    monkeypatch.setattr(compositor, "_extract_mono_pcm_window", synthetic_pause)

    snapped = compositor._snap_keep_range_boundaries_to_audio(
        tmp_path / "source.mp4",
        ranges,
        transcript_words=words,
        sample_rate=sample_rate,
    )

    assert 0.96 < snapped[0]["end"] <= 1.12
    assert snapped[0]["audio_verified_pause_end"] is True
    fade_start = snapped[0]["end"] - snapped[0]["audio_fade_out_seconds"]
    assert 0.96 <= fade_start < snapped[0]["end"] <= 1.12
    guarded = compositor._guard_keep_ranges_from_unselected_words(snapped, words)
    assert guarded[0]["end"] == snapped[0]["end"]
    resolved = compositor.resolve_clip_ranges(
        start=guarded[0]["start"],
        end=guarded[0]["end"],
        keep_ranges=guarded,
    )
    assert resolved.ranges[0]["audio_verified_pause_end"] is True
    assert resolved.ranges[0]["end"] == snapped[0]["end"]


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


def test_restore_partial_selection_restores_only_selected_words():
    words = [
        {"start": 0.0, "end": 1.0},
        {"start": 1.0, "end": 2.0},
        {"start": 2.0, "end": 3.0},
        {"start": 3.0, "end": 4.0},
        {"start": 4.0, "end": 5.0},
    ]
    current = [{
        "start": 1.08,
        "end": 1.92,
        "start_word_index": 1,
        "end_word_index": 1,
    }]

    restored = generation.derive_clip_keep_ranges_from_word_selection(
        words,
        current,
        3,
        3,
        "restore",
        start_padding=0.04,
        end_padding=0.08,
    )

    assert restored["ok"] is True
    assert restored["ranges"] == [
        {"start": 1.08, "end": 1.92, "start_word_index": 1, "end_word_index": 1},
        {"start": 3.08, "end": 3.92, "start_word_index": 3, "end_word_index": 3},
    ]


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


def test_hook_range_stays_first_and_overlapping_normal_part_is_preserved():
    normalized = generation._normalize_edit_keep_ranges([
        {"start": 20.0, "end": 25.0},
        {"start": 20.0, "end": 24.0, "role": "hook", "transition": "flash",
         "start_word_index": 20, "end_word_index": 24},
        {"start": 2.0, "end": 10.0},
    ])

    assert normalized == [
        {"start": 20.0, "end": 24.0, "start_word_index": 20, "end_word_index": 24,
         "role": "hook", "transition": "flash"},
        {"start": 2.0, "end": 10.0},
        {"start": 20.0, "end": 25.0},
    ]


def test_caption_remap_duplicates_hook_words_on_output_timeline():
    segments = [{
        "start": 0.0,
        "end": 6.0,
        "text": "zero one two three four five",
        "words": [
            {"word": value, "start": float(index), "end": float(index) + 0.8}
            for index, value in enumerate(("zero", "one", "two", "three", "four", "five"))
        ],
    }]
    ranges = [
        {"start": 4.0, "end": 5.0, "start_word_index": 4, "end_word_index": 4,
         "role": "hook", "transition": "flash"},
        {"start": 1.0, "end": 5.0, "start_word_index": 1, "end_word_index": 4},
    ]

    remapped = generation._remap_transcript_segments_to_keep_ranges(segments, ranges)

    assert [segment["text"] for segment in remapped] == ["four", "one two three four"]
    assert remapped[0]["words"][0]["start"] == 0.0
    assert remapped[1]["words"][-1]["start"] == 4.0


def test_face_samples_follow_hook_then_normal_output_timeline(monkeypatch):
    def fake_cached_samples(**kwargs):
        start = float(kwargs["start_seconds"])
        return [{"t": 0.0, "source_t": start, "face_count": int(start)}]

    monkeypatch.setattr(generation, "_cached_face_samples_for_range", fake_cached_samples)
    samples = generation._sample_face_presence_for_layout(
        video_id="hook-smoke",
        source_path=Path("/tmp/not-read.mp4"),
        start_seconds=0.0,
        end_seconds=5.0,
        source_keep_ranges=[
            {"start": 4.0, "end": 5.0, "role": "hook", "transition": "flash"},
            {"start": 0.0, "end": 2.0},
            {"start": 4.0, "end": 5.0},
        ],
    )

    assert [(sample["t"], sample["face_count"]) for sample in samples] == [
        (0.0, 4),
        (1.0, 0),
        (3.0, 4),
    ]


def test_audio_snap_does_not_constrain_out_of_order_hook_to_normal_part(monkeypatch):
    ranges = [
        {"start": 14.7, "end": 15.24, "start_word_index": 40, "end_word_index": 41,
         "role": "hook", "transition": "flash"},
        {"start": 0.0, "end": 9.1, "start_word_index": 0, "end_word_index": 25},
    ]
    words = [
        {"index": index, "start": index * 0.36, "end": (index * 0.36) + 0.30,
         "timing_source": "whisper_word"}
        for index in range(50)
    ]
    monkeypatch.setattr(compositor, "_extract_mono_pcm_window", lambda *args, **kwargs: np.zeros(320000))
    monkeypatch.setattr(
        compositor,
        "_lowest_energy_time_in_pcm_window",
        lambda _pcm, *, search_start, search_end, **kwargs: (search_start + search_end) / 2.0,
    )

    snapped = compositor._snap_keep_range_boundaries_to_audio(
        Path("/tmp/not-read.mp4"),
        ranges,
        transcript_words=words,
    )

    assert snapped[0]["role"] == "hook"
    assert snapped[0]["end"] > snapped[0]["start"] > snapped[1]["start"]
    assert snapped[1]["end"] > snapped[1]["start"]


def test_multi_range_cut_disables_async_resampling_per_part(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(compositor, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(compositor, "_has_audio_stream", lambda _path: True)

    def capture(command, **_kwargs):
        captured["command"] = command

    monkeypatch.setattr(compositor, "run_media_subprocess", capture)
    compositor._cut_clip(
        tmp_path / "source.mp4",
        0.0,
        9.1,
        tmp_path / "output.mp4",
        source_keep_ranges=[
            {"start": 14.44, "end": 15.53, "role": "hook", "transition": "flash"},
            {"start": 0.0, "end": 9.1},
        ],
    )

    filter_graph = captured["command"][captured["command"].index("-filter_complex") + 1]
    assert filter_graph.count("aresample=async=0:first_pts=0") == 2
    assert "aresample=async=1:first_pts=0" not in filter_graph


def test_only_later_source_hook_needs_isolated_decoder():
    assert compositor._out_of_order_leading_hook_index([
        {"start": 20.0, "end": 24.0, "role": "hook"},
        {"start": 2.0, "end": 10.0},
        {"start": 20.0, "end": 25.0},
    ]) == 0
    assert compositor._out_of_order_leading_hook_index([
        {"start": 2.0, "end": 4.0, "role": "hook"},
        {"start": 5.0, "end": 10.0},
    ]) is None
    assert compositor._out_of_order_leading_hook_index([
        {"start": 2.0, "end": 4.0},
        {"start": 5.0, "end": 10.0},
    ]) is None
