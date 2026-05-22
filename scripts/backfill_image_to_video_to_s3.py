#!/usr/bin/env python3
import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.video_shorts.config import VIDEOS_DIR
from app.video_shorts.services.db import ensure_image_to_video_jobs_schema, get_db, get_db_readonly
from app.video_shorts.services.storage import build_storage_reference, get_media_storage


JOB_RE = re.compile(r"^image_to_video_(?P<job_id>[a-f0-9-]{36})\.(?P<ext>mp4|mov|mkv)$", re.IGNORECASE)


def _load_job_map() -> Dict[str, Tuple[str, str]]:
    conn = get_db_readonly()
    try:
        try:
            rows = conn.execute(
                """
                SELECT job_id, CAST(user_id AS VARCHAR), COALESCE(output_url, '')
                FROM image_to_video_jobs
                """
            ).fetchall()
        except Exception:
            return {}
    finally:
        conn.close()
    return {str(job_id): (str(user_id or "").strip(), str(output_url or "").strip()) for job_id, user_id, output_url in rows}


def _db_update_output(job_id: str, output_url: str, dry_run: bool) -> None:
    if dry_run:
        print(f"DRYRUN db_update job_id={job_id} output_url={output_url}")
        return
    conn = get_db()
    try:
        ensure_image_to_video_jobs_schema(conn)
        conn.execute(
            """
            UPDATE image_to_video_jobs
            SET output_url = ?, updated_at = now()
            WHERE job_id = ?
            """,
            [output_url, job_id],
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill local image_to_video outputs to S3.")
    parser.add_argument("--dry-run", action="store_true", help="List actions without uploading or updating DB.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of files to process.")
    parser.add_argument(
        "--update-db-markers",
        action="store_true",
        help="Update local output_url values to s3:// markers when upload target is known.",
    )
    args = parser.parse_args()

    storage = get_media_storage("s3")
    output_dir = VIDEOS_DIR / "image_to_video"
    job_map = _load_job_map()

    found_local = 0
    already_present = 0
    would_upload = 0
    uploaded = 0
    missing_owner = 0
    db_update_candidates = 0
    updated_db = 0
    skipped_db = 0
    errors = 0

    print("image_to_video backfill to S3")
    print(f"dry_run={'yes' if args.dry_run else 'no'}")
    print(f"update_db_markers={'yes' if args.update_db_markers else 'no'}")
    print(f"output_dir={output_dir}")

    processed = 0
    for local_path in sorted(output_dir.glob("image_to_video_*.mp4")):
        if args.limit and processed >= args.limit:
            break
        if not local_path.is_file():
            continue
        found_local += 1
        match = JOB_RE.match(local_path.name)
        if not match:
            print(f"SKIP unrecognized_name path={local_path}")
            skipped_db += 1
            continue
        job_id = match.group("job_id")
        user_id, current_output_url = job_map.get(job_id, ("", ""))
        if not user_id:
            missing_owner += 1
            print(f"SKIP missing_owner job_id={job_id} path={local_path}")
            continue
        key = f"image_to_video/{user_id}/{local_path.name}"
        marker = build_storage_reference(key)
        try:
            if storage.exists(key):
                already_present += 1
                print(f"SKIP already_exists key={key}")
            elif args.dry_run:
                would_upload += 1
                print(f"DRYRUN upload key={key} path={local_path}")
            else:
                would_upload += 1
                storage.put_file(local_path, key)
                print(f"UPLOADED key={key} path={local_path}")
                uploaded += 1
            if args.update_db_markers:
                if current_output_url.startswith("/video_shorts/media/") or not current_output_url:
                    db_update_candidates += 1
                    _db_update_output(job_id, marker, args.dry_run)
                    updated_db += 1
                else:
                    skipped_db += 1
                    print(f"SKIP db_update_non_local job_id={job_id} output_url={current_output_url}")
            processed += 1
        except Exception as exc:
            errors += 1
            print(f"ERROR job_id={job_id} key={key} path={local_path} error={exc}")

    print(f"found_local={found_local}")
    print(f"already_present={already_present}")
    print(f"would_upload={would_upload}")
    print(f"uploaded={uploaded}")
    print(f"missing_owner={missing_owner}")
    print(f"db_update_candidates={db_update_candidates}")
    print(f"updated_db={updated_db}")
    print(f"skipped_db={skipped_db}")
    print(f"errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
