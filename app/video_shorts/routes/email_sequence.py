from __future__ import annotations

import os
import json
from datetime import timedelta
from uuid import uuid4

from flask import current_app, flash, g, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.routes.auth import require_admin
from app.video_shorts.services.db import get_db
from app.video_shorts.services.email_sequence import (
    SIGNUP_SEQUENCE_KEY,
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
        if request.method == "POST":
            action = str(request.form.get("action") or "save").strip().lower()
            step = _parsed_step(request.form.get("step"))
            row = _load_step(conn, step) if step is not None else None
            if not row:
                flash("Sequence step not found.", "danger")
                return redirect(url_for("video_shorts_bp.admin_email_sequence"))

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
            sequence_page="steps",
            steps=steps,
            sample_name=SAMPLE_RECIPIENT_NAME,
        )
    finally:
        conn.close()


def _handle_recipient_action(conn, current_user: dict):
    action = str(request.form.get("action") or "").strip().lower()
    user_id = str(request.form.get("user_id") or "").strip()
    recipients_endpoint = "video_shorts_bp.admin_email_sequence_recipients"
    if action == "remove_recipient":
        existing_recipient = conn.execute(
            """
            SELECT email_normalized FROM email_subscriptions
            WHERE list = 'signup_sequence' AND CAST(user_id AS VARCHAR) = ?
            LIMIT 1
            """,
            [user_id],
        ).fetchone()
        if not existing_recipient:
            flash("Sequence recipient not found.", "danger")
            return redirect(url_for(recipients_endpoint))
        conn.execute(
            """
            UPDATE email_subscriptions SET removed_at = CURRENT_TIMESTAMP
            WHERE list = 'signup_sequence' AND CAST(user_id AS VARCHAR) = ?
            """,
            [user_id],
        )
        conn.commit()
        flash(f"{existing_recipient[0]} removed from the sequence.", "success")
        return redirect(url_for(recipients_endpoint))

    if action != "add_recipient":
        flash("Unknown recipient action.", "danger")
        return redirect(url_for(recipients_endpoint))
    user = conn.execute(
        """
        SELECT CAST(id AS VARCHAR), COALESCE(name, ''), lower(trim(COALESCE(email, username, '')))
        FROM shorts_users
        WHERE CAST(id AS VARCHAR) = ? AND COALESCE(is_guest, FALSE) = FALSE
          AND COALESCE(email_verified, FALSE) = TRUE
          AND position('@' in COALESCE(email, username, '')) > 1
        LIMIT 1
        """,
        [user_id],
    ).fetchone()
    if not user:
        flash("Eligible verified user not found.", "danger")
        return redirect(url_for(recipients_endpoint))
    existing = conn.execute(
        """
        SELECT id, status, COALESCE(global_unsubscribed, FALSE), removed_at, enrolled_at
        FROM email_subscriptions
        WHERE list = 'signup_sequence'
          AND (email_normalized = ? OR CAST(user_id AS VARCHAR) = ?)
        LIMIT 1
        """,
        [user[2], user_id],
    ).fetchone()
    restart_needed = bool(existing and (str(existing[1]) != "subscribed" or bool(existing[2]) or existing[3]))
    confirmed = str(request.form.get("confirm_restart") or "") == "1"
    if restart_needed and not confirmed:
        flash("This recipient was removed or unsubscribed. Confirm restart before adding again.", "warning")
        return redirect(url_for(recipients_endpoint, q=user[2]))
    if existing and not restart_needed and existing[4]:
        flash(f"{user[2]} is already in the sequence.", "info")
        return redirect(url_for(recipients_endpoint))
    if existing:
        conn.execute(
            """
            UPDATE email_subscriptions SET
                email_normalized = ?, user_id = ?, status = 'subscribed', source = 'admin',
                subscribed_at = CURRENT_TIMESTAMP, unsubscribed_at = NULL,
                global_unsubscribed = FALSE, enrolled_at = CURRENT_TIMESTAMP,
                enrolled_by = ?, removed_at = NULL
            WHERE id = ?
            """,
            [user[2], user_id, str(current_user.get("id") or ""), existing[0]],
        )
    else:
        conn.execute(
            """
            INSERT INTO email_subscriptions (
                id, email_normalized, user_id, list, status, source,
                unsubscribe_token, subscribed_at, global_unsubscribed,
                enrolled_at, enrolled_by, removed_at
            ) VALUES (?, ?, ?, 'signup_sequence', 'subscribed', 'admin', ?,
                      CURRENT_TIMESTAMP, FALSE, CURRENT_TIMESTAMP, ?, NULL)
            """,
            [str(uuid4()), user[2], user_id, new_email_token(), str(current_user.get("id") or "")],
        )
    conn.commit()
    flash(f"{user[2]} added to the sequence; timing starts now.", "success")
    return redirect(url_for(recipients_endpoint))


def _load_recipient_page_data(conn) -> dict:
    steps = load_signup_sequence_steps(conn)
    query = str(request.args.get("q") or "").strip()
    search_results = []
    if query:
        like_query = f"%{query.lower()}%"
        rows = conn.execute(
            """
            SELECT CAST(u.id AS VARCHAR), COALESCE(u.name, ''),
                   lower(trim(COALESCE(u.email, u.username, ''))),
                   s.status, COALESCE(s.global_unsubscribed, FALSE), s.removed_at, s.enrolled_at
            FROM shorts_users u
            LEFT JOIN email_subscriptions s
              ON s.list = 'signup_sequence'
             AND s.email_normalized = lower(trim(COALESCE(u.email, u.username, '')))
            WHERE COALESCE(u.is_guest, FALSE) = FALSE
              AND COALESCE(u.email_verified, FALSE) = TRUE
              AND position('@' in COALESCE(u.email, u.username, '')) > 1
              AND (lower(COALESCE(u.email, u.username, '')) LIKE ? OR lower(COALESCE(u.name, '')) LIKE ?)
            ORDER BY u.created_at DESC LIMIT 20
            """,
            [like_query, like_query],
        ).fetchall()
        search_results = [
            {
                "id": str(row[0]), "name": str(row[1]), "email": str(row[2]),
                "in_sequence": bool(row[6] and not row[5] and str(row[3]) == "subscribed" and not row[4]),
                "restart": bool(row[3] and (str(row[3]) != "subscribed" or row[4] or row[5])),
            }
            for row in rows
        ]

    bucket_rows = conn.execute(
        """
        SELECT CAST(s.user_id AS VARCHAR), COALESCE(u.name, ''), s.email_normalized,
               s.enrolled_at, s.status, COALESCE(s.global_unsubscribed, FALSE), s.removed_at
        FROM email_subscriptions s
        LEFT JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(s.user_id AS VARCHAR)
        WHERE s.list = 'signup_sequence' AND s.enrolled_at IS NOT NULL
        ORDER BY s.enrolled_at DESC
        """
    ).fetchall()
    sent_rows = conn.execute(
        """
        SELECT CAST(user_id AS VARCHAR), step, status, sent_at
        FROM email_sends WHERE sequence_key = 'signup' AND is_test = FALSE
        ORDER BY step
        """
    ).fetchall()
    sends_by_user: dict[str, list] = {}
    for row in sent_rows:
        sends_by_user.setdefault(str(row[0]), []).append(row)
    bucket = []
    for row in bucket_rows:
        user_sends = sends_by_user.get(str(row[0]), [])
        completed = {int(item[1]) for item in user_sends}
        next_step = next((item for item in steps if item["step"] not in completed), None)
        last_sent = max((int(item[1]) for item in user_sends if str(item[2]) == "sent"), default=None)
        status = "in sequence"
        if row[6]:
            status = "removed"
        elif str(row[4]) == "unsubscribed" or bool(row[5]):
            status = "unsubscribed"
        bucket.append({
            "user_id": str(row[0]), "name": str(row[1]), "email": str(row[2]),
            "enrolled_at": row[3], "status": status, "last_sent_step": last_sent,
            "next_step": next_step["step"] if next_step else None,
            "due_at": row[3] + timedelta(days=next_step["delay_days"]) if next_step and row[3] else None,
        })
    return {"search_query": query, "search_results": search_results, "bucket": bucket}


@video_shorts_bp.route("/admin/email-sequence/recipients", methods=["GET", "POST"])
@require_admin
def admin_email_sequence_recipients():
    current_user = getattr(g, "vs_current_user", None) or {}
    conn = get_db()
    try:
        if request.method == "POST":
            return _handle_recipient_action(conn, current_user)
        return render_template(
            "shorts_admin_email_sequence.html",
            admin_title="Signup Sequence Recipients",
            sequence_page="recipients",
            **_load_recipient_page_data(conn),
        )
    finally:
        conn.close()


def _load_send_log_page_data(conn) -> dict:
    count_rows = conn.execute(
        """
        SELECT step,
               SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END),
               SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
        FROM email_sends WHERE sequence_key = 'signup' AND is_test = FALSE
        GROUP BY step
        """
    ).fetchall()
    counts = {
        int(row[0]): {
            "sent": int(row[1] or 0), "clicked": int(row[2] or 0),
            "skipped": int(row[3] or 0), "failed": int(row[4] or 0),
        }
        for row in count_rows
    }
    log_step = _parsed_step(request.args.get("log_step"))
    allowed_statuses = {"scheduled", "processing", "sent", "failed", "skipped"}
    log_status = str(request.args.get("log_status") or "").lower()
    if log_status not in allowed_statuses:
        log_status = ""
    include_tests = str(request.args.get("include_tests") or "") == "1"
    clauses = ["es.sequence_key = 'signup'"]
    params: list[object] = []
    if not include_tests:
        clauses.append("es.is_test = FALSE")
    if log_step:
        clauses.append("es.step = ?")
        params.append(log_step)
    if log_status:
        clauses.append("es.status = ?")
        params.append(log_status)
    send_log_rows = conn.execute(
        f"""
        SELECT es.email_normalized, COALESCE(u.name, ''), es.step, es.status,
               es.sent_at, es.clicked_at, es.is_test
        FROM email_sends es
        LEFT JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(es.user_id AS VARCHAR)
        WHERE {' AND '.join(clauses)}
        ORDER BY es.created_at DESC LIMIT 100
        """,
        params,
    ).fetchall()
    send_log = [
        {
            "email": str(row[0]), "name": str(row[1]), "step": int(row[2]),
            "status": str(row[3]), "sent_at": row[4], "clicked_at": row[5], "is_test": bool(row[6]),
        }
        for row in send_log_rows
    ]
    return {
        "counts": counts, "send_log": send_log, "log_step": log_step,
        "log_status": log_status, "include_tests": include_tests,
    }


@video_shorts_bp.get("/admin/email-sequence/log")
@require_admin
def admin_email_sequence_log():
    conn = get_db()
    try:
        return render_template(
            "shorts_admin_email_sequence.html",
            admin_title="Signup Sequence Send Log",
            sequence_page="log",
            **_load_send_log_page_data(conn),
        )
    finally:
        conn.close()


def public_email_unsubscribe(token: str):
    clean_token = str(token or "").strip()
    conn = get_db()
    try:
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


def public_nurture_click(send_token: str, link_index: str):
    homepage = _base_url() + "/"
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id, user_id, step, link_urls
            FROM email_sends
            WHERE send_token = ? AND sequence_key = 'signup' AND is_test = FALSE
            LIMIT 1
            """,
            [str(send_token or "")],
        ).fetchone()
        if not row:
            return redirect(homepage, code=302)
        try:
            links = json.loads(str(row[3] or "[]"))
            destination = str(links[int(link_index) - 1]) if int(link_index) >= 1 else ""
        except (TypeError, ValueError, IndexError, json.JSONDecodeError):
            destination = ""
        if not destination.startswith(("http://", "https://")):
            return redirect(homepage, code=302)

        user_agent = str(request.headers.get("User-Agent") or "")
        prefetch = any(
            str(request.headers.get(name) or "").lower() in {"prefetch", "prerender"}
            for name in ("Purpose", "Sec-Purpose", "X-Purpose")
        )
        from app.video_shorts.routes.generation import _is_share_watch_bot_user_agent
        is_bot = _is_share_watch_bot_user_agent(user_agent)
        if not prefetch and not is_bot:
            updated = conn.execute(
                "UPDATE email_sends SET clicked_at = CURRENT_TIMESTAMP WHERE id = ? AND clicked_at IS NULL",
                [row[0]],
            )
            conn.commit()
            if getattr(updated, "rowcount", 0) > 0:
                from app.video_shorts.services.user_events import track_event
                track_event(
                    str(row[1]), "nurture_click",
                    metadata={"send_id": str(row[0]), "step": int(row[2]), "url": destination},
                )
        return redirect(destination, code=302)
    finally:
        conn.close()
