#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app import create_app
from app.video_shorts.routes import generation
from app.video_shorts.services.db import get_db_readonly, table_columns
from app.video_shorts.services.storage import get_media_storage


def _fetch_candidates(video_pk: int | None = None, user_id: str = "") -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        generated_cols = table_columns(conn, "shorts_generated_videos")
        if not generated_cols:
            return []
        fields = [
            "gv.source_video_id",
            "gv.clip_filename",
            "gv.generation_status" if "generation_status" in generated_cols else "NULL AS generation_status",
            "yv.id AS video_pk",
            "CAST(yv.owner_user_id AS VARCHAR) AS owner_user_id",
        ]
        sql = f"""
            SELECT {", ".join(fields)}
            FROM shorts_generated_videos gv
            JOIN youtube_videos yv
              ON CAST(yv.video_id AS VARCHAR) = CAST(gv.source_video_id AS VARCHAR)
            WHERE NULLIF(trim(coalesce(gv.clip_filename, '')), '') IS NOT NULL
        """
        params: list[Any] = []
        if video_pk:
            sql += " AND yv.id = ?"
            params.append(video_pk)
        if user_id:
            sql += " AND CAST(yv.owner_user_id AS VARCHAR) = ?"
            params.append(user_id)
        sql += " ORDER BY yv.id, gv.clip_filename"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        clip_filename = Path(str(row[1] or "").strip()).name
        if not clip_filename or clip_filename in seen:
            continue
        status = str(row[2] or "").strip().lower()
        if status and status not in {"created", "done", "completed", "ready"}:
            continue
        seen.add(clip_filename)
        candidates.append(
            {
                "source_video_id": str(row[0] or "").strip(),
                "clip_filename": clip_filename,
                "generation_status": status,
                "video_pk": row[3],
                "owner_user_id": str(row[4] or "").strip(),
            }
        )
    return candidates


def _poster_exists(filename: str) -> bool:
    key = generation._short_poster_storage_key(filename)
    return bool(key and get_media_storage().exists(key))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill generated Short poster JPGs.")
    parser.add_argument("--video-pk", type=int, default=0, help="Only process Shorts for this youtube_videos.id.")
    parser.add_argument("--user-id", default="", help="Only process Shorts owned by this user id.")
    parser.add_argument("--dry-run", action="store_true", help="Report work without creating posters.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum missing posters to process.")
    args = parser.parse_args()

    app = create_app()
    missing: list[dict[str, Any]] = []
    skipped_missing_short = 0
    skipped_existing_poster = 0
    created = 0
    errors = 0

    with app.app_context():
        candidates = _fetch_candidates(video_pk=args.video_pk or None, user_id=args.user_id.strip())
        for candidate in candidates:
            filename = candidate["clip_filename"]
            if not generation._short_exists(filename):
                skipped_missing_short += 1
                continue
            try:
                if _poster_exists(filename):
                    skipped_existing_poster += 1
                    continue
            except Exception as exc:
                errors += 1
                print(f"ERROR poster_check filename={filename} error={exc}")
                continue
            missing.append(candidate)

        selected = missing[: args.limit] if args.limit and args.limit > 0 else missing
        for candidate in selected:
            filename = candidate["clip_filename"]
            if args.dry_run:
                print(f"DRYRUN create filename={filename} video_pk={candidate['video_pk']}")
                continue
            try:
                if generation._ensure_shared_short_poster(filename):
                    created += 1
                    print(f"CREATED filename={filename} video_pk={candidate['video_pk']}")
                else:
                    print(f"SKIP filename={filename} reason=poster_exists_or_unavailable")
            except Exception as exc:
                errors += 1
                print(f"ERROR create filename={filename} error={exc}")

    print(f"candidate_rows={len(candidates)}")
    print(f"missing_posters={len(missing)}")
    print(f"selected={len(selected)}")
    print(f"skipped_existing_poster={skipped_existing_poster}")
    print(f"skipped_missing_short={skipped_missing_short}")
    print(f"created={created}")
    print(f"errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
