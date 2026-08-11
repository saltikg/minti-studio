from __future__ import annotations

from flask import abort, render_template, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.help_content import get_faq_entries, get_faq_entry


@video_shorts_bp.route("/help/faq/", methods=["GET"])
def help_faq_index():
    entries = get_faq_entries()
    return render_template(
        "vs_help_faq_index.html",
        faq_entries=entries,
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
