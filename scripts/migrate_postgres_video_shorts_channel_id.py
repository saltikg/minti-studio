#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys

import psycopg


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely normalize main.youtube_videos.channel_id to BIGINT in PostgreSQL."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration. Without this flag the script performs a dry run.",
    )
    args = parser.parse_args()

    database_url = (
        os.getenv("VIDEO_SHORTS_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not database_url:
        raise SystemExit("VIDEO_SHORTS_DATABASE_URL or DATABASE_URL is required")

    conn = psycopg.connect(database_url, options="-c search_path=main,public")
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema = 'main'
                  AND table_name = 'youtube_videos'
                  AND column_name = 'channel_id'
                """
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("main.youtube_videos.channel_id not found")
            current_type = row[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM main.youtube_videos
                WHERE channel_id IS NOT NULL
                  AND btrim(channel_id) !~ '^[0-9]+$'
                """
            )
            non_numeric = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM main.youtube_videos v
                LEFT JOIN main.youtube_channels c
                  ON CAST(v.channel_id AS BIGINT) = c.channel_id
                WHERE v.channel_id IS NOT NULL
                  AND btrim(v.channel_id) ~ '^[0-9]+$'
                  AND c.channel_id IS NULL
                """
            )
            orphan_refs = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM main.youtube_videos
                WHERE channel_id IS NULL OR btrim(channel_id) = ''
                """
            )
            empty_count = cur.fetchone()[0]

            print("Postgres channel_id migration")
            print(f"current_type={current_type}")
            print(f"non_numeric_rows={non_numeric}")
            print(f"empty_rows={empty_count}")
            print(f"orphan_channel_refs={orphan_refs}")

            if current_type == "bigint":
                print("already_bigint=yes")
                conn.rollback()
                return 0

            if non_numeric:
                raise RuntimeError(
                    f"Refusing migration: youtube_videos.channel_id has {non_numeric} non-numeric rows"
                )

            if not args.apply:
                print("dry_run=yes")
                conn.rollback()
                return 0

            cur.execute(
                """
                ALTER TABLE main.youtube_videos
                ALTER COLUMN channel_id TYPE BIGINT
                USING NULLIF(btrim(channel_id), '')::BIGINT
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_youtube_videos_channel_id
                ON main.youtube_videos(channel_id)
                """
            )
            conn.commit()

            cur.execute(
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema = 'main'
                  AND table_name = 'youtube_videos'
                  AND column_name = 'channel_id'
                """
            )
            new_type = cur.fetchone()[0]
            print("applied=yes")
            print(f"new_type={new_type}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
