from flask import render_template, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.blog_articles import ensure_default_blog_articles_seeded, list_published_blog_articles
from app.video_shorts.services.usage_metering import load_storage_plan_catalog


@video_shorts_bp.route("/", methods=["GET"])
def home():
    ensure_default_blog_articles_seeded()
    latest_articles = []
    for article in list_published_blog_articles(limit=3):
        published_at = article["published_at"]
        latest_articles.append(
            {
                "slug": article["slug"],
                "title": article["title"],
                "summary": article["summary"],
                "published_on_display": (
                    f"{published_at.strftime('%B')} {published_at.day}, {published_at.year}" if published_at else ""
                ),
                "url": url_for("video_shorts_bp.blog_article", slug=article["slug"]),
            }
        )
    return render_template(
        "vs_home.html",
        pricing_plans=load_storage_plan_catalog(
            plan_ids=["plan_free", "plan_2gb", "plan_10gb", "plan_100gb"]
        ),
        latest_blog_articles=latest_articles,
    )
