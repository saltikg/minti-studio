from __future__ import annotations

import os
from uuid import uuid4

from flask import current_app, flash, g, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import require_admin
from app.video_shorts.services.db import get_db
from app.video_shorts.services.email_sequence import (
    SIGNUP_SEQUENCE_KEY,
    ensure_email_sequence_schema,
    load_signup_sequence_steps,
    new_email_token,
    normalize_sequence_email,
    render_sequence_email,
    sequence_email_headers,
    unsubscribe_url,
)
from app.video_shorts.services.outreach_email_send import send_zoho_email


SAMPLE_RECIPIENT_NAME = "Alex Morgan"


def _load_step(conn, step: int):
    return conn.execute(
        """
        SELECT id, step, delay_days, subject, body_text, active
        FROM email_sequence_steps
        WHERE sequence_key = 'signup' AND step = ?
        LIMIT 1
        """,
        [step],
    ).fetchone()


def _parsed_step(value: object) -> int | None:
    try:
        step = int(value or 0)
    except (TypeError, ValueError):
        return None
    return step if 1 <= step <= 7 else None


def _base_url() -> str:
    return str(current_app.config.get("BASE_URL") or "https://mintistudio.com").rstrip("/")


@video_shorts_bp.route("/admin/email-sequence", methods=["GET", "POST"])
@require_admin
def admin_email_sequence():
    current_user = getattr(g, "vs_current_user", None) or {}
    conn = get_db()
    try:
        ensure_email_sequence_schema(conn)
        conn.commit()
        if request.method == "POST":
            step = _parsed_step(request.form.get("step"))
            row = _load_step(conn, step) if step is not None else None
            if not row:
                flash("Sequence step not found.", "danger")
                return redirect(url_for("video_shorts_bp.admin_email_sequence"))

            action = str(request.form.get("action") or "save").strip().lower()
            subject = str(request.form.get("subject") or "").strip()
            body_text = str(request.form.get("body_text") or "").strip()
            active = str(request.form.get("active") or "").lower() in {"1", "true", "yes", "on"}
            if action == "save":
                if active and (not subject or not body_text):
                    flash("Add both a subject and body before activating a step.", "warning")
                else:
                    conn.execute(
                        """
                        UPDATE email_sequence_steps
                        SET subject = ?, body_text = ?, active = ?,
                            updated_at = CURRENT_TIMESTAMP, updated_by = ?
                        WHERE sequence_key = 'signup' AND step = ?
                        """,
                        [subject, body_text, active, str(current_user.get("id") or ""), step],
                    )
                    conn.commit()
                    flash(f"Step {step} saved.", "success")
                return redirect(url_for("video_shorts_bp.admin_email_sequence", step=step))

            if action == "test":
                admin_email = normalize_sequence_email(current_user.get("email") or current_user.get("username"))
                postal_address = str(os.getenv("COMPANY_POSTAL_ADDRESS") or "").strip()
                if not admin_email or "@" not in admin_email:
                    flash("Your admin account does not have a valid email address.", "danger")
                    return redirect(url_for("video_shorts_bp.admin_email_sequence", step=step))
                if not subject or not body_text:
                    flash("Add both a subject and body before sending a test.", "warning")
                    return redirect(url_for("video_shorts_bp.admin_email_sequence", step=step))
                if not postal_address:
                    flash("COMPANY_POSTAL_ADDRESS must be configured before sending sequence email.", "danger")
                    return redirect(url_for("video_shorts_bp.admin_email_sequence", step=step))

                send_token = new_email_token()
                test_unsubscribe_url = unsubscribe_url(base_url=_base_url(), token=f"test-{send_token}")
                rendered = render_sequence_email(
                    subject=subject,
                    body_text=body_text,
                    recipient_name=str(current_user.get("name") or "there"),
                    unsubscribe_link=test_unsubscribe_url,
                    postal_address=postal_address,
                )
                send_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO email_sends (
                        id, sequence_key, step, user_id, email_normalized,
                        subject_sent, body_sent, send_token, status, attempts,
                        scheduled_at, is_test, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processing', 1,
                            CURRENT_TIMESTAMP, TRUE, CURRENT_TIMESTAMP)
                    """,
                    [
                        send_id,
                        SIGNUP_SEQUENCE_KEY,
                        step,
                        str(current_user.get("id") or ""),
                        admin_email,
                        rendered["subject"],
                        rendered["body_text"],
                        send_token,
                    ],
                )
                conn.commit()
                try:
                    result = send_zoho_email(
                        to_email=admin_email,
                        subject=rendered["subject"],
                        text=rendered["body_text"],
                        extra_headers=sequence_email_headers(unsubscribe_link=test_unsubscribe_url),
                    )
                except Exception as exc:
                    conn.execute(
                        """
                        UPDATE email_sends
                        SET status = 'failed', error = ?
                        WHERE id = ?
                        """,
                        [str(exc)[:2000], send_id],
                    )
                    conn.commit()
                    current_app.logger.exception("Signup sequence test email failed step=%s", step)
                    flash("Test email failed; the attempt was logged.", "danger")
                else:
                    conn.execute(
                        """
                        UPDATE email_sends
                        SET status = 'sent', provider_message_id = ?, sent_at = CURRENT_TIMESTAMP, error = NULL
                        WHERE id = ?
                        """,
                        [str(result.get("message_id") or result.get("request_id") or ""), send_id],
                    )
                    conn.commit()
                    flash(f"Test for step {step} sent to {admin_email}.", "success")
                return redirect(url_for("video_shorts_bp.admin_email_sequence", step=step))

            flash("Unknown sequence action.", "danger")
            return redirect(url_for("video_shorts_bp.admin_email_sequence"))

        steps = load_signup_sequence_steps(conn)
        preview_token = "preview-token"
        preview_link = unsubscribe_url(base_url=_base_url(), token=preview_token)
        for step in steps:
            step["preview"] = render_sequence_email(
                subject=step["subject"],
                body_text=step["body_text"],
                recipient_name=SAMPLE_RECIPIENT_NAME,
                unsubscribe_link=preview_link,
            )
        return render_template(
            "shorts_admin_email_sequence.html",
            admin_title="Signup Email Sequence",
            steps=steps,
            sample_name=SAMPLE_RECIPIENT_NAME,
            postal_address_configured=bool(str(os.getenv("COMPANY_POSTAL_ADDRESS") or "").strip()),
        )
    finally:
        conn.close()


def public_email_unsubscribe(token: str):
    clean_token = str(token or "").strip()
    conn = get_db()
    try:
        ensure_email_sequence_schema(conn)
        conn.commit()
        row = conn.execute(
            """
            SELECT id, email_normalized, status, global_unsubscribed
            FROM email_subscriptions
            WHERE unsubscribe_token = ?
            LIMIT 1
            """,
            [clean_token],
        ).fetchone()
        if request.method == "POST":
            if row:
                conn.execute(
                    """
                    UPDATE email_subscriptions
                    SET status = 'unsubscribed', unsubscribed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    [row[0]],
                )
                conn.commit()
            return render_template("email_unsubscribe_done.html", subscription_found=bool(row))
        return render_template(
            "email_unsubscribe_confirm.html",
            token=clean_token,
            subscription_found=bool(row),
            already_unsubscribed=bool(row and (str(row[2]) == "unsubscribed" or bool(row[3]))),
        )
    finally:
        conn.close()
