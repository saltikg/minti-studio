from scripts.research.apply_niche_channel_classification_v3 import _normalize_result
from scripts.research.apply_niche_channel_classification_v3 import load_mixed_titles


def test_normalize_result_computes_core_share_from_per_title_flags():
    result = _normalize_result(
        {
            "primary_focus": "fitness_training",
            "entity": "individual",
            "reason": "A workout channel.",
            "title_results": [
                {"title": "Menopause HRT update", "core_topic": True},
                {"title": "Workout for women over 50", "core_topic": False},
                {"title": "Strength training", "core_topic": False},
            ],
        },
        [
            {"title": "Menopause HRT update", "format": "long"},
            {"title": "Workout for women over 50", "format": "long"},
            {"title": "Strength training", "format": "short"},
        ],
        {"hormones_midlife_health", "fitness_training", "other"},
    )

    assert result["primary_focus"] == "fitness_training"
    assert result["core_count"] == 1
    assert result["core_share"] == 1 / 3
    assert result["title_count"] == 3


def test_normalize_result_defaults_unknown_values_strictly():
    result = _normalize_result(
        {"primary_focus": "wellness-ish", "entity": "brand", "title_results": []},
        [{"title": "A title", "format": "short"}],
    )

    assert result["primary_focus"] == "other"
    assert result["entity"] == "company_clinic"
    assert result["core_share"] == 0


def test_mixed_title_loader_prefers_long_form_then_fills_with_shorts(monkeypatch):
    monkeypatch.setattr(
        "scripts.research.apply_niche_channel_classification_v3._cached_uploads",
        lambda _channel_id: [
            {"video_id": "s1", "title": "Newest short", "published_at": "2026-11-03"},
            {"video_id": "l1", "title": "Newest long", "published_at": "2026-11-02"},
            {"video_id": "s2", "title": "Older short", "published_at": "2026-11-01"},
        ],
    )

    titles = load_mixed_titles(
        "UC1",
        {
            "s1": {"duration_seconds": 60},
            "l1": {"duration_seconds": 181},
            "s2": {"duration_seconds": 120},
        },
    )

    assert titles == [
        {"title": "Newest long", "format": "long"},
        {"title": "Newest short", "format": "short"},
        {"title": "Older short", "format": "short"},
    ]
