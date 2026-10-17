#!/usr/bin/env python3
"""Sequentially create posters for lifecycle rows whose DB asset is missing."""
from __future__ import annotations

import argparse
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
from app.video_shorts.services import tenant_media
from app.video_shorts.services.db import get_db, get_db_readonly


def _fetch_candidates(video_pk: int | None = None, user_id: str = "") -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        sql = """
            SELECT gv.id, gv.source_video_id, gv.clip_filename, yv.id,
                   CAST(gv.user_id AS VARCHAR), gv.brand_id
            FROM shorts_generated_videos gv
            JOIN youtube_videos yv
              ON CAST(yv.video_id AS VARCHAR) = CAST(gv.source_video_id AS VARCHAR)
             AND CAST(yv.owner_user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
             AND yv.brand_id IS NOT DISTINCT FROM gv.brand_id
            JOIN shorts_storage_assets clip_asset
              ON clip_asset.file_key = gv.storage_file_key
             AND CAST(clip_asset.user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
             AND clip_asset.brand_id IS NOT DISTINCT FROM gv.brand_id
             AND lower(coalesce(clip_asset.status, 'active')) = 'active'
             AND clip_asset.size_bytes > 0
            LEFT JOIN shorts_storage_assets poster_asset
              ON poster_asset.file_key = 'shorts/posters/' ||
                   NULLIF(trim(gv.raw_plan_entry_json->>'poster_filename'), '')
             AND CAST(poster_asset.user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
             AND poster_asset.brand_id IS NOT DISTINCT FROM gv.brand_id
             AND lower(coalesce(poster_asset.status, 'active')) = 'active'
             AND poster_asset.size_bytes > 0
            WHERE NULLIF(trim(coalesce(gv.clip_filename, '')), '') IS NOT NULL
              AND lower(coalesce(gv.generation_status, 'created')) NOT IN
                  ('pending', 'queued', 'processing', 'rendering', 'failed')
              AND poster_asset.file_key IS NULL
        """
        params: list[Any] = []
        if video_pk:
            sql += " AND yv.id = ?"
            params.append(video_pk)
        if user_id:
            sql += " AND CAST(gv.user_id AS VARCHAR) = ?"
            params.append(user_id)
        sql += " ORDER BY gv.created_at, gv.id"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        {
            "generated_id": row[0],
            "source_video_id": str(row[1] or ""),
            "clip_filename": Path(str(row[2] or "")).name,
            "video_pk": int(row[3]),
            "owner_user_id": str(row[4] or ""),
            "brand_id": str(row[5]) if row[5] else None,
        }
        for row in rows
        if row[2] and row[4]
    ]


def _record_poster(candidate: dict[str, Any], poster_filename: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE shorts_generated_videos
            SET raw_plan_entry_json = jsonb_set(
                    COALESCE(raw_plan_entry_json, '{}'::jsonb),
                    '{poster_filename}',
                    to_jsonb(CAST(? AS TEXT)),
                    true
                ),
                updated_at = now()
            WHERE id = ?
              AND CAST(user_id AS VARCHAR) = ?
              AND brand_id IS NOT DISTINCT FROM ?
            """,
            [poster_filename, candidate["generated_id"],
             candidate["owner_user_id"], candidate["brand_id"]],
        )
        conn.commit()
    finally:
        conn.close()

    video = tenant_media.resolve_video(
        video_pk=candidate["video_pk"],
        owner_user_id=candidate["owner_user_id"],
        brand_id=candidate["brand_id"],
    )
    state = tenant_media.load_state(video)
    changed = False
    for field in ("plan", "clips"):
        for entry in state.get(field) or []:
            if candidate["clip_filename"] in (
                entry.get("clip_filename"), entry.get("output_filename")
            ):
                entry["poster_filename"] = poster_filename
                changed = True
    if changed:
        tenant_media.write_state(video, state)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill generated Short poster JPGs sequentially."
    )
    parser.add_argument("--video-pk", type=int, default=0)
    parser.add_argument("--user-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    app = create_app()
    created = 0
    errors = 0
    with app.app_context():
        candidates = _fetch_candidates(args.video_pk or None, args.user_id.strip())
        selected = candidates[: args.limit] if args.limit > 0 else candidates
        for candidate in selected:
            filename = candidate["clip_filename"]
            poster_filename = f"{Path(filename).stem}-poster.jpg"
            if args.dry_run:
                print(f"DRYRUN filename={filename} video_pk={candidate['video_pk']}")
                continue
            try:
                made = generation._ensure_shared_short_poster(
                    filename,
                    overwrite=True,
                    poster_filename=poster_filename,
                    owner_user_id=candidate["owner_user_id"],
                    brand_id=candidate["brand_id"],
                    register_asset=True,
                )
                if not made:
                    raise RuntimeError("poster extraction returned no output")
                _record_poster(candidate, poster_filename)
                created += 1
                print(f"CREATED filename={filename} video_pk={candidate['video_pk']}")
            except Exception as exc:
                errors += 1
                print(f"ERROR filename={filename} error={exc}")

    print(f"missing_posters={len(candidates)}")
    print(f"selected={len(selected)}")
    print(f"created={created}")
    print(f"errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
