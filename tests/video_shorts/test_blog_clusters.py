import json
from datetime import date

from app.video_shorts.services.blog_pipeline import (
    BlogCluster,
    allocate_cluster_topic_counts,
    best_near_duplicate_match,
    effective_cluster_weight,
    rank_clusters_by_deficit,
)
from scripts import blog_judge, blog_pipeline, blog_scout


def _clusters():
    boost_until = date(2026, 12, 10)
    return [
        BlogCluster("doctors", "Doctors", "Clinicians who teach on camera.", "YouTube Shorts for doctors", ("Shorts for doctors",), 20, 60, boost_until),
        BlogCluster("podcasters", "Podcasters", "Interview and conversation shows.", "podcast Shorts", ("Interview clips",), 40, 20, boost_until),
        BlogCluster("general", "General", "Solo educators and coaches.", "YouTube Shorts", (), 40, 20, boost_until),
    ]


def test_effective_weights_and_allocations_follow_pacific_boost_date():
    clusters = _clusters()
    assert [effective_cluster_weight(cluster, today_pt=date(2026, 12, 10)) for cluster in clusters] == [60, 20, 20]
    assert allocate_cluster_topic_counts(clusters, total=15, today_pt=date(2026, 12, 10)) == {
        "doctors": 9,
        "podcasters": 3,
        "general": 3,
    }
    assert allocate_cluster_topic_counts(clusters, total=15, today_pt=date(2026, 12, 11)) == {
        "doctors": 3,
        "podcasters": 6,
        "general": 6,
    }


def test_cluster_rotation_ranks_largest_share_deficit_first():
    ranked = rank_clusters_by_deficit(
        _clusters(),
        ["general"] * 5 + ["podcasters"] * 4 + ["doctors"],
        today_pt=date(2026, 11, 19),
    )
    assert [row["cluster"].slug for row in ranked] == ["doctors", "podcasters", "general"]
    assert round(ranked[0]["deficit"], 2) == 0.5


def test_similarity_filter_catches_same_angle_before_judge():
    existing = [{"title": "How Doctors Can Grow on YouTube in One Hour a Week", "slug": ""}]
    assert best_near_duplicate_match("Grow a Doctor YouTube Channel in One Hour Per Week", existing)
    assert best_near_duplicate_match("Patient Privacy Rules for Camera Setup", existing) is None


def test_judge_prompt_binds_candidate_to_cluster_audience():
    prompt = blog_judge._build_user_prompt(
        [{"id": 7, "title": "Shorts for cardiologists", "status": "candidate", "cluster_slug": "doctors"}],
        [],
        {"doctors": {"audience": "Doctors and clinicians who teach on camera.", "primary_search_phrase": "YouTube Shorts for doctors"}},
    )
    payload = json.loads(prompt)
    assert payload["candidates"][0]["cluster"]["audience"].startswith("Doctors and clinicians")
    assert payload["candidates"][0]["cluster"]["primary_search_phrase"] == "YouTube Shorts for doctors"


def test_title_prompts_require_search_phrases_and_substantial_topics():
    assert "plain, search-style titles" in blog_judge.SYSTEM_PROMPT
    assert "primary_search_phrase" in blog_judge.SYSTEM_PROMPT
    assert "substantial standalone post" in blog_judge.SYSTEM_PROMPT
    assert "plain, search-style titles" in blog_scout.CLUSTER_SCOUT_SYSTEM_PROMPT
    assert "primary_search_phrase" in blog_scout.CLUSTER_SCOUT_SYSTEM_PROMPT
    assert "substantial standalone post" in blog_scout.CLUSTER_SCOUT_SYSTEM_PROMPT


def test_judge_does_not_rewrite_seed_title():
    candidate = {"id": 8, "title": "YouTube Shorts for Doctors: The Complete Guide", "source_type": "seed"}
    decisions, repairs = blog_judge._validate_title_distance(
        candidates=[candidate],
        decisions=[{"id": 8, "decision": "accept", "our_title": "An Abstract Rewrite"}],
        existing=[],
    )
    assert repairs == []
    assert decisions[0]["our_title"] == "YouTube Shorts for Doctors: The Complete Guide"
    assert blog_judge._title_for_update(candidate, decisions[0]) is None


def test_doctors_reviewer_adds_medical_and_privacy_blocking_rules():
    doctors = blog_pipeline._reviewer_prompt("doctors")
    general = blog_pipeline._reviewer_prompt("general")
    assert "medical advice" in doctors
    assert "patient-identifiable information" in doctors
    assert "medical advice" not in general
