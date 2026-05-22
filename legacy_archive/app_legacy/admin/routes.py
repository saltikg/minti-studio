import os

from datetime import date, timedelta
from flask import (
    render_template, request, redirect, url_for,
    session, flash
)
from app.admin import admin_bp
from app.db import connect_ro, connect_rw


ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "mintipass")


# --------------------------
# LOGIN / LOGOUT
# --------------------------
@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pwd = request.form.get("password")
        if pwd == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_bp.dashboard"))
        flash("Wrong password", "danger")
    return render_template("login.html")


@admin_bp.route("/logout")
def logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_bp.login"))


# --------------------------
# DASHBOARD - duplicate contents
# --------------------------
@admin_bp.route("/")
def dashboard():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    con = connect_ro()
    query = """
        SELECT idea_id, title, slug, hero_image_url AS image, updated_at
        FROM blog_contents
        WHERE LOWER(title) IN (
            SELECT LOWER(title)
            FROM blog_contents
            GROUP BY LOWER(title)
            HAVING COUNT(*) > 1
        )
        ORDER BY LOWER(title), updated_at DESC;
    """
    posts = con.execute(query).fetchall()
    cols = [d[0] for d in con.description]
    data = [dict(zip(cols, row)) for row in posts]
    con.close()
    return render_template("dashboard.html", posts=data)


# --------------------------
# ALL CONTENTS
# --------------------------
@admin_bp.route("/all")
def all_contents():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    search = request.args.get("q", "").strip().lower()
    sort = request.args.get("sort", "updated_at")
    order = request.args.get("order", "desc")

    con = connect_ro()

    base_query = """
        SELECT idea_id, title, slug, category_slug, hero_image_url AS image, updated_at
        FROM blog_contents
    """

    where_clause = ""
    params = []
    if search:
        where_clause = "WHERE LOWER(title) LIKE ? OR LOWER(slug) LIKE ? OR LOWER(category_slug) LIKE ?"
        s = f"%{search}%"
        params = [s, s, s]

    sort_clause = f"ORDER BY {sort} {order.upper()}"

    query = f"{base_query} {where_clause} {sort_clause} LIMIT 500;"
    posts = con.execute(query, params).fetchall()
    cols = [d[0] for d in con.description]
    data = [dict(zip(cols, row)) for row in posts]
    con.close()

    return render_template("all_contents.html", posts=data, search=search, sort=sort, order=order)


# --------------------------
# STATS PAGE
# --------------------------
@admin_bp.route("/stats")
def stats():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    today = date.today()
    thirty_days_ago = today - timedelta(days=30)

    start_date = request.args.get("start", thirty_days_ago.strftime("%Y-%m-%d"))
    end_date = request.args.get("end", today.strftime("%Y-%m-%d"))

    con = connect_ro()
    query = f"""
        SELECT strftime('%Y-%m-%d', updated_at) AS day, COUNT(*) AS count
        FROM blog_contents
        WHERE updated_at BETWEEN '{start_date}' AND '{end_date}'
        GROUP BY 1
        ORDER BY 1
    """
    rows = con.execute(query).fetchall()
    con.close()

    labels = [r[0] for r in rows]
    counts = [r[1] for r in rows]

    return render_template("stats.html",
                           labels=labels,
                           counts=counts,
                           start_date=start_date,
                           end_date=end_date)


# --------------------------
# DEFINITIONS PAGES
# --------------------------

 

# --------------------------
# DELETE (single + bulk)
# --------------------------
@admin_bp.route("/delete/<idea_id>", methods=["POST"])
def delete_post(idea_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    try:
        con = connect_rw()
        related_tables = [
            "blog_posts",
            "idea_products",
            "idea_rules_deal",
            "season_phrases",
            "idea_sections",
            "trend_ideas"
        ]
        for t in related_tables:
            try:
                if t == "season_phrases":
                    con.execute(f"DELETE FROM {t} WHERE phrase = ?", [idea_id])
                else:
                    con.execute(f"DELETE FROM {t} WHERE idea_id = ?", [idea_id])
            except Exception:
                pass

        con.execute("DELETE FROM blog_contents WHERE idea_id = ?", [idea_id])
        con.commit()
        con.close()
        flash(f"Deleted {idea_id} and related data", "success")
    except Exception as e:
        flash(f"Error deleting {idea_id}: {e}", "danger")

    return redirect(request.referrer or url_for("admin_bp.dashboard"))


@admin_bp.route("/bulk-delete", methods=["POST"])
def bulk_delete():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    idea_ids = request.form.getlist("selected_ids")
    if not idea_ids:
        flash("No items selected for deletion.", "warning")
        return redirect(request.referrer or url_for("admin_bp.dashboard"))

    try:
        con = connect_rw()
        related_tables = [
            "blog_posts",
            "idea_products",
            "idea_rules_deal",
            "season_phrases",
            "idea_sections",
            "trend_ideas"
        ]
        deleted_count = 0
        for idea_id in idea_ids:
            for t in related_tables:
                try:
                    if t == "season_phrases":
                        con.execute(f"DELETE FROM {t} WHERE phrase = ?", [idea_id])
                    else:
                        con.execute(f"DELETE FROM {t} WHERE idea_id = ?", [idea_id])
                except Exception:
                    pass
            con.execute("DELETE FROM blog_contents WHERE idea_id = ?", [idea_id])
            deleted_count += 1

        con.commit()
        con.close()
        flash(f"Deleted {deleted_count} posts.", "success")
    except Exception as e:
        flash(f"Error during bulk delete: {e}", "danger")

    return redirect(request.referrer or url_for("admin_bp.dashboard"))
