#!/usr/bin/env python3
from __future__ import annotations

import argparse
from io import BytesIO
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app import create_app
from app.video_shorts.routes import generation
from app.video_shorts.services.storage import get_media_storage


def _iter_plan_paths(video_id: str = "") -> list[Path]:
    shorts_dir = generation.SHORTS_DIR
    if video_id:
        path = generation._plan_path(video_id)
        return [path] if path.exists() else []
    return sorted(shorts_dir.glob("*_plan.json"))


def _load_plan(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}, []
    if not isinstance(payload, dict):
        return {}, []
    entries = payload.get("plan") or payload.get("clips") or []
    if not isinstance(entries, list):
        return payload, []
    return payload, [entry for entry in entries if isinstance(entry, dict)]


def _poster_key_for_entry(entry: dict[str, Any]) -> str:
    clip_filename = Path(str(entry.get("clip_filename") or entry.get("output_filename") or "").strip()).name
    poster_filename = Path(str(entry.get("poster_filename") or "").strip()).name
    if poster_filename:
        return generation._short_poster_storage_key_for_poster(poster_filename)
    if clip_filename:
        return generation._short_poster_storage_key(clip_filename)
    return ""


def _image_size_from_storage(key: str) -> tuple[int, int] | None:
    if not key:
        return None
    storage = get_media_storage()
    client = getattr(storage, "client", None)
    bucket_name = getattr(storage, "bucket_name", "")
    if client is not None and bucket_name:
        try:
            response = client.get_object(Bucket=bucket_name, Key=key, Range="bytes=0-65535")
            with Image.open(BytesIO(response["Body"].read())) as img:
                return int(img.width), int(img.height)
        except Exception:
            pass
    for storage_name in ("default", "local"):
        storage = get_media_storage() if storage_name == "default" else get_media_storage("local")
        try:
            if not storage.exists(key):
                continue
            temp_obj = storage.download_to_temp(key)
            try:
                with Image.open(str(temp_obj)) as img:
                    return int(img.width), int(img.height)
            finally:
                cleanup = getattr(temp_obj, "cleanup", None)
                if callable(cleanup):
                    cleanup()
        except Exception:
            continue
    return None


def _aspect(width: int, height: int) -> str:
    return "landscape" if width >= height else "portrait"


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill clip render aspect metadata from generated poster dimensions.")
    parser.add_argument("--video-id", default="", help="Only process one YouTube video id.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum entries to update.")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without writing plan files.")
    args = parser.parse_args()

    app = create_app()
    scanned = 0
    rendered = 0
    already_has_dims = 0
    no_poster = 0
    unreadable_poster = 0
    updated = 0
    changed_files = 0
    sample: list[str] = []

    with app.app_context():
        for path in _iter_plan_paths(args.video_id.strip()):
            payload, entries = _load_plan(path)
            if not payload or not entries:
                continue
            file_changed = False
            for entry in entries:
                scanned += 1
                status = str(entry.get("status") or "").strip().lower()
                clip_filename = str(entry.get("clip_filename") or entry.get("output_filename") or "").strip()
                if status != "created" or not clip_filename:
                    continue
                rendered += 1
                try:
                    width = int(float(entry.get("render_width") or 0))
                    height = int(float(entry.get("render_height") or 0))
                except Exception:
                    width = height = 0
                if width > 0 and height > 0:
                    already_has_dims += 1
                    continue
                poster_key = _poster_key_for_entry(entry)
                if not poster_key:
                    no_poster += 1
                    continue
                size = _image_size_from_storage(poster_key)
                if not size:
                    unreadable_poster += 1
                    continue
                if args.limit and updated >= args.limit:
                    continue
                entry["render_width"], entry["render_height"] = size
                entry["render_aspect"] = _aspect(*size)
                updated += 1
                file_changed = True
                if len(sample) < 10:
                    sample.append(f"{path.name}:{entry.get('plan_index')}={size[0]}x{size[1]}:{entry['render_aspect']}")
            if file_changed and not args.dry_run:
                generation._write_plan_entries(path.name[: -len("_plan.json")], entries)
                changed_files += 1

    print(f"scanned_entries={scanned}")
    print(f"rendered_entries={rendered}")
    print(f"already_has_dims={already_has_dims}")
    print(f"no_poster_key={no_poster}")
    print(f"unreadable_poster={unreadable_poster}")
    print(f"updated={updated}")
    print(f"changed_files={changed_files if not args.dry_run else 0}")
    if sample:
        print("sample_updates=" + "; ".join(sample))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
