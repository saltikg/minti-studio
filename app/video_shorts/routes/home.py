from flask import current_app, jsonify, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.blog_articles import ensure_default_blog_articles_seeded, list_published_blog_articles
from app.video_shorts.services.db import ensure_pricing_interest_schema, get_db
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
    pricing_plans = load_storage_plan_catalog(
        plan_ids=["plan_free", "plan_2gb", "plan_10gb", "plan_100gb"]
    )
    plan_lookup = {str(plan.get("plan_id") or ""): plan for plan in pricing_plans}
    self_serve_tiers = [
        {
            "plan_id": "plan_free",
            "shorts": 10,
            "source_hours": 1,
            "price": int((plan_lookup.get("plan_free") or {}).get("monthly_price") or 0),
            "note": "Free forever",
            "register_url": url_for("video_shorts_bp.register", plan="plan_free"),
        },
        {
            "plan_id": "plan_2gb",
            "shorts": 30,
            "source_hours": 3,
            "price": int((plan_lookup.get("plan_2gb") or {}).get("monthly_price") or 9),
            "note": "",
            "register_url": url_for("video_shorts_bp.register", plan="plan_2gb"),
        },
        {
            "plan_id": "plan_10gb",
            "shorts": 90,
            "source_hours": 9,
            "price": int((plan_lookup.get("plan_10gb") or {}).get("monthly_price") or 19),
            "note": "",
            "register_url": url_for("video_shorts_bp.register", plan="plan_10gb"),
        },
    ]
    autopilot_tiers = [
        {"shorts": 15, "price": 20, "compare_price": 50},
        {"shorts": 30, "price": 40, "compare_price": 100},
        {"shorts": 45, "price": 60, "compare_price": 150},
        {"shorts": 60, "price": 80, "compare_price": 200},
    ]
    return render_template(
        "vs_home.html",
        self_serve_tiers=self_serve_tiers,
        autopilot_tiers=autopilot_tiers,
        autopilot_interest_url=url_for("video_shorts_bp.capture_autopilot_interest"),
        latest_blog_articles=latest_articles,
    )


@video_shorts_bp.route("/pricing/autopilot-interest", methods=["POST"])
def capture_autopilot_interest():
    payload = request.get_json(silent=True) or request.form
    email = str((payload or {}).get("email") or "").strip().lower()
    source = str((payload or {}).get("source") or "pricing").strip() or "pricing"
    try:
        monthly_shorts = int((payload or {}).get("monthly_shorts") or 0)
        monthly_price = int((payload or {}).get("monthly_price") or 0)
        compare_price = int((payload or {}).get("compare_price") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_tier"}), 400
    valid_tiers = {
        15: (20, 50),
        30: (40, 100),
        45: (60, 150),
        60: (80, 200),
    }
    expected = valid_tiers.get(monthly_shorts)
    if not email or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return jsonify({"ok": False, "error": "invalid_email"}), 400
    if not expected or expected[0] != monthly_price or expected[1] != compare_price:
        return jsonify({"ok": False, "error": "invalid_tier"}), 400
    conn = get_db()
    try:
        ensure_pricing_interest_schema(conn)
        conn.execute(
            """
            INSERT INTO pricing_autopilot_leads (
                email,
                monthly_shorts,
                monthly_price,
                compare_price,
                source
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [email, monthly_shorts, monthly_price, compare_price, source],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        current_app.logger.exception(
            "Failed to capture autopilot interest email=%s monthly_shorts=%s",
            email,
            monthly_shorts,
        )
        return jsonify({"ok": False, "error": "save_failed"}), 500
    finally:
        conn.close()
    return jsonify({"ok": True})
