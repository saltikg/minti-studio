from collections import Counter

import pytest
import requests

from app.video_shorts.services.niche_admin_seeds import SeedError, SeedRunner, _clean_name


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


def test_youtube_errors_never_include_request_url_or_key(monkeypatch, tmp_path):
    class Response:
        status_code = 404

        def raise_for_status(self):
            raise requests.HTTPError("https://example.test?key=secret-value")

        def json(self):
            return {"error": {"errors": [{"reason": "playlistNotFound"}]}}

    runner = SeedRunner.__new__(SeedRunner)
    runner.session = type("Session", (), {"get": lambda *_args, **_kwargs: Response()})()
    runner.calls = Counter()
    runner.units_used = 0
    runner.searches = 0
    monkeypatch.setattr(
        "app.video_shorts.services.niche_admin_seeds._cache_read",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "app.video_shorts.services.niche_admin_seeds.require_youtube_quota_available",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.video_shorts.services.niche_admin_seeds.research_youtube_api_key",
        lambda: "secret-value",
    )
    monkeypatch.setattr(
        "app.video_shorts.services.niche_admin_seeds.record_youtube_data_api_call",
        lambda **_kwargs: None,
    )

    with pytest.raises(SeedError) as caught:
        runner.youtube_get("playlistItems", "playlistItems.list", {}, tmp_path / "x.json")

    assert str(caught.value) == "playlistItems.list failed: HTTP 404 (playlistNotFound)"
    assert "secret-value" not in str(caught.value)
