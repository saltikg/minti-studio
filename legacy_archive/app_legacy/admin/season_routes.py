# app/admin/season_routes.py
import json
from flask import render_template, request, redirect, url_for, session, flash, current_app
from app.admin import admin_bp
from .season_research import research_seasons_lite
from app.db import connect_ro, connect_rw

def _get_db(read_only=True):
    return connect_ro() if read_only else connect_rw()

@admin_bp.route("/seasons", methods=["GET", "POST"], endpoint="seasons_page")
def seasons_page():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    # Manual add season (from the small form at the top)
    if request.method == "POST":
        name = (request.form.get("season_name") or "").strip()
        group = (request.form.get("season_group") or "seasonal").strip() or "seasonal"
        seeds = request.form.get("seeds_json") or "[]"
        themes = request.form.get("theme_tokens") or "[]"
        types = request.form.get("type_tokens") or "[]"
        start_date = (request.form.get("start_date") or "").strip() or None
        end_date = (request.form.get("end_date") or "").strip() or None
        locale = (request.form.get("locale") or "US").strip() or "US"
        reason = (request.form.get("reason") or "").strip()

        try:
            con = _get_db(False)
            con.execute("""
                INSERT INTO seasons
                  (season_name, seeds_json, theme_tokens, type_tokens, season_group, start_date, end_date, locale, reason)
                VALUES
                  (?, CAST(? AS JSONB), CAST(? AS JSONB), CAST(? AS JSONB), ?, ?, ?, ?, ?)
            """, [name, seeds, themes, types, group, start_date, end_date, locale, reason])
            con.commit()
            con.close()
            flash(f"Added season {name}", "success")
        except Exception as e:
            current_app.logger.exception("Add season failed")
            flash(f"Add error: {e}", "danger")

        return redirect(url_for("admin_bp.seasons_page"))

    # GET list
    q = request.args.get("q", "").strip().lower()
    where = ""
    params = []
    if q:
        where = """WHERE lower(season_name) LIKE ?
                        OR lower(season_group) LIKE ?
                        OR lower(locale) LIKE ?"""
        s = f"%{q}%"
        params = [s, s, s]

    con = _get_db(True)
    rows = con.execute(f"""
        SELECT id, season_name, season_group,
               CAST(seeds_json AS VARCHAR) AS seeds_json,
               CAST(theme_tokens AS VARCHAR) AS theme_tokens,
               CAST(type_tokens  AS VARCHAR) AS type_tokens,
               start_date, end_date, locale, reason, created_at
        FROM seasons
        {where}
        ORDER BY created_at DESC
        LIMIT 500
    """, params).fetchall()
    cols = [d[0] for d in con.description]
    seasons = [dict(zip(cols, r)) for r in rows]
    con.close()

    candidates = session.get("season_candidates", [])
    return render_template("seasons.html", seasons=seasons, search=q, candidates=candidates)

@admin_bp.route("/seasons/research", methods=["POST"], endpoint="seasons_research")
def seasons_research():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))
    locale = request.form.get("locale", "US")
    window_days = int(request.form.get("window_days", 14))  # yeni

    try:
        cands = research_seasons_lite(locale=locale, window_days=window_days)
        session["season_candidates"] = cands or []
        if not cands:
            flash("No new active evergreen seasons today", "warning")
        else:
            flash(f"{len(cands)} candidate season(s) generated", "success")
    except Exception as e:
        current_app.logger.exception("Season research failed")
        flash(f"Research error: {e}", "danger")
        session["season_candidates"] = []
    return redirect(url_for("admin_bp.seasons_page"))

@admin_bp.route("/seasons/save-candidates", methods=["POST"], endpoint="save_season_candidates")
def save_season_candidates():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    selected = request.form.getlist("selected_name")
    cand = session.get("season_candidates", [])
    cmap = {c.get("season_name"): c for c in cand}
    to_save = [cmap[n] for n in selected if n in cmap]

    if not to_save:
        flash("No candidates selected", "warning")
        return redirect(url_for("admin_bp.seasons_page"))

    con = _get_db(False)
    ok, fail = 0, 0
    for c in to_save:
        try:
            con.execute("""
                INSERT INTO seasons
                  (season_name, seeds_json, theme_tokens, type_tokens, season_group, start_date, end_date, locale, reason)
                VALUES
                  (?, CAST(? AS JSONB), CAST(? AS JSONB), CAST(? AS JSONB), ?, ?, ?, ?, ?)
            """, [
                c.get("season_name"),
                json.dumps(c.get("seeds_json", [])),
                json.dumps(c.get("theme_tokens", [])),
                json.dumps(c.get("type_tokens", [])),
                c.get("season_group", "seasonal"),
                c.get("start_date"),
                c.get("end_date"),
                c.get("locale", "US"),
                c.get("reason", "")
            ])
            ok += 1
        except Exception:
            current_app.logger.exception("Save season failed")
            fail += 1
    con.commit()
    con.close()

    flash(f"Saved {ok} season(s). Failed {fail}.", "success" if ok else "danger")
    session.pop("season_candidates", None)
    return redirect(url_for("admin_bp.seasons_page"))

@admin_bp.route("/seasons/delete/<int:season_id>", methods=["POST"], endpoint="delete_season")
def delete_season(season_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))
    con = _get_db(False)
    try:
        con.execute("DELETE FROM seasons WHERE id = ?", [season_id])
        con.commit()
        flash(f"Season {season_id} deleted", "success")
    except Exception as e:
        flash(f"Delete error: {e}", "danger")
    finally:
        con.close()
    return redirect(url_for("admin_bp.seasons_page"))
