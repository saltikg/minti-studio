from app.video_shorts.services.clip_ranges import clip_timing, resolve_clip_ranges


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


def test_clip_timing_counts_hook_twice_but_uses_normal_part_bounds():
    timing = clip_timing(
        keep_ranges=[
            {"start": 20.0, "end": 25.0, "role": "hook", "transition": "flash"},
            {"start": 2.0, "end": 12.0},
            {"start": 20.0, "end": 30.0},
        ],
    )

    assert timing is not None
    assert timing.start == 2.0
    assert timing.end == 30.0
    assert timing.duration == 25.0


def test_resolver_preserves_hook_first_and_overlap():
    resolution = resolve_clip_ranges(
        start=2.0,
        end=30.0,
        keep_ranges=[
            {"start": 20.0, "end": 25.0, "role": "hook", "transition": "flash"},
            {"start": 2.0, "end": 12.0},
            {"start": 20.0, "end": 30.0},
        ],
        media_duration_seconds=60.0,
    )

    assert resolution.ok
    assert resolution.start == 2.0
    assert resolution.end == 30.0
    assert resolution.ranges[0]["role"] == "hook"
    assert resolution.ranges[0]["start"] == resolution.ranges[2]["start"]
