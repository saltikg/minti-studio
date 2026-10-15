import duckdb
import pytest

from app.video_shorts.services import tenant_media
from app.video_shorts.routes.generation import _reindex_v1_plan_entries


@pytest.fixture(params=[42, 43])
def video_pk(request, monkeypatch, tmp_path):
    db_path = tmp_path / "tenants.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE youtube_videos(id BIGINT, video_id VARCHAR, owner_user_id VARCHAR, brand_id VARCHAR)")
        conn.execute("INSERT INTO youtube_videos VALUES (42, 'demo', 'user-a', 'brand-a'), (43, 'demo', 'user-b', 'brand-b')")
    monkeypatch.setattr(tenant_media, "get_db_readonly", lambda: duckdb.connect(str(db_path), read_only=True))
    return request.param


def test_reindex_preserves_manual_and_created_clip_identity(video_pk):
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

    reindexed = _reindex_v1_plan_entries("demo", entries, video_pk=video_pk)

    assert reindexed[0]["plan_index"] == 13
    assert reindexed[0]["clip_filename"] == "13_demo.mp4"
    assert reindexed[0]["title"] == "Manual published clip"

    assert reindexed[1]["plan_index"] == 6
    assert reindexed[1]["clip_filename"] == "6_demo.mp4"
    assert reindexed[1]["title"] == "Manual draft clip"

    assert reindexed[2]["plan_index"] == 1
    assert reindexed[2]["clip_filename"] == f"1_demo_v{video_pk}.mp4"
    assert reindexed[2]["title"] == "New AI suggestion"

    assert reindexed[3]["plan_index"] == 2
    assert reindexed[3]["clip_filename"] == f"2_demo_v{video_pk}.mp4"
    assert reindexed[3]["title"] == "Another AI suggestion"


def test_reindex_keeps_created_ai_clip_bound_to_its_rendered_filename(video_pk):
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

    reindexed = _reindex_v1_plan_entries("demo", entries, video_pk=video_pk)

    assert reindexed[0]["plan_index"] == 8
    assert reindexed[0]["clip_filename"] == "8_demo.mp4"
    assert reindexed[1]["plan_index"] == 2
    assert reindexed[1]["clip_filename"] == f"2_demo_v{video_pk}.mp4"
