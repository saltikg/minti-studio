from app.video_shorts.routes.generation import _reindex_v1_plan_entries


def test_reindex_preserves_manual_and_created_clip_identity():
    entries = [
        {
            "origin": "manual",
            "plan_index": 1,
            "clip_filename": "13_demo.mp4",
            "output_filename": "13_demo.mp4",
            "title": "Manual published clip",
            "status": "created",
            "publish_status": "published",
        },
        {
            "origin": "manual",
            "plan_index": 4,
            "clip_filename": "6_demo.mp4",
            "output_filename": "6_demo.mp4",
            "title": "Manual draft clip",
            "status": "pending",
            "publish_status": "not_ready",
        },
        {
            "origin": "ai",
            "plan_index": 13,
            "clip_filename": "13_demo.mp4",
            "title": "New AI suggestion",
            "status": "pending",
            "publish_status": "not_ready",
        },
        {
            "origin": "ai",
            "plan_index": 1,
            "clip_filename": "1_demo.mp4",
            "title": "Another AI suggestion",
            "status": "pending",
            "publish_status": "not_ready",
        },
    ]

    reindexed = _reindex_v1_plan_entries("demo", entries)

    assert reindexed[0]["plan_index"] == 13
    assert reindexed[0]["clip_filename"] == "13_demo.mp4"
    assert reindexed[0]["title"] == "Manual published clip"

    assert reindexed[1]["plan_index"] == 6
    assert reindexed[1]["clip_filename"] == "6_demo.mp4"
    assert reindexed[1]["title"] == "Manual draft clip"

    assert reindexed[2]["plan_index"] == 1
    assert reindexed[2]["clip_filename"] == "1_demo.mp4"
    assert reindexed[2]["title"] == "New AI suggestion"

    assert reindexed[3]["plan_index"] == 2
    assert reindexed[3]["clip_filename"] == "2_demo.mp4"
    assert reindexed[3]["title"] == "Another AI suggestion"


def test_reindex_keeps_created_ai_clip_bound_to_its_rendered_filename():
    entries = [
        {
            "origin": "ai",
            "plan_index": 1,
            "clip_filename": "8_demo.mp4",
            "output_filename": "8_demo.mp4",
            "title": "Rendered AI clip",
            "status": "created",
            "publish_status": "ready",
        },
        {
            "origin": "ai",
            "plan_index": 2,
            "clip_filename": "2_demo.mp4",
            "title": "Pending AI clip",
            "status": "pending",
            "publish_status": "not_ready",
        },
    ]

    reindexed = _reindex_v1_plan_entries("demo", entries)

    assert reindexed[0]["plan_index"] == 8
    assert reindexed[0]["clip_filename"] == "8_demo.mp4"
    assert reindexed[1]["plan_index"] == 2
    assert reindexed[1]["clip_filename"] == "2_demo.mp4"
