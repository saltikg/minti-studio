#!/usr/bin/env python3
"""Backfill real Whisper word times for one tenant video without changing its words.

The script always retranscribes the exact stored source audio.  It writes only a
tenant-scoped transcript override, and only when at least 97% of the existing
word sequence matches the new Whisper word sequence after text normalization.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
MIN_MATCH_RATE = 0.97
PLAN_KINDS = ("plan", "plan_v2", "plan_v3", "plan_v4")


def _normalize_word(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _word_text(word: dict[str, Any]) -> str:
    return str(word.get("word") or word.get("text") or "")


def _flatten_words(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for segment_index, segment in enumerate(segments):
        words = segment.get("words") or []
        if not isinstance(words, list):
            continue
        for word_index, word in enumerate(words):
            if not isinstance(word, dict) or not _word_text(word).strip():
                continue
            try:
                start = float(word.get("start"))
                end = float(word.get("end"))
            except (TypeError, ValueError):
                raise ValueError(f"Existing word has invalid timing at segment={segment_index} word={word_index}")
            if start < 0 or end < start:
                raise ValueError(f"Existing word has invalid timing at segment={segment_index} word={word_index}")
            flattened.append(
                {
                    "segment_index": segment_index,
                    "word_index": word_index,
                    "text": _word_text(word),
                    "normalized": _normalize_word(_word_text(word)),
                    "start": start,
                    "end": end,
                }
            )
    return flattened


def _new_words(response: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in getattr(response, "words", None) or []:
        value = raw if isinstance(raw, dict) else {
            "word": getattr(raw, "word", None),
            "start": getattr(raw, "start", None),
            "end": getattr(raw, "end", None),
        }
        normalized = _normalize_word(value.get("word"))
        if not normalized:
            continue
        try:
            start = float(value.get("start"))
            end = float(value.get("end"))
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start:
            continue
        result.append({"text": str(value.get("word") or ""), "normalized": normalized, "start": start, "end": end})
    return result


def align_word_timings(
    segments: list[dict[str, Any]],
    whisper_words: list[dict[str, Any]],
    media_duration: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return copied segments with only timing metadata changed."""
    old_words = _flatten_words(segments)
    if not old_words:
        raise ValueError("Existing transcript has no timed words")
    if not whisper_words:
        raise ValueError("Whisper returned no word timestamps")

    matcher = SequenceMatcher(
        None,
        [word["normalized"] or f"\0old-{index}" for index, word in enumerate(old_words)],
        [word["normalized"] or f"\0new-{index}" for index, word in enumerate(whisper_words)],
        autojunk=False,
    )
    matched: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            matched[block.a + offset] = block.b + offset
    match_rate = len(matched) / len(old_words)

    assigned: list[tuple[float, float] | None] = [None] * len(old_words)
    for old_index, new_index in matched.items():
        assigned[old_index] = (whisper_words[new_index]["start"], whisper_words[new_index]["end"])

    unmatched_runs: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(assigned):
        if assigned[cursor] is not None:
            cursor += 1
            continue
        run_start = cursor
        while cursor < len(assigned) and assigned[cursor] is None:
            cursor += 1
        run_end = cursor - 1
        unmatched_runs.append((run_start, run_end))
        left_index, right_index = run_start - 1, run_end + 1
        if left_index < 0:
            right = assigned[right_index] if right_index < len(assigned) else None
            if right is None:
                continue
            cursor_time = min(float(media_duration), max(0.0, float(right[0])))
            for old_index in range(run_end, run_start - 1, -1):
                duration = old_words[old_index]["end"] - old_words[old_index]["start"]
                end = cursor_time
                start = max(0.0, end - duration)
                assigned[old_index] = (start, end)
                cursor_time = start
            continue
        if right_index >= len(assigned):
            left = assigned[left_index]
            if left is None:
                continue
            cursor_time = min(float(media_duration), max(0.0, float(left[1])))
            for old_index in range(run_start, run_end + 1):
                duration = old_words[old_index]["end"] - old_words[old_index]["start"]
                start = cursor_time
                end = min(float(media_duration), start + duration)
                assigned[old_index] = (start, end)
                cursor_time = end
            continue
        left, right = assigned[left_index], assigned[right_index]
        if left is None or right is None:
            continue
        interval_start = float(left[1])
        interval_end = max(interval_start, float(right[0]))
        step = (interval_end - interval_start) / (run_end - run_start + 1)
        for position, old_index in enumerate(range(run_start, run_end + 1)):
            start = interval_start + position * step
            end = interval_start + (position + 1) * step
            assigned[old_index] = (start, end)

    unresolved = [index for index, timing in enumerate(assigned) if timing is None]
    updated = deepcopy(segments)
    shifts: list[float] = []
    word_changes: list[dict[str, Any]] = []
    if not unresolved:
        for flat_index, (old_word, timing) in enumerate(zip(old_words, assigned)):
            assert timing is not None
            new_start, new_end = timing
            target = updated[old_word["segment_index"]]["words"][old_word["word_index"]]
            target["start"] = round(float(new_start), 6)
            target["end"] = round(float(new_end), 6)
            target["timing_source"] = "whisper_word"
            start_shift = float(new_start) - old_word["start"]
            end_shift = float(new_end) - old_word["end"]
            shifts.extend((abs(start_shift), abs(end_shift)))
            word_changes.append(
                {
                    "index": flat_index,
                    "text": old_word["text"],
                    "old_start": old_word["start"],
                    "old_end": old_word["end"],
                    "new_start": round(float(new_start), 6),
                    "new_end": round(float(new_end), 6),
                    "matched": flat_index in matched,
                }
            )
        for segment in updated:
            words = segment.get("words") or []
            if isinstance(words, list) and words:
                segment["word_timing_source"] = "whisper_word"

    report = {
        "existing_words": len(old_words),
        "whisper_words": len(whisper_words),
        "matched_words": len(matched),
        "match_rate": match_rate,
        "unmatched_words": len(old_words) - len(matched),
        "unmatched_runs": unmatched_runs,
        "unresolved_indexes": unresolved,
        "median_abs_shift_ms": round(statistics.median(shifts) * 1000, 3) if shifts else None,
        "max_abs_shift_ms": round(max(shifts) * 1000, 3) if shifts else None,
        "word_changes": word_changes,
    }
    return updated, report


def _decode_segments(raw: Any) -> list[dict[str, Any]]:
    value = raw
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("Stored transcript segments are not a JSON list")
    return [dict(segment) for segment in value if isinstance(segment, dict)]


def _load_target(cur: Any, video_pk: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT v.id, v.video_id, v.title, v.video_url, v.owner_user_id, v.brand_id,
               v.duration_seconds,
               b.name, u.name, u.email
        FROM youtube_videos v
        JOIN shorts_brands b ON b.id::text = v.brand_id::text
        JOIN shorts_users u ON u.id::text = v.owner_user_id::text
        WHERE v.id=%s AND v.deleted_at IS NULL
        """,
        [video_pk],
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Video id {video_pk} was not found")
    keys = (
        "id", "video_id", "title", "video_url", "owner_user_id", "brand_id",
        "duration_seconds", "brand_name", "user_name", "email",
    )
    target = dict(zip(keys, row))

    cur.execute(
        "SELECT full_text, segments_json, updated_at FROM shorts_transcript_overrides WHERE video_pk=%s",
        [video_pk],
    )
    override = cur.fetchone()
    cur.execute(
        "SELECT full_text, segments_json, whisper_segments_json FROM youtube_transcripts WHERE video_id=%s",
        [target["video_id"]],
    )
    base = cur.fetchone()
    if not base and not override:
        raise ValueError("Video has no stored transcript")
    if override:
        target.update(transcript_source="override", full_text=override[0] or "", segments_json=override[1], override_row=override)
    else:
        raw = (base[2] or base[1]) if base else None
        target.update(transcript_source="base", full_text=(base[0] or "") if base else "", segments_json=raw, override_row=None)
    target["base_row"] = base
    target["segments"] = _decode_segments(target["segments_json"])
    return target


def _storage_key(uri: str) -> str:
    parsed = urlsplit(str(uri or ""))
    if parsed.scheme != "s3":
        raise ValueError(f"Expected an s3:// stored source, got {uri!r}")
    return "/".join(part for part in (parsed.netloc, parsed.path.lstrip("/")) if part)


def _transcribe_stored_audio(target: dict[str, Any]) -> list[dict[str, Any]]:
    import boto3
    from openai import OpenAI

    bucket = str(os.environ["S3_BUCKET_NAME"])
    key = _storage_key(str(target["video_url"]))
    suffix = Path(key).suffix or ".mp4"
    with tempfile.TemporaryDirectory(prefix=f"word_times_{target['id']}_") as tmp_raw:
        tmp = Path(tmp_raw)
        source = tmp / f"source{suffix}"
        audio = tmp / "whisper_audio.mp3"
        boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1")).download_file(bucket, key, str(source))
        ffmpeg = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        subprocess.run(
            [ffmpeg, "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if audio.stat().st_size > 23 * 1024 * 1024:
            raise RuntimeError("Prepared Whisper audio exceeds 23MB; chunked backfill is not implemented")
        languages = [
            str(segment.get("lang") or "").strip().lower()
            for segment in target["segments"]
            if isinstance(segment, dict) and str(segment.get("lang") or "").strip().lower() in {"en", "tr"}
        ]
        request: dict[str, Any] = {
            "model": os.getenv("WHISPER_MODEL", "whisper-1"),
            "response_format": "verbose_json",
            "timestamp_granularities": ["word"],
        }
        if languages:
            request["language"] = max(set(languages), key=languages.count)
        with audio.open("rb") as handle:
            request["file"] = handle
            response = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).audio.transcriptions.create(
                **request,
            )
    return _new_words(response)


def _entry_ranges(entry: dict[str, Any]) -> list[dict[str, Any]]:
    ranges = entry.get("edit_keep_ranges")
    if isinstance(ranges, list) and ranges:
        return [item for item in ranges if isinstance(item, dict)]
    return [entry]


def _boundary_indexes(entry: dict[str, Any], old_words: list[dict[str, Any]]) -> tuple[int, int] | None:
    indexed: list[tuple[int, int]] = []
    for item in _entry_ranges(entry):
        try:
            first, last = int(item["start_word_index"]), int(item["end_word_index"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= first <= last < len(old_words):
            indexed.append((first, last))
    if indexed:
        return min(first for first, _ in indexed), max(last for _, last in indexed)
    try:
        start, end = float(entry["start"]), float(entry["end"])
    except (KeyError, TypeError, ValueError):
        return None
    candidates = [
        index
        for index, word in enumerate(old_words)
        if float(word.get("end", word.get("old_end"))) >= start
        and float(word.get("start", word.get("old_start"))) <= end
    ]
    return (candidates[0], candidates[-1]) if candidates else None


def _load_plan_entries(video_pk: int) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, int]]:
    private_dir = Path(
        os.getenv("VIDEO_SHORTS_PRIVATE_STATE_DIR")
        or ROOT / "app" / "video_shorts" / "services" / "private_state"
    )
    entries: list[tuple[str, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    for kind in PLAN_KINDS:
        path = private_dir / f"{video_pk}_{kind}.json"
        if not path.is_file():
            counts[kind] = 0
            continue
        payload = json.loads(path.read_text())
        raw_entries = payload.get("plan") or payload.get("clips") or []
        clean = [entry for entry in raw_entries if isinstance(entry, dict)] if isinstance(raw_entries, list) else []
        counts[kind] = len(clean)
        entries.extend((kind, entry) for entry in clean)
    return entries, counts


def _generated_snapshot_entries(rows: list[tuple[Any, ...]]) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for generated_id, raw_entry, generated_title, clip_filename, output_filename in rows:
        value = raw_entry
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = None
        if not isinstance(value, dict):
            continue
        entry = dict(value)
        if not entry.get("title") and not entry.get("generated_title"):
            entry["title"] = generated_title
        if not entry.get("clip_filename"):
            entry["clip_filename"] = clip_filename or output_filename
        entry["saved_clip_id"] = generated_id
        entries.append(("generated_snapshot", entry))
    return entries


def _clip_boundary_report(
    entries: list[tuple[str, dict[str, Any]]],
    changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for kind, entry in entries:
        bounds = _boundary_indexes(entry, changes)
        if bounds is None:
            continue
        first, last = bounds
        first_word, last_word = changes[first], changes[last]
        result.append(
            {
                "plan_kind": kind,
                "saved_clip_id": entry.get("saved_clip_id"),
                "plan_index": entry.get("plan_index"),
                "clip_filename": entry.get("clip_filename") or entry.get("output_filename"),
                "title": entry.get("title") or entry.get("generated_title"),
                "first_word": {
                    "index": first,
                    "text": first_word["text"],
                    "old": [first_word["old_start"], first_word["old_end"]],
                    "new": [first_word["new_start"], first_word["new_end"]],
                },
                "last_word": {
                    "index": last,
                    "text": last_word["text"],
                    "old": [last_word["old_start"], last_word["old_end"]],
                    "new": [last_word["new_start"], last_word["new_end"]],
                },
            }
        )
    return result


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _write_backup(target: dict[str, Any], backup_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root.resolve() / f"video_{target['id']}_{stamp}"
    if backup.is_relative_to(ROOT):
        raise ValueError("Backup directory must be outside the repository")
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "video": {key: target.get(key) for key in ("id", "video_id", "title", "owner_user_id", "brand_id")},
        "transcript_source": target["transcript_source"],
        "override_before": target["override_row"],
        "base_before": target["base_row"],
        "effective_full_text": target["full_text"],
        "effective_segments_json": target["segments_json"],
    }
    manifest_path = backup / "backup.json"
    manifest_path.write_text(json.dumps(manifest, default=str, ensure_ascii=False, indent=2))
    if target["override_row"]:
        full_text, segments_json, updated_at = target["override_row"]
        restore = (
            "BEGIN;\n"
            "UPDATE main.shorts_transcript_overrides SET "
            f"full_text={_sql_literal(full_text)}, segments_json={_sql_literal(segments_json)}, "
            f"updated_at={_sql_literal(updated_at)}::timestamptz WHERE video_pk={int(target['id'])};\n"
            "COMMIT;\n"
        )
    else:
        restore = f"BEGIN;\nDELETE FROM main.shorts_transcript_overrides WHERE video_pk={int(target['id'])};\nCOMMIT;\n"
    restore_path = backup / "restore.sql"
    restore_path.write_text(restore)
    for path in (manifest_path, restore_path):
        path.chmod(0o600)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", type=int, help="youtube_videos.id tenant video primary key")
    parser.add_argument("--dry-run", action="store_true", help="request and analyze timestamps without backup or write")
    parser.add_argument(
        "--backup-dir",
        default=str(ROOT.parent / "minti_studio_backups" / "word_timestamps"),
        help="external root for apply-mode backups",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(SCRIPT_DIR))
    from safe_to_deploy import _connect, _load_env_file

    _load_env_file()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        target = _load_target(cur, args.video_id)
        old_segments_json = str(target["segments_json"])
        old_segments_hash = hashlib.sha256(old_segments_json.encode()).hexdigest()
        old_words = _flatten_words(target["segments"])
        plan_entries, plan_counts = _load_plan_entries(args.video_id)
        cur.execute(
            """
            SELECT id, raw_plan_entry_json, generated_title, clip_filename, output_filename
            FROM shorts_generated_videos
            WHERE source_video_id=%s AND user_id=%s AND brand_id IS NOT DISTINCT FROM %s
            ORDER BY created_at, id
            """,
            [target["video_id"], target["owner_user_id"], target["brand_id"]],
        )
        generated_rows = cur.fetchall()
        generated_entries = _generated_snapshot_entries(generated_rows)
        conn.rollback()

        whisper_words = _transcribe_stored_audio(target)
        media_duration = float(target.get("duration_seconds") or 0)
        if media_duration <= 0:
            raise ValueError("Video has no positive media duration for edge timestamp clamping")
        updated_segments, alignment = align_word_timings(
            target["segments"], whisper_words, media_duration
        )
        report = {
            "mode": "DRY_RUN" if args.dry_run else "APPLY",
            "video": {key: target.get(key) for key in ("id", "video_id", "title", "owner_user_id", "brand_id", "brand_name", "user_name")},
            "stored_source": target["video_url"],
            "transcript_source_before": target["transcript_source"],
            "match_rate": round(alignment["match_rate"], 6),
            "matched_words": alignment["matched_words"],
            "existing_words": alignment["existing_words"],
            "whisper_words": alignment["whisper_words"],
            "unmatched_words": alignment["unmatched_words"],
            "unresolved_indexes": alignment["unresolved_indexes"],
            "median_abs_shift_ms": alignment["median_abs_shift_ms"],
            "max_abs_shift_ms": alignment["max_abs_shift_ms"],
            "plan_references": len(plan_entries) + len(generated_entries),
            "current_plan_state_references": sum(plan_counts.values()),
            "plan_references_by_kind": plan_counts,
            "generated_clip_snapshots": len(generated_rows),
            "clip_boundaries": (
                _clip_boundary_report(plan_entries + generated_entries, alignment["word_changes"])
                if alignment["word_changes"]
                else []
            ),
            "result": "ANALYZED",
        }
        if alignment["match_rate"] < MIN_MATCH_RATE or alignment["unresolved_indexes"]:
            report["result"] = "NOT_WRITTEN_MATCH_GATE"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        if args.dry_run:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        backup = _write_backup(target, Path(args.backup_dir))
        serialized = json.dumps(updated_segments, ensure_ascii=False)
        cur = conn.cursor()
        cur.execute("SET LOCAL lock_timeout='5s'")
        cur.execute(
            "SELECT owner_user_id, brand_id FROM youtube_videos WHERE id=%s AND deleted_at IS NULL FOR UPDATE",
            [target["id"]],
        )
        scope = cur.fetchone()
        if not scope or str(scope[0]) != str(target["owner_user_id"]) or str(scope[1] or "") != str(target["brand_id"] or ""):
            raise RuntimeError("Video tenant scope changed; transaction rolled back")
        current = _load_target(cur, args.video_id)
        current_hash = hashlib.sha256(str(current["segments_json"]).encode()).hexdigest()
        if current_hash != old_segments_hash:
            raise RuntimeError("Transcript changed after analysis; transaction rolled back")
        cur.execute(
            """
            INSERT INTO shorts_transcript_overrides (video_pk, full_text, segments_json, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT(video_pk) DO UPDATE SET
                full_text=excluded.full_text,
                segments_json=excluded.segments_json,
                updated_at=excluded.updated_at
            """,
            [target["id"], target["full_text"], serialized],
        )
        conn.commit()
        report["result"] = "APPLIED"
        report["backup_dir"] = str(backup)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
