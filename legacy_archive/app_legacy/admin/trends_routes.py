# app/admin/trends_routes.py
import json
from flask import render_template, request, redirect, url_for, session, flash, current_app

from app.admin import admin_bp
from .trends_research import research_trends_lite
from .youtube_sync import sync_youtube_metadata_only  # NEW
from .youtube_trend_research import research_youtube_trends
from app.db import connect_ro, connect_rw

def _get_db(read_only: bool = True):
    return connect_ro() if read_only else connect_rw()

def _format_duration(sec):
    """
    125 gibi saniye değerini 2:05 veya 1:02:03 formatına çevirir.
    """
    if sec is None:
        return ""
    try:
        sec = int(sec)
    except Exception:
        return ""
    if sec < 0:
        return ""

    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _fetch_youtube_videos_with_flags(limit: int = 100):
    """
    youtube_videos + youtube_channels + youtube_trend_ideas join.
    Her video için:
      - has_captions
      - has_trend_idea (pending veya published)
    döner.
    """
    con = _get_db(True)
    rows = con.execute(
        """
        SELECT
          v.id,
          v.video_id,
          v.video_title,
          v.video_url,
          v.published_at,
          v.has_captions,
          v.caption_lang,
          c.channel_title,
          c.channel_handle,
          CASE WHEN COUNT(ti.id) > 0 THEN TRUE ELSE FALSE END AS has_trend_idea
        FROM youtube_videos v
        JOIN youtube_channels c ON v.channel_id = c.id
        LEFT JOIN youtube_trend_ideas ti
          ON ti.video_id = v.id
          AND lower(ti.status) IN ('pending','published')
        GROUP BY
          v.id,
          v.video_id,
          v.video_title,
          v.video_url,
          v.published_at,
          v.has_captions,
          v.caption_lang,
          c.channel_title,
          c.channel_handle
        ORDER BY v.published_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    cols = [d[0] for d in con.description]
    videos = [dict(zip(cols, r)) for r in rows]
    con.close()
    return videos


# ---------------------------------------------------------------------
# DAILY TRENDS LIST PAGE - ESKI SAYFA
# ---------------------------------------------------------------------
@admin_bp.route("/trends", methods=["GET", "POST"], endpoint="daily_trends_page")
def trends_page():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    q = request.args.get("q", "").strip().lower()
    where, params = "", []
    if q:
        where = "WHERE lower(topic) LIKE ? OR lower(source) LIKE ? OR lower(category) LIKE ?"
        s = f"%{q}%"
        params = [s, s, s]

    con = _get_db(True)
    rows = con.execute(f"""
        SELECT id, topic, source, category, volume, locale, trend_date, detected_at
        FROM daily_trends
        {where}
        ORDER BY detected_at DESC
        LIMIT 300
    """, params).fetchall()

    cols = [d[0] for d in con.description]
    trends = [dict(zip(cols, r)) for r in rows]
    con.close()

    return render_template("trends.html", trends=trends, search=q)


# ---------------------------------------------------------------------
# DAILY TRENDS RESEARCH
# ---------------------------------------------------------------------
@admin_bp.route("/trends/research", methods=["POST"], endpoint="trends_research")
def trends_research():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    locale = request.form.get("locale", "US")
    limit = int(request.form.get("limit", 20))

    try:
        candidates = research_trends_lite(locale=locale, limit=limit)
        session["trend_candidates"] = candidates or []
        if not candidates:
            flash("No new trends found today", "warning")
        else:
            flash(f"{len(candidates)} candidate trend(s) generated", "success")
    except Exception as e:
        current_app.logger.exception("Trend research failed")
        flash(f"Research error: {e}", "danger")
        session["trend_candidates"] = []

    return redirect(url_for("admin_bp.daily_trends_page"))


# ---------------------------------------------------------------------
# DAILY TRENDS SAVE CANDIDATES
# ---------------------------------------------------------------------
@admin_bp.route("/trends/save-candidates", methods=["POST"], endpoint="save_trend_candidates")
def save_trend_candidates():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    selected = request.form.getlist("selected_topic")
    cand = session.get("trend_candidates", [])
    cmap = {c.get("topic"): c for c in cand}
    to_save = [cmap[n] for n in selected if n in cmap]

    if not to_save:
        flash("No candidates selected", "warning")
        return redirect(url_for("admin_bp.daily_trends_page"))

    con = _get_db(False)
    ok, fail = 0, 0
    for t in to_save:
        try:
            con.execute("""
                INSERT INTO daily_trends (topic, source, category, volume, locale, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                t.get("topic"),
                t.get("source"),
                t.get("category"),
                t.get("volume"),
                t.get("locale"),
                t.get("reason"),
            ])
            ok += 1
        except Exception:
            current_app.logger.exception("Save trend failed")
            fail += 1
    con.commit()
    con.close()

    flash(f"Saved {ok} trends. Failed {fail}.", "success" if ok else "danger")
    session.pop("trend_candidates", None)
    return redirect(url_for("admin_bp.daily_trends_page"))


# ---------------------------------------------------------------------
# DAILY TRENDS DELETE
# ---------------------------------------------------------------------
@admin_bp.route("/trends/delete/<int:trend_id>", methods=["POST"], endpoint="delete_trend")
def delete_trend(trend_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    con = _get_db(False)
    try:
        con.execute("DELETE FROM daily_trends WHERE id = ?", [trend_id])
        con.commit()
        flash(f"Trend {trend_id} deleted", "success")
    except Exception as e:
        flash(f"Delete error: {e}", "danger")
    finally:
        con.close()

    return redirect(url_for("admin_bp.daily_trends_page"))


@admin_bp.route("/youtube-trends", methods=["GET"], endpoint="youtube_trends_page")
def youtube_trends_page():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    # Arama ve status filtresi
    q = (request.args.get("q") or "").strip().lower()
    status_filter = (request.args.get("status") or "").strip().lower()

    # Basit filtreler
    has_captions_filter = bool(request.args.get("has_captions"))
    has_ideas_filter = bool(request.args.get("has_ideas"))
    only_shorts_filter = bool(request.args.get("only_shorts"))

    # Sort param
    sort = (request.args.get("sort") or "newest").strip().lower()

    # Sayfalama
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    per_page = 20
    offset = (page - 1) * per_page

    where_clauses = []
    params = []

    # Video araması: video title + channel title
    if q:
        where_clauses.append(
            "(lower(v.video_title) LIKE ? OR lower(c.channel_title) LIKE ?)"
        )
        s = f"%{q}%"
        params.extend([s, s])

    # Filtreler
    if has_captions_filter:
        where_clauses.append("v.has_captions = TRUE")

    if has_ideas_filter:
        where_clauses.append("COALESCE(cnt.idea_count, 0) > 0")

    if only_shorts_filter:
        where_clauses.append("v.is_short = TRUE")

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    base_from = """
        FROM youtube_videos v
        JOIN youtube_channels c ON v.channel_id = c.id
        LEFT JOIN (
          SELECT video_id, COUNT(*) AS idea_count
          FROM youtube_trend_ideas
          GROUP BY video_id
        ) cnt ON cnt.video_id = v.id
    """

    # Sıralama kuralları (SQL injection yok, map ile seçiyoruz)
    sort_map = {
        "newest": "v.stats_fetched_at DESC NULLS LAST, v.published_at DESC NULLS LAST",
        "published": "v.published_at DESC NULLS LAST",
        "views_desc": "v.view_count DESC NULLS LAST, v.stats_fetched_at DESC NULLS LAST",
        "views_asc": "v.view_count ASC NULLS LAST, v.stats_fetched_at DESC NULLS LAST",
        "comments_desc": "v.comment_count DESC NULLS LAST, v.stats_fetched_at DESC NULLS LAST",
    }
    order_sql = sort_map.get(sort, sort_map["newest"])

    con = _get_db(True)

    # Toplam video sayısı
    total = con.execute(
        f"SELECT COUNT(*) {base_from} {where_sql}",
        params,
    ).fetchone()[0]

    # Asıl video listesi
    rows = con.execute(
        f"""
        SELECT
          v.id,
          v.video_id,
          v.video_title,
          v.video_url,
          v.published_at,
          v.has_captions,
          v.caption_lang,
          v.duration_seconds,
          v.is_short,
          v.view_count,
          v.comment_count,
          v.stats_fetched_at,
          c.channel_title,
          COALESCE(cnt.idea_count, 0) AS idea_count,
          CASE
            WHEN v.stats_fetched_at >= current_timestamp - INTERVAL 1 DAY
              THEN TRUE
            ELSE FALSE
          END AS is_new
        {base_from}
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        params + [per_page, offset],
    ).fetchall()

    cols = [d[0] for d in con.description]

    videos = []
    video_ids = []
    for r in rows:
        d = dict(zip(cols, r))
        d["duration_human"] = _format_duration(d.get("duration_seconds"))
        videos.append(d)
        video_ids.append(d["id"])

    # Bu sayfadaki videoların trend fikirleri
    # Bu sayfadaki videoların trend fikirleri ve altyazıları
    ideas_by_video = {}
    captions_by_video = {}

    if video_ids:
        placeholders = ",".join(["?"] * len(video_ids))
        idea_params = list(video_ids)

        status_sql = ""
        if status_filter:
            status_sql = "AND lower(status) = ?"
            idea_params.append(status_filter)

        # Trend fikirleri
        idea_rows = con.execute(
            f"""
            SELECT
              id,
              video_id,
              idea_text,
              status,
              notes,
              created_at
            FROM youtube_trend_ideas
            WHERE video_id IN ({placeholders})
            {status_sql}
            ORDER BY created_at DESC
            """,
            idea_params,
        ).fetchall()

        idea_cols = [d[0] for d in con.description]
        for r in idea_rows:
            d = dict(zip(idea_cols, r))
            ideas_by_video.setdefault(d["video_id"], []).append(d)

        # Altyazılar
        caption_rows = con.execute(
            f"""
            SELECT
              video_id,
              caption_text,
              lang
            FROM youtube_captions
            WHERE video_id IN ({placeholders})
            """,
            video_ids,
        ).fetchall()

        for video_id, caption_text, lang in caption_rows:
            captions_by_video[video_id] = {
                "caption_text": caption_text,
                "lang": lang,
            }

    con.close()

    total_pages = (total + per_page - 1) // per_page if total else 1

    return render_template(
        "youtube_trends.html",
        videos=videos,
        ideas_by_video=ideas_by_video,
        captions_by_video=captions_by_video,
        search=q,
        status_filter=status_filter,
        has_captions_filter=has_captions_filter,
        has_ideas_filter=has_ideas_filter,
        only_shorts_filter=only_shorts_filter,
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        total=total,
        sort=sort,
    )


@admin_bp.route(
    "/youtube-trends/research",
    methods=["POST"],
    endpoint="youtube_trends_research",
)
def youtube_trends_research():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    try:
        limit_per_channel = int(request.form.get("limit_per_channel") or 2)
    except ValueError:
        limit_per_channel = 2
    limit_per_channel = max(1, min(limit_per_channel, 10))

    try:
        inserted = research_youtube_trends(limit_per_channel=limit_per_channel)
        if inserted:
            flash(f"Generated {inserted} YouTube trend idea(s).", "success")
        else:
            flash("No new YouTube trend ideas generated.", "warning")
    except Exception as e:
        current_app.logger.exception("YouTube trend research failed")
        flash(f"Research error: {e}", "danger")

    return redirect(url_for("admin_bp.youtube_trends_page"))





# ---------------------------------------------------------------------
# YOUTUBE: FETCH LATEST VIDEOS PER CHANNEL
# ---------------------------------------------------------------------
@admin_bp.route(
    "/youtube-trends/fetch-latest",
    methods=["POST"],
    endpoint="youtube_fetch_latest",
)
def youtube_fetch_latest():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    try:
        per_channel = int(request.form.get("per_channel") or 3)
    except ValueError:
        per_channel = 3

    try:
        new_count = sync_youtube_metadata_only(max_videos_per_channel=per_channel)
        if new_count == 0:
            flash("No new YouTube videos were added", "warning")
        else:
            flash(f"{new_count} new YouTube videos added", "success")
    except Exception as e:
        current_app.logger.exception("YouTube fetch latest failed")
        flash(f"Fetch error: {e}", "danger")

    return redirect(url_for("admin_bp.youtube_trends_page"))


# ---------------------------------------------------------------------
# YOUTUBE TREND STATUS UPDATE
# ---------------------------------------------------------------------
@admin_bp.route(
    "/youtube-trends/status/<int:idea_id>",
    methods=["POST"],
    endpoint="update_youtube_trend_status",
)
def update_youtube_trend_status(idea_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    new_status = (request.form.get("status") or "").strip().lower()
    if not new_status:
        flash("Status bilgisi eksik", "warning")
        return redirect(url_for("admin_bp.youtube_trends_page"))

    con = _get_db(False)
    try:
        con.execute(
            """
            UPDATE youtube_trend_ideas
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [new_status, idea_id],
        )
        con.commit()
        flash(f"Trend fikri {idea_id} status {new_status} olarak güncellendi", "success")
    except Exception as e:
        current_app.logger.exception("Update youtube trend status failed")
        flash(f"Update error: {e}", "danger")
    finally:
        con.close()

    return redirect(url_for("admin_bp.youtube_trends_page"))


# ---------------------------------------------------------------------
# YOUTUBE TREND DELETE
# ---------------------------------------------------------------------
@admin_bp.route(
    "/youtube-trends/delete/<int:idea_id>",
    methods=["POST"],
    endpoint="delete_youtube_trend",
)
def delete_youtube_trend(idea_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    con = _get_db(False)
    try:
        con.execute("DELETE FROM youtube_trend_ideas WHERE id = ?", [idea_id])
        con.commit()
        flash(f"YouTube trend fikri {idea_id} silindi", "success")
    except Exception as e:
        current_app.logger.exception("Delete youtube trend failed")
        flash(f"Delete error: {e}", "danger")
    finally:
        con.close()

    return redirect(url_for("admin_bp.youtube_trends_page"))






# ---------------------------------------------------------------------
# YOUTUBE VIDEO DELETE (video + caption + ideas)
# ---------------------------------------------------------------------
@admin_bp.route(
    "/youtube-videos/delete/<int:video_id>",
    methods=["POST"],
    endpoint="delete_youtube_video",
)
def delete_youtube_video(video_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    con = _get_db(False)
    try:
        # once bagli caption ve trend fikirlerini sil
        con.execute("DELETE FROM youtube_captions WHERE video_id = ?", [video_id])
        con.execute("DELETE FROM youtube_trend_ideas WHERE video_id = ?", [video_id])
        con.execute("DELETE FROM youtube_videos WHERE id = ?", [video_id])
        con.commit()
        flash(f"YouTube video {video_id} deleted", "success")
    except Exception as e:
        current_app.logger.exception("Delete youtube video failed")
        flash(f"Delete error: {e}", "danger")
    finally:
        con.close()

    return redirect(url_for("admin_bp.youtube_trends_page"))




# ---------------------------------------------------------------------
# YOUTUBE VIDEOS BULK DELETE
# ---------------------------------------------------------------------
@admin_bp.route(
    "/youtube-videos/bulk-delete",
    methods=["POST"],
    endpoint="bulk_delete_youtube_videos",
)
def bulk_delete_youtube_videos():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    video_ids = request.form.getlist("video_ids")
    if not video_ids:
        flash("No videos selected for deletion", "warning")
        return redirect(url_for("admin_bp.youtube_trends_page"))

    # Int e çevir, hatalı olanları at
    try:
        id_list = [int(v) for v in video_ids]
    except ValueError:
        flash("Invalid video ids", "danger")
        return redirect(url_for("admin_bp.youtube_trends_page"))

    placeholders = ",".join(["?"] * len(id_list))

    con = _get_db(False)
    try:
        # Önce bağlı caption ve trend fikirlerini sil
        con.execute(
            f"DELETE FROM youtube_captions WHERE video_id IN ({placeholders})",
            id_list,
        )
        con.execute(
            f"DELETE FROM youtube_trend_ideas WHERE video_id IN ({placeholders})",
            id_list,
        )
        con.execute(
            f"DELETE FROM youtube_videos WHERE id IN ({placeholders})",
            id_list,
        )
        con.commit()
        flash(f"Deleted {len(id_list)} videos and related captions and ideas", "success")
    except Exception as e:
        current_app.logger.exception("Bulk delete youtube videos failed")
        flash(f"Bulk delete error: {e}", "danger")
    finally:
        con.close()

    return redirect(url_for("admin_bp.youtube_trends_page"))


# ---------------------------------------------------------------------
# YOUTUBE CHANNELS LIST + CREATE
# ---------------------------------------------------------------------
@admin_bp.route("/youtube-channels", methods=["GET", "POST"], endpoint="youtube_channels_page")
def youtube_channels_page():
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    if request.method == "POST":
        handle = (request.form.get("channel_handle") or "").strip()
        url = (request.form.get("channel_url") or "").strip()
        title = (request.form.get("channel_title") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        is_active = bool(request.form.get("is_active"))

        if not handle or not url:
            flash("Channel handle and URL are required", "warning")
        else:
            con = _get_db(False)
            try:
                # simple duplicate check
                exists = con.execute(
                    """
                    SELECT 1
                    FROM youtube_channels
                    WHERE channel_handle = ? OR channel_url = ?
                    """,
                    [handle, url],
                ).fetchone()
                if exists:
                    flash("This channel already exists", "warning")
                else:
                    con.execute(
                        """
                        INSERT INTO youtube_channels
                          (channel_handle, channel_url, channel_title, is_active, notes)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [handle, url, title or handle, is_active, notes],
                    )
                    con.commit()
                    flash("Channel added", "success")
            except Exception:
                current_app.logger.exception("Add youtube channel failed")
                flash("Error while adding channel", "danger")
            finally:
                con.close()

        return redirect(url_for("admin_bp.youtube_channels_page"))

    # GET - list channels
    con = _get_db(True)
    rows = con.execute(
        """
        SELECT
          id,
          channel_handle,
          channel_url,
          channel_title,
          is_active,
          notes,
          last_checked_at,
          created_at
        FROM youtube_channels
        ORDER BY created_at DESC
        """
    ).fetchall()
    cols = [d[0] for d in con.description]
    channels = [dict(zip(cols, r)) for r in rows]
    con.close()

    return render_template("youtube_channels.html", channels=channels)


# ---------------------------------------------------------------------
# YOUTUBE CHANNEL TOGGLE ACTIVE
# ---------------------------------------------------------------------
@admin_bp.route(
    "/youtube-channels/toggle/<int:channel_id>",
    methods=["POST"],
    endpoint="toggle_youtube_channel",
)
def toggle_youtube_channel(channel_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    new_active_val = request.form.get("is_active")
    new_active = True if new_active_val in ("1", "true", "on") else False

    con = _get_db(False)
    try:
        con.execute(
            "UPDATE youtube_channels SET is_active = ? WHERE id = ?",
            [new_active, channel_id],
        )
        con.commit()
        flash("Channel status updated", "success")
    except Exception:
        current_app.logger.exception("Toggle youtube channel failed")
        flash("Error while updating channel", "danger")
    finally:
        con.close()

    return redirect(url_for("admin_bp.youtube_channels_page"))


# ---------------------------------------------------------------------
# YOUTUBE CHANNEL DELETE
# ---------------------------------------------------------------------
@admin_bp.route(
    "/youtube-channels/delete/<int:channel_id>",
    methods=["POST"],
    endpoint="delete_youtube_channel",
)
def delete_youtube_channel(channel_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_bp.login"))

    con = _get_db(False)
    try:
        # delete related ideas
        con.execute(
            "DELETE FROM youtube_trend_ideas WHERE channel_id = ?",
            [channel_id],
        )
        # delete captions for videos of this channel
        con.execute(
            """
            DELETE FROM youtube_captions
            WHERE video_id IN (
              SELECT id FROM youtube_videos WHERE channel_id = ?
            )
            """,
            [channel_id],
        )
        # delete videos
        con.execute(
            "DELETE FROM youtube_videos WHERE channel_id = ?",
            [channel_id],
        )
        # delete channel
        con.execute(
            "DELETE FROM youtube_channels WHERE id = ?",
            [channel_id],
        )
        con.commit()
        flash("Channel and related data deleted", "success")
    except Exception:
        current_app.logger.exception("Delete youtube channel failed")
        flash("Error while deleting channel", "danger")
    finally:
        con.close()

    return redirect(url_for("admin_bp.youtube_channels_page"))
