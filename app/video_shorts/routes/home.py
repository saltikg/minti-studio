from flask import render_template, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.blog_content import get_latest_blog_articles
from app.video_shorts.services.usage_metering import load_storage_plan_catalog


@video_shorts_bp.route("/", methods=["GET"])
def home():
    latest_articles = []
    for article in get_latest_blog_articles(limit=3):
        published_on = article["published_on"]
        latest_articles.append(
            {
                "slug": article["slug"],
                "title": article["title"],
                "description": article["description"],
                "published_on_display": f"{published_on.strftime('%B')} {published_on.day}, {published_on.year}",
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
