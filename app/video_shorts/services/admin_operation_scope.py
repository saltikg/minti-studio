"""Server-validated, request-local scope for explicit admin customer operations."""

from __future__ import annotations

from typing import Any, Dict, Optional

from flask import g, has_request_context, session

ADMIN_OPERATION_BRAND_SESSION_KEY = "admin_operation_brand_id"
ADMIN_OPERATION_OWNER_SESSION_KEY = "admin_operation_owner_id"


def _is_admin(current_user: Optional[Dict[str, Any]]) -> bool:
    return bool(current_user and str(current_user.get("role") or "").strip().lower() == "admin")


def _validated_scope(
    conn,
    *,
    target_owner_id: str,
    target_brand_id: str,
    acting_admin_id: str,
) -> Optional[Dict[str, str]]:
    if not target_owner_id or not target_brand_id or not acting_admin_id:
        return None
    row = conn.execute(
        """
        SELECT CAST(b.owner_user_id AS VARCHAR), CAST(b.id AS VARCHAR)
        FROM shorts_brands b
        JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(b.owner_user_id AS VARCHAR)
        WHERE CAST(b.id AS VARCHAR) = ?
          AND CAST(b.owner_user_id AS VARCHAR) = ?
          AND CAST(u.id AS VARCHAR) = ?
        LIMIT 1
        """,
        [target_brand_id, target_owner_id, target_owner_id],
    ).fetchone()
    if not row:
        return None
    return {
        "owner_user_id": str(row[0]),
        "brand_id": str(row[1]),
        "acting_admin_id": acting_admin_id,
    }


def select_admin_operation_scope(
    conn,
    *,
    current_user: Optional[Dict[str, Any]],
    target_owner_id: str,
    target_brand_id: str,
) -> Optional[Dict[str, str]]:
    """Validate and persist an admin-only target without changing normal brand state."""
    if not _is_admin(current_user):
        return None
    scope = _validated_scope(
        conn,
        target_owner_id=str(target_owner_id or "").strip(),
        target_brand_id=str(target_brand_id or "").strip(),
        acting_admin_id=str(current_user.get("id") or "").strip(),
    )
    if not scope or not has_request_context():
        return None
    session[ADMIN_OPERATION_OWNER_SESSION_KEY] = scope["owner_user_id"]
    session[ADMIN_OPERATION_BRAND_SESSION_KEY] = scope["brand_id"]
    return scope


def resolve_admin_operation_scope(
    conn,
    *,
    current_user: Optional[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Revalidate the target on every explicit admin-operation request."""
    if not _is_admin(current_user) or not has_request_context():
        return None
    scope = _validated_scope(
        conn,
        target_owner_id=str(session.get(ADMIN_OPERATION_OWNER_SESSION_KEY) or "").strip(),
        target_brand_id=str(session.get(ADMIN_OPERATION_BRAND_SESSION_KEY) or "").strip(),
        acting_admin_id=str(current_user.get("id") or "").strip(),
    )
    if scope:
        return scope
    clear_admin_operation_scope()
    return None


def clear_admin_operation_scope() -> None:
    if not has_request_context():
        return
    session.pop(ADMIN_OPERATION_OWNER_SESSION_KEY, None)
    session.pop(ADMIN_OPERATION_BRAND_SESSION_KEY, None)


def set_request_admin_operation_scope(scope: Dict[str, str]) -> None:
    """Only explicit, admin-guarded routes call this; normal routes never read it."""
    g.vs_admin_operation_scope = dict(scope)


def request_admin_operation_scope() -> Optional[Dict[str, str]]:
    scope = getattr(g, "vs_admin_operation_scope", None)
    if not isinstance(scope, dict):
        return None
    owner_user_id = str(scope.get("owner_user_id") or "").strip()
    brand_id = str(scope.get("brand_id") or "").strip()
    acting_admin_id = str(scope.get("acting_admin_id") or "").strip()
    if not owner_user_id or not brand_id or not acting_admin_id:
        return None
    return {
        "owner_user_id": owner_user_id,
        "brand_id": brand_id,
        "acting_admin_id": acting_admin_id,
    }
