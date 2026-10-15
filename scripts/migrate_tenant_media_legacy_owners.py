#!/usr/bin/env python3
"""Manually freeze legacy plan owners on production before tenant-media cutover.

Default: read-only report. --apply creates the registry and inserts missing
decisions. Existing decisions are immutable; no media or plan files are changed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from safe_to_deploy import _connect, _load_env_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    _load_env_file()
    shorts = Path(__file__).resolve().parents[1] / "app/video_shorts/static/shorts"
    with _connect() as conn:
        with conn.cursor() as cur:
            if args.apply:
                cur.execute("LOCK TABLE youtube_videos IN SHARE ROW EXCLUSIVE MODE")
                cur.execute("""CREATE TABLE IF NOT EXISTS shorts_video_legacy_owners (
                    youtube_id VARCHAR PRIMARY KEY, video_pk BIGINT NOT NULL,
                    ownership_rule VARCHAR NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            else:
                cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT video_id, MIN(id) FROM youtube_videos GROUP BY video_id")
            sources = cur.fetchall()
            count = 0
            for youtube_id, earliest_pk in sources:
                owner_pk, rule = earliest_pk, "earliest_video_row"
                path = shorts / f"{youtube_id}_plan.json"
                names = []
                if path.exists():
                    data = json.loads(path.read_text())
                    names = sorted({str(e[k]) for e in data.get("plan", data.get("clips", []))
                                    if isinstance(e, dict) for k in ("clip_filename", "output_filename") if e.get(k)})
                if names:
                    # Use the earliest recorded render among filenames actually
                    # present in the legacy plan, rather than its latest job IDs.
                    cur.execute("""SELECT v.id FROM shorts_generated_videos s
                        JOIN youtube_videos v ON v.video_id = s.source_video_id
                         AND v.owner_user_id = s.user_id
                         AND v.brand_id IS NOT DISTINCT FROM s.brand_id
                        WHERE v.video_id = %s AND
                         (s.clip_filename = ANY(%s) OR s.output_filename = ANY(%s))
                        ORDER BY s.created_at NULLS LAST, s.id, v.id LIMIT 1""", [youtube_id, names, names])
                    row = cur.fetchone()
                    if row:
                        owner_pk, rule = row[0], "earliest_legacy_plan_render"
                if args.apply:
                    cur.execute("""INSERT INTO shorts_video_legacy_owners (youtube_id, video_pk, ownership_rule)
                        VALUES (%s, %s, %s) ON CONFLICT (youtube_id) DO NOTHING""", [youtube_id, owner_pk, rule])
                if path.exists():
                    print(f"{youtube_id} video_pk={owner_pk} rule={rule}")
                count += 1
            print(f"{'APPLIED' if args.apply else 'READ_ONLY'} sources={count}")


if __name__ == "__main__":
    main()
