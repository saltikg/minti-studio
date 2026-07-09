from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

import stripe


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


def get_price_id_for_plan(plan_id: str, interval: str) -> Optional[str]:
    normalized_plan_id = (plan_id or "").strip()
    normalized_interval = (interval or "").strip().lower()
    return PLAN_INTERVAL_TO_PRICE_ID.get((normalized_plan_id, normalized_interval))


def get_plan_for_price_id(price_id: str) -> Optional[Tuple[str, str]]:
    normalized_price_id = (price_id or "").strip()
    if not normalized_price_id:
        return None
    return PRICE_ID_TO_PLAN_INTERVAL.get(normalized_price_id)

