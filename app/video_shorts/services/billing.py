from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import stripe
from app.video_shorts.services.db import ensure_auth_user_schema, get_db, get_db_readonly


STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_PUBLISHABLE_KEY = (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


logger = logging.getLogger(__name__)

# TEST price IDs — replace with live price IDs before going live.
_PRICE_ID_DEFAULTS: Dict[Tuple[str, str], str] = {
    ("plan_2gb", "month"): "price_1TrQtrLKj5DJxc02rKyfv8sA",
    ("plan_2gb", "year"): "price_1TrQv6LKj5DJxc02w4x6MnzC",
    ("plan_10gb", "month"): "price_1TrQwHLKj5DJxc029j9LsgPK",
    ("plan_10gb", "year"): "price_1TrQwoLKj5DJxc029Ya4U2Hs",
    ("plan_100gb", "month"): "price_1TrQxMLKj5DJxc02OOdhKFGx",
    ("plan_100gb", "year"): "price_1TrQxiLKj5DJxc02f98iDC4Y",
}

_PRICE_ID_ENV_OVERRIDES: Dict[Tuple[str, str], str] = {
    ("plan_2gb", "month"): "STRIPE_PRICE_PLAN_2GB_MONTH",
    ("plan_2gb", "year"): "STRIPE_PRICE_PLAN_2GB_YEAR",
    ("plan_10gb", "month"): "STRIPE_PRICE_PLAN_10GB_MONTH",
    ("plan_10gb", "year"): "STRIPE_PRICE_PLAN_10GB_YEAR",
    ("plan_100gb", "month"): "STRIPE_PRICE_PLAN_100GB_MONTH",
    ("plan_100gb", "year"): "STRIPE_PRICE_PLAN_100GB_YEAR",
}

AUTOPILOT_TIER_MONTHLY_PRICES: Dict[int, int] = {
    15: 20,
    30: 40,
    45: 60,
    60: 80,
}

_AUTOPILOT_PRICE_ID_ENV_OVERRIDES: Dict[int, str] = {
    15: "STRIPE_PRICE_AUTOPILOT_15_MONTH",
    30: "STRIPE_PRICE_AUTOPILOT_30_MONTH",
    45: "STRIPE_PRICE_AUTOPILOT_45_MONTH",
    60: "STRIPE_PRICE_AUTOPILOT_60_MONTH",
}


def _build_price_map() -> Dict[Tuple[str, str], str]:
    price_map: Dict[Tuple[str, str], str] = {}
    for key, default_value in _PRICE_ID_DEFAULTS.items():
        env_name = _PRICE_ID_ENV_OVERRIDES[key]
        value = (os.getenv(env_name) or default_value).strip()
        if value:
            price_map[key] = value
    return price_map


PLAN_INTERVAL_TO_PRICE_ID: Dict[Tuple[str, str], str] = _build_price_map()
PRICE_ID_TO_PLAN_INTERVAL: Dict[str, Tuple[str, str]] = {
    price_id: plan_key for plan_key, price_id in PLAN_INTERVAL_TO_PRICE_ID.items()
}


def _build_autopilot_price_map() -> Dict[int, str]:
    return {
        tier: value
        for tier, env_name in _AUTOPILOT_PRICE_ID_ENV_OVERRIDES.items()
        if (value := (os.getenv(env_name) or "").strip())
    }


AUTOPILOT_TIER_TO_PRICE_ID: Dict[int, str] = _build_autopilot_price_map()
PRICE_ID_TO_AUTOPILOT_TIER: Dict[str, int] = {
    price_id: tier for tier, price_id in AUTOPILOT_TIER_TO_PRICE_ID.items()
}


def stripe_is_configured() -> bool:
    return bool(STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY)


def get_price_id_for_plan(plan_id: str, interval: str) -> Optional[str]:
    normalized_plan_id = (plan_id or "").strip()
    normalized_interval = (interval or "").strip().lower()
    return PLAN_INTERVAL_TO_PRICE_ID.get((normalized_plan_id, normalized_interval))


def get_plan_for_price_id(price_id: str) -> Optional[Tuple[str, str]]:
    normalized_price_id = (price_id or "").strip()
    if not normalized_price_id:
        return None
    return PRICE_ID_TO_PLAN_INTERVAL.get(normalized_price_id)


def get_price_id_for_autopilot_tier(tier: int) -> Optional[str]:
    try:
        normalized_tier = int(tier)
    except (TypeError, ValueError):
        return None
    return AUTOPILOT_TIER_TO_PRICE_ID.get(normalized_tier)


def get_autopilot_tier_for_price_id(price_id: str) -> Optional[int]:
    return PRICE_ID_TO_AUTOPILOT_TIER.get((price_id or "").strip())


def autopilot_trial_end(service_mode_chosen_at: Any) -> Optional[datetime]:
    if not isinstance(service_mode_chosen_at, datetime):
        return None
    chosen_at = service_mode_chosen_at
    if chosen_at.tzinfo is None:
        chosen_at = chosen_at.replace(tzinfo=timezone.utc)
    else:
        chosen_at = chosen_at.astimezone(timezone.utc)
    return chosen_at + timedelta(days=30)


def interval_is_supported(interval: str) -> bool:
    return (interval or "").strip().lower() in {"month", "year"}


def plan_is_paid(plan_id: str) -> bool:
    return (plan_id or "").strip() in {"plan_2gb", "plan_10gb", "plan_100gb"}


def build_checkout_return_url(base_url: str) -> str:
    root = (base_url or "").rstrip("/")
    return f"{root}/video_shorts/billing/complete?session_id={{CHECKOUT_SESSION_ID}}"


def create_customer(*, email: str, shorts_user_id: str) -> stripe.Customer:
    return stripe.Customer.create(
        email=(email or "").strip() or None,
        metadata={"shorts_user_id": str(shorts_user_id)},
    )


def create_embedded_checkout_session(
    *,
    customer_id: str,
    price_id: str,
    shorts_user_id: str,
    plan_id: str,
    interval: str,
    return_url: str,
    trial_days: int | None = None,
    trial_end: datetime | None = None,
    billing_mode: str = "self",
    service_tier: int | None = None,
) -> stripe.checkout.Session:
    metadata = {
        "shorts_user_id": str(shorts_user_id),
        "plan_id": str(plan_id),
        "interval": str(interval),
        "billing_mode": str(billing_mode or "self"),
    }
    if service_tier is not None:
        metadata["service_tier"] = str(int(service_tier))
    subscription_data: Dict[str, Any] = {"metadata": metadata}
    if trial_end:
        normalized_trial_end = trial_end
        if normalized_trial_end.tzinfo is None:
            normalized_trial_end = normalized_trial_end.replace(tzinfo=timezone.utc)
        subscription_data["trial_end"] = int(normalized_trial_end.timestamp())
        metadata["trial_end"] = str(int(normalized_trial_end.timestamp()))
    elif trial_days:
        subscription_data["trial_period_days"] = int(trial_days)
        metadata["trial_days"] = str(int(trial_days))
    checkout_options: Dict[str, Any] = {
        "mode": "subscription",
        "ui_mode": "embedded_page",
        "redirect_on_completion": "if_required",
        "allow_promotion_codes": True,
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "return_url": return_url,
        "metadata": metadata,
        "subscription_data": subscription_data,
    }
    if str(billing_mode or "").strip().lower() == "autopilot":
        checkout_options["payment_method_collection"] = "always"
    return stripe.checkout.Session.create(**checkout_options)


def retrieve_checkout_session(session_id: str) -> stripe.checkout.Session:
    return stripe.checkout.Session.retrieve((session_id or "").strip())


def retrieve_subscription(subscription_id: str) -> stripe.Subscription:
    return stripe.Subscription.retrieve((subscription_id or "").strip())


def update_subscription_price(*, subscription_id: str, price_id: str) -> stripe.Subscription:
    subscription = retrieve_subscription(subscription_id)
    items = getattr(subscription, "items", None)
    data = getattr(items, "data", None) if items is not None else None
    if data is None and isinstance(items, dict):
        data = items.get("data")
    first_item = data[0] if data else None
    item_id = getattr(first_item, "id", None)
    if item_id is None and isinstance(first_item, dict):
        item_id = first_item.get("id")
    if not item_id:
        raise ValueError("subscription_item_missing")
    return stripe.Subscription.modify(
        (subscription_id or "").strip(),
        items=[{"id": item_id, "price": price_id}],
        proration_behavior="none",
    )


def create_billing_portal_session(*, customer_id: str, return_url: str) -> stripe.billing_portal.Session:
    return stripe.billing_portal.Session.create(
        customer=(customer_id or "").strip(),
        return_url=return_url,
    )


def schedule_subscription_cancel_at_period_end(subscription_id: str) -> stripe.Subscription:
    return stripe.Subscription.modify(
        (subscription_id or "").strip(),
        cancel_at_period_end=True,
    )


def construct_webhook_event(*, payload: bytes, signature: str) -> stripe.Event:
    return stripe.Webhook.construct_event(
        payload=payload,
        sig_header=signature,
        secret=STRIPE_WEBHOOK_SECRET,
    )


def _subscription_period_end(subscription: Any) -> Optional[datetime]:
    unix_ts = getattr(subscription, "current_period_end", None)
    if unix_ts is None and isinstance(subscription, dict):
        unix_ts = subscription.get("current_period_end")
    if not unix_ts:
        items = getattr(subscription, "items", None)
        if items is None and isinstance(subscription, dict):
            items = subscription.get("items")
        data = getattr(items, "data", None) if items is not None else None
        if data is None and isinstance(items, dict):
            data = items.get("data")
        first_item = data[0] if data else None
        if first_item is not None:
            unix_ts = getattr(first_item, "current_period_end", None)
            if unix_ts is None and isinstance(first_item, dict):
                unix_ts = first_item.get("current_period_end")
    if not unix_ts:
        return None
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
    except Exception:
        return None


def _subscription_price_id(subscription: Any) -> str:
    items = subscription.get("items") if isinstance(subscription, dict) else getattr(subscription, "items", None)
    if not items:
        return ""
    data = getattr(items, "data", None)
    if data is None and isinstance(items, dict):
        data = items.get("data")
    if not data:
        return ""
    first_item = data[0]
    price = getattr(first_item, "price", None)
    if price is None and isinstance(first_item, dict):
        price = first_item.get("price")
    if not price:
        return ""
    price_id = getattr(price, "id", None)
    if price_id is None and isinstance(price, dict):
        price_id = price.get("id")
    return (price_id or "").strip()


def _subscription_cancel_at_period_end(subscription: Any) -> Optional[bool]:
    value = getattr(subscription, "cancel_at_period_end", None)
    if value is None and isinstance(subscription, dict):
        value = subscription.get("cancel_at_period_end")
    if value is None:
        return None
    return bool(value)


def resolve_plan_interval_from_subscription(subscription: Any) -> Optional[Tuple[str, str]]:
    price_id = _subscription_price_id(subscription)
    if not price_id:
        return None
    return get_plan_for_price_id(price_id)


def normalize_subscription_payload(subscription: Any) -> Dict[str, Any]:
    resolved = resolve_plan_interval_from_subscription(subscription)
    plan_id = resolved[0] if resolved else None
    interval = resolved[1] if resolved else None
    price_id = _subscription_price_id(subscription)
    autopilot_tier = get_autopilot_tier_for_price_id(price_id)
    if autopilot_tier is not None:
        interval = "month"
    status = getattr(subscription, "status", None)
    if status is None and isinstance(subscription, dict):
        status = subscription.get("status")
    subscription_id = getattr(subscription, "id", None)
    if subscription_id is None and isinstance(subscription, dict):
        subscription_id = subscription.get("id")
    customer_id = getattr(subscription, "customer", None)
    if customer_id is None and isinstance(subscription, dict):
        customer_id = subscription.get("customer")
    return {
        "plan_id": plan_id,
        "billing_interval": interval,
        "billing_mode": "autopilot" if autopilot_tier is not None else "self",
        "service_tier": autopilot_tier,
        "subscription_status": (status or "").strip() or None,
        "stripe_subscription_id": (subscription_id or "").strip() or None,
        "stripe_customer_id": (customer_id or "").strip() or None,
        "subscription_current_period_end": _subscription_period_end(subscription),
        "subscription_cancel_at_period_end": _subscription_cancel_at_period_end(subscription),
        "price_id": price_id,
    }


def _extract_free_period_state(subscription: Any) -> Dict[str, Any]:
    def _lookup(obj: Any, key: str):
        value = getattr(obj, key, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(key)
        return value

    coupon = None
    discounts = _lookup(subscription, "discounts")
    discount_entries = None
    if discounts is not None:
        discount_entries = _lookup(discounts, "data")
        if discount_entries is None and isinstance(discounts, (list, tuple)):
            discount_entries = discounts
    if discount_entries:
        first_discount = discount_entries[0]
        coupon = _lookup(first_discount, "coupon")
    if coupon is None:
        legacy_discount = _lookup(subscription, "discount")
        if legacy_discount is not None:
            coupon = _lookup(legacy_discount, "coupon")

    percent_off = _lookup(coupon, "percent_off") if coupon is not None else None
    duration = (_lookup(coupon, "duration") or "").strip().lower() if coupon is not None else ""
    duration_in_months = _lookup(coupon, "duration_in_months") if coupon is not None else None

    try:
        is_free = float(percent_off) == 100.0
    except Exception:
        is_free = False

    free_months = 0
    if is_free:
        if duration == "once":
            free_months = 1
        elif duration == "repeating":
            try:
                free_months = int(duration_in_months or 0)
            except Exception:
                free_months = 0

    return {
        "free_period_active": bool(is_free and free_months >= 1),
        "free_months": free_months if free_months >= 1 else 0,
    }


def _update_user_subscription_snapshot(
    *,
    user_id: str,
    plan_id: Optional[str],
    service_tier: Optional[int],
    stripe_customer_id: Optional[str],
    stripe_subscription_id: Optional[str],
    subscription_status: Optional[str],
    subscription_current_period_end,
    subscription_cancel_at_period_end: Optional[bool],
    billing_interval: Optional[str],
) -> None:
    conn = get_db()
    try:
        ensure_auth_user_schema(conn)
        conn.execute(
            """
            UPDATE shorts_users
            SET plan_id = COALESCE(?, plan_id),
                service_tier = COALESCE(?, service_tier),
                stripe_customer_id = COALESCE(?, stripe_customer_id),
                stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                subscription_status = COALESCE(?, subscription_status),
                subscription_current_period_end = COALESCE(?, subscription_current_period_end),
                subscription_cancel_at_period_end = COALESCE(?, subscription_cancel_at_period_end),
                billing_interval = COALESCE(?, billing_interval),
                autopilot_billing_paused_at = CASE
                    WHEN lower(COALESCE(?, '')) IN ('active', 'trialing') THEN NULL
                    ELSE autopilot_billing_paused_at
                END,
                updated_at = now()
            WHERE id = ?
            """,
            [
                plan_id,
                service_tier,
                stripe_customer_id,
                stripe_subscription_id,
                subscription_status,
                subscription_current_period_end,
                subscription_cancel_at_period_end,
                billing_interval,
                subscription_status,
                user_id,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def load_billing_user_state(user_id: str, *, refresh_live: bool = False) -> Optional[Dict[str, Any]]:
    normalized_user_id = (user_id or "").strip()
    if not normalized_user_id:
        return None
    def _read_state() -> Optional[Dict[str, Any]]:
        conn = get_db_readonly()
        try:
            row = conn.execute(
                """
                SELECT
                    CAST(id AS VARCHAR),
                    email,
                    stripe_customer_id,
                    stripe_subscription_id,
                    subscription_status,
                    subscription_current_period_end,
                    subscription_cancel_at_period_end,
                    billing_interval,
                    plan_id,
                    service_mode,
                    service_tier,
                    service_mode_chosen_at,
                    autopilot_billing_reminder_sent_at,
                    autopilot_billing_paused_at
                FROM shorts_users
                WHERE id = ?
                """,
                [normalized_user_id],
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {
            "id": row[0],
            "email": row[1],
            "stripe_customer_id": row[2],
            "stripe_subscription_id": row[3],
            "subscription_status": row[4],
            "subscription_current_period_end": row[5],
            "subscription_cancel_at_period_end": bool(row[6]) if row[6] is not None else None,
            "billing_interval": row[7],
            "plan_id": row[8],
            "service_mode": str(row[9] or "").strip().lower(),
            "service_tier": int(row[10]) if row[10] is not None else None,
            "service_mode_chosen_at": row[11],
            "autopilot_billing_reminder_sent_at": row[12],
            "autopilot_billing_paused_at": row[13],
            "free_period_active": False,
            "free_months": 0,
        }

    state = _read_state()
    if not state:
        return None
    subscription_id = (state.get("stripe_subscription_id") or "").strip()
    if not refresh_live or not subscription_id or not STRIPE_SECRET_KEY:
        if state.get("subscription_cancel_at_period_end") is None:
            state["subscription_cancel_at_period_end"] = False
        return state
    try:
        live_subscription = retrieve_subscription(subscription_id)
        normalized = normalize_subscription_payload(live_subscription)
        free_period_state = _extract_free_period_state(live_subscription)
        current_cancel = state.get("subscription_cancel_at_period_end")
        if (
            current_cancel is None
            or current_cancel != normalized.get("subscription_cancel_at_period_end")
            or state.get("subscription_current_period_end") != normalized.get("subscription_current_period_end")
            or (state.get("subscription_status") or None) != normalized.get("subscription_status")
            or (state.get("billing_interval") or None) != normalized.get("billing_interval")
            or (
                normalized.get("plan_id") is not None
                and (state.get("plan_id") or None) != normalized.get("plan_id")
            )
            or (
                normalized.get("service_tier") is not None
                and state.get("service_tier") != normalized.get("service_tier")
            )
        ):
            _update_user_subscription_snapshot(
                user_id=normalized_user_id,
                plan_id=normalized.get("plan_id"),
                service_tier=normalized.get("service_tier"),
                stripe_customer_id=normalized.get("stripe_customer_id"),
                stripe_subscription_id=normalized.get("stripe_subscription_id"),
                subscription_status=normalized.get("subscription_status"),
                subscription_current_period_end=normalized.get("subscription_current_period_end"),
                subscription_cancel_at_period_end=normalized.get("subscription_cancel_at_period_end"),
                billing_interval=normalized.get("billing_interval"),
            )
            state = _read_state() or state
        state["free_period_active"] = bool(free_period_state.get("free_period_active"))
        state["free_months"] = int(free_period_state.get("free_months") or 0)
    except Exception:
        logger.exception("Failed to refresh live Stripe subscription state user_id=%s subscription_id=%s", normalized_user_id, subscription_id)
    if state.get("subscription_cancel_at_period_end") is None:
        state["subscription_cancel_at_period_end"] = False
    if "free_period_active" not in state:
        state["free_period_active"] = False
    if "free_months" not in state:
        state["free_months"] = 0
    return state


def user_has_managed_subscription(user_state: Optional[Dict[str, Any]]) -> bool:
    if not user_state:
        return False
    customer_id = (user_state.get("stripe_customer_id") or "").strip()
    subscription_id = (user_state.get("stripe_subscription_id") or "").strip()
    status = (user_state.get("subscription_status") or "").strip().lower()
    if not customer_id or not subscription_id:
        return False
    return status not in {"", "canceled", "incomplete_expired"}


def process_autopilot_billing_deadlines(*, now: Optional[datetime] = None) -> bool:
    """Send one due reminder or pause one expired no-card Autopilot account."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)
    reminder_cutoff = current - timedelta(days=25)
    trial_cutoff = current - timedelta(days=30)

    conn = get_db()
    try:
        ensure_auth_user_schema(conn)
        expired = conn.execute(
            """
            SELECT CAST(id AS VARCHAR)
            FROM shorts_users
            WHERE lower(COALESCE(service_mode, '')) = 'autopilot'
              AND service_mode_chosen_at IS NOT NULL
              AND service_mode_chosen_at <= ?
              AND NULLIF(trim(COALESCE(stripe_subscription_id, '')), '') IS NULL
              AND autopilot_billing_paused_at IS NULL
            ORDER BY service_mode_chosen_at ASC
            LIMIT 1
            """,
            [trial_cutoff],
        ).fetchone()
        if expired:
            conn.execute(
                """
                UPDATE shorts_users
                SET subscription_status = 'paused',
                    autopilot_billing_paused_at = ?,
                    updated_at = now()
                WHERE CAST(id AS VARCHAR) = ?
                  AND autopilot_billing_paused_at IS NULL
                """,
                [current, str(expired[0])],
            )
            conn.commit()
            logger.info("Paused expired no-card Autopilot account user_id=%s", expired[0])
            return True

        reminder = conn.execute(
            """
            SELECT CAST(id AS VARCHAR), email, name, service_mode_chosen_at
            FROM shorts_users
            WHERE lower(COALESCE(service_mode, '')) = 'autopilot'
              AND service_mode_chosen_at IS NOT NULL
              AND service_mode_chosen_at <= ?
              AND service_mode_chosen_at > ?
              AND NULLIF(trim(COALESCE(stripe_subscription_id, '')), '') IS NULL
              AND autopilot_billing_reminder_sent_at IS NULL
            ORDER BY service_mode_chosen_at ASC
            LIMIT 1
            """,
            [reminder_cutoff, trial_cutoff],
        ).fetchone()
        if not reminder:
            conn.commit()
            return False
        claimed = conn.execute(
            """
            UPDATE shorts_users
            SET autopilot_billing_reminder_sent_at = ?, updated_at = now()
            WHERE CAST(id AS VARCHAR) = ?
              AND autopilot_billing_reminder_sent_at IS NULL
            """,
            [current, str(reminder[0])],
        )
        if getattr(claimed, "rowcount", 1) == 0:
            conn.commit()
            return False
        conn.commit()
    finally:
        conn.close()

    trial_end = autopilot_trial_end(reminder[3])
    trial_end_label = trial_end.strftime("%B %d, %Y") if trial_end else "your trial end date"
    billing_url = (
        f"{(os.getenv('BASE_URL') or 'https://mintistudio.com').rstrip('/')}"
        "/video_shorts/shorts/storage/plans?checkout=current"
    )
    try:
        from app.video_shorts.services.email_verification import send_autopilot_billing_reminder_email

        send_autopilot_billing_reminder_email(
            to_email=str(reminder[1] or "").strip(),
            recipient_name=str(reminder[2] or "").strip(),
            billing_url=billing_url,
            trial_end_label=trial_end_label,
        )
    except Exception:
        reset_conn = get_db()
        try:
            reset_conn.execute(
                """
                UPDATE shorts_users
                SET autopilot_billing_reminder_sent_at = NULL, updated_at = now()
                WHERE CAST(id AS VARCHAR) = ?
                """,
                [str(reminder[0])],
            )
            reset_conn.commit()
        finally:
            reset_conn.close()
        raise
    logger.info("Sent Autopilot billing reminder user_id=%s", reminder[0])
    return True
