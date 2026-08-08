from __future__ import annotations

import json
from typing import Any

from flask import abort, current_app, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.blog_content import (
    get_blog_article,
    get_blog_articles,
    get_latest_blog_articles,
)


def _base_url() -> str:
    return (current_app.config.get("BASE_URL") or request.url_root).rstrip("/")


def _absolute_url(path: str) -> str:
    return f"{_base_url()}{path}"


def _serialize_article(article: dict[str, Any]) -> dict[str, Any]:
    article = dict(article)
    published_on = article["published_on"]
    article["published_on_iso"] = published_on.isoformat()
    article["published_on_display"] = f"{published_on.strftime('%B')} {published_on.day}, {published_on.year}"
    article["url"] = url_for("video_shorts_bp.blog_article", slug=article["slug"])
    article["canonical_url"] = _absolute_url(article["url"])
    return article


def _article_json_ld(article: dict[str, Any]) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": article["title"],
        "description": article["description"],
        "datePublished": article["published_on_iso"],
        "dateModified": article["published_on_iso"],
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
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@video_shorts_bp.route("/blog/", methods=["GET"])
def blog_index():
    articles = [_serialize_article(article) for article in get_blog_articles()]
    canonical_url = _absolute_url(url_for("video_shorts_bp.blog_index"))
    return render_template(
        "vs_blog_index.html",
        articles=articles,
        latest_articles=get_latest_blog_articles(limit=3),
        canonical_url=canonical_url,
        meta_title="MintiStudio Blog | Shorts workflow, scheduling, and publishing tips",
        meta_description=(
            "Actionable guides on turning long videos into Shorts, scheduling releases, and building a more consistent short-form publishing workflow."
        ),
    )


@video_shorts_bp.route("/blog/<slug>/", methods=["GET"])
def blog_article(slug: str):
    article = get_blog_article(slug)
    if not article:
        abort(404)
    article = _serialize_article(article)
    related_articles = [
        _serialize_article(candidate)
        for candidate in get_latest_blog_articles(limit=3)
        if candidate["slug"] != slug
    ][:2]
    return render_template(
        "vs_blog_article.html",
        article=article,
        related_articles=related_articles,
        canonical_url=article["canonical_url"],
        meta_title=article.get("meta_title") or f"{article['title']} | MintiStudio Blog",
        meta_description=article.get("meta_description") or article["description"],
        article_json_ld=_article_json_ld(article),
    )
