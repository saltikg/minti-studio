#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Set

from dotenv import load_dotenv


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.video_shorts.config import SHORTS_DIR
from app.video_shorts.services.storage import get_media_storage


def _iter_plan_filenames() -> Iterable[str]:
    if not SHORTS_DIR.exists():
        return []
    filenames: Set[str] = set()
    for plan_path in SHORTS_DIR.glob("*_plan*.json"):
        try:
            payload = json.loads(plan_path.read_text())
        except Exception:
            continue
        entries = payload.get("plan") or payload.get("clips") or []
        for entry in entries:
            filename = str(entry.get("clip_filename") or entry.get("output_filename") or "").strip()
            if not filename:
                continue
            if Path(filename).suffix.lower() not in {".mp4", ".mov", ".mkv"}:
                continue
            filenames.add(Path(filename).name)
    return sorted(filenames)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete local short clips only when the matching S3 object already exists."
    )
    parser.add_argument("--dry-run", action="store_true", help="List actions without deleting.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of local files to process.")
    args = parser.parse_args()

    storage = get_media_storage("s3")
    candidates = list(_iter_plan_filenames())
    found_local = 0
    already_missing_local = 0
    missing_in_s3 = 0
    would_delete = 0
    deleted = 0
    errors = 0

    print("Cleanup local shorts after S3 backfill")
    print(f"dry_run={'yes' if args.dry_run else 'no'}")
    print(f"shorts_dir={SHORTS_DIR}")

    processed = 0
    for filename in candidates:
        if args.limit and processed >= args.limit:
            break
        local_path = SHORTS_DIR / filename
        if not local_path.exists() or not local_path.is_file():
            already_missing_local += 1
            continue
        found_local += 1
        key = f"shorts/{filename}"
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

    print(f"candidate_filenames={len(candidates)}")
    print(f"found_local={found_local}")
    print(f"already_missing_local={already_missing_local}")
    print(f"missing_in_s3={missing_in_s3}")
    print(f"would_delete={would_delete}")
    print(f"deleted={deleted}")
    print(f"errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
