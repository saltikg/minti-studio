from pathlib import Path

from app.video_shorts.services.blog_markdown import render_markdown
from app.video_shorts.services.help_content import build_faq_search_index, get_faq_categories, get_faq_entry


def test_editor_guide_uses_blog_markup_and_safe_heading_ids():
    entry = get_faq_entry("editor-guide")

    assert entry is not None
    assert entry["type"] == "guide"
    rendered = render_markdown(str(entry["body"]))

    assert 'class="steps"' in rendered
    assert 'class="callout ' in rendered
    assert '<figure class="figure">' in rendered
    assert '<div class="table-shell">' in rendered
    assert '<h2 id="add-a-video">' in rendered
    assert '<h2 id="faq">' in rendered


def test_blog_heading_ids_only_allow_lowercase_slug_values():
    rendered = render_markdown(
        "## Allowed {#allowed-heading}\n\n"
        "## Uppercase is stripped {#Not-Allowed}\n\n"
        "### Underscores are stripped {#not_allowed}"
    )

    assert '<h2 id="allowed-heading">' in rendered
    assert 'id="Not-Allowed"' not in rendered
    assert 'id="not_allowed"' not in rendered


def test_editor_guide_is_first_creating_shorts_article_and_searchable():
    creating_shorts = next(
        category for category in get_faq_categories() if category["slug"] == "creating-shorts"
    )
    slugs = [entry["slug"] for entry in creating_shorts["entries"]]

    assert slugs[:2] == ["editor-guide", "creating-shorts"]
    guide_result = next(
        result for result in build_faq_search_index() if result["slug"] == "editor-guide"
    )
    assert guide_result["question"] == "Editor guide — from long video to Shorts"
    assert "Captions" in guide_result["search_text"]
    assert guide_result["href"] == "/video_shorts/help/faq/editor-guide/"


def test_blog_richtext_assets_are_shared_by_blog_and_help_templates():
    template_dir = Path("app/video_shorts/templates")
    blog_template = (template_dir / "vs_blog_article.html").read_text(encoding="utf-8")
    help_template = (template_dir / "vs_help_faq_article.html").read_text(encoding="utf-8")

    for partial in (
        "_vs_blog_article_styles.html",
        "_vs_blog_lightbox.html",
        "_vs_blog_lightbox_script.html",
    ):
        assert partial in blog_template
        assert partial in help_template
