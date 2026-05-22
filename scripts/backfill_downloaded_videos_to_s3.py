#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.video_shorts.config import VIDEOS_DIR
from app.video_shorts.services.storage import get_media_storage


MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def _is_candidate(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.parent != VIDEOS_DIR:
        return False
    if path.name == "1-short_bg_8.png":
        return False
    if path.name.startswith("tmp"):
        return False
    if path.suffix.lower() not in MEDIA_SUFFIXES:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill local downloaded/source videos to S3.")
    parser.add_argument("--dry-run", action="store_true", help="List actions without uploading.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of files to process.")
    args = parser.parse_args()

    storage = get_media_storage("s3")
    candidates = [path for path in sorted(VIDEOS_DIR.iterdir()) if _is_candidate(path)]

    found_local = 0
    already_present = 0
    would_upload = 0
    uploaded = 0
    errors = 0

    print("Downloaded videos backfill to S3")
    print(f"dry_run={'yes' if args.dry_run else 'no'}")
    print(f"videos_dir={VIDEOS_DIR}")

    processed = 0
    for local_path in candidates:
        if args.limit and processed >= args.limit:
            break
        found_local += 1
        key = f"videos/{local_path.name}"
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
                uploaded += 1
                print(f"UPLOADED key={key} path={local_path}")
            processed += 1
        except Exception as exc:
            errors += 1
            print(f"ERROR key={key} path={local_path} error={exc}")
            processed += 1

    print(f"found_local={found_local}")
    print(f"already_present={already_present}")
    print(f"would_upload={would_upload}")
    print(f"uploaded={uploaded}")
    print(f"errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
