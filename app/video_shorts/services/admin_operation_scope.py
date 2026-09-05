"""Server-validated, request-local scope for explicit admin operations."""

from __future__ import annotations

from typing import Any, Dict, Optional

from flask import g


ADMIN_OPERATION_WORKSPACE_KINDS = {"lead", "customer"}


def _is_admin(current_user: Optional[Dict[str, Any]]) -> bool:
    return bool(current_user and str(current_user.get("role") or "").strip().lower() == "admin")


def resolve_admin_operation_request_scope(
    conn,
    *,
    current_user: Optional[Dict[str, Any]],
    brand_id: str,
    workspace_kind: str,
) -> Optional[Dict[str, str]]:
    """Resolve one admin target exclusively from the current request context.

    ``brand_id`` is an identifier, not a capability: admin role, owner linkage,
    and lead/customer state are all verified from Postgres for every request.
    """
    if not _is_admin(current_user):
        return None
    brand_id = str(brand_id or "").strip()
    workspace_kind = str(workspace_kind or "").strip().lower()
    acting_admin_id = str(current_user.get("id") or "").strip()
    if not brand_id or workspace_kind not in ADMIN_OPERATION_WORKSPACE_KINDS or not acting_admin_id:
        return None
    row = conn.execute(
        """
        SELECT CAST(b.owner_user_id AS VARCHAR), CAST(b.id AS VARCHAR)
        FROM shorts_brands b
        JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(b.owner_user_id AS VARCHAR)
        WHERE CAST(b.id AS VARCHAR) = ?
          AND CAST(u.id AS VARCHAR) = CAST(b.owner_user_id AS VARCHAR)
        LIMIT 1
        """,
        [brand_id],
    ).fetchone()
    if not row:
        return None
    owner_user_id = str(row[0] or "").strip()
    resolved_brand_id = str(row[1] or "").strip()
    if not owner_user_id or not resolved_brand_id:
        return None
    if workspace_kind == "lead":
        workspace_row = conn.execute(
            """
            SELECT 1 FROM autopilot_leads
            WHERE CAST(user_id AS VARCHAR) = ?
              AND CAST(brand_id AS VARCHAR) = ?
              AND converted_at IS NULL
            LIMIT 1
            """,
            [owner_user_id, resolved_brand_id],
        ).fetchone()
    else:
        workspace_row = conn.execute(
            """
            SELECT 1
            FROM autopilot_leads l
            JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(l.user_id AS VARCHAR)
            WHERE CAST(l.user_id AS VARCHAR) = ?
              AND CAST(l.brand_id AS VARCHAR) = ?
              AND l.converted_at IS NOT NULL
              AND lower(coalesce(u.service_mode, '')) = 'autopilot'
            LIMIT 1
            """,
            [owner_user_id, resolved_brand_id],
        ).fetchone()
    if not workspace_row:
        return None
    return {
        "owner_user_id": owner_user_id,
        "brand_id": resolved_brand_id,
        "workspace_kind": workspace_kind,
        "acting_admin_id": acting_admin_id,
    }


def set_request_admin_operation_scope(scope: Dict[str, str]) -> None:
    """Persist a validated target for only the lifetime of this HTTP request."""
    g.vs_admin_operation_scope = dict(scope)
    g.vs_admin_operation_workspace_kind = str(scope.get("workspace_kind") or "").strip().lower()


def request_admin_operation_scope() -> Optional[Dict[str, str]]:
    scope = getattr(g, "vs_admin_operation_scope", None)
    if not isinstance(scope, dict):
        return None
    owner_user_id = str(scope.get("owner_user_id") or "").strip()
    brand_id = str(scope.get("brand_id") or "").strip()
    workspace_kind = str(scope.get("workspace_kind") or "").strip().lower()
    acting_admin_id = str(scope.get("acting_admin_id") or "").strip()
    if (
        not owner_user_id
        or not brand_id
        or workspace_kind not in ADMIN_OPERATION_WORKSPACE_KINDS
        or not acting_admin_id
    ):
        return None
    return {
        "owner_user_id": owner_user_id,
        "brand_id": brand_id,
        "workspace_kind": workspace_kind,
        "acting_admin_id": acting_admin_id,
    }
