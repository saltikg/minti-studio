from app.video_shorts.services import customer_niche


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _CustomerConnection:
    def execute(self, sql, params=None):
        if "FROM shorts_brands" in sql:
            return _Result([("brand-1", "Brand", 7, "niche-slug", "Niche")])
        raise AssertionError("unexpected query")


def test_connected_channel_is_you_when_source_channels_share_niche(monkeypatch):
    rows = [
        {"youtube_channel_id": "UC-B", "title": "Source B", "subscriber_count": 300},
        {"youtube_channel_id": "UC-A", "title": "Publishing A", "subscriber_count": 200},
        {"youtube_channel_id": "UC-C", "title": "Source C", "subscriber_count": 100},
    ]
    monkeypatch.setattr(
        customer_niche, "load_niche_snapshot",
        lambda *_args, **_kwargs: ({"id": 7, "slug": "niche-slug", "name": "Niche"}, rows),
    )
    monkeypatch.setattr(customer_niche, "_ranked_pool", lambda *_args: rows)
    monkeypatch.setattr(customer_niche, "brand_publishing_channel_id", lambda *_args: "UC-A")
    monkeypatch.setattr(customer_niche, "load_latest_niche_outliers", lambda *_args, **_kwargs: ("", []))
    monkeypatch.setattr(customer_niche, "load_niche_short_ideas", lambda *_args, **_kwargs: ("", []))
    monkeypatch.setattr(customer_niche, "load_brand_weekly_ideas", lambda *_args, **_kwargs: ("", []))
    monkeypatch.setattr(customer_niche, "archive_match_customer_enabled", lambda: False)

    page = customer_niche.load_customer_niche_page(
        _CustomerConnection(), owner_user_id="owner-1", brand_id="brand-1"
    )

    assert page["place"]["rank"] == 2
    assert [row["youtube_channel_id"] for row in page["place"]["neighbors"] if row["is_you"]] == ["UC-A"]


def test_compact_number():
    assert customer_niche.compact_number(28_800) == "28.8K"
    assert customer_niche.compact_number(1_200_000) == "1.2M"
    assert customer_niche.compact_number(720_473) == "720K"
    assert customer_niche.compact_number(999) == "999"


def test_display_short_title_strips_hashtags_and_keeps_nonempty_fallback():
    assert customer_niche.display_short_title("Estrogen facts #menopause #HRT!!!") == "Estrogen facts"
    assert customer_niche.display_short_title("A secret #faith#prayer") == "A secret"
    assert customer_niche.display_short_title("#menopause #HRT") == "#menopause #HRT"


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
