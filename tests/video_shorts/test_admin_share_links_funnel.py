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

    funnel, side_metrics = _build_admin_share_links_funnel(rows)

    assert [step["key"] for step in funnel] == [
        "sent",
        "viewed",
        "played",
        "watched_75",
        "entered_feed",
        "reached_trial_card",
    ]
    assert [step["count"] for step in funnel] == [2, 2, 2, 1, 1, 1]
    assert all(
        funnel[index]["count"] <= funnel[index - 1]["count"]
        for index in range(1, len(funnel))
    )
    metrics_by_key = {metric["key"]: metric for metric in side_metrics}
    assert metrics_by_key["clicked"]["count"] == 1
    assert metrics_by_key["requested"]["count"] == 2
    assert metrics_by_key["converted"]["count"] == 2
    assert metrics_by_key["requested"]["leads"] == [
        "A — a@example.net",
        "B — b@example.net",
    ]


def test_admin_share_links_account_filter_has_required_exclusions() -> None:
    sql = _admin_share_link_account_filter_sql("sl")

    assert "NOT IN (99, 171, 194)" in sql
    assert "mintistudio.com" in sql
    assert "example.com" in sql
    assert "substr" in sql
    assert "+test@" in sql
