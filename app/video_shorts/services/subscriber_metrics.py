from __future__ import annotations

from typing import Iterable, Sequence, Mapping

from app.video_shorts.services.db import table_columns

SUBSCRIBER_SNAPSHOT_TABLE = "shorts_channel_subscriber_daily"

SUBSCRIBER_SNAPSHOT_COLUMNS: Sequence[str] = (
    "snapshot_date",
    "effective_at",
    "brand_id",
    "channel_type",
    "channel_id",
    "channel_name",
    "subscriber_count",
    "subscriber_count_exact",
    "subscribers_gained",
    "subscribers_lost",
    "subscribers_net",
    "subscriber_count_api_rounded",
    "stats_source",
)

SUBSCRIBER_SNAPSHOT_INSERT_SQL = f"""
INSERT INTO {SUBSCRIBER_SNAPSHOT_TABLE} ({', '.join(SUBSCRIBER_SNAPSHOT_COLUMNS)})
VALUES ({', '.join('?' for _ in SUBSCRIBER_SNAPSHOT_COLUMNS)})
ON CONFLICT (channel_type, channel_id, snapshot_date)
DO UPDATE SET
    effective_at = EXCLUDED.effective_at,
    channel_name = EXCLUDED.channel_name,
    subscriber_count = EXCLUDED.subscriber_count,
    subscriber_count_exact = EXCLUDED.subscriber_count_exact,
    subscribers_gained = EXCLUDED.subscribers_gained,
    subscribers_lost = EXCLUDED.subscribers_lost,
    subscribers_net = EXCLUDED.subscribers_net,
    subscriber_count_api_rounded = EXCLUDED.subscriber_count_api_rounded,
    stats_source = EXCLUDED.stats_source
"""


def ensure_subscriber_snapshot_table(conn) -> None:
    if getattr(conn, "backend_name", "") == "postgres":
        return
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SUBSCRIBER_SNAPSHOT_TABLE} (
                snapshot_date DATE NOT NULL,
                effective_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                brand_id VARCHAR,
                channel_type VARCHAR NOT NULL,
                channel_id VARCHAR NOT NULL,
                channel_name VARCHAR,
                subscriber_count BIGINT,
                subscriber_count_exact BIGINT,
                subscribers_gained BIGINT,
                subscribers_lost BIGINT,
                subscribers_net BIGINT,
                subscriber_count_api_rounded BIGINT,
                stats_source VARCHAR,
                PRIMARY KEY (channel_type, channel_id, snapshot_date)
            )
            """
        )
        existing_columns = table_columns(conn, SUBSCRIBER_SNAPSHOT_TABLE)
        new_columns = [
            ("brand_id", "VARCHAR"),
            ("subscriber_count_exact", "BIGINT"),
            ("subscribers_gained", "BIGINT"),
            ("subscribers_lost", "BIGINT"),
            ("subscribers_net", "BIGINT"),
            ("subscriber_count_api_rounded", "BIGINT"),
        ]
        for column, col_type in new_columns:
            if column not in existing_columns:
                conn.execute(
                    f"ALTER TABLE {SUBSCRIBER_SNAPSHOT_TABLE} ADD COLUMN {column} {col_type}"
                )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{SUBSCRIBER_SNAPSHOT_TABLE}_date ON {SUBSCRIBER_SNAPSHOT_TABLE}(snapshot_date)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{SUBSCRIBER_SNAPSHOT_TABLE}_channel ON {SUBSCRIBER_SNAPSHOT_TABLE}(channel_type)"
        )
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{SUBSCRIBER_SNAPSHOT_TABLE}_brand ON {SUBSCRIBER_SNAPSHOT_TABLE}(brand_id, snapshot_date)"
            )
        except Exception:
            pass
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return
        raise


def ensure_youtube_channel_baseline_columns(conn) -> None:
    try:
        columns = table_columns(conn, "youtube_channels")
    except Exception as exc:
        if "youtube_channels" in str(exc).lower():
            return
        raise
    updates = [
        ("baseline_subscribers_exact", "BIGINT"),
        ("baseline_date", "DATE"),
    ]
    for column, col_type in updates:
        if column in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE youtube_channels ADD COLUMN {column} {col_type}")
        except Exception:
            pass


def fetch_youtube_channel_baseline(conn, channel_id: str):
    row = conn.execute(
        """
        SELECT baseline_date, baseline_subscribers_exact
        FROM youtube_channels
        WHERE youtube_channel_id = ?
        LIMIT 1
        """,
        [channel_id],
    ).fetchone()
    if not row:
        return None
    return row[0], row[1]


def insert_subscriber_snapshot(
    conn,
    records: Iterable[Mapping[str, object]],
) -> int:
    params = [tuple(record.get(col) for col in SUBSCRIBER_SNAPSHOT_COLUMNS) for record in records]
    if not params:
        return 0
    conn.executemany(SUBSCRIBER_SNAPSHOT_INSERT_SQL, params)
    return len(params)
