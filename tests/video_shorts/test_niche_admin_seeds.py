from app.video_shorts.services.niche_admin_seeds import SeedRunner, _clean_name


def test_clean_name_matches_case_accents_and_spacing():
    assert _clean_name("Nûr Neva Hizmetten") == _clean_name("nur-neva hizmetten")


def test_resolve_channel_url_uses_channels_lookup(monkeypatch):
    runner = SeedRunner.__new__(SeedRunner)
    expected = {"id": "UC123"}
    monkeypatch.setattr(
        runner,
        "_channels_lookup",
        lambda *, channel_id="", handle="": expected if channel_id == "UC123" else None,
    )

    channel_id, item = runner.resolve("https://www.youtube.com/channel/UC123")

    assert channel_id == "UC123"
    assert item == expected


def test_plain_name_requires_exact_normalized_title(monkeypatch):
    runner = SeedRunner.__new__(SeedRunner)
    runner.niche_slug = "test"
    monkeypatch.setattr(
        runner,
        "youtube_get",
        lambda *_args, **_kwargs: {
            "items": [{"id": {"channelId": "UC123"}, "snippet": {"title": "Herkul Clips"}}]
        },
    )

    channel_id, item = runner.resolve("Herkul")

    assert channel_id == ""
    assert item is None
