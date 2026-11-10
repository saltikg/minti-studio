#!/usr/bin/env python3
"""Audit/backfill the render-coordinate contract for auto-layout clip segments.

Resolution is derived only from the clip's frozen render snapshot, its exact
producing job, or its own rendered asset.  Mutable account settings and newer
jobs are deliberately never consulted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.video_shorts.services import tenant_media
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.storage import get_media_storage


PREVIEW_HEIGHT = 1280
MISMATCH_LIMIT_PX = 10.0
VALID_COORDINATE_HEIGHTS = {1280, 1920}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _height_from_options(options: Any) -> int | None:
    resolution = str(_json_object(options).get("export_resolution") or "").strip()
    return {"720": 1280, "1080": 1920}.get(resolution)


def _job_payload(conn, job_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not job_id:
        return {}
    if job_id not in cache:
        row = conn.execute(
            "SELECT payload_json FROM shorts_render_jobs WHERE CAST(id AS VARCHAR) = ? LIMIT 1",
            [job_id],
        ).fetchone()
        cache[job_id] = _json_object(row[0]) if row else {}
    return cache[job_id]


def _asset_height(filename: str) -> int | None:
    if not filename:
        return None
    storage = get_media_storage()
    key = tenant_media.asset_key(filename)
    managed = None
    try:
        managed = storage.download_to_temp(key)
        path = Path(managed)
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=height", "-of", "csv=p=0", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        height = int((result.stdout or "").strip())
        return height if height in VALID_COORDINATE_HEIGHTS else None
    except Exception:
        return None
    finally:
        cleanup = getattr(managed, "cleanup", None)
        if callable(cleanup):
            cleanup()


def _resolve_height(entry: dict[str, Any], conn, job_cache: dict[str, dict[str, Any]]) -> tuple[int | None, str]:
    snapshot_height = _height_from_options(entry.get("render_settings_snapshot"))
    if snapshot_height:
        return snapshot_height, "render_settings_snapshot"
    job_id = str(entry.get("render_job_id") or "").strip()
    payload = _job_payload(conn, job_id, job_cache)
    job_height = _height_from_options(payload.get("options"))
    if job_height:
        return job_height, "producing_job"
    filename = str(entry.get("output_filename") or entry.get("clip_filename") or "").strip()
    asset_height = _asset_height(filename)
    if asset_height:
        return asset_height, "rendered_asset"
    return None, "unresolved"


def _write_plan(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=path.name + ".", dir=path.parent, delete=False
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _update_generated_snapshot(
    conn,
    entry: dict[str, Any],
    coordinate_height: int,
    *,
    source_video_id: str,
    owner_user_id: str,
    brand_id: str | None,
) -> int:
    filename = str(entry.get("output_filename") or entry.get("clip_filename") or "").strip()
    if not filename:
        return 0
    rows = conn.execute(
        """
        SELECT CAST(id AS VARCHAR), raw_plan_entry_json
        FROM shorts_generated_videos
        WHERE (clip_filename = ? OR output_filename = ?)
          AND CAST(source_video_id AS VARCHAR) = ?
          AND CAST(user_id AS VARCHAR) = ?
          AND brand_id IS NOT DISTINCT FROM ?
        """,
        [filename, filename, source_video_id, owner_user_id, brand_id],
    ).fetchall()
    changed = 0
    json_sql = "CAST(? AS JSONB)" if getattr(conn, "backend_name", "") == "postgres" else "?"
    for row in rows:
        raw = _json_object(row[1])
        segments = raw.get("auto_layout_segments")
        if not isinstance(segments, list) or not segments:
            continue
        next_segments = []
        row_changed = False
        for segment in segments:
            if not isinstance(segment, dict):
                next_segments.append(segment)
                continue
            next_segment = dict(segment)
            if next_segment.get("coordinate_height") != coordinate_height:
                next_segment["coordinate_height"] = coordinate_height
                row_changed = True
            next_segments.append(next_segment)
        if not row_changed:
            continue
        raw["auto_layout_segments"] = next_segments
        conn.execute(
            f"UPDATE shorts_generated_videos SET raw_plan_entry_json = {json_sql} WHERE CAST(id AS VARCHAR) = ?",
            [json.dumps(raw, ensure_ascii=False), str(row[0])],
        )
        changed += 1
    return changed


def run(*, apply: bool, mismatch_limit: float) -> int:
    app = create_app()
    del app
    read_conn = get_db_readonly()
    write_conn = get_db() if apply else None
    job_cache: dict[str, dict[str, Any]] = {}
    counts = {
        "plans": 0, "clips": 0, "segments": 0, "720": 0, "1080": 0,
        "backfilled_clips": 0, "generated_rows": 0, "unresolved": 0, "mismatches": 0,
    }
    sources: dict[str, int] = {}
    unresolved: list[str] = []
    mismatches: list[str] = []
    try:
        for video, plan_path in tenant_media.iter_plans(migrate=False):
            try:
                data = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                unresolved.append(f"plan={plan_path} reason=read_error:{exc}")
                counts["unresolved"] += 1
                continue
            field = "plan" if isinstance(data.get("plan"), list) else "clips"
            entries = data.get(field)
            if not isinstance(entries, list):
                continue
            plan_changed = False
            plan_has_auto = False
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                segments = entry.get("auto_layout_segments")
                if not isinstance(segments, list) or not segments:
                    continue
                plan_has_auto = True
                counts["clips"] += 1
                counts["segments"] += len(segments)
                height, source = _resolve_height(entry, read_conn, job_cache)
                sources[source] = sources.get(source, 0) + 1
                clip_label = str(entry.get("output_filename") or entry.get("clip_filename") or entry.get("plan_index"))
                if not height:
                    counts["unresolved"] += 1
                    unresolved.append(f"video_pk={video.video_pk} clip={clip_label}")
                    continue
                counts["1080" if height == 1920 else "720"] += 1
                clip_changed = False
                for index, segment in enumerate(segments):
                    if not isinstance(segment, dict):
                        continue
                    stored_height = segment.get("coordinate_height")
                    try:
                        stored_height_number = float(stored_height)
                    except (TypeError, ValueError):
                        stored_height_number = 0.0
                    caption_top = segment.get("caption_top")
                    try:
                        top = float(caption_top)
                    except (TypeError, ValueError):
                        top = None
                    if top is not None:
                        expected_top = top * PREVIEW_HEIGHT / height
                        preview_top = top * PREVIEW_HEIGHT / stored_height_number if stored_height_number > 0 else top
                        delta = abs(preview_top - expected_top)
                        if delta > mismatch_limit:
                            counts["mismatches"] += 1
                            mismatches.append(
                                f"video_pk={video.video_pk} clip={clip_label} segment={index} "
                                f"stored={stored_height!r} actual={height} delta={delta:.2f}px"
                            )
                    if apply and stored_height_number != height:
                        segment["coordinate_height"] = height
                        clip_changed = True
                if clip_changed:
                    plan_changed = True
                    counts["backfilled_clips"] += 1
                    counts["generated_rows"] += _update_generated_snapshot(
                        write_conn,
                        entry,
                        height,
                        source_video_id=video.youtube_id,
                        owner_user_id=video.owner_user_id,
                        brand_id=video.brand_id,
                    )
            if plan_has_auto:
                counts["plans"] += 1
            if apply and plan_changed:
                _write_plan(plan_path, data)
        if write_conn:
            write_conn.commit()
    except Exception:
        if write_conn:
            write_conn.rollback()
        raise
    finally:
        read_conn.close()
        if write_conn:
            write_conn.close()

    mode = "BACKFILL" if apply else "AUDIT"
    print(
        f"{mode}_SUMMARY plans={counts['plans']} clips={counts['clips']} segments={counts['segments']} "
        f"720={counts['720']} 1080={counts['1080']} unresolved={counts['unresolved']} "
        f"mismatches_gt_{mismatch_limit:g}px={counts['mismatches']} sources={json.dumps(sources, sort_keys=True)}"
    )
    if apply:
        print(
            f"BACKFILL_APPLIED clips={counts['backfilled_clips']} "
            f"generated_rows={counts['generated_rows']}"
        )
    for line in unresolved:
        print(f"UNRESOLVED {line}")
    for line in mismatches:
        print(f"MISMATCH {line}")
    if counts["unresolved"] or (not apply and counts["mismatches"]):
        return 1
    print("BACKFILL_OK" if apply else "AUDIT_OK")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Backfill plans and generated clip snapshots")
    parser.add_argument("--mismatch-limit", type=float, default=MISMATCH_LIMIT_PX)
    args = parser.parse_args()
    raise SystemExit(run(apply=args.apply, mismatch_limit=max(0.0, args.mismatch_limit)))


if __name__ == "__main__":
    main()
