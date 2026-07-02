from __future__ import annotations

import hashlib
import hmac
import json

from flask import Response, current_app, jsonify, request

from app.video_shorts.config import INSTAGRAM_WEBHOOK_VERIFY_TOKEN, META_APP_SECRET
from app.video_shorts.services.instagram_comment_webhook import enqueue_instagram_comment_events


def _signature_matches(raw_body: bytes, signature_header: str) -> bool:
    if not META_APP_SECRET:
        return False
    header_value = (signature_header or "").strip()
    if not header_value.startswith("sha256="):
        return False
    provided = header_value.split("=", 1)[1].strip()
    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


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
    if not _signature_matches(raw_body, request.headers.get("X-Hub-Signature-256", "")):
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
