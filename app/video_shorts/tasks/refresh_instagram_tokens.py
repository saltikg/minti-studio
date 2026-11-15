#!/usr/bin/env python3
"""Refresh every stored Instagram long-lived token when it enters its refresh window."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import html
import logging
import os
from typing import Dict, Optional
from uuid import uuid4

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.email_verification import send_resend_email
from app.video_shorts.services.instagram_metrics_state import claim_instagram_metrics_alert
from src.trends.instagram_tokens import (
    describe_instagram_refresh_error,
    get_instagram_data,
    list_instagram_credentials,
    refresh_instagram_token_if_needed,
)

logger = logging.getLogger(__name__)
NEAR_EXPIRY_DAYS = 7
ALERT_KEY_PREFIX = "instagram_token_refresh"


def _parse_datetime(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _token_identity(token_owner: str) -> tuple[str, str]:
    owner_user_id, separator, brand_id = str(token_owner or "").partition("::")
    if not owner_user_id or not separator or not brand_id or "::" in brand_id:
        raise ValueError("Instagram token owner is not brand-scoped.")
    return owner_user_id, brand_id


def _brand_names() -> Dict[str, str]:
    conn = get_db_readonly()
    try:
        rows = conn.execute("SELECT CAST(id AS VARCHAR), name FROM shorts_brands").fetchall()
        return {str(row[0]): str(row[1] or row[0]) for row in rows}
    finally:
        conn.close()


def _alert_reason(
    *,
    action: str,
    expires_at: object,
    now: datetime,
) -> Optional[str]:
    if action == "failed":
        return "refresh failed"
    expiry = _parse_datetime(expires_at)
    if expiry is not None and expiry <= now + timedelta(days=NEAR_EXPIRY_DAYS) and action != "refreshed":
        return "expires within 7 days and was not refreshed"
    return None


def _send_admin_alert(result: Dict[str, object], reason: str) -> None:
    token_owner = str(result["token_owner"])
    summary = {**result, "reason": reason}
    if not claim_instagram_metrics_alert(
        summary,
        cooldown_hours=24,
        alert_key=f"{ALERT_KEY_PREFIX}:{token_owner}",
    ):
        logger.info(
            "Instagram token refresh alert suppressed brand=%s account=%s reason=%s",
            result["brand"],
            result["account"],
            reason,
        )
        return
    text = "\n".join(
        [
            "Instagram publishing token needs attention.",
            f"Brand: {result['brand']}",
            f"Account: {result['account']}",
            f"Reason: {reason}",
            f"Expires at: {result.get('expires_at_after') or result.get('expires_at_before') or 'unknown'}",
            f"Error: {result.get('error') or 'none'}",
            "Reconnect the Instagram account if the token cannot be refreshed while still valid.",
        ]
    )
    send_resend_email(
        to_email=(os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip(),
        subject=f"Minti Instagram token alert: {result['brand']} / {result['account']}",
        html="<p>" + html.escape(text).replace("\n", "<br>") + "</p>",
        text=text,
        error_message="Instagram token refresh alert could not be sent.",
    )


def run_instagram_token_refresh_job(
    *,
    send_alerts: bool = True,
    now: Optional[datetime] = None,
) -> list[Dict[str, object]]:
    now = now or datetime.now(timezone.utc)
    brands = _brand_names()
    results: list[Dict[str, object]] = []
    for listed in list_instagram_credentials():
        token_owner = str(listed.get("user_id") or "")
        owner_user_id, brand_id = _token_identity(token_owner)
        current = get_instagram_data(owner_user_id, brand_id=brand_id)
        if not current:
            continue
        brand = brands.get(brand_id, brand_id)
        account = str(
            current.get("instagram_username")
            or current.get("instagram_business_account_id")
            or "<unknown>"
        )
        before_expiry = current.get("expires_at")
        before_refreshed_at = current.get("token_refreshed_at")
        result: Dict[str, object] = {
            "token_owner": token_owner,
            "brand": brand,
            "account": account,
            "expires_at_before": before_expiry,
            "expires_at_after": before_expiry,
            "action": "skipped: outside refresh window",
            "error": None,
        }
        try:
            refreshed = refresh_instagram_token_if_needed(
                user_id=owner_user_id,
                brand_id=brand_id,
                current=current,
            ) or current
            result["expires_at_after"] = refreshed.get("expires_at")
            if (
                refreshed.get("token_refreshed_at") != before_refreshed_at
                or refreshed.get("expires_at") != before_expiry
            ):
                result["action"] = "refreshed"
        except Exception as exc:
            safe_error = describe_instagram_refresh_error(
                exc,
                token=current.get("page_access_token"),
            )
            result["action"] = "failed"
            result["error"] = safe_error
            logger.error(
                "Instagram token refresh failed brand=%s account=%s error=%s",
                brand,
                account,
                safe_error,
            )
        reason = _alert_reason(
            action=str(result["action"]),
            expires_at=result.get("expires_at_after"),
            now=now,
        )
        result["alert"] = "would fire" if reason else "none"
        if reason and send_alerts:
            try:
                _send_admin_alert(result, reason)
                result["alert"] = "sent or cooldown-suppressed"
            except Exception as exc:
                logger.error(
                    "Instagram token refresh admin alert failed brand=%s account=%s error=%s",
                    brand,
                    account,
                    exc.__class__.__name__,
                )
        results.append(result)
        print(
            f"brand={brand} account={account} expires_at_before={before_expiry or '-'} "
            f"expires_at_after={result['expires_at_after'] or '-'} action={result['action']}"
        )
    return results


def verify_near_expiry_alert_dry_run() -> bool:
    """Exercise the near-expiry alert predicate with a row that is always rolled back."""
    conn = get_db()
    fake_owner = f"dry-run-{uuid4()}::dry-run-brand"
    expires_at = (datetime.now(timezone.utc) + timedelta(days=6)).replace(microsecond=0).isoformat()
    try:
        conn.execute(
            """
            INSERT INTO instagram_oauth_tokens (
                user_id, page_access_token, instagram_business_account_id,
                instagram_username, token_created_at, token_refreshed_at,
                expires_at, scopes, updated_at
            )
            VALUES (?, 'dry-run-token', 'dry-run-account', 'dry_run_account', ?, ?, ?, '', ?)
            """,
            [fake_owner, datetime.now(timezone.utc), datetime.now(timezone.utc), expires_at, datetime.now(timezone.utc)],
        )
        row = conn.execute(
            "SELECT expires_at FROM instagram_oauth_tokens WHERE user_id = ?",
            [fake_owner],
        ).fetchone()
        would_fire = bool(
            row
            and _alert_reason(
                action="skipped: outside refresh window",
                expires_at=row[0],
                now=datetime.now(timezone.utc),
            )
        )
        print(
            f"DRY_RUN brand=dry-run-brand account=dry_run_account expires_at={row[0] if row else '-'} "
            f"alert_would_fire={str(would_fire).lower()} real_alert_sent=false"
        )
        return would_fire
    finally:
        conn.rollback()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-alerts", action="store_true", help="Do not claim or send admin alerts.")
    parser.add_argument(
        "--verify-alert-dry-run",
        action="store_true",
        help="Insert a fake near-expiry row in a rolled-back transaction and send no alert.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.verify_alert_dry_run:
        return 0 if verify_near_expiry_alert_dry_run() else 1
    run_instagram_token_refresh_job(send_alerts=not args.no_alerts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
