#!/usr/bin/env python3
"""Audit rendered clip cuts against adjacent transcript word timings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.routes.generation import _transcript_words_with_sentence_indexes
from app.video_shorts.services.compositor import (
    _guard_keep_ranges_from_unselected_words,
    transcript_word_gap_contexts,
)
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.transcript_service import (
    _decode_transcript_segments,
    _normalize_segments_for_use,
)


EPSILON = 0.001


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _ranges(entry: dict[str, Any]) -> list[dict[str, Any]]:
    keep = entry.get("edit_keep_ranges")
    if isinstance(keep, list) and keep:
        return [dict(item) for item in keep if isinstance(item, dict)]
    try:
        return [{"start": float(entry["start"]), "end": float(entry["end"])}]
    except (KeyError, TypeError, ValueError):
        return []


def _stored_segments(conn, video_id: str, video_pk: int) -> list[dict[str, Any]]:
    """Read stored timing JSON without invoking lazy transcript mutations."""
    override = conn.execute(
        "SELECT segments_json FROM shorts_transcript_overrides WHERE video_pk = ?",
        [video_pk],
    ).fetchone()
    raw = override[0] if override else None
    if not raw:
        row = conn.execute(
            "SELECT segments_json, whisper_segments_json FROM youtube_transcripts WHERE video_id = ?",
            [video_id],
        ).fetchone()
        raw = (row[1] or row[0]) if row else None
    return _normalize_segments_for_use(_decode_transcript_segments(raw))


def _violations(ranges: list[dict[str, Any]], contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for index, (item, context) in enumerate(zip(ranges, contexts)):
        start = float(item["start"])
        end = float(item["end"])
        checks = (
            ("start_before_previous_word_end", start, context.get("previous_end"), "lt"),
            ("start_after_first_kept_word_start", start, context.get("first_start"), "gt"),
            ("end_before_last_kept_word_end", end, context.get("last_end"), "lt"),
            ("end_after_next_word_start", end, context.get("next_start"), "gt"),
        )
        for name, actual, limit, direction in checks:
            if limit is None:
                continue
            crossed = actual < float(limit) - EPSILON if direction == "lt" else actual > float(limit) + EPSILON
            if crossed:
                issues.append(
                    {
                        "range": index,
                        "boundary": name,
                        "cut": round(actual, 6),
                        "word_edge": round(float(limit), 6),
                    }
                )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Newest rendered clips to inspect (0 = all)")
    args = parser.parse_args()
    conn = get_db_readonly()
    try:
        sql = """
            SELECT gv.id, gv.source_video_id, gv.generated_title,
                   gv.raw_plan_entry_json, yv.id
              FROM shorts_generated_videos gv
              LEFT JOIN youtube_videos yv
                ON CAST(yv.video_id AS VARCHAR) = CAST(gv.source_video_id AS VARCHAR)
               AND CAST(yv.owner_user_id AS VARCHAR) = CAST(gv.user_id AS VARCHAR)
               AND yv.brand_id IS NOT DISTINCT FROM gv.brand_id
             WHERE lower(coalesce(gv.generation_status, '')) = 'created'
               AND gv.raw_plan_entry_json IS NOT NULL
             ORDER BY gv.updated_at DESC
        """
        params: list[Any] = []
        if args.limit > 0:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        inspected = mapped = 0
        existing_affected: list[dict[str, Any]] = []
        new_logic_mismatches: list[dict[str, Any]] = []
        words_by_video: dict[int, list[dict[str, Any]]] = {}
        for generated_id, video_id, title, raw_entry, video_pk in rows:
            inspected += 1
            entry = _decode(raw_entry)
            ranges = _ranges(entry)
            if not ranges or video_pk is None:
                continue
            video_pk = int(video_pk)
            if video_pk not in words_by_video:
                words_by_video[video_pk] = _transcript_words_with_sentence_indexes(
                    _stored_segments(conn, str(video_id), video_pk)
                )
            words = words_by_video[video_pk]
            contexts = transcript_word_gap_contexts(ranges, words)
            if len(contexts) != len(ranges):
                continue
            mapped += 1
            issues = _violations(ranges, contexts)
            if issues:
                existing_affected.append(
                    {"clip_id": generated_id, "video_id": video_id, "title": title, "issues": issues}
                )
            corrected = _guard_keep_ranges_from_unselected_words(ranges, words)
            corrected_issues = _violations(corrected, transcript_word_gap_contexts(corrected, words))
            if corrected_issues:
                new_logic_mismatches.append(
                    {"clip_id": generated_id, "video_id": video_id, "issues": corrected_issues}
                )
    finally:
        conn.close()

    print(f"AUDIT rendered={inspected} mapped={mapped} existing_affected={len(existing_affected)}")
    for item in existing_affected:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    print(f"NEW_LOGIC_MISMATCHES={len(new_logic_mismatches)}")
    for item in new_logic_mismatches:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return 0 if not new_logic_mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
