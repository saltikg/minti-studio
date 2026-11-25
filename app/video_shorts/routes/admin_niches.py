from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import require_admin
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niche_outliers import load_latest_niche_outliers
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
    for rank, outlier in enumerate(outlier_rows, start=1):
        channel = pool_by_channel[str(outlier.get("youtube_channel_id") or "")]
        outlier["rank"] = rank
        outlier["channel_title"] = channel.get("title")
        outlier["credential"] = channel.get("credential")
        best_by_channel.setdefault(str(outlier.get("youtube_channel_id") or ""), outlier)

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
    )


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
