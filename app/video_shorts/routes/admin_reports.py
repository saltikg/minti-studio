from __future__ import annotations

import io
from datetime import datetime
from functools import wraps
from uuid import uuid4

from flask import abort, current_app, g, jsonify, redirect, render_template, request, send_file, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.autopilot_monthly_reports import (
    PT,
    build_monthly_report_email,
    customer_first_name,
    load_admin_report_rows,
    load_report_record,
    month_label,
    month_range,
    month_value,
    new_send_log_id,
    parse_report_month,
    report_storage_key,
    report_tracking_token,
    resolve_converted_customer,
    safe_report_filename,
    send_monthly_report_email,
    validate_report_storage_key,
)
from app.video_shorts.services.dashboard_metrics import load_monthly_report_metrics
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.stats_report import issue_report_grant, render_report_pdf
from app.video_shorts.services.storage import get_media_storage


def require_reports_admin(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        current_user = getattr(g, "vs_current_user", None) or {}
        if str(current_user.get("role") or "").strip().lower() != "admin":
            abort(403)
        return view_func(*args, **kwargs)

    return wrapped


def _month_or_400(raw: object):
    try:
        return parse_report_month(raw)
    except ValueError as exc:
        abort(400, description=str(exc))


def _scope_or_403(conn, brand_id: str) -> dict[str, str]:
    scope = resolve_converted_customer(conn, brand_id=brand_id)
    if not scope or scope.get("brand_id") != str(brand_id or "").strip():
        abort(403)
    return scope


def _record_or_403(conn, scope: dict[str, str], report_month):
    record = load_report_record(
        conn,
        owner_user_id=scope["owner_user_id"],
        brand_id=scope["brand_id"],
        month=report_month,
    )
    if not record:
        abort(403)
    if (
        str(record.get("owner_user_id") or "") != scope["owner_user_id"]
        or str(record.get("brand_id") or "") != scope["brand_id"]
    ):
        abort(403)
    return record


@video_shorts_bp.route("/admin/reports", methods=["GET"])
@require_reports_admin
def admin_monthly_reports():
    report_month = _month_or_400(request.args.get("month"))
    today = datetime.now(PT).date()
    conn = get_db_readonly()
    try:
        rows = load_admin_report_rows(conn, month=report_month, today=today)
    finally:
        conn.close()
    start_date, end_date = month_range(report_month)
    for row in rows:
        row["dashboard_url"] = url_for(
            "video_shorts_bp.video_stats_page",
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            admin_operation_brand=row["brand_id"],
            admin_operation_kind="customer",
        )
        row["email_subject"] = f"Your Shorts report — {month_label(report_month)}"
    return render_template(
        "shorts_admin_monthly_reports.html",
        admin_title="Monthly reports",
        rows=rows,
        selected_month=month_value(report_month),
        selected_month_label=month_label(report_month),
    )


@video_shorts_bp.route("/admin/reports/<brand_id>/<month>/generate", methods=["POST"])
@require_reports_admin
def admin_generate_monthly_report(brand_id: str, month: str):
    report_month = _month_or_400(month)
    start_date, end_date = month_range(report_month)
    conn = get_db_readonly()
    try:
        scope = _scope_or_403(conn, brand_id)
        existing = load_report_record(
            conn,
            owner_user_id=scope["owner_user_id"],
            brand_id=scope["brand_id"],
            month=report_month,
        )
        if existing and str(existing.get("status") or "").lower() == "sent":
            return jsonify(ok=False, message="Sent reports are locked and cannot be regenerated."), 409
        if not scope.get("recipient") or "@" not in scope["recipient"]:
            return jsonify(ok=False, message="The customer account does not have a valid email address."), 409
        metrics = load_monthly_report_metrics(
            conn,
            owner_user_id=scope["owner_user_id"],
            brand_id=scope["brand_id"],
            brand_name=scope["brand_name"],
            start_date=start_date,
            end_date=end_date,
            today=datetime.now(PT).date(),
        )
        headline = str(((metrics.get("report_summary") or {}).get("headline")) or "").strip()
    finally:
        conn.close()

    token = issue_report_grant(
        owner_user_id=scope["owner_user_id"],
        brand_id=scope["brand_id"],
        start_date=start_date,
        end_date=end_date,
    )
    try:
        pdf_bytes, elapsed = render_report_pdf(
            token=token,
            brand_id=scope["brand_id"],
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        current_app.logger.exception("Monthly report render failed brand_id=%s month=%s", brand_id, month)
        return jsonify(ok=False, message=str(exc) or "The PDF could not be generated."), 502

    storage = get_media_storage()
    new_key = report_storage_key(scope["owner_user_id"], scope["brand_id"], report_month)
    storage.put_bytes(pdf_bytes, new_key, content_type="application/pdf")
    report_id = str((existing or {}).get("id") or uuid4())
    click_token = report_tracking_token()
    write_conn = get_db()
    try:
        saved = write_conn.execute(
            """
            INSERT INTO autopilot_monthly_reports (
                id, owner_user_id, brand_id, month, status, s3_key, recipient,
                headline, generated_at, sent_at, provider_message_id, error,
                tracking_token, first_clicked_at, click_count
            ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, CURRENT_TIMESTAMP, NULL, NULL, NULL, ?, NULL, 0)
            ON CONFLICT (owner_user_id, brand_id, month) DO UPDATE SET
                s3_key = excluded.s3_key,
                recipient = excluded.recipient,
                headline = excluded.headline,
                generated_at = CURRENT_TIMESTAMP,
                error = NULL,
                tracking_token = excluded.tracking_token,
                first_clicked_at = NULL,
                click_count = 0
            WHERE autopilot_monthly_reports.status = 'draft'
            RETURNING id
            """,
            [
                report_id,
                scope["owner_user_id"],
                scope["brand_id"],
                report_month,
                new_key,
                scope["recipient"],
                headline,
                click_token,
            ],
        ).fetchone()
        if not saved:
            raise RuntimeError("Sent reports are locked and cannot be regenerated.")
        write_conn.commit()
    except Exception as exc:
        write_conn.rollback()
        try:
            storage.delete(new_key)
        except Exception:
            current_app.logger.exception("Could not remove unused monthly report object key=%s", new_key)
        return jsonify(ok=False, message=str(exc)), 409
    finally:
        write_conn.close()

    old_key = str((existing or {}).get("s3_key") or "").strip()
    if old_key and old_key != new_key:
        try:
            validate_report_storage_key(
                old_key,
                owner_user_id=scope["owner_user_id"],
                brand_id=scope["brand_id"],
                month=report_month,
            )
            storage.delete(old_key)
        except Exception:
            current_app.logger.exception("Could not remove replaced monthly report object key=%s", old_key)
    current_app.logger.info(
        "admin_monthly_report_generated owner=%s brand=%s month=%s bytes=%s seconds=%.3f",
        scope["owner_user_id"], scope["brand_id"], month_value(report_month), len(pdf_bytes), elapsed,
    )
    return jsonify(ok=True, message="Draft generated.")


@video_shorts_bp.route("/admin/reports/<brand_id>/<month>/preview", methods=["GET"])
@require_reports_admin
def admin_preview_monthly_report(brand_id: str, month: str):
    report_month = _month_or_400(month)
    conn = get_db_readonly()
    try:
        scope = _scope_or_403(conn, brand_id)
        record = _record_or_403(conn, scope, report_month)
    finally:
        conn.close()
    try:
        key = validate_report_storage_key(
            record["s3_key"],
            owner_user_id=scope["owner_user_id"],
            brand_id=scope["brand_id"],
            month=report_month,
        )
        pdf_bytes = get_media_storage().read_bytes(key)
    except Exception:
        current_app.logger.exception("Monthly report preview failed brand_id=%s month=%s", brand_id, month)
        abort(404)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=safe_report_filename(scope["brand_name"], report_month),
        max_age=0,
    )


@video_shorts_bp.route("/admin/reports/<brand_id>/<month>/send", methods=["POST"])
@require_reports_admin
def admin_send_monthly_report(brand_id: str, month: str):
    report_month = _month_or_400(month)
    start_date, end_date = month_range(report_month)
    data = request.get_json(silent=True) or {}
    conn = get_db_readonly()
    try:
        scope = _scope_or_403(conn, brand_id)
        record = _record_or_403(conn, scope, report_month)
    finally:
        conn.close()
    if not scope.get("recipient") or "@" not in scope["recipient"]:
        return jsonify(ok=False, message="The customer account does not have a valid email address."), 409
    is_resend = str(record.get("status") or "").lower() == "sent"
    if is_resend and data.get("confirm_resend") is not True:
        return jsonify(ok=False, requires_confirmation=True, message="This frozen report was already sent. Send it again?"), 409

    try:
        key = validate_report_storage_key(
            record["s3_key"],
            owner_user_id=scope["owner_user_id"],
            brand_id=scope["brand_id"],
            month=report_month,
        )
        pdf_bytes = get_media_storage().read_bytes(key)
    except Exception:
        current_app.logger.exception("Monthly report attachment load failed brand_id=%s month=%s", brand_id, month)
        return jsonify(ok=False, message="The frozen PDF could not be loaded."), 500

    base_url = str(current_app.config.get("BASE_URL") or "https://mintistudio.com").rstrip("/")
    tracking_url = f"{base_url}{url_for('video_shorts_bp.monthly_report_click', token=record['tracking_token'])}"
    email = build_monthly_report_email(
        first_name=customer_first_name(scope.get("account_name"), scope["recipient"]),
        month=report_month,
        headline=str(record.get("headline") or ""),
        dashboard_url=tracking_url,
    )
    filename = safe_report_filename(scope["brand_name"], report_month)
    provider_message_id = ""
    send_error = ""
    try:
        result = send_monthly_report_email(
            to_email=scope["recipient"],
            subject=email["subject"],
            html_body=email["html"],
            text_body=email["text"],
            pdf_bytes=pdf_bytes,
            filename=filename,
        )
        provider_message_id = str(result.get("message_id") or result.get("request_id") or "").strip()
    except Exception as exc:
        send_error = str(exc) or exc.__class__.__name__

    write_conn = get_db()
    try:
        write_conn.execute(
            """
            INSERT INTO autopilot_monthly_report_sends (
                id, report_id, recipient, sent_at, provider_message_id, error
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            """,
            [new_send_log_id(), record["id"], scope["recipient"], provider_message_id or None, send_error or None],
        )
        if send_error:
            write_conn.execute(
                """
                UPDATE autopilot_monthly_reports SET error = ?, recipient = ?
                WHERE id = ? AND owner_user_id = ? AND brand_id = ? AND month = ?
                """,
                [send_error, scope["recipient"], record["id"], scope["owner_user_id"], scope["brand_id"], report_month],
            )
        else:
            write_conn.execute(
                """
                UPDATE autopilot_monthly_reports
                SET status = 'sent', recipient = ?, sent_at = CURRENT_TIMESTAMP,
                    provider_message_id = ?, error = NULL
                WHERE id = ? AND owner_user_id = ? AND brand_id = ? AND month = ?
                """,
                [scope["recipient"], provider_message_id, record["id"], scope["owner_user_id"], scope["brand_id"], report_month],
            )
        write_conn.commit()
    except Exception:
        write_conn.rollback()
        current_app.logger.exception("Monthly report send result could not be recorded report_id=%s", record["id"])
        return jsonify(ok=False, message="The send result could not be recorded."), 500
    finally:
        write_conn.close()
    if send_error:
        return jsonify(ok=False, message=send_error), 502
    return jsonify(ok=True, message="Report sent.", recipient=scope["recipient"], subject=email["subject"])


@video_shorts_bp.route("/r/<token>", methods=["GET"])
def monthly_report_click(token: str):
    target = url_for("video_shorts_bp.video_stats_page")
    clean_token = str(token or "").strip()
    if not clean_token:
        return redirect(target)
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT month FROM autopilot_monthly_reports
            WHERE tracking_token = ? AND status = 'sent'
            LIMIT 1
            """,
            [clean_token],
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE autopilot_monthly_reports
                SET first_clicked_at = COALESCE(first_clicked_at, CURRENT_TIMESTAMP),
                    click_count = COALESCE(click_count, 0) + 1
                WHERE tracking_token = ? AND status = 'sent'
                """,
                [clean_token],
            )
            conn.commit()
            report_month = row[0]
            start_date, end_date = month_range(report_month)
            target = url_for(
                "video_shorts_bp.video_stats_page",
                start=start_date.isoformat(),
                end=end_date.isoformat(),
            )
    except Exception:
        conn.rollback()
        current_app.logger.exception("Monthly report click tracking failed")
    finally:
        conn.close()
    return redirect(target)
