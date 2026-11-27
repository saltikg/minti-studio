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


class _SequenceClient:
    def __init__(self, payloads):
        responses = iter(payloads)
        self.chat = type(
            "Chat", (),
            {"completions": type(
                "Completions", (),
                {"create": lambda _self, **_kwargs: _Response(next(responses))},
            )()},
        )()


def test_eligible_rows_excludes_off_topic_and_possibly_promoted():
    rows = [
        {"video_id": "keep", "in_category": True, "engagement": 0.02, "baseline_engagement": 0.02},
        {"video_id": "off", "in_category": False, "engagement": 0.02, "baseline_engagement": 0.02},
        {"video_id": "paid", "in_category": True, "engagement": 0.002, "baseline_engagement": 0.01},
    ]

    assert [row["video_id"] for row in niche_rising_topics._eligible_rows(rows)] == ["keep"]


def test_aggregate_fills_with_highest_ratio_breakout_and_uses_clean_angle_titles():
    rows = [
        {"video_id": "a", "youtube_channel_id": "UC1", "title": "A", "views": 10},
        {"video_id": "b", "youtube_channel_id": "UC2", "title": "B", "views": 30},
        {"video_id": "c", "youtube_channel_id": "UC1", "title": "C", "views": 20},
        {"video_id": "d", "youtube_channel_id": "UC1", "title": "D #menopause", "views": 99, "outlier_ratio": 12},
    ]
    merged = [
        {"label": "estrogen cream use", "video_ids": ["a", "b", "c"]},
        {"label": "bladder symptom relief", "video_ids": ["d"]},
    ]

    topics = niche_rising_topics._aggregate_topics(
        merged, rows, {"UC1": "One", "UC2": "Two"}, {"d": "bladder symptom relief #health"}
    )

    assert len(topics) == 2
    assert topics[0]["channel_count"] == 2
    assert topics[0]["total_views"] == 60
    assert [item["title"] for item in topics[0]["examples"]] == ["B", "C"]
    assert topics[1]["label"] == "bladder symptom relief"
    assert topics[1]["is_breakout"] is True
    assert topics[1]["max_outlier_ratio"] == 12
    assert topics[1]["examples"][0]["title"] == "bladder symptom relief"


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


def test_brand_match_rejects_short_with_only_broad_subject_overlap():
    payload = (
        '{"matches":[{"topic_key":"prophets-grave","short_id":1,'
        '"archive_video_id":null,"evidence":"A sacred place"}]}'
    )

    matches, _, _ = niche_rising_topics._match_brand(
        [{"topic_key": "prophets-grave", "label": "Prophet Muhammad's grave location"}],
        [{"id": 1, "title": "What Makes a Place Sacred?"}],
        [],
        _Client(payload),
    )

    assert matches[0]["status"] == niche_rising_topics.STATUS_IDEA


def test_merge_groups_specific_angles_into_middle_level_theme():
    payload = (
        '{"topics":[{"label":"Duanın kabul olması","video_ids":["a","b"]}]}'
    )
    labels = {
        "a": "Kabul olmayan dua sanmak",
        "b": "Duanın kapıları açması",
    }

    topics, _, _ = niche_rising_topics._merge_labels(labels, "en", _Client(payload))

    assert topics == [{"label": "Duanın kabul olması", "video_ids": ["a", "b"]}]


def test_merge_rejects_broad_category_theme_and_falls_back_to_angles():
    payload = (
        '{"topics":[{"label":"Hormone Replacement Therapy","video_ids":["a","b"]}]}'
    )
    labels = {
        "a": "HRT after breast cancer safety",
        "b": "HRT after sixty fracture prevention",
    }

    topics, _, _ = niche_rising_topics._merge_labels(labels, "en", _Client(payload))

    assert topics == [
        {"label": "HRT after breast cancer safety", "video_ids": ["a"]},
        {"label": "HRT after sixty fracture prevention", "video_ids": ["b"]},
    ]


def test_merge_keeps_valid_initial_cluster_when_critic_over_splits_it():
    client = _SequenceClient([
        '{"topics":[{"label":"Duanın kabul olması","video_ids":["a","b"]}]}',
        '{"topics":[{"label":"Kabul olmayan dua","video_ids":["a"]},'
        '{"label":"Kapıları açan dua","video_ids":["b"]}]}',
    ])

    topics, _, _ = niche_rising_topics._merge_labels(
        {"a": "Kabul olmayan dua sanmak", "b": "Duanın kapıları açması"}, "tr", client,
    )

    assert topics == [{"label": "Duanın kabul olması", "video_ids": ["a", "b"]}]


def test_archive_match_requires_verbatim_transcript_evidence():
    topics = [{"topic_key": "age-60", "label": "HRT after age 60"}]
    archive = [{
        "id": 7, "title": "Long HRT discussion",
        "segments": [{
            "start_seconds": 90, "end_seconds": 125,
            "text": "Starting hormone therapy after age 60 needs an individual risk discussion.",
        }],
    }]
    accepted = _Client(
        '{"matches":[{"topic_key":"age-60","short_id":null,"archive_video_id":7,'
        '"segment_start_seconds":90,"evidence":"hormone therapy after age 60"}]}'
    )
    rejected = _Client(
        '{"matches":[{"topic_key":"age-60","short_id":null,"archive_video_id":7,'
        '"segment_start_seconds":90,"evidence":"a made up broad match"}]}'
    )

    matches, _, _ = niche_rising_topics._match_brand(topics, [], archive, accepted)
    assert matches[0]["status"] == niche_rising_topics.STATUS_ARCHIVE
    assert matches[0]["evidence"] == "hormone therapy after age 60"
    assert matches[0]["archive_start_seconds"] == 90

    matches, _, _ = niche_rising_topics._match_brand(topics, [], archive, rejected)
    assert matches[0]["status"] == niche_rising_topics.STATUS_IDEA


def test_archive_match_rejects_broad_transcript_excerpt_without_specific_topic_terms():
    topics = [{"topic_key": "nervous-system", "label": "Nervous system regulation for midlife"}]
    archive = [{
        "id": 7, "title": "Midlife energy",
        "segments": [{
            "start_seconds": 30, "end_seconds": 65,
            "text": "Estrogen influences blood sugar regulation and the mitochondria in our cells.",
        }],
    }]
    client = _Client(
        '{"matches":[{"topic_key":"nervous-system","short_id":null,"archive_video_id":7,'
        '"segment_start_seconds":30,'
        '"evidence":"Estrogen influences blood sugar regulation and the mitochondria"}]}'
    )

    matches, _, _ = niche_rising_topics._match_brand(topics, [], archive, client)

    assert matches[0]["status"] == niche_rising_topics.STATUS_IDEA


def test_clean_label_rejects_broad_and_requires_three_to_seven_words():
    assert niche_rising_topics._clean_label("Aile ve İlişkiler") == ""
    assert niche_rising_topics._clean_label("HRT after age 60") == "HRT after age 60"
    assert niche_rising_topics._clean_label("HRT issues") == ""


def test_clean_theme_requires_middle_level_label():
    assert niche_rising_topics._clean_theme("Dua") == ""
    assert niche_rising_topics._clean_theme("Duanın kabul olması") == "Duanın kabul olması"


def test_segment_windows_builds_timestamped_thirty_second_chunks():
    segments = [
        {"start": index * 10, "end": (index + 1) * 10, "text": f"part {index}"}
        for index in range(6)
    ]

    windows = niche_rising_topics._segment_windows(segments)

    assert [(item["start_seconds"], item["end_seconds"]) for item in windows] == [(0.0, 30.0), (30.0, 60.0)]
    assert windows[0]["text"] == "part 0 part 1 part 2"


def test_archive_candidates_are_shortlisted_per_theme_by_term_overlap():
    topics = [{"topic_key": "accepted-prayer", "label": "Duanın kabul olması"}]
    archive = [{
        "id": 7, "title": "Prayer talk", "segments": [
            {"start_seconds": 0, "end_seconds": 35, "text": "Namaz ve secde hakkında konuşma"},
            {"start_seconds": 35, "end_seconds": 70, "text": "Duanın kabul olması için sabır gerekir"},
        ],
    }]

    candidates = niche_rising_topics._archive_candidates_for_topics(topics, archive)

    assert len(candidates["accepted-prayer"]) == 1
    assert candidates["accepted-prayer"][0]["start_seconds"] == 35


def test_format_seconds_for_archive_evidence():
    assert niche_rising_topics._format_seconds(125.2) == "2:05"
