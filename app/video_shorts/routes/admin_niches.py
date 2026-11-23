from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import require_admin
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns
from app.video_shorts.services.niches import (
    OUTREACH_LABELS,
    apply_table_filters,
    attach_outreach_statuses,
    default_pool,
    list_niches,
    load_niche_snapshot,
    load_snapshot_dates,
    pool_kpis,
)


@video_shorts_bp.route("/admin/niches", methods=["GET"])
@require_admin
def admin_niches():
    conn = get_db_readonly()
    if not table_columns(conn, "niches") or not table_columns(conn, "niche_channels"):
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

    include_partial = request.args.get("include_partial") == "1"
    pool = default_pool(rows, include_partial=include_partial)
    attach_outreach_statuses(conn, pool)
    for rank, row in enumerate(pool, start=1):
        row["rank"] = rank
    subscriber_band = str(request.args.get("subscriber_band") or "").strip()
    outreach_status = str(request.args.get("outreach_status") or "").strip()
    visible_rows = apply_table_filters(pool, subscriber_band, outreach_status)
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
        include_partial=include_partial,
        subscriber_band=subscriber_band,
        outreach_status=outreach_status,
        outreach_labels=OUTREACH_LABELS,
    )


@video_shorts_bp.route("/admin/niches/channel", methods=["POST"])
@require_admin
def admin_niche_channel_update():
    niche_slug = str(request.form.get("niche") or "").strip()
    channel_id = str(request.form.get("youtube_channel_id") or "").strip()
    snapshot_date = str(request.form.get("snapshot_date") or "").strip()
    if not niche_slug or not channel_id or not snapshot_date:
        abort(400)
    note = str(request.form.get("admin_note") or "").strip()
    if len(note) > 4000:
        abort(400)
    replied = request.form.get("replied") == "1"
    conn = get_db()
    try:
        result = conn.execute(
            """
            UPDATE niche_channels nc
            SET replied = ?, admin_note = ?, updated_at = CURRENT_TIMESTAMP
            FROM niches n
            WHERE nc.niche_id = n.id
              AND n.slug = ?
              AND nc.youtube_channel_id = ?
              AND nc.snapshot_date = ?
            """,
            [replied, note, niche_slug, channel_id, snapshot_date],
        )
        if result.rowcount != 1:
            conn.rollback()
            abort(404)
        conn.commit()
    finally:
        conn.close()
    flash("Niche channel updated.", "success")
    return_to = str(request.form.get("return_to") or "").strip()
    if not return_to.startswith("/video_shorts/admin/niches"):
        return_to = url_for("video_shorts_bp.admin_niches", niche=niche_slug)
    return redirect(return_to)
