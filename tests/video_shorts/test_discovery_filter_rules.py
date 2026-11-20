from app.video_shorts.routes.api import _hard_numeric_disqualification_reason


def _row(subscribers: int, shorts: int = 0):
    return {
        "subscriber_count": subscribers,
        "longform_last_60d": 2,
        "shorts_last_15d": shorts,
    }


def test_hard_gate_accepts_one_thousand_subscribers():
    row = _row(1000)
    assert _hard_numeric_disqualification_reason(row) is None
    assert row["already_posting_shorts"] is False


def test_hard_gate_rejects_below_one_thousand_subscribers():
    assert "< 1000" in _hard_numeric_disqualification_reason(_row(999))


def test_frequent_shorts_are_flagged_not_rejected():
    row = _row(10_000, shorts=9)
    assert _hard_numeric_disqualification_reason(row) is None
    assert row["already_posting_shorts"] is True
