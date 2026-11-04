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
        {"start": 0.0, "end": 2.75, "mode": "fill"},
        {"start": 2.75, "end": 6.0, "mode": "split"},
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
    assert {"start": 8.25, "end": 11.75, "mode": "split"} in segments
