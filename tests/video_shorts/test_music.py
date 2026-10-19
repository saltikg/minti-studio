import math

from app.video_shorts.services.music import (
    build_final_timeline_speech_ranges,
    music_db_for_level,
    music_linear_gain,
    music_volume_expression,
)


def test_music_level_curve_matches_design_formula():
    assert music_linear_gain(0) == 0
    assert music_db_for_level(50) == -20
    assert math.isclose(music_linear_gain(50), 0.1)
    assert music_db_for_level(100) == 0
    assert math.isclose(music_linear_gain(100), 1.0)


def test_speech_ranges_follow_final_cut_timeline_and_merge_short_gaps():
    segments = [
        {"start": 10, "end": 13, "words": [{"start": 10.2, "end": 10.8}, {"start": 11.0, "end": 11.4}]},
        {"start": 20, "end": 21, "words": [{"start": 20.1, "end": 20.7}]},
    ]
    ranges = build_final_timeline_speech_ranges(
        segments,
        clip_start=10,
        clip_end=21,
        edit_keep_ranges=[{"start": 10, "end": 12}, {"start": 20, "end": 21}],
    )
    assert ranges == [
        {"start": 0.2, "end": 1.4},
        {"start": 2.1, "end": 2.7},
    ]


def test_duck_expression_uses_ten_db_reduction_and_ramps():
    expression = music_volume_expression(50, [{"start": 1, "end": 2}], duck=True)
    assert "between(t,1.000,2.000)" in expression
    assert "between(t,0.850,1.000)" in expression
    assert "between(t,2.000,2.150)" in expression
    assert "0.031622777" in expression

