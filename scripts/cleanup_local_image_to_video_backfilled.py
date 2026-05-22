#!/usr/bin/env python3
import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Tuple

from dotenv import load_dotenv


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.video_shorts.config import VIDEOS_DIR
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.storage import get_media_storage


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete local image_to_video outputs only when the matching S3 object already exists."
    )
    parser.add_argument("--dry-run", action="store_true", help="List actions without deleting.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of local files to process.")
    args = parser.parse_args()

    storage = get_media_storage("s3")
    output_dir = VIDEOS_DIR / "image_to_video"
    job_map = _load_job_map()

    found_local = 0
    missing_owner = 0
    missing_in_s3 = 0
    already_missing_local = 0
    would_delete = 0
    deleted = 0
    errors = 0

    print("Cleanup local image_to_video outputs after S3 backfill")
    print(f"dry_run={'yes' if args.dry_run else 'no'}")
    print(f"output_dir={output_dir}")

    processed = 0
    for local_path in sorted(output_dir.glob("image_to_video_*.mp4")):
        if args.limit and processed >= args.limit:
            break
        if not local_path.exists() or not local_path.is_file():
            already_missing_local += 1
            continue
        found_local += 1
        match = JOB_RE.match(local_path.name)
        if not match:
            processed += 1
            continue
        job_id = match.group("job_id")
        user_id, _ = job_map.get(job_id, ("", ""))
        if not user_id:
            missing_owner += 1
            print(f"SKIP missing_owner job_id={job_id} path={local_path}")
            processed += 1
            continue
        key = f"image_to_video/{user_id}/{local_path.name}"
        try:
            if not storage.exists(key):
                missing_in_s3 += 1
                print(f"SKIP missing_in_s3 key={key} path={local_path}")
                processed += 1
                continue
            would_delete += 1
            if args.dry_run:
                print(f"DRYRUN delete path={local_path} key={key}")
            else:
                local_path.unlink()
                print(f"DELETED path={local_path} key={key}")
                deleted += 1
            processed += 1
        except Exception as exc:
            errors += 1
            print(f"ERROR key={key} path={local_path} error={exc}")
            processed += 1

    print(f"found_local={found_local}")
    print(f"already_missing_local={already_missing_local}")
    print(f"missing_owner={missing_owner}")
    print(f"missing_in_s3={missing_in_s3}")
    print(f"would_delete={would_delete}")
    print(f"deleted={deleted}")
    print(f"errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
