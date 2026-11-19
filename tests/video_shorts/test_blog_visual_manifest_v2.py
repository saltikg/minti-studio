import pytest

from scripts import blog_pipeline


def test_manifest_only_offers_existing_active_entries():
    active = blog_pipeline._load_manifest()

    assert active
    assert all(item["status"] == "active" for item in active)
    assert all(item["exists"] for item in active)
    assert "editor-loop-founder" not in {item["id"] for item in active}
    assert "video-from-url" not in {item["id"] for item in active}


def test_manifest_videos_have_existing_posters_and_sources():
    for item in blog_pipeline._load_manifest():
        if item["type"] != "video":
            continue
        assert blog_pipeline._manifest_source_path(item["filename"]).is_file()
        assert blog_pipeline._manifest_source_path(item["poster"]).is_file()


def test_image_stage_rejects_non_active_manifest_media():
    content = (
        '![Old UI](/video_shorts/static/img/blog/example/9-generate-video-screen.png '
        '"Old workflow [screenshot]")'
    )

    assert blog_pipeline._image_stage_manifest_violations(content) == [
        "library image is not active in the manifest: 9-generate-video-screen.png"
    ]


def test_image_stage_accepts_active_copied_image_and_motion():
    content = (
        '![New UI](/video_shorts/static/img/blog/example/tab-clip.jpg "Current UI [screenshot]")\n\n'
        ":::motion real-short-doctor-busy\nFinished Short\n:::"
    )

    assert blog_pipeline._image_stage_manifest_violations(content) == []


def test_replace_refuses_retired_id(monkeypatch):
    article = {
        "slug": "example",
        "content_md": "<!-- IMAGE_1 -->",
        "visuals": [{"marker": "IMAGE_1", "type": "screenshot", "screenshot_id": "video-from-url"}],
    }

    with pytest.raises(RuntimeError, match="not active"):
        blog_pipeline._replace_screenshot_placeholders(article, blog_pipeline._load_manifest(), copy_files=False)
