from flask import render_template

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import DEFAULT_STORAGE_PLANS


def _format_storage_gb(quota_bytes: int) -> str:
    size_gb = int(round((quota_bytes or 0) / float(1024 ** 3)))
    return f"{size_gb} GB storage"


def _format_transcription_hours(minutes: int) -> str:
    hours = int(round((minutes or 0) / 60.0))
    return f"{hours}h transcription"


def _pricing_plans() -> list[dict]:
    plans: list[dict] = []
    for plan in DEFAULT_STORAGE_PLANS:
        monthly_price = int(plan.get("price_monthly") or 0)
        yearly_price = int(plan.get("price_yearly", monthly_price * 12) or 0)
        monthly_compare = monthly_price * 12
        plans.append(
            {
                "plan_id": plan.get("plan_id"),
                "label": plan.get("label"),
                "monthly_price": monthly_price,
                "yearly_price": yearly_price,
                "yearly_compare_monthly": monthly_compare,
                "monthly_note": "forever" if monthly_price == 0 else "per month",
                "yearly_note": "forever" if yearly_price == 0 else f"${monthly_compare} if monthly",
                "lines": [
                    f"{int(plan.get('monthly_export_limit') or 0)} shorts / month",
                    _format_transcription_hours(int(plan.get("monthly_transcription_minutes") or 0)),
                    _format_storage_gb(int(plan.get("quota_bytes") or 0)),
                ],
                "is_featured": str(plan.get("plan_id") or "") == "plan_10gb",
                "button_label": "Start free" if monthly_price == 0 else "Get started",
            }
        )
    return plans


@video_shorts_bp.route("/", methods=["GET"])
def home():
    return render_template("vs_home.html", pricing_plans=_pricing_plans())
