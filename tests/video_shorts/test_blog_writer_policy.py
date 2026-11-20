from pathlib import Path

from flask import Flask

import scripts.blog_pipeline as blog_pipeline
from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.blog import (
    DOCTORS_SHORTS_GUIDE_SLUG,
    OLD_DOCTORS_SHORTS_GUIDE_SLUG,
)
from scripts.update_doctors_shorts_guide import (
    AUTOPILOT_DESCRIPTION,
    META_DESCRIPTION,
    TARGET_TITLE,
    rewrite_content,
)


def test_writer_policy_keeps_topic_title_and_blocks_prices_and_bad_meta(monkeypatch):
    topic = {"title": TARGET_TITLE}
    article = blog_pipeline._normalize_article_payload(
        {
            "title": "A rewritten literary title that should not survive",
            "slug": "test",
            "summary": "The plan costs $20.",
            "content_md": (
                "Intro with the required CTA https://mintistudio.com/video_shorts/register and a $20 price.\n\n"
                "## Where Minti Studio helps\n\nPitch one.\n\n"
                "## DIY or Autopilot\n\nPitch two.\n\n" + "word " * 1100
            ),
            "meta_title": "An intentionally overlong meta title that cannot fit within sixty characters cleanly",
            "meta_description": "This description ends in the middle",
            "visuals": [],
        },
        topic,
    )
    monkeypatch.setattr(blog_pipeline, "_unique_slug", lambda slug: slug)
    checks = blog_pipeline._code_checks(article, [], [], topic)

    assert article["title"] == TARGET_TITLE
    assert "auto-generated posts must not mention prices" in checks["blocking_issues"]
    assert "meta title must be complete and at most 60 characters" in checks["blocking_issues"]
    assert "meta description must be a complete sentence of at most 155 characters" in checks["blocking_issues"]
    assert "more than one product pitch section before the final CTA" in checks["blocking_issues"]


def test_writer_and_reviewer_prompts_make_policy_blocking():
    writer = blog_pipeline._writer_prompt()
    reviewer = blog_pipeline._reviewer_prompt("doctors")

    assert "title and H1 must equal topic.title exactly" in writer
    assert "Never state a price" in writer
    assert "at most one product pitch section" in writer
    assert "Never truncate either field" in writer
    assert "any price mention" in reviewer
    assert "more than one product pitch section" in reviewer
    assert "no required approval step" in reviewer


def test_product_facts_use_current_autopilot_description_without_prices():
    facts = Path("app/video_shorts/blog_pipeline/minti_facts.md").read_text(encoding="utf-8")

    assert AUTOPILOT_DESCRIPTION in facts
    assert "$" not in facts
    assert "per month" not in facts.lower()
    assert "Nothing goes out that they don't want" not in facts


def test_doctors_post_rewrite_removes_extra_pitches_and_prices():
    source = f"""## Where Minti Studio helps

If your bottleneck is not ideas but time, Autopilot is the cleanest fit. You share a link and review every prepared Short before anything goes out.

## DIY or Autopilot: a simple choice

Autopilot is $20 per month and DIY is $9.

## A practical example of a Shorts workflow for a clinician

Keep this example.

If you want more control, the DIY plans let you work through the process yourself. If you want less work, use Autopilot.

## The practical next step

Use Autopilot when you want done-for-you help.
"""
    updated = rewrite_content(source)

    assert AUTOPILOT_DESCRIPTION in updated
    assert "DIY or Autopilot" not in updated
    assert "$" not in updated
    assert "per month" not in updated
    assert "Keep this example." in updated
    assert "If you want more control" not in updated
    assert len(META_DESCRIPTION) <= 155
    assert META_DESCRIPTION.endswith(".")


def test_old_doctors_slug_redirects_to_new_slug():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app.register_blueprint(video_shorts_bp)
    response = app.test_client().get(
        f"/video_shorts/blog/{OLD_DOCTORS_SHORTS_GUIDE_SLUG}/",
        follow_redirects=False,
    )

    assert response.status_code == 301
    assert response.headers["Location"].endswith(f"/video_shorts/blog/{DOCTORS_SHORTS_GUIDE_SLUG}/")
