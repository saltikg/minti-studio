from __future__ import annotations

import calendar
import html
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.video_shorts.services.customer_short_notifications import customer_notify_from_name
from app.video_shorts.services.dashboard_metrics import load_monthly_report_metrics
from app.video_shorts.services.outreach_email_send import send_zoho_email


PT = ZoneInfo("America/Los_Angeles")
REPORT_STATUSES = {"draft", "sent"}


def parse_report_month(value: object, *, default: Optional[date] = None) -> date:
    raw = str(value or "").strip()
    if not raw:
        if default is not None:
            return default.replace(day=1)
        today = datetime.now(PT).date()
        return date(today.year - (1 if today.month == 1 else 0), 12 if today.month == 1 else today.month - 1, 1)
    if not re.fullmatch(r"\d{4}-\d{2}", raw):
        raise ValueError("Month must use YYYY-MM.")
    try:
        parsed = date.fromisoformat(f"{raw}-01")
    except ValueError as exc:
        raise ValueError("Choose a valid report month.") from exc
    return parsed


def month_range(month: date) -> tuple[date, date]:
    start = month.replace(day=1)
    return start, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])


def month_value(month: date) -> str:
    return month.strftime("%Y-%m")


def month_label(month: date) -> str:
    return month.strftime("%B %Y")


def format_pt_date(value: object) -> str:
    if not isinstance(value, datetime):
        return ""
    parsed = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    local = parsed.astimezone(PT)
    return f"{local:%b} {local.day}, {local:%Y}"


def report_tracking_token() -> str:
    return secrets.token_urlsafe(32)


def report_storage_prefix(owner_user_id: str, brand_id: str, month: date) -> str:
    owner = str(owner_user_id or "").strip()
    brand = str(brand_id or "").strip()
    if not owner or not brand:
        raise ValueError("A complete report tenant is required.")
    return f"private/{owner}/{brand}/monthly-reports/{month_value(month)}/"


def report_storage_key(owner_user_id: str, brand_id: str, month: date) -> str:
    return f"{report_storage_prefix(owner_user_id, brand_id, month)}{uuid4()}.pdf"


def validate_report_storage_key(key: object, *, owner_user_id: str, brand_id: str, month: date) -> str:
    clean_key = str(key or "").strip().lstrip("/")
    if not clean_key.startswith(report_storage_prefix(owner_user_id, brand_id, month)) or not clean_key.endswith(".pdf"):
        raise ValueError("The stored report does not belong to this customer and month.")
    return clean_key


def safe_report_filename(brand_name: object, month: date) -> str:
    safe_brand = re.sub(r"[^A-Za-z0-9]+", "-", str(brand_name or "").strip()).strip("-") or "Brand"
    return f"Minti-{safe_brand}-{month_value(month)}.pdf"


def customer_first_name(account_name: object, recipient: object) -> str:
    parts = str(account_name or "").strip().split()
    honorifics = {"dr", "mr", "mrs", "ms", "miss", "prof", "professor"}
    if parts and parts[0].lower().rstrip(".") in honorifics:
        parts = parts[1:]
    if parts:
        return parts[0]
    local_part = str(recipient or "").strip().split("@", 1)[0]
    return local_part or "there"


def build_monthly_report_email(
    *,
    first_name: str,
    month: date,
    headline: str,
    dashboard_url: str,
) -> dict[str, str]:
    label = month_label(month)
    greeting = str(first_name or "").strip() or "there"
    clean_headline = " ".join(str(headline or "").split()) or "Your Shorts kept building your audience this month."
    subject = f"Your Shorts report — {label}"
    email_html = f"""<!doctype html>
<html><body style="margin:0;background:#f5f7f8;color:#1f2a2e;font-family:Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7f8"><tr><td style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px"><tr><td style="padding:28px 24px;font-size:16px;line-height:1.55">
<p style="margin:0 0 18px">Hi {html.escape(greeting)},</p>
<p style="margin:0 0 18px">Here’s how your Shorts did in {html.escape(label)}. Your full report is attached.</p>
<p style="margin:0 0 24px"><strong>{html.escape(clean_headline)}</strong></p>
<p style="margin:0 0 24px"><a href="{html.escape(dashboard_url, quote=True)}" style="display:inline-block;background:#14b8a6;color:#fff;text-decoration:none;font-weight:700;padding:12px 18px;border-radius:8px">See your dashboard</a></p>
<p style="margin:22px 0 0">— The Minti Studio team</p>
</td></tr></table></td></tr></table>
</body></html>"""
    text = (
        f"Hi {greeting},\n\n"
        f"Here’s how your Shorts did in {label}. Your full report is attached.\n\n"
        f"{clean_headline}\n\n"
        f"See your dashboard: {dashboard_url}\n\n"
        "— The Minti Studio team"
    )
    return {"subject": subject, "html": email_html, "text": text}


def send_monthly_report_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    pdf_bytes: bytes,
    filename: str,
) -> dict[str, object]:
    return send_zoho_email(
        to_email=to_email,
        subject=subject,
        html=html_body,
        text=text_body,
        from_name=customer_notify_from_name(),
        attachments=[
            {
                "filename": filename,
                "content_type": "application/pdf",
                "data": pdf_bytes,
            }
        ],
    )


def resolve_converted_customer(conn, *, brand_id: str) -> Optional[dict[str, str]]:
    row = conn.execute(
        """
        SELECT CAST(b.owner_user_id AS VARCHAR), CAST(b.id AS VARCHAR),
               COALESCE(NULLIF(b.name, ''), 'Customer'),
               COALESCE(NULLIF(u.email, ''), NULLIF(u.username, ''), ''),
               COALESCE(NULLIF(u.name, ''), NULLIF(l.creator_name, ''), NULLIF(b.name, ''), '')
        FROM shorts_brands b
        JOIN shorts_users u
          ON CAST(u.id AS VARCHAR) = CAST(b.owner_user_id AS VARCHAR)
         AND lower(coalesce(u.service_mode, '')) = 'autopilot'
        JOIN autopilot_leads l
          ON CAST(l.user_id AS VARCHAR) = CAST(b.owner_user_id AS VARCHAR)
         AND CAST(l.brand_id AS VARCHAR) = CAST(b.id AS VARCHAR)
         AND l.converted_at IS NOT NULL
        WHERE CAST(b.id AS VARCHAR) = CAST(? AS VARCHAR)
        ORDER BY l.converted_at DESC NULLS LAST, l.id DESC
        LIMIT 1
        """,
        [str(brand_id or "").strip()],
    ).fetchone()
    if not row:
        return None
    return {
        "owner_user_id": str(row[0] or "").strip(),
        "brand_id": str(row[1] or "").strip(),
        "brand_name": str(row[2] or "Customer").strip() or "Customer",
        "recipient": str(row[3] or "").strip(),
        "account_name": str(row[4] or "").strip(),
    }


def load_converted_customers(conn) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT CAST(b.owner_user_id AS VARCHAR), CAST(b.id AS VARCHAR),
               COALESCE(NULLIF(b.name, ''), 'Customer'),
               COALESCE(NULLIF(u.email, ''), NULLIF(u.username, ''), ''),
               COALESCE(NULLIF(u.name, ''), NULLIF(l.creator_name, ''), NULLIF(b.name, ''), '')
        FROM shorts_brands b
        JOIN shorts_users u
          ON CAST(u.id AS VARCHAR) = CAST(b.owner_user_id AS VARCHAR)
         AND lower(coalesce(u.service_mode, '')) = 'autopilot'
        JOIN (
            SELECT user_id, brand_id, creator_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY CAST(user_id AS VARCHAR), CAST(brand_id AS VARCHAR)
                       ORDER BY converted_at DESC NULLS LAST, id DESC
                   ) AS row_number
            FROM autopilot_leads
            WHERE converted_at IS NOT NULL
        ) l
          ON CAST(l.user_id AS VARCHAR) = CAST(b.owner_user_id AS VARCHAR)
         AND CAST(l.brand_id AS VARCHAR) = CAST(b.id AS VARCHAR)
         AND l.row_number = 1
        ORDER BY lower(COALESCE(NULLIF(u.email, ''), NULLIF(u.username, ''), '')), lower(b.name)
        """
    ).fetchall()
    return [
        {
            "owner_user_id": str(row[0] or "").strip(),
            "brand_id": str(row[1] or "").strip(),
            "brand_name": str(row[2] or "Customer").strip() or "Customer",
            "recipient": str(row[3] or "").strip(),
            "account_name": str(row[4] or "").strip(),
        }
        for row in rows
    ]


def load_report_record(conn, *, owner_user_id: str, brand_id: str, month: date) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, owner_user_id, brand_id, month, status, s3_key, recipient,
               headline, generated_at, sent_at, provider_message_id, error,
               tracking_token, first_clicked_at, click_count
        FROM autopilot_monthly_reports
        WHERE CAST(owner_user_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND CAST(brand_id AS VARCHAR) = CAST(? AS VARCHAR)
          AND month = ?
        LIMIT 1
        """,
        [owner_user_id, brand_id, month],
    ).fetchone()
    if not row:
        return None
    keys = (
        "id", "owner_user_id", "brand_id", "month", "status", "s3_key", "recipient",
        "headline", "generated_at", "sent_at", "provider_message_id", "error",
        "tracking_token", "first_clicked_at", "click_count",
    )
    return dict(zip(keys, row))


def load_admin_report_rows(conn, *, month: date, today: date) -> list[dict[str, Any]]:
    start_date, end_date = month_range(month)
    customers = load_converted_customers(conn)
    rows: list[dict[str, Any]] = []
    for customer in customers:
        metrics = load_monthly_report_metrics(
            conn,
            owner_user_id=customer["owner_user_id"],
            brand_id=customer["brand_id"],
            brand_name=customer["brand_name"],
            start_date=start_date,
            end_date=end_date,
            today=today,
        )
        record = load_report_record(
            conn,
            owner_user_id=customer["owner_user_id"],
            brand_id=customer["brand_id"],
            month=month,
        )
        connections = metrics.get("connections") or {}
        platforms = [
            label
            for key, label in (("youtube", "YouTube"), ("instagram", "Instagram"), ("facebook", "Facebook"))
            if connections.get(key)
        ]
        status = str((record or {}).get("status") or "").strip().lower()
        if status == "sent":
            status_label = f"Sent · {format_pt_date(record.get('sent_at'))}"
            if record.get("first_clicked_at") or int(record.get("click_count") or 0):
                status_label += " · Opened ✓"
        elif status == "draft":
            status_label = f"Draft · {format_pt_date(record.get('generated_at'))}"
        else:
            status_label = "Not generated"
        rows.append(
            {
                **customer,
                "platforms": platforms,
                "shorts_published": int(metrics.get("shorts_published") or 0),
                "report": record,
                "status": status or "not_generated",
                "status_label": status_label,
            }
        )
    return rows


def new_send_log_id() -> str:
    return str(uuid4())
