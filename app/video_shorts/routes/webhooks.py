from __future__ import annotations

import hashlib
import hmac
import json

from flask import Response, current_app, jsonify, request

from app.video_shorts.config import (
    INSTAGRAM_APP_SECRET,
    INSTAGRAM_WEBHOOK_VERIFY_TOKEN,
    META_APP_SECRET,
)
from app.video_shorts.services.instagram_comment_webhook import enqueue_instagram_comment_events


def _instagram_webhook_secret() -> tuple[str, str]:
    if INSTAGRAM_APP_SECRET:
        return INSTAGRAM_APP_SECRET, "INSTAGRAM_APP_SECRET"
    if META_APP_SECRET:
        return META_APP_SECRET, "META_APP_SECRET"
    return "", "missing"


def _signature_debug(raw_body: bytes, signature_header: str) -> dict[str, object]:
    secret, secret_source = _instagram_webhook_secret()
    header_value = (signature_header or "").strip()
    provided = ""
    if header_value.startswith("sha256="):
        provided = header_value.split("=", 1)[1].strip()
    expected = ""
    if secret:
        expected = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    return {
        "secret_source": secret_source,
        "header_present": bool(signature_header),
        "body_len": len(raw_body or b""),
        "provided_prefix": provided[:6] if provided else "",
        "expected_prefix": expected[:6] if expected else "",
        "match": bool(provided and expected and hmac.compare_digest(provided, expected)),
    }


def _signature_matches(raw_body: bytes, signature_header: str) -> bool:
    debug = _signature_debug(raw_body, signature_header)
    if debug["secret_source"] == "missing":
        return False
    header_value = (signature_header or "").strip()
    if not header_value.startswith("sha256="):
        return False
    return bool(debug["match"])


def instagram_webhook_verify():
    mode = (request.args.get("hub.mode") or "").strip()
    verify_token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge") or ""
    if (
        mode == "subscribe"
        and INSTAGRAM_WEBHOOK_VERIFY_TOKEN
        and verify_token == INSTAGRAM_WEBHOOK_VERIFY_TOKEN
    ):
        return Response(challenge, status=200, mimetype="text/plain")
    return Response(status=403)


def instagram_webhook_event():
    raw_body = request.get_data(cache=False) or b""
    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not _signature_matches(raw_body, signature_header):
        debug = _signature_debug(raw_body, signature_header)
        current_app.logger.warning(
            "Instagram webhook signature rejected header_present=%s body_len=%s secret_source=%s provided_prefix=%s expected_prefix=%s prefix_match=%s",
            debug["header_present"],
            debug["body_len"],
            debug["secret_source"],
            debug["provided_prefix"] or "-",
            debug["expected_prefix"] or "-",
            debug["match"],
        )
        return Response(status=403)

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception:
        current_app.logger.warning("Instagram webhook received invalid JSON payload.")
        return jsonify(success=True, queued=0, skipped=0)

    # Queue onto the existing worker-backed jobs table so we can return 200
    # immediately and keep AI moderation/upsert work off the webhook request.
    stats = enqueue_instagram_comment_events(payload if isinstance(payload, dict) else {})
    return jsonify(success=True, queued=stats["queued"], skipped=stats["skipped"])


def register_instagram_webhook_routes(app) -> None:
    app.add_url_rule(
        "/webhooks/instagram",
        view_func=instagram_webhook_verify,
        methods=["GET"],
        endpoint="instagram_webhook_verify",
    )
    app.add_url_rule(
        "/webhooks/instagram",
        view_func=instagram_webhook_event,
        methods=["POST"],
        endpoint="instagram_webhook_event",
    )
