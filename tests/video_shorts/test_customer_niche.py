from app.video_shorts.services import customer_niche


def test_compact_number():
    assert customer_niche.compact_number(28_800) == "28.8K"
    assert customer_niche.compact_number(1_200_000) == "1.2M"
    assert customer_niche.compact_number(999) == "999"


def test_top_outlier_cards_filters_promoted_and_caps_channels():
    pool = {
        "UC1": {"title": "Channel One"},
        "UC2": {"title": "Channel Two"},
    }
    rows = [
        {
            "youtube_channel_id": "UC1", "video_id": "one", "title": "One",
            "views": 20_000, "outlier_ratio": 5, "duration_s": 30,
            "engagement": 0.02, "baseline_engagement": 0.02,
            "on_topic": True,
        },
        {
            "youtube_channel_id": "UC1", "video_id": "two", "title": "Two",
            "views": 18_000, "outlier_ratio": 4, "duration_s": 40,
            "engagement": 0.02, "baseline_engagement": 0.02,
            "on_topic": True,
        },
        {
            "youtube_channel_id": "UC1", "video_id": "three", "title": "Three",
            "views": 17_000, "outlier_ratio": 3.5, "duration_s": 50,
            "engagement": 0.02, "baseline_engagement": 0.02,
            "on_topic": True,
        },
        {
            "youtube_channel_id": "UC2", "video_id": "promoted", "title": "Promoted",
            "views": 50_000, "outlier_ratio": 8, "duration_s": 20,
            "engagement": 0.002, "baseline_engagement": 0.01,
            "on_topic": True,
        },
        {
            "youtube_channel_id": "UC2", "video_id": "small", "title": "Small",
            "views": 4_999, "outlier_ratio": 6, "duration_s": 25,
            "engagement": 0.02, "baseline_engagement": 0.01,
            "on_topic": True,
        },
        {
            "youtube_channel_id": "UC2", "video_id": "off-topic", "title": "First Aid",
            "views": 100_000, "outlier_ratio": 10, "duration_s": 45,
            "engagement": 0.02, "baseline_engagement": 0.01,
            "on_topic": False,
        },
    ]

    cards = customer_niche._top_outlier_cards(
        rows,
        pool_by_channel=pool,
        own_channel_ids={"UC1"},
    )

    assert [card["video_id"] for card in cards] == ["one", "two"]
    assert all(card["channel_name"] == "You" for card in cards)
