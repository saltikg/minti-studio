#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional
from uuid import uuid4

import psycopg

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.video_shorts.config import VIDEO_SHORTS_DATABASE_URL
from app.video_shorts.services.video_metrics import ANALYTICS_ARCHIVE_TABLE, SNAPSHOT_TABLE


TARGET_BRAND_NAME = "Hocaefendiden Kisa Kisa"
TARGET_BRAND_SLUG = "hocaefendiden-kisa-kisa"


@dataclass
class MigrationContext:
    conn: psycopg.Connection
    target_user_id: str
    brand_id: str
    brand_name: str


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name = %s
        LIMIT 1
        """,
        [table_name],
    )
    return cur.fetchone() is not None


def column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
        """,
        [table_name, column_name],
    )
    return cur.fetchone() is not None


def ensure_column(cur, table_name: str, column_name: str, sql_type: str) -> None:
    if not table_exists(cur, table_name):
        return
    if column_exists(cur, table_name, column_name):
        return
    cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type}")


def ensure_brand_schema(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shorts_brands (
            id VARCHAR PRIMARY KEY,
            owner_user_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            slug VARCHAR,
            is_default BOOLEAN DEFAULT false,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_shorts_brands_owner
        ON shorts_brands(owner_user_id)
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shorts_brands_owner_name
        ON shorts_brands(owner_user_id, lower(name))
        """
    )
    ensure_column(cur, "shorts_users", "last_brand_id", "VARCHAR")
    for table_name in (
        "youtube_channels",
        "youtube_videos",
        "shorts_categories",
        "shorts_static_images",
        "shorts_static_image_categories",
        "image_to_video_jobs",
        "shorts_instagram_queue",
        "shorts_facebook_queue",
        "shorts_tiktok_queue",
        SNAPSHOT_TABLE,
        "shorts_channel_subscriber_daily",
        ANALYTICS_ARCHIVE_TABLE,
    ):
        ensure_column(cur, table_name, "brand_id", "VARCHAR")


def infer_target_user_id(cur, explicit_user_id: Optional[str]) -> str:
    if explicit_user_id:
        return explicit_user_id
    cur.execute(
        """
        WITH scored AS (
            SELECT owner_user_id AS user_id, COUNT(*) * 10 AS score
            FROM youtube_videos
            WHERE owner_user_id IS NOT NULL
            GROUP BY owner_user_id
            UNION ALL
            SELECT owner_user_id AS user_id, COUNT(*) * 3 AS score
            FROM youtube_channels
            WHERE owner_user_id IS NOT NULL
            GROUP BY owner_user_id
            UNION ALL
            SELECT user_id, COUNT(*) AS score
            FROM shorts_instagram_queue
            WHERE user_id IS NOT NULL
            GROUP BY user_id
            UNION ALL
            SELECT user_id, COUNT(*) AS score
            FROM shorts_facebook_queue
            WHERE user_id IS NOT NULL
            GROUP BY user_id
            UNION ALL
            SELECT user_id, COUNT(*) AS score
            FROM shorts_tiktok_queue
            WHERE user_id IS NOT NULL
            GROUP BY user_id
            UNION ALL
            SELECT user_id, COUNT(*) AS score
            FROM shorts_static_images
            WHERE user_id IS NOT NULL
            GROUP BY user_id
        )
        SELECT user_id
        FROM scored
        WHERE user_id IS NOT NULL
          AND position('::' in CAST(user_id AS text)) = 0
        GROUP BY user_id
        ORDER BY SUM(score) DESC, user_id
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError("No target user could be inferred from existing data.")
    return str(row[0])


def ensure_brand(cur, user_id: str, brand_name: str) -> str:
    cur.execute(
        """
        SELECT id
        FROM shorts_brands
        WHERE owner_user_id = %s
          AND lower(name) = lower(%s)
        LIMIT 1
        """,
        [user_id, brand_name],
    )
    row = cur.fetchone()
    if row and row[0]:
        brand_id = str(row[0])
        cur.execute(
            """
            UPDATE shorts_brands
            SET name = %s,
                slug = %s,
                is_default = true,
                updated_at = now()
            WHERE id = %s
            """,
            [brand_name, TARGET_BRAND_SLUG, brand_id],
        )
        cur.execute(
            """
            UPDATE shorts_brands
            SET is_default = false,
                updated_at = now()
            WHERE owner_user_id = %s
              AND id <> %s
            """,
            [user_id, brand_id],
        )
        return brand_id

    cur.execute(
        """
        SELECT id
        FROM shorts_brands
        WHERE owner_user_id = %s
        ORDER BY COALESCE(is_default, false) DESC, created_at ASC, id ASC
        LIMIT 1
        """,
        [user_id],
    )
    row = cur.fetchone()
    if row and row[0]:
        brand_id = str(row[0])
        cur.execute(
            """
            UPDATE shorts_brands
            SET name = %s,
                slug = %s,
                is_default = true,
                updated_at = now()
            WHERE id = %s
            """,
            [brand_name, TARGET_BRAND_SLUG, brand_id],
        )
        cur.execute(
            """
            UPDATE shorts_brands
            SET is_default = false,
                updated_at = now()
            WHERE owner_user_id = %s
              AND id <> %s
            """,
            [user_id, brand_id],
        )
        return brand_id

    brand_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO shorts_brands (id, owner_user_id, name, slug, is_default, created_at, updated_at)
        VALUES (%s, %s, %s, %s, true, now(), now())
        """,
        [brand_id, user_id, brand_name, TARGET_BRAND_SLUG],
    )
    return brand_id


def update_and_count(cur, sql: str, params: Iterable[object]) -> int:
    cur.execute(sql, list(params))
    return cur.rowcount or 0


def backfill_owner_columns(ctx: MigrationContext) -> Dict[str, int]:
    cur = ctx.conn.cursor()
    counts: Dict[str, int] = {}
    counts["youtube_channels.owner_user_id"] = update_and_count(
        cur,
        """
        UPDATE youtube_channels
        SET owner_user_id = %s
        WHERE owner_user_id IS NULL
        """,
        [ctx.target_user_id],
    ) if table_exists(cur, "youtube_channels") else 0
    counts["youtube_videos.owner_user_id"] = update_and_count(
        cur,
        """
        UPDATE youtube_videos
        SET owner_user_id = %s
        WHERE owner_user_id IS NULL
        """,
        [ctx.target_user_id],
    ) if table_exists(cur, "youtube_videos") else 0
    cur.close()
    return counts


def backfill_brand_columns(ctx: MigrationContext) -> Dict[str, int]:
    cur = ctx.conn.cursor()
    counts: Dict[str, int] = {}
    mapping = (
        ("youtube_channels", "owner_user_id"),
        ("youtube_videos", "owner_user_id"),
        ("shorts_categories", "user_id"),
        ("shorts_static_images", "user_id"),
        ("shorts_static_image_categories", "user_id"),
        ("image_to_video_jobs", "user_id"),
        ("shorts_instagram_queue", "user_id"),
        ("shorts_facebook_queue", "user_id"),
        ("shorts_tiktok_queue", "user_id"),
    )
    for table_name, owner_col in mapping:
        if not table_exists(cur, table_name):
            continue
        counts[f"{table_name}.brand_id"] = update_and_count(
            cur,
            f"""
            UPDATE {table_name}
            SET brand_id = %s
            WHERE {owner_col} = %s
              AND (brand_id IS NULL OR btrim(CAST(brand_id AS text)) = '')
            """,
            [ctx.brand_id, ctx.target_user_id],
        )
    cur.execute(
        """
        UPDATE shorts_users
        SET last_brand_id = %s,
            updated_at = now()
        WHERE id = %s
        """,
        [ctx.brand_id, ctx.target_user_id],
    )
    counts["shorts_users.last_brand_id"] = cur.rowcount or 0
    cur.close()
    return counts


def scope_user_ids(ctx: MigrationContext) -> Dict[str, int]:
    cur = ctx.conn.cursor()
    scoped_user_id = f"{ctx.target_user_id}::{ctx.brand_id}"
    counts: Dict[str, int] = {}
    queue_tables = (
        "shorts_instagram_queue",
        "shorts_facebook_queue",
        "shorts_tiktok_queue",
    )
    for table_name in queue_tables:
        if not table_exists(cur, table_name):
            continue
        counts[f"{table_name}.user_id"] = update_and_count(
            cur,
            f"""
            UPDATE {table_name}
            SET user_id = %s
            WHERE user_id = %s
              AND brand_id = %s
              AND position('::' in CAST(user_id AS text)) = 0
            """,
            [scoped_user_id, ctx.target_user_id, ctx.brand_id],
        )
    token_tables = (
        "youtube_oauth_tokens_v2",
        "instagram_oauth_tokens",
        "instagram_oauth_pending",
        "facebook_page_tokens",
        "tiktok_oauth_tokens",
    )
    for table_name in token_tables:
        if not table_exists(cur, table_name) or not column_exists(cur, table_name, "user_id"):
            continue
        counts[f"{table_name}.user_id"] = update_and_count(
            cur,
            f"""
            UPDATE {table_name}
            SET user_id = %s
            WHERE user_id = %s
              AND position('::' in CAST(user_id AS text)) = 0
            """,
            [scoped_user_id, ctx.target_user_id],
        )
    cur.close()
    return counts


def backfill_analytics(ctx: MigrationContext) -> Dict[str, int]:
    cur = ctx.conn.cursor()
    counts: Dict[str, int] = {}

    def run(name: str, sql: str, params: Iterable[object]) -> None:
        counts[name] = update_and_count(cur, sql, params)

    if table_exists(cur, SNAPSHOT_TABLE):
        run(
            f"{SNAPSHOT_TABLE}.youtube",
            f"""
            UPDATE {SNAPSHOT_TABLE} m
            SET brand_id = v.brand_id
            FROM youtube_videos v
            WHERE m.channel_type = 'youtube'
              AND m.video_id = v.video_id
              AND v.owner_user_id = %s
              AND v.brand_id IS NOT NULL
              AND (m.brand_id IS NULL OR btrim(CAST(m.brand_id AS text)) = '')
            """,
            [ctx.target_user_id],
        )
        if table_exists(cur, "shorts_instagram_queue"):
            run(
                f"{SNAPSHOT_TABLE}.instagram",
                f"""
                UPDATE {SNAPSHOT_TABLE} m
                SET brand_id = q.brand_id
                FROM shorts_instagram_queue q
                WHERE m.channel_type = 'instagram'
                  AND m.video_id = q.instagram_media_id
                  AND q.brand_id IS NOT NULL
                  AND (m.brand_id IS NULL OR btrim(CAST(m.brand_id AS text)) = '')
                """,
                [],
            )
        if table_exists(cur, "shorts_facebook_queue"):
            run(
                f"{SNAPSHOT_TABLE}.facebook",
                f"""
                UPDATE {SNAPSHOT_TABLE} m
                SET brand_id = q.brand_id
                FROM shorts_facebook_queue q
                WHERE m.channel_type = 'facebook'
                  AND m.video_id = q.facebook_video_id
                  AND q.brand_id IS NOT NULL
                  AND (m.brand_id IS NULL OR btrim(CAST(m.brand_id AS text)) = '')
                """,
                [],
            )
        if table_exists(cur, "shorts_tiktok_queue"):
            run(
                f"{SNAPSHOT_TABLE}.tiktok",
                f"""
                UPDATE {SNAPSHOT_TABLE} m
                SET brand_id = q.brand_id
                FROM shorts_tiktok_queue q
                WHERE m.channel_type = 'tiktok'
                  AND m.video_id = q.tiktok_video_id
                  AND q.brand_id IS NOT NULL
                  AND (m.brand_id IS NULL OR btrim(CAST(m.brand_id AS text)) = '')
                """,
                [],
            )

    if table_exists(cur, ANALYTICS_ARCHIVE_TABLE):
        run(
            f"{ANALYTICS_ARCHIVE_TABLE}.youtube",
            f"""
            UPDATE {ANALYTICS_ARCHIVE_TABLE} a
            SET brand_id = v.brand_id
            FROM youtube_videos v
            WHERE a.channel_type = 'youtube'
              AND a.video_id = v.video_id
              AND v.owner_user_id = %s
              AND v.brand_id IS NOT NULL
              AND (a.brand_id IS NULL OR btrim(CAST(a.brand_id AS text)) = '')
            """,
            [ctx.target_user_id],
        )
        if table_exists(cur, "shorts_instagram_queue"):
            run(
                f"{ANALYTICS_ARCHIVE_TABLE}.instagram",
                f"""
                UPDATE {ANALYTICS_ARCHIVE_TABLE} a
                SET brand_id = q.brand_id
                FROM shorts_instagram_queue q
                WHERE a.channel_type = 'instagram'
                  AND a.video_id = q.instagram_media_id
                  AND q.brand_id IS NOT NULL
                  AND (a.brand_id IS NULL OR btrim(CAST(a.brand_id AS text)) = '')
                """,
                [],
            )
        if table_exists(cur, "shorts_facebook_queue"):
            run(
                f"{ANALYTICS_ARCHIVE_TABLE}.facebook",
                f"""
                UPDATE {ANALYTICS_ARCHIVE_TABLE} a
                SET brand_id = q.brand_id
                FROM shorts_facebook_queue q
                WHERE a.channel_type = 'facebook'
                  AND a.video_id = q.facebook_video_id
                  AND q.brand_id IS NOT NULL
                  AND (a.brand_id IS NULL OR btrim(CAST(a.brand_id AS text)) = '')
                """,
                [],
            )
        if table_exists(cur, "shorts_tiktok_queue"):
            run(
                f"{ANALYTICS_ARCHIVE_TABLE}.tiktok",
                f"""
                UPDATE {ANALYTICS_ARCHIVE_TABLE} a
                SET brand_id = q.brand_id
                FROM shorts_tiktok_queue q
                WHERE a.channel_type = 'tiktok'
                  AND a.video_id = q.tiktok_video_id
                  AND q.brand_id IS NOT NULL
                  AND (a.brand_id IS NULL OR btrim(CAST(a.brand_id AS text)) = '')
                """,
                [],
            )

    if table_exists(cur, "shorts_channel_subscriber_daily"):
        run(
            "shorts_channel_subscriber_daily.youtube",
            """
            UPDATE shorts_channel_subscriber_daily s
            SET brand_id = c.brand_id
            FROM youtube_channels c
            WHERE s.channel_type = 'youtube'
              AND CAST(s.channel_id AS text) = CAST(c.youtube_channel_id AS text)
              AND c.owner_user_id = %s
              AND c.brand_id IS NOT NULL
              AND (s.brand_id IS NULL OR btrim(CAST(s.brand_id AS text)) = '')
            """,
            [ctx.target_user_id],
        )
        if table_exists(cur, "instagram_oauth_tokens"):
            run(
                "shorts_channel_subscriber_daily.instagram",
                """
                UPDATE shorts_channel_subscriber_daily s
                SET brand_id = split_part(CAST(t.user_id AS text), '::', 2)
                FROM instagram_oauth_tokens t
                WHERE s.channel_type = 'instagram'
                  AND CAST(s.channel_id AS text) = CAST(t.instagram_business_account_id AS text)
                  AND position('::' in CAST(t.user_id AS text)) > 0
                  AND (s.brand_id IS NULL OR btrim(CAST(s.brand_id AS text)) = '')
                """,
                [],
            )
        if table_exists(cur, "facebook_page_tokens"):
            run(
                "shorts_channel_subscriber_daily.facebook",
                """
                UPDATE shorts_channel_subscriber_daily s
                SET brand_id = split_part(CAST(t.user_id AS text), '::', 2)
                FROM facebook_page_tokens t
                WHERE s.channel_type = 'facebook'
                  AND CAST(s.channel_id AS text) = CAST(t.page_id AS text)
                  AND position('::' in CAST(t.user_id AS text)) > 0
                  AND (s.brand_id IS NULL OR btrim(CAST(s.brand_id AS text)) = '')
                """,
                [],
            )
        if table_exists(cur, "tiktok_oauth_tokens"):
            run(
                "shorts_channel_subscriber_daily.tiktok",
                """
                UPDATE shorts_channel_subscriber_daily s
                SET brand_id = split_part(CAST(t.user_id AS text), '::', 2)
                FROM tiktok_oauth_tokens t
                WHERE s.channel_type = 'tiktok'
                  AND CAST(s.channel_id AS text) = CAST(t.open_id AS text)
                  AND position('::' in CAST(t.user_id AS text)) > 0
                  AND (s.brand_id IS NULL OR btrim(CAST(s.brand_id AS text)) = '')
                """,
                [],
            )
    cur.close()
    return counts


def fetch_summary(ctx: MigrationContext) -> Dict[str, int]:
    cur = ctx.conn.cursor()
    summary: Dict[str, int] = {}
    queries = {
        "youtube_channels": "SELECT COUNT(*) FROM youtube_channels WHERE owner_user_id = %s AND brand_id = %s",
        "youtube_videos": "SELECT COUNT(*) FROM youtube_videos WHERE owner_user_id = %s AND brand_id = %s",
        "shorts_instagram_queue": "SELECT COUNT(*) FROM shorts_instagram_queue WHERE brand_id = %s",
        "shorts_facebook_queue": "SELECT COUNT(*) FROM shorts_facebook_queue WHERE brand_id = %s",
        "shorts_tiktok_queue": "SELECT COUNT(*) FROM shorts_tiktok_queue WHERE brand_id = %s",
        "shorts_static_images": "SELECT COUNT(*) FROM shorts_static_images WHERE user_id = %s AND brand_id = %s",
        "shorts_static_image_categories": "SELECT COUNT(*) FROM shorts_static_image_categories WHERE user_id = %s AND brand_id = %s",
        "image_to_video_jobs": "SELECT COUNT(*) FROM image_to_video_jobs WHERE user_id = %s AND brand_id = %s",
        SNAPSHOT_TABLE: f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE} WHERE brand_id = %s",
        ANALYTICS_ARCHIVE_TABLE: f"SELECT COUNT(*) FROM {ANALYTICS_ARCHIVE_TABLE} WHERE brand_id = %s",
        "shorts_channel_subscriber_daily": "SELECT COUNT(*) FROM shorts_channel_subscriber_daily WHERE brand_id = %s",
    }
    for key, sql in queries.items():
        if not table_exists(cur, key):
            continue
        params = [ctx.target_user_id, ctx.brand_id] if sql.count("%s") == 2 else [ctx.brand_id]
        cur.execute(sql, params)
        row = cur.fetchone()
        summary[key] = int(row[0] or 0) if row else 0
    cur.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Minti Studio records into a brand workspace.")
    parser.add_argument("--user-id", dest="user_id", help="Target shorts_users.id to migrate.")
    parser.add_argument("--brand-name", dest="brand_name", default=TARGET_BRAND_NAME)
    args = parser.parse_args()

    if not VIDEO_SHORTS_DATABASE_URL:
        raise RuntimeError("VIDEO_SHORTS_DATABASE_URL is not configured.")

    conn = psycopg.connect(VIDEO_SHORTS_DATABASE_URL, options="-c search_path=main,public")
    try:
        with conn.cursor() as cur:
            ensure_brand_schema(cur)
            target_user_id = infer_target_user_id(cur, args.user_id)
            brand_id = ensure_brand(cur, target_user_id, args.brand_name)
        ctx = MigrationContext(
            conn=conn,
            target_user_id=target_user_id,
            brand_id=brand_id,
            brand_name=args.brand_name,
        )
        owner_counts = backfill_owner_columns(ctx)
        brand_counts = backfill_brand_columns(ctx)
        scoped_counts = scope_user_ids(ctx)
        analytics_counts = backfill_analytics(ctx)
        summary = fetch_summary(ctx)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"target_user_id={target_user_id}")
    print(f"brand_id={brand_id}")
    print(f"brand_name={args.brand_name}")
    for section_name, section in (
        ("owner_backfill", owner_counts),
        ("brand_backfill", brand_counts),
        ("scoped_user_ids", scoped_counts),
        ("analytics_backfill", analytics_counts),
        ("summary", summary),
    ):
        print(f"[{section_name}]")
        for key, value in sorted(section.items()):
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
