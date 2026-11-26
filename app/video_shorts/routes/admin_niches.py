from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import require_admin
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niche_outliers import load_latest_niche_outliers
from app.video_shorts.services.niche_definitions import (
    parse_topic_lines,
    update_niche_definition,
)
from app.video_shorts.services.niches import (
    OUTREACH_LABELS,
    apply_table_filters,
    attach_outreach_statuses,
    excluded_niche_rows,
    final_niche_pool,
    list_niches,
    load_niche_snapshot,
    load_snapshot_dates,
    near_miss_niche_rows,
    pool_kpis,
)


@video_shorts_bp.route("/admin/niches", methods=["GET"])
@require_admin
def admin_niches():
    conn = get_db_readonly()
    niche_channel_columns = table_columns(conn, "niche_channels")
    strict_columns = {
        "primary_niche",
        "on_topic_share",
        "on_topic_count",
        "entity",
        "reclass_reason",
        "views_90d",
        "admin_exclude",
        "admin_include",
        "exclude_reason",
        "country",
        "audience_language",
        "relevance",
        "relevance_reason",
    }
    if (
        not table_columns(conn, "niches")
        or not niche_channel_columns
        or not strict_columns.issubset(niche_channel_columns)
    ):
        return render_template(
            "shorts_admin_niches.html",
            admin_title="Niches",
            schema_missing=True,
            niches=[],
            rows=[],
            kpis=pool_kpis([]),
        )

    niches = list_niches(conn)
    selected_slug = str(request.args.get("niche") or (niches[0]["slug"] if niches else "")).strip()
    selected, rows = load_niche_snapshot(conn, selected_slug, str(request.args.get("snapshot") or ""))
    if selected is None and niches:
        selected, rows = load_niche_snapshot(conn, niches[0]["slug"])
        selected_slug = niches[0]["slug"]

    tab = str(request.args.get("tab") or "pool").strip().lower()
    if tab not in {"pool", "top_shorts", "excluded", "near_misses"}:
        tab = "pool"
    pool = final_niche_pool(rows, clinicians_only=False)
    pool_by_channel = {
        str(row.get("youtube_channel_id") or ""): row
        for row in pool
    }
    outlier_as_of, outlier_rows = load_latest_niche_outliers(
        conn,
        selected["id"] if selected else None,
    )
    outlier_rows = [
        row for row in outlier_rows
        if str(row.get("youtube_channel_id") or "") in pool_by_channel
    ]
    best_by_channel = {}
    for outlier in outlier_rows:
        channel = pool_by_channel[str(outlier.get("youtube_channel_id") or "")]
        outlier["channel_title"] = channel.get("title")
        outlier["credential"] = channel.get("credential")
        engagement = float(outlier.get("engagement") or 0)
        normal_engagement = float(outlier.get("baseline_engagement") or 0)
        if normal_engagement > 0 and engagement >= 2 * normal_engagement:
            outlier["engagement_badge"] = "high_engagement"
        elif normal_engagement > 0 and engagement < normal_engagement / 3:
            outlier["engagement_badge"] = "possibly_promoted"
        else:
            outlier["engagement_badge"] = ""
        outlier["topic_badge"] = (
            "off_topic" if outlier.get("topic_status") == "off_topic" else ""
        )
        if outlier.get("topic_status") != "off_topic":
            best_by_channel.setdefault(str(outlier.get("youtube_channel_id") or ""), outlier)
    short_sort = str(request.args.get("short_sort") or "views").strip().lower()
    sort_fields = {
        "published": "published_at",
        "views": "views",
        "ratio": "outlier_ratio",
        "engagement": "engagement",
        "duration": "duration_s",
    }
    if short_sort not in sort_fields:
        short_sort = "views"
    short_dir = str(request.args.get("short_dir") or "desc").strip().lower()
    if short_dir not in {"asc", "desc"}:
        short_dir = "desc"
    shorts_show_all = str(request.args.get("shorts_all") or "") == "1"
    outlier_rows.sort(
        key=lambda row: row.get(sort_fields[short_sort]),
        reverse=short_dir == "desc",
    )
    outlier_total_count = len(outlier_rows)
    if not shorts_show_all:
        outlier_rows = [
            row for row in outlier_rows if row.get("topic_status") != "off_topic"
        ]
        channel_counts = {}
        capped_rows = []
        for outlier in outlier_rows:
            channel_id = str(outlier.get("youtube_channel_id") or "")
            if channel_counts.get(channel_id, 0) >= 2:
                continue
            capped_rows.append(outlier)
            channel_counts[channel_id] = channel_counts.get(channel_id, 0) + 1
        outlier_rows = capped_rows
    for rank, outlier in enumerate(outlier_rows, start=1):
        outlier["rank"] = rank

    if tab == "top_shorts":
        tab_rows = []
    elif tab == "excluded":
        tab_rows = excluded_niche_rows(rows, clinicians_only=False)
    elif tab == "near_misses":
        tab_rows = near_miss_niche_rows(rows, clinicians_only=False)
    else:
        tab_rows = pool
    tab_rows.sort(
        key=lambda row: (-int(row.get("subscriber_count") or 0), str(row.get("title") or "").lower())
    )
    attach_outreach_statuses(conn, tab_rows)
    for rank, row in enumerate(tab_rows, start=1):
        row["rank"] = rank
        if tab == "pool":
            row["best_short_30d"] = best_by_channel.get(
                str(row.get("youtube_channel_id") or "")
            )
    subscriber_band = str(request.args.get("subscriber_band") or "").strip()
    outreach_status = str(request.args.get("outreach_status") or "").strip()
    visible_rows = apply_table_filters(tab_rows, subscriber_band, outreach_status)
    snapshot_dates = load_snapshot_dates(conn, selected["id"]) if selected else []
    return render_template(
        "shorts_admin_niches.html",
        admin_title="Niches",
        schema_missing=False,
        niches=niches,
        selected=selected,
        snapshot_dates=snapshot_dates,
        rows=visible_rows,
        pool_size=len(pool),
        kpis=pool_kpis(pool),
        tab=tab,
        excluded_count=len(excluded_niche_rows(rows, clinicians_only=False)),
        near_miss_count=len(near_miss_niche_rows(rows, clinicians_only=False)),
        subscriber_band=subscriber_band,
        outreach_status=outreach_status,
        outreach_labels=OUTREACH_LABELS,
        outlier_as_of=outlier_as_of,
        outlier_rows=outlier_rows,
        outlier_total_count=outlier_total_count,
        short_sort=short_sort,
        short_dir=short_dir,
        shorts_show_all=shorts_show_all,
    )


@video_shorts_bp.route("/admin/niches/definition", methods=["POST"])
@require_admin
def admin_niche_definition_update():
    niche_slug = str(request.form.get("niche") or "").strip()
    if not niche_slug:
        abort(400)
    conn = get_db()
    try:
        version = update_niche_definition(
            conn,
            slug=niche_slug,
            name=str(request.form.get("name") or ""),
            description=str(request.form.get("description") or ""),
            core_topics=parse_topic_lines(request.form.get("core_topics")),
            adjacent_topics=parse_topic_lines(request.form.get("adjacent_topics")),
            excluded_topics=parse_topic_lines(request.form.get("excluded_topics")),
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("video_shorts_bp.admin_niches", niche=niche_slug))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash(f"Niche definition saved as version {version}.", "success")
    return redirect(url_for("video_shorts_bp.admin_niches", niche=niche_slug))


@video_shorts_bp.route("/admin/niches/short-topic", methods=["POST"])
@require_admin
def admin_niche_short_topic_update():
    niche_slug = str(request.form.get("niche") or "").strip()
    video_id = str(request.form.get("video_id") or "").strip()
    action = str(request.form.get("action") or "").strip()
    if not niche_slug or not video_id or action not in {"mark_on_topic", "mark_off_topic"}:
        abort(400)
    topic_status = "on_topic" if action == "mark_on_topic" else "off_topic"
    conn = get_db()
    try:
        if not table_columns(conn, "niche_outlier_short_topic_overrides"):
            abort(503)
        exists = conn.execute(
            """
            SELECT 1
            FROM niche_outlier_shorts outlier
            JOIN niches niche ON niche.id = outlier.niche_id
            WHERE niche.slug = ? AND outlier.video_id = ?
            LIMIT 1
            """,
            [niche_slug, video_id],
        ).fetchone()
        if not exists:
            abort(404)
        conn.execute(
            """
            INSERT INTO niche_outlier_short_topic_overrides (
                niche_id, video_id, topic_status, updated_at
            )
            SELECT id, ?, ?, CURRENT_TIMESTAMP FROM niches WHERE slug = ?
            ON CONFLICT (niche_id, video_id) DO UPDATE SET
                topic_status = EXCLUDED.topic_status,
                updated_at = CURRENT_TIMESTAMP
            """,
            [video_id, topic_status, niche_slug],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash("Short topic override updated.", "success")
    return_to = str(request.form.get("return_to") or "").strip()
    if not return_to.startswith("/video_shorts/admin/niches"):
        return_to = url_for(
            "video_shorts_bp.admin_niches", niche=niche_slug, tab="top_shorts"
        )
    return redirect(return_to)


@video_shorts_bp.route("/admin/niches/channel", methods=["POST"])
@require_admin
def admin_niche_channel_update():
    niche_slug = str(request.form.get("niche") or "").strip()
    channel_id = str(request.form.get("youtube_channel_id") or "").strip()
    snapshot_date = str(request.form.get("snapshot_date") or "").strip()
    if not niche_slug or not channel_id or not snapshot_date:
        abort(400)
    action = str(request.form.get("action") or "").strip().lower()
    reason = str(request.form.get("reason") or "").strip()
    if action not in {"exclude", "include", "clear"}:
        abort(400)
    if len(reason) > 1000 or (action in {"exclude", "include"} and not reason):
        flash("Add a reason before applying an override.", "warning")
        return_to = str(request.form.get("return_to") or "").strip()
        if not return_to.startswith("/video_shorts/admin/niches"):
            return_to = url_for("video_shorts_bp.admin_niches", niche=niche_slug)
        return redirect(return_to)
    admin_exclude = action == "exclude"
    admin_include = action == "include"
    stored_reason = reason if action != "clear" else ""
    conn = get_db()
    try:
        result = conn.execute(
            """
            UPDATE niche_channels nc
            SET admin_exclude = ?, admin_include = ?, exclude_reason = ?,
                updated_at = CURRENT_TIMESTAMP
            FROM niches n
            WHERE nc.niche_id = n.id
              AND n.slug = ?
              AND nc.youtube_channel_id = ?
              AND nc.snapshot_date = ?
            """,
            [admin_exclude, admin_include, stored_reason, niche_slug, channel_id, snapshot_date],
        )
        if result.rowcount != 1:
            conn.rollback()
            abort(404)
        conn.commit()
    finally:
        conn.close()
    flash("Niche override updated.", "success")
    return_to = str(request.form.get("return_to") or "").strip()
    if not return_to.startswith("/video_shorts/admin/niches"):
        return_to = url_for("video_shorts_bp.admin_niches", niche=niche_slug)
    return redirect(return_to)
