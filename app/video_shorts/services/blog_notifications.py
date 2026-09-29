from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

from app.video_shorts.services.outreach_email_send import (
    OUTREACH_ZOHO_FROM_EMAIL,
    OUTREACH_ZOHO_FROM_NAME,
    _zoho_smtp_settings,
)


BLOG_NOTIFY_EMAIL = os.getenv("BLOG_NOTIFY_EMAIL", "info@mintistudio.com")
BLOG_PUBLIC_BASE_URL = (os.getenv("BLOG_PUBLIC_BASE_URL") or "https://mintistudio.com").rstrip("/")


def _line(label: str, value: Any) -> str:
    return f"{label}: {value if value not in (None, '') else '-'}"


def _absolute_url(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("http"):
        return text
    if text.startswith("/"):
        return f"{BLOG_PUBLIC_BASE_URL}{text}"
    return text


def send_blog_run_notification(
    *,
    subject: str,
    status_label: str,
    title: str,
    public_url: str | None,
    admin_edit_url: str | None,
    run_detail_url: str | None,
    reviewer_score: int | None,
    total_cost: str | None,
    images: list[str],
    reason: str | None = None,
    to_email: str | None = None,
) -> dict[str, object]:
    settings = _zoho_smtp_settings()
    recipient = str(to_email or BLOG_NOTIFY_EMAIL or "").strip()
    if not recipient or "@" not in recipient:
        raise RuntimeError("Blog notification recipient email is missing")

    body_lines = [
        _line("Status", status_label),
        _line("Title", title),
    ]
    if public_url:
        body_lines.append(_line("Public URL", public_url))
    body_lines.extend(
        [
            _line("Admin edit URL", admin_edit_url),
            _line("Run detail URL", run_detail_url),
            _line("Reviewer score", reviewer_score),
            _line("Total cost", total_cost),
            "Images/screenshots used:",
        ]
    )
    body_lines.extend([f"- {_absolute_url(item)}" for item in images] or ["- -"])
    if reason:
        body_lines.extend(["", _line("Reason", reason)])
    text = "\n".join(body_lines).strip() + "\n"

    message_id = make_msgid(domain="mintistudio.com")
    message = EmailMessage()
    message["From"] = formataddr((OUTREACH_ZOHO_FROM_NAME, OUTREACH_ZOHO_FROM_EMAIL))
    message["To"] = recipient
    message["Reply-To"] = OUTREACH_ZOHO_FROM_EMAIL
    message["Subject"] = str(subject or "").strip()
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = message_id
    message.set_content(text)

    context = ssl.create_default_context()
    if int(settings["port"]) == 465:
        with smtplib.SMTP_SSL(settings["host"], settings["port"], context=context, timeout=20) as smtp:
            smtp.login(settings["user"], settings["password"])
            refused = smtp.send_message(message, from_addr=OUTREACH_ZOHO_FROM_EMAIL, to_addrs=[recipient])
    else:
        with smtplib.SMTP(settings["host"], settings["port"], timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(settings["user"], settings["password"])
            refused = smtp.send_message(message, from_addr=OUTREACH_ZOHO_FROM_EMAIL, to_addrs=[recipient])
    if refused:
        raise RuntimeError(f"Zoho SMTP refused recipient: {refused}")
    return {"status": "accepted", "message_id": message_id, "transport": "zoho", "to": recipient}
