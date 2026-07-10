from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import stripe
from app.video_shorts.services.db import get_db_readonly


STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_PUBLISHABLE_KEY = (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

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
) -> stripe.checkout.Session:
    metadata = {
        "shorts_user_id": str(shorts_user_id),
        "plan_id": str(plan_id),
        "interval": str(interval),
    }
    return stripe.checkout.Session.create(
        mode="subscription",
        ui_mode="embedded_page",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        return_url=return_url,
        metadata=metadata,
        subscription_data={"metadata": metadata},
    )


def retrieve_checkout_session(session_id: str) -> stripe.checkout.Session:
    return stripe.checkout.Session.retrieve((session_id or "").strip())


def retrieve_subscription(subscription_id: str) -> stripe.Subscription:
    return stripe.Subscription.retrieve((subscription_id or "").strip())


def create_billing_portal_session(*, customer_id: str, return_url: str) -> stripe.billing_portal.Session:
    return stripe.billing_portal.Session.create(
        customer=(customer_id or "").strip(),
        return_url=return_url,
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
    items = getattr(subscription, "items", None)
    if items is None and isinstance(subscription, dict):
        items = subscription.get("items")
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


def resolve_plan_interval_from_subscription(subscription: Any) -> Optional[Tuple[str, str]]:
    price_id = _subscription_price_id(subscription)
    if not price_id:
        return None
    return get_plan_for_price_id(price_id)


def normalize_subscription_payload(subscription: Any) -> Dict[str, Any]:
    resolved = resolve_plan_interval_from_subscription(subscription)
    plan_id = resolved[0] if resolved else None
    interval = resolved[1] if resolved else None
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
        "subscription_status": (status or "").strip() or None,
        "stripe_subscription_id": (subscription_id or "").strip() or None,
        "stripe_customer_id": (customer_id or "").strip() or None,
        "subscription_current_period_end": _subscription_period_end(subscription),
        "price_id": _subscription_price_id(subscription),
    }


def load_billing_user_state(user_id: str) -> Optional[Dict[str, Any]]:
    normalized_user_id = (user_id or "").strip()
    if not normalized_user_id:
        return None
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
                billing_interval,
                plan_id
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
        "billing_interval": row[6],
        "plan_id": row[7],
    }


def user_has_managed_subscription(user_state: Optional[Dict[str, Any]]) -> bool:
    if not user_state:
        return False
    customer_id = (user_state.get("stripe_customer_id") or "").strip()
    subscription_id = (user_state.get("stripe_subscription_id") or "").strip()
    status = (user_state.get("subscription_status") or "").strip().lower()
    if not customer_id or not subscription_id:
        return False
    return status not in {"", "canceled", "incomplete_expired"}
