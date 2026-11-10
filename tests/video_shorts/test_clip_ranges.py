from app.video_shorts.services.clip_ranges import clip_timing


def test_clip_timing_sums_kept_ranges_and_uses_outer_kept_words():
    timing = clip_timing(
        start=10,
        end=100,
        keep_ranges=[
            {"start": 12.25, "end": 20.5},
            {"start": 72.0, "end": 81.75},
        ],
    )

    assert timing is not None
    assert timing.start == 12.25
    assert timing.end == 81.75
    assert timing.duration == 18.0


def test_clip_timing_falls_back_to_contiguous_span():
    timing = clip_timing(start=15.5, end=29.75)

    assert timing is not None
    assert timing.start == 15.5
    assert timing.end == 29.75
    assert timing.duration == 14.25
