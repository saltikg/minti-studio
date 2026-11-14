from app.video_shorts.routes.generation import (
    _admin_share_link_account_filter_sql,
    _build_admin_share_links_funnel,
)


def test_admin_share_links_funnel_is_unique_and_cumulative() -> None:
    rows = [
        {
            "share_link_id": 1,
            "recipient_key": "lead-a",
            "recipient_name": "A",
            "recipient_email": "a@example.net",
            "sent": True,
            "opened": True,
            "played": True,
            "watched_75": True,
            "entered_feed": True,
            "reached_trial_card": True,
            "requested": True,
            "converted": True,
            "clicked": True,
        },
        {
            "share_link_id": 2,
            "recipient_key": "lead-a",
            "recipient_name": "A duplicate link",
            "recipient_email": "a@example.net",
            "sent": True,
            "opened": True,
            "played": True,
            "watched_75": False,
            "entered_feed": False,
            "reached_trial_card": False,
            "requested": False,
            "converted": False,
            "clicked": True,
        },
        {
            "share_link_id": 3,
            "recipient_key": "lead-b",
            "recipient_name": "B",
            "recipient_email": "b@example.net",
            "sent": True,
            "opened": True,
            "played": True,
            "watched_75": False,
            "entered_feed": True,
            "reached_trial_card": True,
            "requested": True,
            "converted": True,
            "clicked": False,
        },
    ]

    funnel, clicked = _build_admin_share_links_funnel(rows)

    assert [step["key"] for step in funnel] == [
        "sent",
        "viewed",
        "played",
        "watched_75",
        "entered_feed",
        "reached_trial_card",
        "requested",
        "converted",
    ]
    assert [step["count"] for step in funnel] == [2, 2, 2, 1, 1, 1, 1, 1]
    assert all(
        funnel[index]["count"] <= funnel[index - 1]["count"]
        for index in range(1, len(funnel))
    )
    assert funnel[-2]["leads"] == ["A — a@example.net"]
    assert funnel[-1]["leads"] == ["A — a@example.net"]
    assert clicked["count"] == 1


def test_admin_share_links_account_filter_has_required_exclusions() -> None:
    sql = _admin_share_link_account_filter_sql("sl")

    assert "NOT IN (99, 171, 194)" in sql
    assert "mintistudio.com" in sql
    assert "example.com" in sql
    assert "test%@%" in sql
