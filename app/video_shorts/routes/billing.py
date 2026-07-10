from __future__ import annotations

import logging
from urllib.parse import urlparse
from typing import Any, Dict, Optional

import stripe
from flask import current_app, flash, g, jsonify, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.services.billing import (
    STRIPE_PUBLISHABLE_KEY,
    STRIPE_WEBHOOK_SECRET,
    build_checkout_return_url,
    create_billing_portal_session,
    construct_webhook_event,
    create_customer,
    create_embedded_checkout_session,
    get_plan_for_price_id,
    get_price_id_for_plan,
    interval_is_supported,
    normalize_subscription_payload,
    plan_is_paid,
    resolve_plan_interval_from_subscription,
    retrieve_checkout_session,
    retrieve_subscription,
    stripe_is_configured,
)
from app.video_shorts.services.db import ensure_auth_user_schema, get_db, get_db_readonly


logger = logging.getLogger(__name__)


def _billing_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _current_user_or_401():
    current_user = getattr(g, "vs_current_user", None)
    if current_user is None:
        from app.video_shorts.routes.auth import _current_user

        current_user = _current_user()
    if not current_user:
        return None, _billing_error("unauthorized", 401)
    return current_user, None


def _is_public_host(host: str) -> bool:
    value = (host or "").strip().lower()
    if not value:
        return False
    host_only = value.split(":", 1)[0]
    return host_only not in {"127.0.0.1", "localhost", "0.0.0.0"}


def _public_base_url() -> str:
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip()
    forwarded_host = (request.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
    if forwarded_proto and _is_public_host(forwarded_host):
        return f"{forwarded_proto}://{forwarded_host}"

    host = (request.headers.get("Host") or "").strip()
    if _is_public_host(host):
        scheme = forwarded_proto or request.scheme or "https"
        return f"{scheme}://{host}"

    parsed = urlparse((current_app.config.get("BASE_URL") or "").strip())
    if parsed.scheme and _is_public_host(parsed.netloc):
        return f"{parsed.scheme}://{parsed.netloc}"

    return request.url_root.rstrip("/")


def _load_billing_user(user_id: str) -> Optional[Dict[str, Any]]:
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
                plan_id
            FROM shorts_users
            WHERE id = ?
            """,
            [user_id],
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
        "subscription_cancel_at_period_end": bool(row[6]) if row[6] is not None else False,
        "billing_interval": row[7],
        "plan_id": row[8],
    }


def _user_has_managed_subscription(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False
    customer_id = (user.get("stripe_customer_id") or "").strip()
    subscription_id = (user.get("stripe_subscription_id") or "").strip()
    status = (user.get("subscription_status") or "").strip().lower()
    if not customer_id or not subscription_id:
        return False
    return status not in {"", "canceled", "incomplete_expired"}


def _save_customer_id(user_id: str, customer_id: str) -> None:
    conn = get_db()
    try:
        ensure_auth_user_schema(conn)
        conn.execute(
            """
            UPDATE shorts_users
            SET stripe_customer_id = ?,
                updated_at = now()
            WHERE id = ?
            """,
            [customer_id, user_id],
        )
        conn.commit()
    finally:
        conn.close()


def _find_user_by_customer_id(conn, customer_id: str) -> Optional[str]:
    if not customer_id:
        return None
    row = conn.execute(
        """
        SELECT CAST(id AS VARCHAR)
        FROM shorts_users
        WHERE stripe_customer_id = ?
        LIMIT 1
        """,
        [customer_id],
    ).fetchone()
    return row[0] if row else None


def _update_user_subscription_state(
    *,
    user_id: str,
    plan_id: str,
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
            SET plan_id = ?,
                stripe_customer_id = ?,
                stripe_subscription_id = ?,
                subscription_status = ?,
                subscription_current_period_end = ?,
                subscription_cancel_at_period_end = ?,
                billing_interval = ?,
                updated_at = now()
            WHERE id = ?
            """,
            [
                plan_id,
                stripe_customer_id,
                stripe_subscription_id,
                subscription_status,
                subscription_current_period_end,
                subscription_cancel_at_period_end,
                billing_interval,
                user_id,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _update_user_status_only(
    *,
    user_id: str,
    subscription_status: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    subscription_current_period_end=None,
    subscription_cancel_at_period_end: Optional[bool] = None,
    billing_interval: Optional[str] = None,
) -> None:
    conn = get_db()
    try:
        ensure_auth_user_schema(conn)
        conn.execute(
            """
            UPDATE shorts_users
            SET stripe_customer_id = COALESCE(?, stripe_customer_id),
                stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                subscription_status = ?,
                subscription_current_period_end = COALESCE(?, subscription_current_period_end),
                subscription_cancel_at_period_end = COALESCE(?, subscription_cancel_at_period_end),
                billing_interval = COALESCE(?, billing_interval),
                updated_at = now()
            WHERE id = ?
            """,
            [
                stripe_customer_id,
                stripe_subscription_id,
                subscription_status,
                subscription_current_period_end,
                subscription_cancel_at_period_end,
                billing_interval,
                user_id,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _resolve_user_id_for_event(*, metadata: Dict[str, Any], customer_id: str) -> Optional[str]:
    metadata_user_id = (metadata.get("shorts_user_id") or "").strip() if metadata else ""
    if metadata_user_id:
        return metadata_user_id
    conn = get_db_readonly()
    try:
        return _find_user_by_customer_id(conn, customer_id)
    finally:
        conn.close()


def _extract_metadata(payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {}
    metadata = getattr(payload, "metadata", None)
    if metadata is None and isinstance(payload, dict):
        metadata = payload.get("metadata")
    if not metadata:
        return {}
    try:
        if isinstance(metadata, dict):
            return {str(key): value for key, value in metadata.items()}
        to_dict_recursive = getattr(metadata, "to_dict_recursive", None)
        if callable(to_dict_recursive):
            value = to_dict_recursive()
            if isinstance(value, dict):
                return {str(key): item for key, item in value.items()}
        to_dict = getattr(metadata, "to_dict", None)
        if callable(to_dict):
            value = to_dict()
            if isinstance(value, dict):
                return {str(key): item for key, item in value.items()}
        items_method = getattr(metadata, "items", None)
        if callable(items_method):
            try:
                return {str(key): value for key, value in items_method()}
            except Exception:
                pass
        extracted: Dict[str, Any] = {}
        for key in metadata:
            extracted[str(key)] = metadata[key]
        return extracted
    except Exception as exc:
        logger.warning("Stripe metadata extraction failed for payload_type=%s: %s", type(payload).__name__, exc)
        return {}


@video_shorts_bp.route("/billing/create-checkout-session", methods=["POST"])
def create_checkout_session_route():
    current_user, error = _current_user_or_401()
    if error:
        return error
    if not stripe_is_configured():
        return _billing_error("billing_not_configured", 503)

    payload = request.get_json(silent=True) or {}
    plan_id = (payload.get("plan_id") or "").strip()
    interval = (payload.get("interval") or "").strip().lower()
    if not plan_is_paid(plan_id) or not interval_is_supported(interval):
        return _billing_error("invalid_plan", 400)

    price_id = get_price_id_for_plan(plan_id, interval)
    if not price_id:
        return _billing_error("invalid_price_mapping", 400)

    user = _load_billing_user(current_user["id"])
    if not user:
        return _billing_error("user_not_found", 404)
    if _user_has_managed_subscription(user):
        return _billing_error("subscription_managed_in_portal", 409)

    customer_id = (user.get("stripe_customer_id") or "").strip()
    if not customer_id:
        customer = create_customer(
            email=(user.get("email") or current_user.get("email") or "").strip(),
            shorts_user_id=current_user["id"],
        )
        customer_id = (customer.id or "").strip()
        if customer_id:
            _save_customer_id(current_user["id"], customer_id)

    base_url = _public_base_url()
    return_url = build_checkout_return_url(base_url)
    try:
        session = create_embedded_checkout_session(
            customer_id=customer_id,
            price_id=price_id,
            shorts_user_id=current_user["id"],
            plan_id=plan_id,
            interval=interval,
            return_url=return_url,
        )
    except stripe.StripeError as exc:
        logger.exception(
            "Stripe checkout session creation failed user_id=%s plan_id=%s interval=%s customer_id=%s return_url=%s",
            current_user["id"],
            plan_id,
            interval,
            customer_id or "-",
            return_url,
        )
        message = getattr(exc, "user_message", None) or str(exc) or "Stripe checkout session creation failed."
        return _billing_error(message, 500)
    except Exception as exc:
        logger.exception(
            "Unexpected checkout session creation failure user_id=%s plan_id=%s interval=%s customer_id=%s return_url=%s",
            current_user["id"],
            plan_id,
            interval,
            customer_id or "-",
            return_url,
        )
        return _billing_error(str(exc) or "Unexpected checkout session creation failure.", 500)
    return jsonify({"client_secret": session.client_secret})


@video_shorts_bp.route("/billing/portal", methods=["GET"])
def billing_portal():
    current_user, error = _current_user_or_401()
    if error:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    user = _load_billing_user(current_user["id"])
    customer_id = (user or {}).get("stripe_customer_id") or ""
    if not customer_id:
        flash("No Stripe subscription was found for this account.", "warning")
        return redirect(url_for("video_shorts_bp.shorts_storage_plans"))
    base_url = _public_base_url()
    return_url = f"{base_url}{url_for('video_shorts_bp.shorts_storage_plans')}"
    try:
        session = create_billing_portal_session(customer_id=customer_id, return_url=return_url)
    except stripe.StripeError:
        logger.exception("Stripe billing portal creation failed user_id=%s customer_id=%s", current_user["id"], customer_id)
        flash("Could not open Stripe Billing Portal right now.", "danger")
        return redirect(url_for("video_shorts_bp.shorts_storage_plans"))
    except Exception:
        logger.exception("Unexpected billing portal creation failure user_id=%s customer_id=%s", current_user["id"], customer_id)
        flash("Could not open Stripe Billing Portal right now.", "danger")
        return redirect(url_for("video_shorts_bp.shorts_storage_plans"))
    return redirect(getattr(session, "url", None) or url_for("video_shorts_bp.shorts_storage_plans"))


@video_shorts_bp.route("/billing/complete", methods=["GET"])
def billing_complete():
    current_user, error = _current_user_or_401()
    if error:
        return redirect(url_for("video_shorts_bp.login", next=request.url))
    session_id = (request.args.get("session_id") or "").strip()
    session_obj = None
    status_summary = "missing"
    error_message = None
    if session_id:
        try:
            session_obj = retrieve_checkout_session(session_id)
            status_summary = f"{getattr(session_obj, 'status', None) or 'unknown'} / {getattr(session_obj, 'payment_status', None) or 'unknown'}"
        except Exception as exc:
            error_message = str(exc)
    return render_template(
        "billing_complete.html",
        stripe_session_id=session_id,
        stripe_session=session_obj,
        stripe_status_summary=status_summary,
        stripe_error_message=error_message,
        billing_redirect_url=url_for("video_shorts_bp.shorts_storage_plans"),
    )


def _sync_subscription_to_user(subscription: Any, *, metadata: Optional[Dict[str, Any]] = None) -> bool:
    normalized = normalize_subscription_payload(subscription)
    user_id = _resolve_user_id_for_event(
        metadata=metadata or _extract_metadata(subscription),
        customer_id=normalized.get("stripe_customer_id") or "",
    )
    if not user_id:
        logger.warning(
            "Stripe webhook subscription event could not match a user subscription_id=%s customer_id=%s",
            normalized.get("stripe_subscription_id") or "-",
            normalized.get("stripe_customer_id") or "-",
        )
        return False
    if not normalized.get("plan_id") or not normalized.get("billing_interval"):
        logger.warning(
            "Stripe webhook subscription price mapping missing subscription_id=%s price_id=%s",
            normalized.get("stripe_subscription_id") or "-",
            normalized.get("price_id") or "-",
        )
        return False
    _update_user_subscription_state(
        user_id=user_id,
        plan_id=normalized["plan_id"],
        stripe_customer_id=normalized.get("stripe_customer_id"),
        stripe_subscription_id=normalized.get("stripe_subscription_id"),
        subscription_status=normalized.get("subscription_status"),
        subscription_current_period_end=normalized.get("subscription_current_period_end"),
        subscription_cancel_at_period_end=normalized.get("subscription_cancel_at_period_end"),
        billing_interval=normalized.get("billing_interval"),
    )
    return True


def _handle_checkout_completed(event_obj: Any) -> bool:
    metadata = _extract_metadata(event_obj)
    subscription_id = (getattr(event_obj, "subscription", None) or (event_obj.get("subscription") if isinstance(event_obj, dict) else "") or "").strip()
    if not subscription_id:
        logger.warning("Stripe checkout.session.completed missing subscription id")
        return False
    subscription = retrieve_subscription(subscription_id)
    return _sync_subscription_to_user(subscription, metadata=metadata)


def _handle_subscription_updated(subscription: Any) -> bool:
    return _sync_subscription_to_user(subscription)


def _handle_subscription_deleted(subscription: Any) -> bool:
    normalized = normalize_subscription_payload(subscription)
    metadata = _extract_metadata(subscription)
    user_id = _resolve_user_id_for_event(
        metadata=metadata,
        customer_id=normalized.get("stripe_customer_id") or "",
    )
    if not user_id:
        logger.warning(
            "Stripe customer.subscription.deleted unmatched subscription_id=%s customer_id=%s",
            normalized.get("stripe_subscription_id") or "-",
            normalized.get("stripe_customer_id") or "-",
        )
        return False
    _update_user_subscription_state(
        user_id=user_id,
        plan_id="plan_free",
        stripe_customer_id=normalized.get("stripe_customer_id"),
        stripe_subscription_id=None,
        subscription_status="canceled",
        subscription_current_period_end=normalized.get("subscription_current_period_end"),
        subscription_cancel_at_period_end=False,
        billing_interval=None,
    )
    return True


def _handle_invoice_payment_failed(invoice: Any) -> bool:
    customer_id = (getattr(invoice, "customer", None) or (invoice.get("customer") if isinstance(invoice, dict) else "") or "").strip()
    subscription_id = (getattr(invoice, "subscription", None) or (invoice.get("subscription") if isinstance(invoice, dict) else "") or "").strip()
    metadata = _extract_metadata(invoice)
    user_id = _resolve_user_id_for_event(metadata=metadata, customer_id=customer_id)
    if not user_id:
        logger.warning(
            "Stripe invoice.payment_failed unmatched subscription_id=%s customer_id=%s",
            subscription_id or "-",
            customer_id or "-",
        )
        return False
    interval = None
    if subscription_id:
        try:
            subscription = retrieve_subscription(subscription_id)
            resolved = resolve_plan_interval_from_subscription(subscription)
            interval = resolved[1] if resolved else None
        except Exception:
            logger.exception("Failed to refresh subscription during invoice.payment_failed subscription_id=%s", subscription_id)
    _update_user_status_only(
        user_id=user_id,
        subscription_status="past_due",
        stripe_customer_id=customer_id or None,
        stripe_subscription_id=subscription_id or None,
        subscription_cancel_at_period_end=None,
        billing_interval=interval,
    )
    return True


@video_shorts_bp.route("/billing/webhook", methods=["POST"])
def billing_webhook():
    raw_body = request.get_data(cache=False) or b""
    signature = request.headers.get("Stripe-Signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        return _billing_error("webhook_not_configured", 400)
    try:
        event = construct_webhook_event(payload=raw_body, signature=signature)
    except Exception as exc:
        return _billing_error(f"invalid_webhook: {exc}", 400)

    event_type = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else "")
    data = getattr(event, "data", None)
    event_object = getattr(data, "object", None) if data is not None else None
    if event_object is None and isinstance(event, dict):
        event_object = ((event.get("data") or {}).get("object"))

    try:
        if event_type == "checkout.session.completed":
            _handle_checkout_completed(event_object)
        elif event_type == "customer.subscription.updated":
            _handle_subscription_updated(event_object)
        elif event_type == "customer.subscription.deleted":
            _handle_subscription_deleted(event_object)
        elif event_type == "invoice.payment_failed":
            _handle_invoice_payment_failed(event_object)
    except stripe.StripeError:
        logger.exception("Stripe webhook handler failed event_type=%s", event_type)
        return _billing_error("stripe_error", 500)
    except Exception:
        logger.exception("Unexpected Stripe webhook failure event_type=%s", event_type)
        return _billing_error("webhook_handler_error", 500)

    return jsonify({"received": True})
