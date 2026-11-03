#!/usr/bin/env python3
"""Label recent ICP-fit discovery leads with the face check without changing status."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app import create_app
from app.video_shorts.routes.api import (
    _store_discovery_icp_llm_usage,
    diagnose_channel,
)
from app.video_shorts.services.db import get_db
from app.video_shorts.services.discovery_backfill_guard import (
    DISCOVERY_BACKFILL_QUOTA_FEATURE,
    require_discovery_backfill_window,
)
from app.video_shorts.services.discovery_face_check import check_face_on_camera


MODEL_PRICES_PER_MILLION = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
}


def _estimated_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = MODEL_PRICES_PER_MILLION.get(model, (0.0, 0.0))
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def _usage(response) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return (
        int(getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None) or 0),
        int(getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None) or 0),
    )


def _report(conn, *, processed: int, usage_rows: list[dict]) -> dict:
    window_sql = "icp_fit IS TRUE AND first_seen_at >= CURRENT_TIMESTAMP - INTERVAL '60 days' AND face_checked_at IS NOT NULL"
    verdicts = {
        str(row[0] or "unlabeled"): int(row[1] or 0)
        for row in conn.execute(
            f"SELECT face_check_verdict, COUNT(*) FROM discovery_leads WHERE {window_sql} GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }
    presenter_types = {
        str(row[0] or "unknown"): int(row[1] or 0)
        for row in conn.execute(
            f"SELECT face_check_presenter_type, COUNT(*) FROM discovery_leads WHERE {window_sql} GROUP BY 1 ORDER BY 2 DESC, 1"
        ).fetchall()
    }
    agreement = {}
    for label, clause in (
        ("promoted", "COALESCE(CAST(autopilot_lead_id AS VARCHAR), '') <> ''"),
        ("dismissed", "status = 'dismissed'"),
    ):
        row = conn.execute(
            f"""
            SELECT COUNT(*), COUNT(*) FILTER (WHERE face_check_verdict = 'face_fail')
            FROM discovery_leads
            WHERE {window_sql} AND {clause}
            """
        ).fetchone()
        agreement[label] = {"checked": int(row[0] or 0), "face_fail": int(row[1] or 0)}

    def examples(verdict: str, limit: int) -> list[dict]:
        rows = conn.execute(
            f"""
            SELECT channel_title, channel_url, face_check_reason
            FROM discovery_leads
            WHERE {window_sql} AND face_check_verdict = ?
            ORDER BY face_checked_at DESC, id DESC
            LIMIT ?
            """,
            [verdict, limit],
        ).fetchall()
        return [
            {"channel": str(row[0] or ""), "link": str(row[1] or ""), "reason": str(row[2] or "")}
            for row in rows
        ]

    total_cost = sum(row["estimated_cost_usd"] for row in usage_rows)
    return {
        "processed_this_run": processed,
        "verdicts": verdicts,
        "presenter_types": presenter_types,
        "agreement": agreement,
        "face_fail_examples": examples("face_fail", 10),
        "face_unsure_examples": examples("face_unsure", 5),
        "vision_usage": {
            "channels_billed_this_run": len(usage_rows),
            "input_tokens": sum(row["input_tokens"] for row in usage_rows),
            "output_tokens": sum(row["output_tokens"] for row in usage_rows),
            "estimated_total_cost_usd": round(total_cost, 6),
            "estimated_cost_per_billed_channel_usd": round(total_cost / len(usage_rows), 6) if usage_rows else 0,
            "by_model": dict(Counter(row["model"] for row in usage_rows)),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Store labels. Without this flag, only show the eligible count.")
    parser.add_argument("--force", action="store_true", help="Recheck leads that already have a label.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    args = parser.parse_args()
    if args.shard_count < 1 or args.shard_index < 0 or args.shard_index >= args.shard_count:
        parser.error("--shard-index must be between 0 and --shard-count - 1")
    require_discovery_backfill_window()

    app = create_app()
    with app.app_context():
        conn = get_db()
        try:
            where = "icp_fit IS TRUE AND first_seen_at >= CURRENT_TIMESTAMP - INTERVAL '60 days'"
            if not args.force:
                where += " AND face_checked_at IS NULL"
            if args.shard_count > 1:
                where += f" AND MOD(id, {args.shard_count}) = {args.shard_index}"
            limit_sql = f" LIMIT {max(0, args.limit)}" if args.limit else ""
            rows = conn.execute(
                f"SELECT id, youtube_channel_id, channel_title, raw_json, face_check_verdict FROM discovery_leads WHERE {where} ORDER BY first_seen_at, id{limit_sql}"
            ).fetchall()
            if not args.apply:
                print(json.dumps({"dry_run": True, "eligible": len(rows)}, indent=2))
                return 0

            usage_rows: list[dict] = []
            transitions = Counter()
            frame_totals = Counter()
            youtube_api_fallbacks = 0
            for index, row in enumerate(rows, start=1):
                lead_id, channel_id, channel_title, raw_json, old_verdict = row
                try:
                    raw_payload = raw_json if isinstance(raw_json, dict) else json.loads(str(raw_json or "{}"))
                    samples = list(raw_payload.get("face_check_video_samples") or [])
                    if len(samples) < 3:
                        diagnosed = diagnose_channel(
                            str(channel_id),
                            resolve_email=False,
                            quota_feature=DISCOVERY_BACKFILL_QUOTA_FEATURE,
                        )
                        samples = list(diagnosed.get("face_check_video_samples") or [])
                        youtube_api_fallbacks += 1
                    raw_payload["face_check_video_samples"] = samples
                    result = check_face_on_camera(samples)
                except Exception as exc:
                    result = {
                        "face_check_verdict": "face_unsure",
                        "face_check_presenter_type": "mixed",
                        "face_check_same_person_count": 0,
                        "face_check_reason": f"Face check error: {type(exc).__name__}: {exc}"[:500],
                    }
                response = result.pop("_face_check_response", None)
                model = str(result.pop("_face_check_model", "") or "")
                frame_totals.update(result.pop("_face_check_frame_stats", {}) or {})
                result.pop("face_check_usable_video_count", None)
                transitions[(str(old_verdict or "unlabeled"), result["face_check_verdict"])] += 1
                if response is not None:
                    input_tokens, output_tokens = _usage(response)
                    usage_rows.append(
                        {
                            "model": model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "estimated_cost_usd": _estimated_cost(model, input_tokens, output_tokens),
                        }
                    )
                    _store_discovery_icp_llm_usage(
                        run_id=None,
                        response=response,
                        purpose="face_check_backfill",
                        model=model,
                        conn=conn,
                    )
                raw_json_sql = "CAST(? AS JSONB)" if getattr(conn, "backend_name", "") == "postgres" else "?"
                conn.execute(
                    f"""
                    UPDATE discovery_leads
                    SET face_check_verdict = ?,
                        face_check_presenter_type = ?,
                        face_check_same_person_count = ?,
                        face_check_reason = ?,
                        raw_json = {raw_json_sql},
                        face_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    [
                        result["face_check_verdict"],
                        result["face_check_presenter_type"],
                        result["face_check_same_person_count"],
                        result["face_check_reason"],
                        json.dumps(raw_payload, ensure_ascii=False, default=str),
                        lead_id,
                    ],
                )
                conn.commit()
                print(
                    f"[{index}/{len(rows)}] {channel_title or channel_id}: {result['face_check_verdict']} "
                    f"({result['face_check_presenter_type']})",
                    flush=True,
                )
            report = _report(conn, processed=len(rows), usage_rows=usage_rows)
            report["verdict_transitions"] = {
                f"{old}->{new}": count for (old, new), count in sorted(transitions.items())
            }
            report["frame_availability"] = dict(frame_totals)
            report["youtube_api_fallback_channels"] = youtube_api_fallbacks
            print(json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
