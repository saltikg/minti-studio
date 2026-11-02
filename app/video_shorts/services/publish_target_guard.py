"""Fail-closed provider-target checks shared by social publishing workers."""

from __future__ import annotations

import html
import logging
import os
from typing import Optional

from app.video_shorts.services.db import get_db_readonly


logger = logging.getLogger(__name__)


class PublishTargetMismatch(RuntimeError):
    """A queued publish no longer resolves to its snapshotted provider target."""

    def __init__(self, *, platform: str, schedule_id: str, brand_id: Optional[str], expected: object, actual: object):
        self.platform = str(platform or "unknown")
        self.schedule_id = str(schedule_id or "")
        self.brand_id = str(brand_id or "")
        self.expected = str(expected or "")
        self.actual = str(actual or "")
        super().__init__(
            f"{self.platform} target mismatch: expected={self.expected or '(missing)'} "
            f"actual={self.actual or '(missing)'}"
        )


def require_target_match(
    *,
    platform: str,
    schedule_id: str,
    brand_id: Optional[str],
    expected: object,
    actual: object,
) -> None:
    expected_value = str(expected or "").strip()
    actual_value = str(actual or "").strip()
    if not expected_value or not actual_value or expected_value != actual_value:
        raise PublishTargetMismatch(
            platform=platform,
            schedule_id=schedule_id,
            brand_id=brand_id,
            expected=expected_value,
            actual=actual_value,
        )


def require_token_owner(*, token_owner: object, owner_user_id: object, brand_id: object) -> None:
    expected = f"{str(owner_user_id or '').strip()}::{str(brand_id or '').strip()}"
    actual = str(token_owner or "").strip()
    if not expected.partition("::")[0] or not expected.partition("::")[2] or actual != expected:
        raise PublishTargetMismatch(
            platform="token_owner",
            schedule_id="",
            brand_id=str(brand_id or ""),
            expected=expected,
            actual=actual,
        )


def send_target_mismatch_alert(error: PublishTargetMismatch) -> None:
    """Best-effort alert; publishing stays blocked even when email delivery fails."""
    from app.video_shorts.services.email_verification import send_resend_email

    recipients: list[str] = []
    try:
        conn = get_db_readonly()
        try:
            rows = conn.execute(
                "SELECT email FROM shorts_users WHERE lower(COALESCE(role, '')) = 'admin'"
            ).fetchall()
        finally:
            conn.close()
        recipients.extend(str(row[0] or "").strip() for row in rows if row and "@" in str(row[0] or ""))
    except Exception:
        logger.exception("Could not load admin recipients for target mismatch alert")
    fallback = (os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip()
    if fallback and "@" in fallback:
        recipients.append(fallback)
    recipients = list(dict.fromkeys(recipients))
    message = (
        f"Publishing was blocked before provider upload. Platform={error.platform}; "
        f"schedule_id={error.schedule_id or '(unknown)'}; brand_id={error.brand_id or '(missing)'}; "
        f"expected_target={error.expected or '(missing)'}; actual_target={error.actual or '(missing)'}"
    )
    for recipient in recipients:
        try:
            send_resend_email(
                to_email=recipient,
                subject=f"Minti publish blocked: {error.platform} target mismatch",
                html=f"<p>{html.escape(message)}</p>",
                text=message,
                error_message="Target mismatch alert could not be sent.",
            )
        except Exception:
            logger.exception(
                "Target mismatch alert failed platform=%s schedule_id=%s recipient=%s",
                error.platform,
                error.schedule_id,
                recipient,
            )
