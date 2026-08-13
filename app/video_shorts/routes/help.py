from __future__ import annotations

from flask import abort, render_template, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.help_content import (
    HELP_CATEGORY_DEFS,
    build_faq_search_index,
    get_faq_categories,
    get_faq_entries,
    get_faq_entry,
)


@video_shorts_bp.route("/help/faq/", methods=["GET"])
def help_faq_index():
    entries = get_faq_entries()
    categories = get_faq_categories()
    search_index = build_faq_search_index(entries)
    popular_topics: list[str] = []
    seen_topics: set[str] = set()
    for category in HELP_CATEGORY_DEFS:
        for topic in category.get("topics", []):
            normalized = str(topic).strip()
            if normalized and normalized not in seen_topics:
                seen_topics.add(normalized)
                popular_topics.append(normalized)
            if len(popular_topics) >= 5:
                break
        if len(popular_topics) >= 5:
            break
    return render_template(
        "vs_help_faq_index.html",
        faq_entries=entries,
        faq_categories=categories,
        faq_search_index=search_index,
        popular_topics=popular_topics,
        support_href=url_for("video_shorts_bp.contact_page"),
        page_title="FAQ",
        page_subtitle="Short answers to the questions creators hit most when scheduling and publishing clips.",
    )


@video_shorts_bp.route("/help/faq/<slug>/", methods=["GET"])
def help_faq_article(slug: str):
    entry = get_faq_entry(slug)
    if not entry:
        abort(404)
    return render_template(
        "vs_help_faq_article.html",
        entry=entry,
        back_href=url_for("video_shorts_bp.help_faq_index"),
    )
