from flask import (
    render_template, request, redirect, url_for,
    flash, session, jsonify
)
from app.admin import admin_bp
from app.admin.idea_planner.services import (
    trend_collector,
    planner_context,
    planner_llm,
    planner_storage
)

# ---------------------------------------------------------------------
# 🧩 Idea Planner – Ana Panel
# ---------------------------------------------------------------------
@admin_bp.route("/idea-planner")
def idea_planner_dashboard():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))
    return render_template("idea_planner.html")


# ---------------------------------------------------------------------
# 1️⃣ Trendleri Güncelle
# ---------------------------------------------------------------------
@admin_bp.route("/update-trends", methods=["POST"])
def update_trends():
    ...
    flash("Trends updated successfully!", "success")
    return redirect(url_for("admin_bp.trends"))


# ---------------------------------------------------------------------
# 2️⃣ Fikir Planı Oluştur (LLM)
# ---------------------------------------------------------------------
@admin_bp.route("/idea-planner/plan", methods=["POST"])
def generate_plan():
    if not session.get("is_admin"):
        return jsonify({"error": "Unauthorized"}), 403
    context = planner_context()
    ideas = planner_llm(context)
    return jsonify({"ideas": ideas})

# ---------------------------------------------------------------------
# 3️⃣ Kaydet DB’ye
# ---------------------------------------------------------------------
@admin_bp.route("/idea-planner/save", methods=["POST"])
def save_plan():
    if not session.get("is_admin"):
        return jsonify({"error": "Unauthorized"}), 403
    ideas = request.json.get("ideas", [])
    result = planner_storage(ideas)
    return jsonify(result)
