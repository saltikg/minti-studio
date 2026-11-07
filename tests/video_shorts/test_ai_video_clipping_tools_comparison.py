from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
from flask import Flask

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes import blog as blog_routes
from app.video_shorts.services.blog_markdown import render_markdown
from scripts.update_ai_video_clipping_tools_comparison import (
    POST_SOURCE,
    OLD_PATH,
    OLD_SLUG,
    RELATED_LINK,
    TARGET_PATH,
    TARGET_SLUG,
    add_related_link,
    parse_post_source,
    update_comparison_article,
)


def _source():
    return parse_post_source(Path(POST_SOURCE).read_text(encoding="utf-8"))


def test_comparison_source_uses_new_canonical_url_and_preserves_seo_metadata() -> None:
    source = _source()

    assert source.slug == TARGET_SLUG == "best-ai-video-clipping-tools-2026"
    assert source.title == "Best AI Video Clipping Tools in 2026: 6 Tools Compared"
    assert source.meta_title == "Best AI Video Clipping Tools in 2026 | MintiStudio"
    assert source.meta_description.startswith("Compare the best AI video clipping tools")
    assert 140 <= len(source.meta_description) <= 180


def test_comparison_is_balanced_and_contains_verified_workflow_links() -> None:
    source = _source()
    body = source.body

    for product in ("MintiStudio", "OpusClip", "Klap", "Vizard", "Riverside", "Descript"):
        assert product in body
    assert "/tools/youtube-to-shorts" in body
    assert "/services/short-form-video-editing" in body
    assert "/video_shorts/blog/done-for-you-vs-diy-video-clipping/" in body
    assert "hands-on tested every product" in body
    assert "real editors" not in body.lower()
    assert "our editors" not in body.lower()
    assert "#1 overall" not in body.lower()


def test_comparison_markdown_has_one_template_h1_and_semantic_sections() -> None:
    source = _source()
    html = render_markdown(source.body)

    assert "<h1" not in html
    assert html.count("<h2") >= 10
    assert "Quick comparison of the best AI video clipping tools in 2026" in html
    assert "Frequently asked questions" in html
    assert "<table>" in html


def test_backlink_insertion_is_idempotent() -> None:
    original = "Opening\n\n## Related guides\n\n- Existing guide"
    updated, changed = add_related_link(original)

    assert changed is True
    assert RELATED_LINK in updated
    assert TARGET_PATH in updated

    unchanged, changed_again = add_related_link(updated)
    assert unchanged == updated
    assert changed_again is False


def test_backlink_insertion_adds_related_section_when_missing() -> None:
    updated, changed = add_related_link("Article body")

    assert changed is True
    assert "## Related guides" in updated
    assert TARGET_PATH in updated


def test_old_internal_link_is_rewritten_to_new_url() -> None:
    updated, changed = add_related_link(f"Read [{OLD_SLUG}]({OLD_PATH})")

    assert changed is True
    assert OLD_PATH not in updated
    assert TARGET_PATH in updated


def test_slug_migration_preserves_article_id_and_publication_time() -> None:
    conn = duckdb.connect(":memory:")
    published_at = datetime(2026, 10, 7, 13, 0, 0)
    conn.execute(
        """
        CREATE TABLE blog_articles (
            id INTEGER PRIMARY KEY,
            title VARCHAR,
            slug VARCHAR UNIQUE,
            summary VARCHAR,
            content VARCHAR,
            meta_title VARCHAR,
            meta_description VARCHAR,
            author_name VARCHAR,
            reading_time INTEGER,
            status VARCHAR,
            published_at TIMESTAMP,
            content_updated_at TIMESTAMP,
            import_source VARCHAR,
            import_source_id VARCHAR,
            updated_at TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO blog_articles (id, title, slug, status, published_at) VALUES (42, 'Old', ?, 'published', ?)",
        [OLD_SLUG, published_at],
    )

    article_id = update_comparison_article(conn, _source())
    row = conn.execute(
        "SELECT id, slug, published_at FROM blog_articles WHERE id = 42"
    ).fetchone()

    assert article_id == 42
    assert row == (42, TARGET_SLUG, published_at)
    assert conn.execute("SELECT COUNT(*) FROM blog_articles").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM blog_articles WHERE slug = ?", [OLD_SLUG]).fetchone()[0] == 0
    conn.close()


def test_old_route_redirects_and_new_route_is_indexable_and_self_canonical(monkeypatch) -> None:
    source = _source()
    published_at = datetime(2026, 10, 7, 13, 0, 0)
    article = {
        "id": 42,
        "title": source.title,
        "slug": source.slug,
        "summary": source.meta_description,
        "content": source.body,
        "cover_image_url": "",
        "meta_title": source.meta_title,
        "meta_description": source.meta_description,
        "author_name": "MintiStudio Team",
        "reading_time": source.reading_time,
        "view_count": 0,
        "published_at": published_at,
        "content_updated_at": published_at,
        "updated_at": published_at,
        "status": "published",
    }
    monkeypatch.setattr(blog_routes, "ensure_default_blog_articles_seeded", lambda: None)
    monkeypatch.setattr(
        blog_routes,
        "get_published_blog_article_by_slug",
        lambda slug: dict(article) if slug == TARGET_SLUG else None,
    )
    monkeypatch.setattr(blog_routes, "increment_blog_article_view_count", lambda article_id: None)
    monkeypatch.setattr(blog_routes, "list_published_blog_articles", lambda limit=None: [])

    app = Flask(__name__)
    app.config.update(TESTING=True, BASE_URL="https://mintistudio.com", SECRET_KEY="test-secret")
    app.add_url_rule(
        "/services/short-form-video-editing",
        endpoint="short_form_video_editing_service",
        view_func=lambda: "service",
    )
    app.register_blueprint(video_shorts_bp)
    client = app.test_client()

    old_response = client.get(OLD_PATH, follow_redirects=False)
    assert old_response.status_code == 301
    assert old_response.headers["Location"].endswith(TARGET_PATH)

    new_response = client.get(TARGET_PATH)
    assert new_response.status_code == 200
    html = new_response.get_data(as_text=True)
    assert f"<title>{source.meta_title}</title>" in html
    assert f'<link rel="canonical" href="https://mintistudio.com{TARGET_PATH}">' in html
    assert '<meta name="robots" content="index, follow">' in html
    assert html.count("<h1") == 1
    assert f'<h1 class="vs-blog-article-title">{source.title}</h1>' in html
    assert OLD_PATH not in html
