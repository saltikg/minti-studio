from __future__ import annotations

from datetime import date
from typing import Iterable, List, Sequence

SNAPSHOT_TABLE = "shorts_video_daily_snapshots"
LEGACY_SNAPSHOT_TABLE = "shorts_video_daily_metrics"
ANALYTICS_ARCHIVE_TABLE = "shorts_video_daily_analytics_archive"
LEGACY_ANALYTICS_TABLE = "shorts_video_daily_analytics"

SNAPSHOT_COLUMNS: Sequence[str] = (
    "snapshot_date",
    "effective_at",
    "brand_id",
    "channel_type",
    "video_id",
    "channel_id",
    "channel_name",
    "video_title",
    "impressions",
    "views",
    "comments",
    "likes",
    "shares",
    "reach",
    "saved",
    "stats_source",
)

SNAPSHOT_INSERT_SQL = f"""
INSERT INTO {SNAPSHOT_TABLE} ({', '.join(SNAPSHOT_COLUMNS)})
VALUES ({', '.join('?' for _ in SNAPSHOT_COLUMNS)})
ON CONFLICT (channel_type, video_id, snapshot_date)
DO UPDATE SET
    effective_at = EXCLUDED.effective_at,
    brand_id = EXCLUDED.brand_id,
    channel_id = EXCLUDED.channel_id,
    channel_name = EXCLUDED.channel_name,
    video_title = EXCLUDED.video_title,
    impressions = EXCLUDED.impressions,
    views = EXCLUDED.views,
    comments = EXCLUDED.comments,
    likes = EXCLUDED.likes,
    shares = EXCLUDED.shares,
    reach = EXCLUDED.reach,
    saved = EXCLUDED.saved,
    stats_source = EXCLUDED.stats_source
"""


def _table_exists(conn, table_name: str) -> bool:
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_name = ?
            LIMIT 1
            """,
            [table_name],
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _rename_table_if_needed(conn, *, old_name: str, new_name: str) -> None:
    if old_name == new_name:
        return
    if _table_exists(conn, new_name) or not _table_exists(conn, old_name):
        return
    conn.execute(f"ALTER TABLE {old_name} RENAME TO {new_name}")


def ensure_snapshot_table(conn) -> None:
    try:
        _rename_table_if_needed(conn, old_name=LEGACY_SNAPSHOT_TABLE, new_name=SNAPSHOT_TABLE)
        _rename_table_if_needed(
            conn,
            old_name=LEGACY_ANALYTICS_TABLE,
            new_name=ANALYTICS_ARCHIVE_TABLE,
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
                snapshot_date DATE NOT NULL,
                effective_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                brand_id VARCHAR,
                channel_type VARCHAR NOT NULL,
                video_id VARCHAR NOT NULL,
                channel_id VARCHAR,
                channel_name VARCHAR,
                video_title VARCHAR,
                impressions BIGINT,
                views BIGINT,
                comments BIGINT,
                likes BIGINT,
                shares BIGINT,
                reach BIGINT,
                saved BIGINT,
                stats_source VARCHAR,
                PRIMARY KEY (channel_type, video_id, snapshot_date)
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{SNAPSHOT_TABLE}_date ON {SNAPSHOT_TABLE}(snapshot_date)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{SNAPSHOT_TABLE}_channel ON {SNAPSHOT_TABLE}(channel_type)"
        )
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{SNAPSHOT_TABLE}_brand ON {SNAPSHOT_TABLE}(brand_id, snapshot_date)"
            )
        except Exception:
            pass
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise
