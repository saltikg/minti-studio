from scripts import backfill_blog_visual_library_v2 as backfill


def test_doctors_backfill_moves_media_to_new_slug_and_adds_preview():
    content = (
        "Most doctors do not stop because Shorts are ineffective. They stop because the workflow is annoying.\n\n"
        "![Paste link](/video_shorts/static/img/blog/old/3-minti-youtube-video-url-paste.png \"Start [screenshot]\")\n\n"
        "![Clip detail](/video_shorts/static/img/blog/old/4-manual-changing-video-details.png \"Review [screenshot]\")"
    )

    rewritten, changes, lost = backfill._rewrite_article(
        backfill.DOCTORS_SLUG,
        content,
        backfill._active_manifest(),
        apply=False,
    )

    assert ":::motion real-short-doctor-busy" in rewritten
    assert f"/img/blog/{backfill.DOCTORS_SLUG}/your-clips-rendered.jpg" in rewritten
    assert "Many doctors stop not because Shorts don't work, but because the workflow is annoying." in rewritten
    assert "old/" not in rewritten
    assert len(changes) == 2
    assert lost == []


def test_backfill_removes_unmatched_and_caps_library_visuals():
    content = "\n\n".join(
        [
            '![Transcript](/video_shorts/static/img/blog/getting-started/04-edit-transcript.png "Transcript")',
            '![Styles](/video_shorts/static/img/blog/getting-started/06-crop-style.png "Styles")',
            '![Schedule](/video_shorts/static/img/blog/getting-started/09-schedule-publish.png "Schedule")',
            '![Stats](/video_shorts/static/img/blog/getting-started/10-track.png "Stats")',
        ]
    )

    rewritten, changes, lost = backfill._rewrite_article(
        "auto-post",
        content,
        backfill._active_manifest(),
        apply=False,
    )

    assert len(changes) == 2
    assert len(lost) == 2
    assert "make-a-clip-transcript.jpg" in rewritten
    assert "design-styles.jpg" in rewritten
    assert "tab-schedule.jpg" not in rewritten
    assert "10-track.png" not in rewritten


def test_doctors_cover_moves_to_new_slug_in_dry_run():
    old_url = (
        "/video_shorts/static/img/blog/"
        f"{backfill.DOCTORS_OLD_SLUG}/cover.png"
    )

    new_url, change = backfill._migrate_doctors_cover(
        backfill.DOCTORS_SLUG,
        old_url,
        apply=False,
    )

    assert new_url == f"/video_shorts/static/img/blog/{backfill.DOCTORS_SLUG}/cover.png"
    assert change == {"from": old_url, "to": new_url}
