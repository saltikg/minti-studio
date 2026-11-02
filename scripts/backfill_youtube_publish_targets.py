#!/usr/bin/env python3
"""Verify YouTube token identities and snapshot pending publish targets.

Run without --apply for a read-only preview. Token refresh and channels.list are
read-only provider operations; --apply persists verified identities and fills
only missing schedule snapshots.
"""

from __future__ import annotations

import argparse

from app import create_app
from app.video_shorts.services.db import get_db
from app.video_shorts.services.youtube_oauth import get_connected_channel_info
from app.video_shorts.services.youtube_token_store import (
    list_stored_refresh_tokens,
    split_scoped_user_id,
    update_youtube_connection_identity,
)


def _print_row(kind: str, *values: object) -> None:
    print("\t".join([kind, *(str(value or "") for value in values)]))


def run(*, apply: bool) -> int:
    channel_by_scope: dict[tuple[str, str], dict[str, str]] = {}
    failures = 0
    print("TYPE\tUSER_ID\tBRAND_ID\tCHANNEL_ID\tCHANNEL_TITLE\tRESULT")
    for token in list_stored_refresh_tokens():
        owner_user_id, brand_id = split_scoped_user_id(token.get("user_id"))
        if not owner_user_id or not brand_id:
            failures += 1
            _print_row("TOKEN", owner_user_id, brand_id, "", "", "unscoped-token")
            continue
        channel_info, error = get_connected_channel_info(owner_user_id, brand_id)
        channel_id = str((channel_info or {}).get("id") or "").strip()
        channel_title = str((channel_info or {}).get("title") or "").strip()
        if not channel_id:
            failures += 1
            _print_row("TOKEN", owner_user_id, brand_id, "", "", error or "lookup-failed")
            continue
        result = "verified"
        if apply:
            update_youtube_connection_identity(
                user_id=owner_user_id,
                brand_id=brand_id,
                youtube_channel_id=channel_id,
                channel_title=channel_title,
            )
            result = "updated"
        channel_by_scope[(owner_user_id, brand_id)] = {
            "channel_id": channel_id,
            "channel_title": channel_title,
        }
        _print_row("TOKEN", owner_user_id, brand_id, channel_id, channel_title, result)

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, brand_id, expected_youtube_channel_id,
                   status, publish_at, upload_due_at
            FROM shorts_youtube_schedules
            WHERE status IN ('pending', 'queued')
              AND youtube_video_id IS NULL
            ORDER BY COALESCE(publish_at, upload_due_at), id
            """
        ).fetchall()
        print("TYPE\tSCHEDULE_ID\tUSER_ID\tBRAND_ID\tEXPECTED\tCURRENT\tSTATUS\tPUBLISH_AT\tRESULT")
        for row in rows:
            schedule_id, user_id, brand_id, expected, status, publish_at, _upload_due_at = row
            current = (channel_by_scope.get((str(user_id), str(brand_id))) or {}).get("channel_id", "")
            result = "already-filled"
            if not current:
                failures += 1
                result = "token-identity-unavailable"
            elif expected and str(expected) != current:
                failures += 1
                result = "MISMATCH-NOT-CHANGED"
            elif not expected:
                result = "would-fill"
                if apply:
                    conn.execute(
                        """
                        UPDATE shorts_youtube_schedules
                        SET expected_youtube_channel_id = ?, updated_at = now()
                        WHERE id = ? AND COALESCE(expected_youtube_channel_id, '') = ''
                        """,
                        [current, schedule_id],
                    )
                    result = "filled"
            _print_row(
                "SCHEDULE",
                schedule_id,
                user_id,
                brand_id,
                expected or current,
                current,
                status,
                publish_at,
                result,
            )
        if apply:
            conn.commit()
    finally:
        conn.close()
    print(f"SUMMARY\tfailures={failures}\tapply={int(apply)}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        return run(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
