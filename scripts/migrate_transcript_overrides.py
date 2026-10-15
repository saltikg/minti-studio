#!/usr/bin/env python3
"""Manually create tenant transcript override storage; never copy shared edits."""
import argparse

from safe_to_deploy import _connect, _load_env_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    _load_env_file()
    with _connect() as conn, conn.cursor() as cur:
        if args.apply:
            cur.execute("""CREATE TABLE IF NOT EXISTS shorts_transcript_overrides (
                video_pk BIGINT PRIMARY KEY REFERENCES youtube_videos(id) ON DELETE CASCADE,
                full_text TEXT NOT NULL, segments_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        else:
            cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT to_regclass('main.shorts_transcript_overrides')")
        exists = bool(cur.fetchone()[0])
        count = 0
        if exists:
            cur.execute("SELECT count(*) FROM shorts_transcript_overrides")
            count = cur.fetchone()[0]
        print(f"{'APPLIED' if args.apply else 'READ_ONLY'} table_exists={exists} overrides={count} migrated=0")


if __name__ == "__main__":
    main()
