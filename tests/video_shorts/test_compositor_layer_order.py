from pathlib import Path

from app.video_shorts.services import compositor


def test_title_overlay_is_applied_after_caption(monkeypatch, tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    clip = tmp_path / "clip.mp4"
    subtitle = tmp_path / "caption.srt"
    output = tmp_path / "output.mp4"
    title_overlay = tmp_path / "title.png"
    for path in (background, clip, title_overlay):
        path.touch()
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nCaption\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(compositor, "_resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(
        compositor,
        "_fit_title_text",
        lambda *args, **kwargs: {"text": "Title"},
    )
    monkeypatch.setattr(
        compositor,
        "_render_title_overlay",
        lambda *args, **kwargs: (
            title_overlay,
            {"origin_x": 10, "origin_y": 20},
        ),
    )
    captured: list[str] = []

    def _capture(command, **_kwargs):
        captured.extend(command)

    monkeypatch.setattr(compositor, "run_media_subprocess", _capture)

    compositor._compose_with_background(
        background,
        clip,
        "Title",
        "",
        output,
        subtitle_path=subtitle,
    )

    graph = captured[captured.index("-filter_complex") + 1]
    assert graph.index("subtitles=") < graph.index("[title_src]overlay=")
    assert graph.count("[title_src]overlay=") == 1
    assert captured[captured.index("-map") + 1] == "[ov_title]"
