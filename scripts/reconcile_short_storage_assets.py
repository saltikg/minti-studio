#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.db import get_db  # noqa: E402
from app.video_shorts.services.storage import get_media_storage  # noqa: E402


def _existing_short_asset_keys() -> set[str]:
    keys: set[str] = set()
    storages = [get_media_storage()]
    if storages[0].backend_name != "local":
        storages.append(get_media_storage("local"))
    for storage in storages:
        for entry in storage.list_prefix("shorts/"):
            key = str(entry.key or "").strip()
            if not key.startswith("shorts/") or key.startswith("shorts/posters/"):
                continue
            name = Path(key).name
            if name:
                keys.add(f"short:{name}")
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mark stale rendered-Short asset records missing without deleting rows or media."
    )
    parser.add_argument("--apply", action="store_true", help="Apply status updates; default is dry-run.")
    args = parser.parse_args()

    existing_keys = _existing_short_asset_keys()
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT file_key
            FROM shorts_storage_assets
            WHERE file_key LIKE 'short:%'
              AND lower(coalesce(status, 'active')) = 'active'
            ORDER BY file_key
            """
        ).fetchall()
        indexed_keys = {str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()}
        missing_keys = sorted(indexed_keys - existing_keys)
        print(
            "short asset reconciliation: "
            f"indexed_active={len(indexed_keys)} existing={len(existing_keys)} missing={len(missing_keys)}"
        )
        for key in missing_keys[:20]:
            print(f"  missing {key}")
        if len(missing_keys) > 20:
            print(f"  ... {len(missing_keys) - 20} more")
        if not args.apply or not missing_keys:
            conn.rollback()
            print("dry_run=1" if not args.apply else "updated=0")
            return 0

        conn.executemany(
            """
            UPDATE shorts_storage_assets
            SET status = 'missing', updated_at = now()
            WHERE file_key = ?
              AND lower(coalesce(status, 'active')) = 'active'
            """,
            [(key,) for key in missing_keys],
        )
        conn.commit()
        print(f"updated={len(missing_keys)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
