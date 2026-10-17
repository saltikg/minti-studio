#!/usr/bin/env python3
"""One-off data repair formerly performed while rendering every page."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app import create_app
from app.video_shorts.services.brands import ensure_brand_for_user
from app.video_shorts.services.db import get_db
from app.video_shorts.services.usage_metering import (
    _backfill_transcription_events,
    _ensure_usage_row,
    _fetch_usage_history,
    _month_start,
)


def main() -> int:
    app = create_app()
    users_processed = 0
    brand_backfills = 0
    usage_rows_checked = 0
    transcription_events_created = 0
    with app.app_context():
        conn = get_db()
        try:
            users = conn.execute(
                "SELECT CAST(id AS VARCHAR), COALESCE(name, username, '') FROM shorts_users ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()

        for user_id, user_name in users:
            conn = get_db()
            try:
                before = conn.execute(
                    "SELECT COUNT(*) FROM shorts_brands WHERE owner_user_id = ?",
                    [str(user_id)],
                ).fetchone()[0]
                ensure_brand_for_user(conn, user_id=str(user_id), user_name=str(user_name or ""))
                after = conn.execute(
                    "SELECT COUNT(*) FROM shorts_brands WHERE owner_user_id = ?",
                    [str(user_id)],
                ).fetchone()[0]
                brand_backfills += max(0, int(after or 0) - int(before or 0))

                period_start = _month_start()
                _ensure_usage_row(conn, str(user_id), period_start)
                usage_rows_checked += 1
                periods = [period_start]
                for item in _fetch_usage_history(conn, str(user_id), period_start):
                    try:
                        periods.append(date.fromisoformat(str(item["period_start"])))
                    except (TypeError, ValueError):
                        continue
                for item_period in periods:
                    transcription_events_created += len(
                        _backfill_transcription_events(conn, str(user_id), item_period)
                    )
                conn.commit()
                users_processed += 1
            finally:
                conn.close()

    print(f"users_processed={users_processed}")
    print(f"brands_created={brand_backfills}")
    print(f"usage_rows_checked={usage_rows_checked}")
    print(f"transcription_events_created={transcription_events_created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
