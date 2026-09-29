#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_llm import BLOG_MODEL_JUDGE, call_json, log_usage  # noqa: E402
from app.video_shorts.services.blog_pipeline import (  # noqa: E402
    BLOG_JUDGE_MIN_SCORE,
    best_duplicate_match,
    json_dumps_compact,
    list_existing_articles_and_topics,
    normalize_keyword,
    row_to_dict,
    seed_topics,
)
from app.video_shorts.services.db import get_db  # noqa: E402


SYSTEM_PROMPT = """You judge blog topic candidates for MintiStudio.

Audience: solo educators, coaches, consultants, podcasters, mostly US English, who make long-form video and have something to sell.
MintiStudio turns long videos into Shorts. It offers DIY self-serve plus Autopilot done-for-you at $20/mo. It is not a tool you learn and run yourself.

A topic fits only if a genuinely useful article for that audience can naturally lead to MintiStudio. Trends, dance/meme content, gaming, and generic social-media news are off topic.
Duplicate means the same reader question as an existing article or queued topic, even with different wording.
The competitor headline is a demand signal only. Do not reuse or lightly reword it. our_title and angle must be MintiStudio's own.

Return JSON object only:
{"decisions":[{"id":123,"decision":"accept|duplicate|offtopic|lowfit","fit_score":0,"judge_reason":"one line","duplicate_of":null,"our_title":"...","primary_keyword":"...","category":"persona|monetization|comparison|craft|news","intent":"commercial|informational","angle":"...","brief":"2-3 sentences","is_timely":false,"expires_in_days":null}]}
"""


def _candidate_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, title, source_name, source_url, source_title, brief, created_at
        FROM blog_topics
        WHERE status = 'candidate'
        ORDER BY created_at ASC
        LIMIT 100
        """
    ).fetchall()
    return [row_to_dict(conn.description, row) for row in rows]


def _expire_timely(conn, *, dry_run: bool) -> int:
    rows = conn.execute(
        """
        SELECT id FROM blog_topics
        WHERE status = 'queued'
          AND expires_at IS NOT NULL
          AND expires_at < CURRENT_TIMESTAMP
        """
    ).fetchall()
    if not rows:
        return 0
    if not dry_run:
        conn.execute(
            """
            UPDATE blog_topics
            SET status = 'rejected_lowfit', judge_reason = 'expired (timely topic)'
            WHERE status = 'queued'
              AND expires_at IS NOT NULL
              AND expires_at < CURRENT_TIMESTAMP
            """
        )
    return len(rows)


def _apply_prefilter(conn, candidates: list[dict[str, Any]], existing: list[dict[str, str]], *, dry_run: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    remaining = []
    rejected = []
    for candidate in candidates:
        match = best_duplicate_match(str(candidate.get("title") or ""), existing)
        if match:
            rejected.append({"id": candidate["id"], "decision": "duplicate", "duplicate_of": match, "judge_reason": "prefilter match"})
            if not dry_run:
                conn.execute(
                    """
                    UPDATE blog_topics
                    SET status = 'rejected_duplicate', duplicate_of = ?, judge_reason = 'prefilter match'
                    WHERE id = ?
                    """,
                    [match, candidate["id"]],
                )
        else:
            remaining.append(candidate)
    return remaining, rejected


def _build_user_prompt(candidates: list[dict[str, Any]], existing: list[dict[str, str]]) -> str:
    payload = {
        "existing": existing,
        "candidates": [
            {
                "id": row["id"],
                "headline": row.get("source_title") or row.get("title"),
                "summary": row.get("brief") or "",
                "source_name": row.get("source_name") or "",
                "source_url": row.get("source_url") or "",
            }
            for row in candidates
        ],
    }
    return json_dumps_compact(payload)


def _status_for_decision(decision: dict[str, Any]) -> str:
    raw = str(decision.get("decision") or "").strip().lower()
    score = int(decision.get("fit_score") or 0)
    if raw == "accept":
        return "queued" if score >= BLOG_JUDGE_MIN_SCORE else "rejected_lowfit"
    if raw == "duplicate":
        return "rejected_duplicate"
    if raw == "offtopic":
        return "rejected_offtopic"
    return "rejected_lowfit"


def _apply_decisions(conn, decisions: list[dict[str, Any]], *, dry_run: bool) -> None:
    if dry_run:
        return
    now = datetime.now(timezone.utc)
    for decision in decisions:
        topic_id = int(decision.get("id") or 0)
        status = _status_for_decision(decision)
        expires_at = None
        if decision.get("is_timely") and decision.get("expires_in_days"):
            expires_at = now + timedelta(days=max(1, int(decision.get("expires_in_days") or 1)))
        title = str(decision.get("our_title") or "").strip() or None
        conn.execute(
            """
            UPDATE blog_topics
            SET status = ?,
                fit_score = ?,
                judge_reason = ?,
                duplicate_of = ?,
                title = COALESCE(?, title),
                primary_keyword = ?,
                category = ?,
                intent = ?,
                angle = ?,
                brief = ?,
                is_timely = ?,
                expires_at = ?
            WHERE id = ?
            """,
            [
                status,
                int(decision.get("fit_score") or 0),
                str(decision.get("judge_reason") or "")[:500],
                decision.get("duplicate_of"),
                title,
                normalize_keyword(decision.get("primary_keyword")),
                decision.get("category"),
                decision.get("intent"),
                decision.get("angle"),
                decision.get("brief"),
                bool(decision.get("is_timely")),
                expires_at,
                topic_id,
            ],
        )


def judge(*, dry_run: bool = False, seed: bool = False) -> dict[str, Any]:
    seed_result = seed_topics() if seed and not dry_run else {"inserted": [], "skipped": []}
    conn = get_db()
    try:
        expired = _expire_timely(conn, dry_run=dry_run)
        candidates = _candidate_rows(conn)
        existing = list_existing_articles_and_topics(conn)
        remaining, prefiltered = _apply_prefilter(conn, candidates, existing, dry_run=dry_run)
        decisions = list(prefiltered)
        usage = None
        if remaining:
            result = call_json(
                "judge",
                model=BLOG_MODEL_JUDGE,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=_build_user_prompt(remaining, existing),
            )
            payload = json.loads(result.content)
            llm_decisions = payload.get("decisions") or []
            decisions.extend(llm_decisions)
            _apply_decisions(conn, llm_decisions, dry_run=dry_run)
            if not dry_run:
                log_usage("judge", result)
            usage = {
                "model": result.model,
                "input_tokens": result.input_tokens,
                "cached_input_tokens": result.cached_input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": str(result.cost_usd),
            }
        if not dry_run:
            conn.commit()
        counts: dict[str, int] = {}
        for decision in decisions:
            status = _status_for_decision(decision) if decision.get("decision") != "duplicate" or decision.get("judge_reason") != "prefilter match" else "rejected_duplicate"
            counts[status] = counts.get(status, 0) + 1
        return {"expired": expired, "decisions": decisions, "counts": counts, "usage": usage, "seeds": seed_result}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", action="store_true", help="Insert idempotent seed topics before judging.")
    args = parser.parse_args()
    result = judge(dry_run=args.dry_run, seed=args.seed)
    print("BLOG_JUDGE_DRY_RUN" if args.dry_run else "BLOG_JUDGE_DONE")
    if result["seeds"]["inserted"] or result["seeds"]["skipped"]:
        print("seeds_inserted=" + json.dumps(result["seeds"]["inserted"]))
        print("seeds_skipped=" + json.dumps(result["seeds"]["skipped"]))
    print("expired=" + str(result["expired"]))
    print("counts=" + json.dumps(result["counts"], sort_keys=True))
    if result["usage"]:
        print("usage=" + json.dumps(result["usage"], sort_keys=True))
    for decision in result["decisions"][:10]:
        print(json.dumps(decision, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
