import pytest

from app.video_shorts.services import blog_markdown


@pytest.fixture(autouse=True)
def static_url_helper(monkeypatch):
    def fake_url_for(endpoint, *, filename):
        assert endpoint == "video_shorts_bp.static"
        return f"/video_shorts/static/{filename}"

    monkeypatch.setattr(blog_markdown, "url_for", fake_url_for)


def test_motion_block_renders_exact_figure_with_caption():
    rendered = blog_markdown.render_markdown(
        ":::motion weekly-routine\nA calm weekly routine\n:::"
    )

    assert rendered == (
        '<figure class="blog-motion"><video autoplay muted loop playsinline preload="metadata" '
        'poster="/video_shorts/static/motion/weekly-routine.jpg" '
        'aria-label="A calm weekly routine">'
        '<source src="/video_shorts/static/motion/weekly-routine.webm" type="video/webm">'
        '<source src="/video_shorts/static/motion/weekly-routine.mp4" type="video/mp4">'
        '</video><figcaption>A calm weekly routine</figcaption></figure>'
    )


def test_motion_block_renders_exact_figure_without_caption():
    rendered = blog_markdown.render_markdown(":::motion weekly-routine\n:::")

    assert rendered == (
        '<figure class="blog-motion"><video autoplay muted loop playsinline preload="metadata" '
        'poster="/video_shorts/static/motion/weekly-routine.jpg" aria-label="Animation">'
        '<source src="/video_shorts/static/motion/weekly-routine.webm" type="video/webm">'
        '<source src="/video_shorts/static/motion/weekly-routine.mp4" type="video/mp4">'
        '</video></figure>'
    )


def test_motion_caption_is_html_escaped():
    rendered = blog_markdown.render_markdown(
        ":::motion weekly-routine\n<script>alert('x')</script>\n:::"
    )

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in rendered


@pytest.mark.parametrize(
    "name",
    ["../x", "A", "has space", "a/b", "", "a" * 61],
)
def test_invalid_motion_names_render_nothing(name):
    rendered = blog_markdown.render_markdown(f":::motion {name}\nCaption\n:::")

    assert rendered == ""
    assert "Caption" not in rendered
    assert ":::motion" not in rendered


def test_raw_video_figure_and_source_html_remain_stripped():
    rendered = blog_markdown.render_markdown(
        '<figure class="raw"><video autoplay><source src="/bad.webm"></video>'
        "Fallback</figure>"
    )

    assert "<figure" not in rendered
    assert "<video" not in rendered
    assert "<source" not in rendered
    assert "Fallback" in rendered


def test_existing_image_figure_and_youtube_embed_render_unchanged():
    rendered = blog_markdown.render_markdown(
        "![Demo](/video_shorts/static/img/blog/demo.jpg)\n\n"
        "[youtube: dQw4w9WgXcQ]"
    )

    assert '<figure class="figure">' in rendered
    assert '<img src="/video_shorts/static/img/blog/demo.jpg" alt="Demo" loading="lazy">' in rendered
    assert '<div class="yt-embed"><iframe src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in rendered
