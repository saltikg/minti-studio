from app.video_shorts.services import niche_rising_topics


class _Response:
    def __init__(self, payload):
        self.choices = [type("Choice", (), {"message": type("Message", (), {"content": payload})()})()]
        self.usage = type("Usage", (), {"prompt_tokens": 10, "completion_tokens": 5})()


class _Client:
    def __init__(self, payload):
        self.chat = type(
            "Chat", (),
            {"completions": type("Completions", (), {"create": lambda _self, **_kwargs: _Response(payload)})()},
        )()


def test_eligible_rows_excludes_off_topic_and_possibly_promoted():
    rows = [
        {"video_id": "keep", "in_category": True, "engagement": 0.02, "baseline_engagement": 0.02},
        {"video_id": "off", "in_category": False, "engagement": 0.02, "baseline_engagement": 0.02},
        {"video_id": "paid", "in_category": True, "engagement": 0.002, "baseline_engagement": 0.01},
    ]

    assert [row["video_id"] for row in niche_rising_topics._eligible_rows(rows)] == ["keep"]


def test_aggregate_adds_highest_ratio_breakout_when_fewer_than_three_rising_topics():
    rows = [
        {"video_id": "a", "youtube_channel_id": "UC1", "title": "A", "views": 10},
        {"video_id": "b", "youtube_channel_id": "UC2", "title": "B", "views": 30},
        {"video_id": "c", "youtube_channel_id": "UC1", "title": "C", "views": 20},
        {"video_id": "d", "youtube_channel_id": "UC1", "title": "D", "views": 99, "outlier_ratio": 12},
    ]
    merged = [
        {"label": "estrogen cream use", "video_ids": ["a", "b", "c"]},
        {"label": "bladder symptom relief", "video_ids": ["d"]},
    ]

    topics = niche_rising_topics._aggregate_topics(
        merged, rows, {"UC1": "One", "UC2": "Two"}
    )

    assert len(topics) == 2
    assert topics[0]["channel_count"] == 2
    assert topics[0]["total_views"] == 60
    assert [item["title"] for item in topics[0]["examples"]] == ["B", "C"]
    assert topics[1]["label"] == "bladder symptom relief"
    assert topics[1]["is_breakout"] is True
    assert topics[1]["max_outlier_ratio"] == 12


def test_truncate_evidence_caps_at_twenty_words():
    evidence = " ".join(f"word{index}" for index in range(25))

    assert len(niche_rising_topics._truncate_evidence(evidence).split()) == 20


def test_brand_match_prefers_published_short_over_archive():
    payload = (
        '{"matches":[{"topic_key":"hrt-supply","short_id":1,'
        '"archive_video_id":7,"evidence":"HRT supply update"}]}'
    )

    matches, input_tokens, output_tokens = niche_rising_topics._match_brand(
        [{"topic_key": "hrt-supply", "label": "HRT supply news"}],
        [{"id": 1, "title": "HRT supply update"}],
        [{"id": 7, "title": "Long HRT discussion", "transcript": "Supply details"}],
        _Client(payload),
    )

    assert matches[0]["status"] == niche_rising_topics.STATUS_COVERED
    assert matches[0]["archive_video_id"] is None
    assert matches[0]["evidence"] == "HRT supply update"
    assert (input_tokens, output_tokens) == (10, 5)


def test_archive_match_requires_verbatim_transcript_evidence():
    topics = [{"topic_key": "age-60", "label": "HRT after age 60"}]
    archive = [{
        "id": 7, "title": "Long HRT discussion",
        "transcript": "Starting hormone therapy after age 60 needs an individual risk discussion.",
    }]
    accepted = _Client(
        '{"matches":[{"topic_key":"age-60","short_id":null,"archive_video_id":7,'
        '"evidence":"hormone therapy after age 60"}]}'
    )
    rejected = _Client(
        '{"matches":[{"topic_key":"age-60","short_id":null,"archive_video_id":7,'
        '"evidence":"a made up broad match"}]}'
    )

    matches, _, _ = niche_rising_topics._match_brand(topics, [], archive, accepted)
    assert matches[0]["status"] == niche_rising_topics.STATUS_ARCHIVE
    assert matches[0]["evidence"] == "hormone therapy after age 60"

    matches, _, _ = niche_rising_topics._match_brand(topics, [], archive, rejected)
    assert matches[0]["status"] == niche_rising_topics.STATUS_IDEA


def test_archive_match_rejects_broad_transcript_excerpt_without_specific_topic_terms():
    topics = [{"topic_key": "nervous-system", "label": "Nervous system regulation for midlife"}]
    archive = [{
        "id": 7, "title": "Midlife energy",
        "transcript": "Estrogen influences blood sugar regulation and the mitochondria in our cells.",
    }]
    client = _Client(
        '{"matches":[{"topic_key":"nervous-system","short_id":null,"archive_video_id":7,'
        '"evidence":"Estrogen influences blood sugar regulation and the mitochondria"}]}'
    )

    matches, _, _ = niche_rising_topics._match_brand(topics, [], archive, client)

    assert matches[0]["status"] == niche_rising_topics.STATUS_IDEA


def test_clean_label_rejects_broad_and_requires_three_to_seven_words():
    assert niche_rising_topics._clean_label("Aile ve İlişkiler") == ""
    assert niche_rising_topics._clean_label("HRT after age 60") == "HRT after age 60"
    assert niche_rising_topics._clean_label("HRT issues") == ""
