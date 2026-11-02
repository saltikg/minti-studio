"""One-time, server-side OAuth state bound to an explicit owner and brand."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
from typing import Any, Dict, Optional

from app.video_shorts.services.admin_operation_scope import request_admin_operation_scope
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import get_db, get_db_readonly


TABLE = "social_oauth_states"
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    nonce VARCHAR PRIMARY KEY,
    platform VARCHAR NOT NULL,
    owner_user_id VARCHAR NOT NULL,
    brand_id VARCHAR NOT NULL,
    actor_user_id VARCHAR NOT NULL,
    admin_operation_kind VARCHAR,
    code_verifier TEXT,
    next_url TEXT,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class OAuthStateError(ValueError):
    pass


def _brand_exists(owner_user_id: str, brand_id: str) -> bool:
    conn = get_db_readonly()
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM shorts_brands WHERE id = ? AND owner_user_id = ? LIMIT 1",
                [brand_id, owner_user_id],
            ).fetchone()
        )
    finally:
        conn.close()


def oauth_target(current_user: Dict[str, Any]) -> Dict[str, str]:
    actor_user_id = str((current_user or {}).get("id") or "").strip()
    if not actor_user_id:
        raise OAuthStateError("OAuth actor is missing.")
    operation_scope = request_admin_operation_scope()
    if operation_scope:
        if str((current_user or {}).get("role") or "").strip().lower() != "admin":
            raise OAuthStateError("Admin operation scope requires an admin actor.")
        owner_user_id = str(operation_scope.get("owner_user_id") or "").strip()
        brand_id = str(operation_scope.get("brand_id") or "").strip()
        operation_kind = str(operation_scope.get("workspace_kind") or "").strip().lower()
    else:
        owner_user_id = actor_user_id
        brand_id = str(current_brand_id() or "").strip()
        operation_kind = ""
    if not owner_user_id or not brand_id or not _brand_exists(owner_user_id, brand_id):
        raise OAuthStateError("OAuth target brand is invalid.")
    return {
        "owner_user_id": owner_user_id,
        "brand_id": brand_id,
        "actor_user_id": actor_user_id,
        "admin_operation_kind": operation_kind,
    }


def create_oauth_state(
    *,
    platform: str,
    current_user: Dict[str, Any],
    code_verifier: Optional[str] = None,
    next_url: Optional[str] = None,
    nonce: Optional[str] = None,
) -> str:
    target = oauth_target(current_user)
    nonce = str(nonce or "").strip() or secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=15)
    conn = get_db()
    try:
        conn.execute(SCHEMA_SQL)
        conn.execute(f"DELETE FROM {TABLE} WHERE expires_at < ?", [datetime.now(timezone.utc).replace(tzinfo=None)])
        conn.execute(
            f"""
            INSERT INTO {TABLE}
                (nonce, platform, owner_user_id, brand_id, actor_user_id,
                 admin_operation_kind, code_verifier, next_url, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                nonce,
                str(platform or "").strip().lower(),
                target["owner_user_id"],
                target["brand_id"],
                target["actor_user_id"],
                target["admin_operation_kind"] or None,
                code_verifier,
                next_url,
                expires_at,
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return nonce


def consume_oauth_state(*, platform: str, nonce: object, current_user: Dict[str, Any]) -> Dict[str, str]:
    clean_nonce = str(nonce or "").strip()
    actor_user_id = str((current_user or {}).get("id") or "").strip()
    if not clean_nonce or not actor_user_id:
        raise OAuthStateError("OAuth state is missing.")
    conn = get_db()
    try:
        conn.execute(SCHEMA_SQL)
        row = conn.execute(
            f"""
            DELETE FROM {TABLE}
            WHERE nonce = ? AND platform = ? AND actor_user_id = ? AND expires_at >= ?
            RETURNING owner_user_id, brand_id, actor_user_id,
                      COALESCE(admin_operation_kind, ''), COALESCE(code_verifier, ''),
                      COALESCE(next_url, '')
            """,
            [
                clean_nonce,
                str(platform or "").strip().lower(),
                actor_user_id,
                datetime.now(timezone.utc).replace(tzinfo=None),
            ],
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        raise OAuthStateError("OAuth state is invalid, expired, or already consumed.")
    owner_user_id, brand_id, stored_actor, operation_kind, code_verifier, next_url = [str(value or "") for value in row]
    if stored_actor != actor_user_id:
        raise OAuthStateError("OAuth state belongs to another signed-in user.")
    if operation_kind:
        if str((current_user or {}).get("role") or "").strip().lower() != "admin":
            raise OAuthStateError("OAuth admin scope is no longer authorized.")
    elif owner_user_id != actor_user_id:
        raise OAuthStateError("OAuth owner changed before callback.")
    if not _brand_exists(owner_user_id, brand_id):
        raise OAuthStateError("OAuth target brand no longer belongs to its owner.")
    return {
        "owner_user_id": owner_user_id,
        "brand_id": brand_id,
        "actor_user_id": stored_actor,
        "admin_operation_kind": operation_kind,
        "code_verifier": code_verifier,
        "next_url": next_url,
    }


def oauth_state_exists(*, platform: str, nonce: object) -> bool:
    clean_nonce = str(nonce or "").strip()
    if not clean_nonce:
        return False
    conn = None
    try:
        conn = get_db_readonly()
        row = conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE nonce = ? AND platform = ? AND expires_at >= ? LIMIT 1",
            [clean_nonce, str(platform or "").strip().lower(), datetime.now(timezone.utc).replace(tzinfo=None)],
        ).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()
