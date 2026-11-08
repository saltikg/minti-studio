import pytest

from app.video_shorts.routes import generation
from app.video_shorts.routes.generation import _build_shot_based_layout_segments


def test_shot_layout_votes_across_the_shot_and_switches_at_the_cut():
    samples = [
        {"t": 0.0, "face_count": 1},
        {"t": 0.5, "face_count": 1},
        {"t": 1.0, "face_count": 1},
        {"t": 2.15, "face_count": 0},
        {"t": 2.4, "face_count": 1},
        {"t": 2.5, "face_count": 2},
        {"t": 3.0, "face_count": 2},
        {"t": 3.5, "face_count": 2},
    ]

    segments, decisions = _build_shot_based_layout_segments(
        samples, duration_seconds=4.0, scene_cuts=[2.0],
    )

    assert segments == [
        {"start": 0.0, "end": 2.0, "mode": "fill"},
        {"start": 2.0, "end": 4.0, "mode": "split"},
    ]
    assert decisions[1]["counts"] == {0: 1, 1: 1, 2: 3}


def test_shot_layout_breaks_count_ties_toward_more_faces():
    segments, decisions = _build_shot_based_layout_segments(
        [{"t": 0.0, "face_count": 1}, {"t": 1.0, "face_count": 2}],
        duration_seconds=3.0,
        scene_cuts=[2.0],
    )

    assert decisions[0]["face_count"] == 2
    assert segments[0]["mode"] == "split"


def test_no_scene_cut_keeps_sample_based_switching():
    segments, decisions = _build_shot_based_layout_segments(
        [
            {"t": 0.0, "face_count": 1},
            {"t": 2.5, "face_count": 1},
            {"t": 3.0, "face_count": 2},
            {"t": 5.5, "face_count": 2},
        ],
        duration_seconds=6.0,
        scene_cuts=[],
    )

    assert decisions == []
    assert segments == [
        {"start": 0.0, "end": 3.0, "mode": "fill"},
        {"start": 3.0, "end": 6.0, "mode": "split"},
    ]


def test_missed_scene_cut_preserves_a_long_split_inside_the_shot():
    samples = [
        {"t": index * 0.5, "face_count": 2 if 8.5 <= index * 0.5 <= 11.5 else 1}
        for index in range(80)
    ]

    segments, decisions = _build_shot_based_layout_segments(
        samples, duration_seconds=40.0, scene_cuts=[20.0],
    )

    assert decisions[0]["mode"] == "fill"
    assert {"start": 8.5, "end": 11.5, "mode": "split"} in segments


def test_confirmed_split_boundary_is_not_moved_to_an_unrelated_nearby_cut(monkeypatch, tmp_path):
    monkeypatch.setattr(generation, "_probe_video_fps", lambda _source_path: 30.0)
    segments = [
        {"start": 0.0, "end": 5.0, "mode": "fill"},
        {"start": 5.0, "end": 11.75, "mode": "split"},
        {"start": 11.75, "end": 15.0, "mode": "fill"},
    ]

    result, snapped = generation._snap_face_aware_segments(
        segments,
        source_path=tmp_path / "source.mp4",
        absolute_start=767.005,
        known_scene_candidates=[(0.25, 767.005 + 12.173)],
    )

    assert result[0]["end"] == 5.0
    assert result[1]["end"] == pytest.approx(11.766667)
    assert snapped[1]["from"] == 11.75


def test_split_transition_uses_shot_cut_between_face_samples():
    segments, _decisions = _build_shot_based_layout_segments(
        [
            {"t": 4.5, "face_count": 1},
            {"t": 5.5, "face_count": 2},
            {"t": 6.0, "face_count": 2},
            {"t": 6.5, "face_count": 2},
            {"t": 7.0, "face_count": 2},
        ],
        duration_seconds=8.0,
        scene_cuts=[5.0],
    )

    assert segments == [
        {"start": 0.0, "end": 5.0, "mode": "fill"},
        {"start": 5.0, "end": 8.0, "mode": "split"},
    ]


def test_real_cut_two_frames_after_first_split_sample_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(generation, "_probe_video_fps", lambda _source_path: 30.0)
    calls = []

    def scene_candidates(_source_path, *, start, duration, cut_samples=None):
        calls.append((start, duration))
        return [(0.73, 105.0 + (2.0 / 30.0))]

    monkeypatch.setattr(generation, "_face_aware_scene_candidates", scene_candidates)
    segments = [
        {"start": 0.0, "end": 5.0, "mode": "fill"},
        {"start": 5.0, "end": 8.0, "mode": "split"},
    ]
    samples = [
        {"t": 4.5, "face_count": 1},
        {"t": 5.0, "face_count": 2},
        {"t": 5.5, "face_count": 2},
    ]

    result, snapped, cuts = generation._snap_mode_transitions_to_real_cuts(
        segments,
        samples,
        source_path=tmp_path / "source.mp4",
        absolute_start=100.0,
    )

    assert result[0]["end"] == pytest.approx(5.066667)
    assert result[1]["start"] == pytest.approx(5.066667)
    assert snapped[0]["reason"] == "real_frame_cut"
    assert snapped[0]["scan_window"] == [4.5, 5.2]
    assert cuts == [pytest.approx(105.066667)]
    assert calls == [(104.5, pytest.approx(0.7))]


def test_face_jump_without_real_cut_uses_first_confirmed_sample(monkeypatch, tmp_path):
    monkeypatch.setattr(generation, "_probe_video_fps", lambda _source_path: 30.0)
    monkeypatch.setattr(generation, "_face_aware_scene_candidates", lambda *args, **kwargs: [])
    segments = [
        {"start": 0.0, "end": 5.0, "mode": "fill"},
        {"start": 5.0, "end": 8.0, "mode": "split"},
    ]
    samples = [
        {"t": 4.5, "face_count": 1},
        {"t": 5.0, "face_count": 2},
        {"t": 5.5, "face_count": 2},
    ]

    result, snapped, cuts = generation._snap_mode_transitions_to_real_cuts(
        segments,
        samples,
        source_path=tmp_path / "source.mp4",
        absolute_start=100.0,
    )

    assert result == segments
    assert snapped[0]["to"] == 5.0
    assert snapped[0]["reason"] == "first_confirmed_sample"
    assert cuts == []


def test_smoothed_mode_without_exact_face_count_still_scans_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(generation, "_probe_video_fps", lambda _source_path: 30.0)
    monkeypatch.setattr(generation, "_face_aware_scene_candidates", lambda *args, **kwargs: [])
    segments = [
        {"start": 0.0, "end": 30.0, "mode": "split"},
        {"start": 30.0, "end": 31.0, "mode": "fill"},
        {"start": 31.0, "end": 35.0, "mode": "split"},
    ]
    samples = [
        {"t": 29.5, "face_count": 2},
        {"t": 30.0, "face_count": 2},
        {"t": 30.5, "face_count": 3},
        {"t": 31.0, "face_count": 2},
    ]

    result, snapped, cuts = generation._snap_mode_transitions_to_real_cuts(
        segments,
        samples,
        source_path=tmp_path / "source.mp4",
        absolute_start=100.0,
    )

    assert result[0]["end"] == 30.5
    assert result[1]["start"] == 30.5
    assert snapped[0]["sample_gap"] == [30.0, 30.5]
    assert snapped[0]["reason"] == "first_confirmed_sample"
    assert cuts == []


def test_multiface_track_picks_nearest_single_face_in_same_shot():
    samples = [
        {"t": 0.0, "faces": [{"cx_ratio": 0.2, "cy_ratio": 0.4, "w_ratio": 0.2, "h_ratio": 0.3}]},
        {"t": 0.5, "faces": [
            {"cx_ratio": 0.22, "cy_ratio": 0.4, "w_ratio": 0.15, "h_ratio": 0.25},
            {"cx_ratio": 0.78, "cy_ratio": 0.4, "w_ratio": 0.25, "h_ratio": 0.35},
        ]},
    ]

    rows = generation._build_face_track_rows(samples, absolute_start=10.0, shot_cuts=[])

    assert rows[1]["found"] is True
    assert rows[1]["cx_ratio"] == pytest.approx(0.22)


def test_multiface_track_uses_largest_face_without_single_face_neighbor():
    samples = [{"t": 0.5, "faces": [
        {"cx_ratio": 0.22, "cy_ratio": 0.4, "w_ratio": 0.15, "h_ratio": 0.25},
        {"cx_ratio": 0.78, "cy_ratio": 0.4, "w_ratio": 0.25, "h_ratio": 0.35},
    ]}]

    rows = generation._build_face_track_rows(samples, absolute_start=10.0, shot_cuts=[])

    assert rows[0]["found"] is True
    assert rows[0]["cx_ratio"] == pytest.approx(0.78)
