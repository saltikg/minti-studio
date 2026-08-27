from __future__ import annotations

import json
from typing import Any

from flask import abort, current_app, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.blog_articles import (
    ensure_default_blog_articles_seeded,
    get_published_blog_article_by_slug,
    increment_blog_article_view_count,
    list_published_blog_articles,
)
from app.video_shorts.services.blog_markdown import render_markdown


def _base_url() -> str:
    return (current_app.config.get("BASE_URL") or request.url_root).rstrip("/")


def _absolute_url(path: str) -> str:
    return f"{_base_url()}{path}"


def _serialize_article(article: dict[str, Any]) -> dict[str, Any]:
    article = dict(article)
    published_at = article["published_at"]
    updated_at = article.get("updated_at")
    article["published_on_iso"] = published_at.isoformat() if published_at else ""
    article["updated_at_iso"] = updated_at.isoformat() if updated_at else article["published_on_iso"]
    article["published_on_display"] = (
        f"{published_at.strftime('%B')} {published_at.day}, {published_at.year}" if published_at else ""
    )
    article["url"] = url_for("video_shorts_bp.blog_article", slug=article["slug"])
    article["canonical_url"] = _absolute_url(article["url"])
    article["cover_image_absolute_url"] = (
        _absolute_url(article["cover_image_url"]) if article.get("cover_image_url") else ""
    )
    article["content_html"] = render_markdown(article.get("content") or "")
    return article


def _article_json_ld(article: dict[str, Any]) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": article["title"],
        "description": article["meta_description"] or article["summary"],
        "datePublished": article["published_on_iso"],
        "dateModified": article["updated_at_iso"],
        "author": {
            "@type": "Organization",
            "name": article["author_name"],
        },
        "publisher": {
            "@type": "Organization",
            "name": "MintiStudio",
            "url": _absolute_url(url_for("video_shorts_bp.home")),
        },
        "mainEntityOfPage": article["canonical_url"],
        "url": article["canonical_url"],
    }
    if article.get("cover_image_url"):
        payload["image"] = [article["cover_image_absolute_url"]]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# TODO: No sitemap.xml integration point exists in the current app yet.
# Add published blog_articles once a sitemap route/module is introduced.


@video_shorts_bp.route("/blog/", methods=["GET"])
def blog_index():
    ensure_default_blog_articles_seeded()
    articles = [_serialize_article(article) for article in list_published_blog_articles()]
    canonical_url = _absolute_url(url_for("video_shorts_bp.blog_index"))
    return render_template(
        "vs_blog_index.html",
        articles=articles,
        canonical_url=canonical_url,
        meta_title="MintiStudio Blog | Shorts workflow, scheduling, and publishing tips",
        meta_description=(
            "Actionable guides on turning long videos into Shorts, scheduling releases, and building a more consistent short-form publishing workflow."
        ),
    )


@video_shorts_bp.route("/blog/<slug>/", methods=["GET"])
def blog_article(slug: str):
    ensure_default_blog_articles_seeded()
    article = get_published_blog_article_by_slug(slug)
    if not article:
        abort(404)
    article = _serialize_article(article)
    increment_blog_article_view_count(article["id"])
    article["view_count"] = int(article.get("view_count") or 0) + 1
    related_articles = [
        _serialize_article(candidate)
        for candidate in list_published_blog_articles(limit=3)
        if candidate["slug"] != slug
    ][:2]
    return render_template(
        "vs_blog_article.html",
        article=article,
        related_articles=related_articles,
        canonical_url=article["canonical_url"],
        meta_title=article.get("meta_title") or f"{article['title']} | MintiStudio Blog",
        meta_description=article.get("meta_description") or article["summary"],
        article_json_ld=_article_json_ld(article),
    )
