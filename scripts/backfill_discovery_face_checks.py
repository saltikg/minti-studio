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
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        conn = get_db()
        try:
            where = "icp_fit IS TRUE AND first_seen_at >= CURRENT_TIMESTAMP - INTERVAL '60 days'"
            if not args.force:
                where += " AND face_checked_at IS NULL"
            limit_sql = f" LIMIT {max(0, args.limit)}" if args.limit else ""
            rows = conn.execute(
                f"SELECT id, youtube_channel_id, channel_title FROM discovery_leads WHERE {where} ORDER BY first_seen_at, id{limit_sql}"
            ).fetchall()
            if not args.apply:
                print(json.dumps({"dry_run": True, "eligible": len(rows)}, indent=2))
                return 0

            usage_rows: list[dict] = []
            for index, row in enumerate(rows, start=1):
                lead_id, channel_id, channel_title = row
                try:
                    diagnosed = diagnose_channel(str(channel_id), resolve_email=False)
                    result = check_face_on_camera(diagnosed.get("_face_check_thumbnail_urls") or [])
                except Exception as exc:
                    result = {
                        "face_check_verdict": "face_unsure",
                        "face_check_presenter_type": "mixed",
                        "face_check_same_person_count": 0,
                        "face_check_reason": f"Face check error: {type(exc).__name__}: {exc}"[:500],
                    }
                response = result.pop("_face_check_response", None)
                model = str(result.pop("_face_check_model", "") or "")
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
                conn.execute(
                    """
                    UPDATE discovery_leads
                    SET face_check_verdict = ?,
                        face_check_presenter_type = ?,
                        face_check_same_person_count = ?,
                        face_check_reason = ?,
                        face_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    [
                        result["face_check_verdict"],
                        result["face_check_presenter_type"],
                        result["face_check_same_person_count"],
                        result["face_check_reason"],
                        lead_id,
                    ],
                )
                conn.commit()
                print(
                    f"[{index}/{len(rows)}] {channel_title or channel_id}: {result['face_check_verdict']} "
                    f"({result['face_check_presenter_type']})",
                    flush=True,
                )
            print(json.dumps(_report(conn, processed=len(rows), usage_rows=usage_rows), indent=2, ensure_ascii=False))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
