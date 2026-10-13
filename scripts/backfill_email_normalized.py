#!/usr/bin/env python3
"""Backfill shorts_users.email_normalized and report duplicate normalized emails."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.routes.auth import normalize_signup_email
from app.video_shorts.services.db import ensure_auth_user_schema, ensure_storage_user_schema, get_db


def run(*, dry_run: bool = False) -> dict[str, int]:
    conn = get_db()
    try:
        ensure_storage_user_schema(conn)
        ensure_auth_user_schema(conn)
        rows = conn.execute(
            """
            SELECT CAST(id AS VARCHAR), COALESCE(email, username, '')
            FROM shorts_users
            WHERE COALESCE(email, username, '') <> ''
            """
        ).fetchall()
        updated = 0
        for user_id, email in rows:
            normalized = normalize_signup_email(email or "")
            if not normalized:
                continue
            if not dry_run:
                conn.execute(
                    "UPDATE shorts_users SET email_normalized = ? WHERE CAST(id AS VARCHAR) = ?",
                    [normalized, str(user_id)],
                )
            updated += 1
        if not dry_run:
            conn.commit()
        collisions = conn.execute(
            """
            SELECT email_normalized, COUNT(*)
            FROM shorts_users
            WHERE COALESCE(email_normalized, '') <> ''
            GROUP BY email_normalized
            HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC, email_normalized
            """
        ).fetchall()
        collision_groups = len(collisions)
        collision_users = sum(int(row[1] or 0) for row in collisions)
        print(
            "EMAIL_NORMALIZED_BACKFILL "
            f"dry_run={int(dry_run)} updated={updated} "
            f"collision_groups={collision_groups} collision_users={collision_users}"
        )
        for normalized, count in collisions[:20]:
            print(f"COLLISION {normalized} count={int(count or 0)}")
        if collision_groups > 20:
            print(f"COLLISION ... {collision_groups - 20} more")
        return {
            "updated": updated,
            "collision_groups": collision_groups,
            "collision_users": collision_users,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
