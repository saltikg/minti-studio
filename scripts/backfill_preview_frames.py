#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


@dataclass
class Candidate:
    pk: int
    video_id: str
    title: str
    video_url: str
    duration_seconds: float | None


def _load_env() -> None:
    if load_dotenv is None:
        return
    prod_env = Path("/home/ubuntu/apps/minti_studio/.env")
    local_env = ROOT / ".env"
    load_dotenv(prod_env if prod_env.exists() else local_env)


def _source_key_for_video(video_id: str, video_url: str) -> str:
    from app.video_shorts.services.storage import get_media_storage, is_storage_reference, storage_reference_key

    clean_video_id = str(video_id or "").strip()
    clean_url = str(video_url or "").strip()
    if not clean_video_id:
        return ""
    if is_storage_reference(clean_url):
        key = storage_reference_key(clean_url)
        expected_prefix = f"videos/{clean_video_id}."
        if key.startswith(expected_prefix) and "/" not in key[len("videos/") :]:
            return key
        return ""
    storage = get_media_storage()
    for suffix in (".mp4", ".mov", ".mkv", ".webm"):
        key = f"videos/{clean_video_id}{suffix}"
        try:
            if storage.exists(key):
                return key
        except Exception:
            continue
    return ""


def _source_available(video_id: str, video_url: str) -> tuple[bool, str]:
    from app.video_shorts.services.media_utils import _find_source_video

    local_path = _find_source_video(video_id)
    if local_path and local_path.exists():
        return True, str(local_path)
    key = _source_key_for_video(video_id, video_url)
    if key:
        return True, key
    return False, ""


def _fetch_candidates() -> list[Candidate]:
    from app.video_shorts.routes import generation
    from app.video_shorts.services.db import get_db_readonly

    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT id, video_id, title, video_url, duration_seconds
            FROM youtube_videos
            WHERE lower(coalesce(download_status, '')) = 'downloaded'
              AND coalesce(video_id, '') != ''
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    candidates: list[Candidate] = []
    for row in rows:
        video_id = str(row[1] or "").strip()
        if not video_id:
            continue
        if generation._preview_frame_cache_path(video_id).exists():
            continue
        try:
            duration_seconds = float(row[4]) if row[4] is not None else None
        except Exception:
            duration_seconds = None
        candidates.append(
            Candidate(
                pk=int(row[0]),
                video_id=video_id,
                title=str(row[2] or "").strip(),
                video_url=str(row[3] or "").strip(),
                duration_seconds=duration_seconds,
            )
        )
    return candidates


def _dry_run(candidates: list[Candidate], sample_size: int) -> int:
    available: list[tuple[Candidate, str]] = []
    no_source: list[Candidate] = []
    for candidate in candidates:
        ok, source = _source_available(candidate.video_id, candidate.video_url)
        if ok:
            available.append((candidate, source))
        else:
            no_source.append(candidate)
    print(f"missing_preview_total={len(candidates)}")
    print(f"source_available={len(available)}")
    print(f"no_source={len(no_source)}")
    for label, rows in (("sample_available", available[:sample_size]),):
        print(label + ":")
        for candidate, source in rows:
            print(f"  id={candidate.pk} video_id={candidate.video_id} source={source} title={candidate.title[:80]}")
    if no_source:
        print("no_source_sample:")
        for candidate in no_source[:sample_size]:
            print(f"  id={candidate.pk} video_id={candidate.video_id} title={candidate.title[:80]}")
    return len(available)


def _real_run(candidates: list[Candidate], limit: int | None, sleep_seconds: float) -> int:
    from app.video_shorts.routes import generation
    from app.video_shorts.services.media_utils import _cleanup_resolved_source_video, _resolve_source_video

    selected = candidates[:limit] if limit is not None and limit >= 0 else candidates
    ok_count = 0
    failed: list[tuple[Candidate, str]] = []
    no_source: list[Candidate] = []
    for index, candidate in enumerate(selected, start=1):
        source_path = None
        source_is_temp = False
        try:
            source_path, source_is_temp = _resolve_source_video(candidate.video_id)
            if not source_path or not source_path.exists():
                no_source.append(candidate)
                print(f"[{index}/{len(selected)}] no-source id={candidate.pk} video_id={candidate.video_id}", flush=True)
                continue
            preview_path = generation._ensure_preview_frame(candidate.video_id, source_path, candidate.duration_seconds)
            if preview_path and preview_path.exists():
                ok_count += 1
                print(f"[{index}/{len(selected)}] ok id={candidate.pk} video_id={candidate.video_id} path={preview_path}", flush=True)
            else:
                failed.append((candidate, "preview_not_created"))
                print(f"[{index}/{len(selected)}] failed id={candidate.pk} video_id={candidate.video_id} error=preview_not_created", flush=True)
        except Exception as exc:
            failed.append((candidate, f"{type(exc).__name__}: {exc}"))
            print(f"[{index}/{len(selected)}] failed id={candidate.pk} video_id={candidate.video_id} error={type(exc).__name__}: {exc}", flush=True)
        finally:
            _cleanup_resolved_source_video(source_path, source_is_temp)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    print(f"SUMMARY ok={ok_count} failed={len(failed)} no_source={len(no_source)} total={len(selected)}")
    if failed:
        print("failed_sample:")
        for candidate, error in failed[:20]:
            print(f"  id={candidate.pk} video_id={candidate.video_id} error={error}")
    if no_source:
        print("no_source:")
        for candidate in no_source[:100]:
            print(f"  id={candidate.pk} video_id={candidate.video_id} title={candidate.title[:80]}")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill missing crop preview frames for downloaded videos.")
    parser.add_argument("--dry-run", action="store_true", help="Only print counts and samples.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of missing-preview videos to process.")
    parser.add_argument("--sample-size", type=int, default=10, help="Sample size for dry-run output.")
    parser.add_argument("--sleep", type=float, default=0.5, help="Seconds to sleep between real-run items.")
    args = parser.parse_args(argv)

    _load_env()
    try:
        os.nice(10)
    except Exception:
        pass

    from app import create_app

    app = create_app()
    with app.app_context():
        candidates = _fetch_candidates()
        if args.dry_run:
            _dry_run(candidates, max(0, args.sample_size))
            return 0
        return _real_run(candidates, args.limit, max(0.0, args.sleep))


if __name__ == "__main__":
    raise SystemExit(main())
