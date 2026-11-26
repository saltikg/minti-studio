from scripts.research.apply_niche_channel_classification_v3 import _normalize_result


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
        ["Menopause HRT update", "Workout for women over 50", "Strength training"],
    )

    assert result["primary_focus"] == "fitness_training"
    assert result["core_count"] == 1
    assert result["core_share"] == 1 / 3


def test_normalize_result_defaults_unknown_values_strictly():
    result = _normalize_result(
        {"primary_focus": "wellness-ish", "entity": "brand", "title_results": []},
        ["A title"],
    )

    assert result["primary_focus"] == "other"
    assert result["entity"] == "company_clinic"
    assert result["core_share"] == 0
