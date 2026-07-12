from flask import render_template

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.usage_metering import load_storage_plan_catalog


@video_shorts_bp.route("/", methods=["GET"])
def home():
    return render_template(
        "vs_home.html",
        pricing_plans=load_storage_plan_catalog(
            plan_ids=["plan_free", "plan_2gb", "plan_10gb", "plan_100gb"]
        ),
    )
