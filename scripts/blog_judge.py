#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from itertools import islice
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
    title_similarity,
)
from app.video_shorts.services.db import get_db, table_columns  # noqa: E402


SYSTEM_PROMPT = """You judge blog topic candidates for MintiStudio.

Audience: solo educators, coaches, consultants, podcasters, mostly US English, who make long-form video and have something to sell.
MintiStudio turns long videos into Shorts. It offers DIY self-serve plus Autopilot done-for-you at $20/mo. It is not a tool you learn and run yourself.

A topic fits if a genuinely useful article for OUR AUDIENCE can naturally lead to MintiStudio.
Do not judge fit by whether the topic is literally about clipping. Creator monetization, Shorts
features/rules, YouTube Partner Program changes, sponsorship/shopping tools, policy changes, and
analytics changes are IN SCOPE when they affect solo educators, coaches, consultants, or podcasters
who publish long-form plus Shorts and have something to sell. Use category="news" or
category="monetization" for those. The brief must explain how the change affects our audience and
where MintiStudio naturally helps.

Wrong past rejections to avoid:
- "Made On YouTube: creator monetization / shopping" is in scope.
- "Made On YouTube: Shorts series / TV features" is in scope when framed for creators packaging and selling expertise.

Generic AI video tools, viewer-side YouTube features with no creator/business angle, broad industry
news, gaming, dance/meme content, and generic social-media news are off topic.

Duplicate means the same reader question, not the same words. Compare every candidate against ALL
existing articles and all queued/in_production/draft_ready/published topics, including seeds.
Examples:
- "best video repurposing tools" == "best OpusClip alternatives"
- "clip podcast highlights" == "make Shorts from podcast episodes"
Return duplicate_of with the exact existing title or slug.

Decision rules:
- accept: directly strong fit for our audience.
- adapt: the source is useful demand/news but the source framing is wrong, too broad, or too tool-led.
  Create an audience-first our_title and fill adapted_from with the original source framing in a short phrase.
- duplicate: same reader question as an existing article/topic.
- offtopic: cannot naturally help our audience.
- lowfit: weak but not completely unrelated.

For YouTube creator videos, use source_summary as a lightweight transcript summary. If accepted or
adapted, the brief must include claims to verify plus official sources to check in Phase 2. Do not
trust creator commentary as final authority.

Originality rule: our_title must be written from our audience's problem, not the competitor's SEO
phrasing. It must not reuse the source headline's main phrase or structure.
Bad: source "Best Video Repurposing Tools" -> ours "Best Video Repurposing Tools: What to Pick"
Good: source "Best Video Repurposing Tools" -> duplicate of our comparison seed, reject
Good: source "How to add captions to Shorts" -> ours "Do Coaches Need Captions on Every Short?"

Fit rubric: return sub-scores and fit_score as their sum:
- audience_fit 0-40: does it serve educators/coaches/consultants/podcasters specifically?
- minti_bridge 0-30: can the article lead naturally to MintiStudio / Autopilot?
- intent 0-20: commercial > informational
- timeliness 0-10

Always fill angle with what MintiStudio's article says that the competitor's does not.
Always fill brief in 2-3 sentences: reader, problem, promise, and where MintiStudio fits.
The competitor headline/summary is a demand signal only. Never reuse that text.

Return JSON object only.
{"decisions":[{"id":123,"decision":"accept|adapt|duplicate|offtopic|lowfit","fit_score":82,"audience_fit":32,"minti_bridge":26,"intent_score":16,"timeliness":8,"judge_reason":"one line","duplicate_of":null,"our_title":"...","primary_keyword":"...","category":"persona|monetization|comparison|craft|news","intent":"commercial|informational","angle":"...","brief":"2-3 sentences","adapted_from":null,"is_timely":false,"expires_in_days":null}]}
"""

BATCH_SIZE = 25
TITLE_SIMILARITY_REJECT_THRESHOLD = 0.60


def _topic_rows(conn, *, mode: str = "candidate") -> list[dict[str, Any]]:
    if mode == "rejudge_queued":
        where = "status = 'queued' AND source_type <> 'seed'"
    elif mode == "enrich_seeds":
        where = "source_type = 'seed'"
    elif mode == "rejudge_rejected":
        where = "source_type IN ('competitor_blog', 'youtube_news') AND status IN ('rejected_duplicate', 'rejected_offtopic')"
    else:
        where = "status = 'candidate'"
    columns = table_columns(conn, "blog_topics")
    source_summary_sql = "source_summary" if "source_summary" in columns else "NULL AS source_summary"
    adapted_from_sql = "adapted_from" if "adapted_from" in columns else "NULL AS adapted_from"
    rows = conn.execute(
        f"""
        SELECT id, title, source_type, source_name, source_url, source_title, brief,
               created_at, status, fit_score, judge_reason, duplicate_of,
               {source_summary_sql}, {adapted_from_sql}
        FROM blog_topics
        WHERE {where}
        ORDER BY created_at ASC
        """
    ).fetchall()
    return [row_to_dict(conn.description, row) for row in rows]


def _chunks(rows: list[dict[str, Any]], size: int = BATCH_SIZE):
    iterator = iter(rows)
    while True:
        chunk = list(islice(iterator, size))
        if not chunk:
            return
        yield chunk


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


def _fit_breakdown(decision: dict[str, Any]) -> dict[str, int]:
    return {
        "audience_fit": int(decision.get("audience_fit") or 0),
        "minti_bridge": int(decision.get("minti_bridge") or 0),
        "intent": int(decision.get("intent_score") or 0),
        "timeliness": int(decision.get("timeliness") or 0),
    }


def _ensure_fit_score(decision: dict[str, Any]) -> None:
    breakdown = _fit_breakdown(decision)
    total = sum(breakdown.values())
    if total > 0:
        decision["fit_score"] = total


def _apply_prefilter(conn, candidates: list[dict[str, Any]], existing: list[dict[str, str]], *, dry_run: bool, preserve_seed: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    remaining = []
    rejected = []
    for candidate in candidates:
        match = best_duplicate_match(str(candidate.get("title") or ""), existing)
        if match:
            rejected.append({"id": candidate["id"], "decision": "duplicate", "duplicate_of": match, "judge_reason": "prefilter match", "our_title": candidate.get("title")})
            if not dry_run:
                if preserve_seed:
                    previous = str(candidate.get("judge_reason") or "").strip()
                    reason = f"manual seed; duplicate flag: {match}"
                    if previous and "manual seed" not in previous:
                        reason = f"{previous}; duplicate flag: {match}"
                    conn.execute(
                        """
                        UPDATE blog_topics
                        SET judge_reason = ?
                        WHERE id = ?
                        """,
                        [reason[:500], candidate["id"]],
                    )
                else:
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
    ids = [row["id"] for row in candidates]
    payload = {
        "instruction": f"Return exactly {len(ids)} decisions, one for every candidate id in candidate_ids. Do not omit any candidate.",
        "candidate_ids": ids,
        "existing": existing,
        "candidates": [
            {
                "id": row["id"],
                "headline": row.get("source_title") or row.get("title"),
                "summary": row.get("brief") or "",
                "source_name": row.get("source_name") or "",
                "source_url": row.get("source_url") or "",
                "current_title": row.get("title") or "",
                "current_status": row.get("status") or "",
                "current_fit_score": row.get("fit_score"),
                "source_summary": row.get("source_summary") or "",
            }
            for row in candidates
        ],
    }
    return json_dumps_compact(payload)


def _missing_decision_ids(candidates: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> list[int]:
    expected = {int(row["id"]) for row in candidates}
    returned = set()
    for decision in decisions:
        try:
            returned.add(int(decision.get("id") or 0))
        except (TypeError, ValueError):
            continue
    return sorted(expected - returned)


def _status_for_decision(decision: dict[str, Any]) -> str:
    raw = str(decision.get("decision") or "").strip().lower()
    score = int(decision.get("fit_score") or 0)
    if raw in {"accept", "adapt"}:
        return "queued" if score >= BLOG_JUDGE_MIN_SCORE else "rejected_lowfit"
    if raw == "duplicate":
        return "rejected_duplicate"
    if raw == "offtopic":
        return "rejected_offtopic"
    return "rejected_lowfit"


def _decision_for_id(decisions: list[dict[str, Any]], topic_id: int) -> dict[str, Any] | None:
    for decision in decisions:
        try:
            if int(decision.get("id") or 0) == int(topic_id):
                return decision
        except (TypeError, ValueError):
            continue
    return None


def _validate_title_distance(
    *,
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    existing: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[Any]]:
    revised = list(decisions)
    repair_results: list[Any] = []
    by_id = {int(row["id"]): row for row in candidates}
    for decision in list(revised):
        if str(decision.get("decision") or "").strip().lower() not in {"accept", "adapt"}:
            continue
        topic_id = int(decision.get("id") or 0)
        candidate = by_id.get(topic_id)
        source_title = str((candidate or {}).get("source_title") or (candidate or {}).get("title") or "")
        our_title = str(decision.get("our_title") or "")
        if title_similarity(our_title, source_title) < TITLE_SIMILARITY_REJECT_THRESHOLD:
            continue
        repair_prompt = _build_user_prompt([candidate], existing) + (
            "\n\nThe proposed our_title was too close to the source headline. "
            "Return one decision for this candidate with a genuinely different our_title, "
            "written from the MintiStudio audience's problem. If no distinct title is possible, return lowfit."
        )
        result = call_json("judge", model=BLOG_MODEL_JUDGE, system_prompt=SYSTEM_PROMPT, user_prompt=repair_prompt)
        repair_results.append(result)
        payload = json.loads(result.content)
        replacement = (payload.get("decisions") or [{}])[0]
        if replacement:
            _ensure_fit_score(replacement)
        replacement_title = str(replacement.get("our_title") or "")
        if not replacement or title_similarity(replacement_title, source_title) >= TITLE_SIMILARITY_REJECT_THRESHOLD:
            decision.update(
                {
                    "decision": "lowfit",
                    "fit_score": min(int(decision.get("fit_score") or 0), BLOG_JUDGE_MIN_SCORE - 1),
                    "judge_reason": "title too close to source",
                }
            )
        else:
            for index, current in enumerate(revised):
                if int(current.get("id") or 0) == topic_id:
                    revised[index] = replacement
                    break
    return revised, repair_results


def _format_prev_reason(row: dict[str, Any], decision: dict[str, Any]) -> str:
    previous = str(row.get("judge_reason") or "").strip()
    reason = str(decision.get("judge_reason") or "").strip()
    if previous and not previous.startswith("prev:"):
        return f"prev: {previous}; {reason}"[:500]
    return reason[:500]


def _apply_decisions(conn, candidates: list[dict[str, Any]], decisions: list[dict[str, Any]], *, dry_run: bool, preserve_seed: bool = False, rejudge: bool = False) -> None:
    if dry_run:
        return
    by_id = {int(row["id"]): row for row in candidates}
    now = datetime.now(timezone.utc)
    has_adapted_from = "adapted_from" in table_columns(conn, "blog_topics")
    for decision in decisions:
        _ensure_fit_score(decision)
        topic_id = int(decision.get("id") or 0)
        candidate = by_id.get(topic_id, {})
        status = _status_for_decision(decision)
        expires_at = None
        if decision.get("is_timely") and decision.get("expires_in_days"):
            expires_at = now + timedelta(days=max(1, int(decision.get("expires_in_days") or 1)))
        title = str(decision.get("our_title") or "").strip() or None
        adapted_sql = ", adapted_from = ?" if has_adapted_from else ""
        params = (
            ([] if preserve_seed else [status, int(decision.get("fit_score") or 0)])
            + [
                _format_prev_reason(candidate, decision) if rejudge else str(decision.get("judge_reason") or "")[:500],
                candidate.get("duplicate_of") if preserve_seed else decision.get("duplicate_of"),
                None if preserve_seed else title,
                normalize_keyword(decision.get("primary_keyword")),
                decision.get("category"),
                decision.get("intent"),
                decision.get("angle"),
                decision.get("brief"),
                json.dumps(_fit_breakdown(decision), ensure_ascii=True),
                bool(decision.get("is_timely")),
                expires_at,
            ]
        )
        if has_adapted_from:
            params.append(None if preserve_seed else (str(decision.get("adapted_from") or "").strip() or None))
        params.append(topic_id)
        conn.execute(
            f"""
            UPDATE blog_topics
            SET {"status = ?," if not preserve_seed else ""}
                {"fit_score = ?," if not preserve_seed else ""}
                judge_reason = ?,
                duplicate_of = ?,
                title = COALESCE(?, title),
                primary_keyword = ?,
                category = ?,
                intent = ?,
                angle = ?,
                brief = ?,
                fit_breakdown = ?,
                is_timely = ?,
                expires_at = ?{adapted_sql}
            WHERE id = ?
            """,
            params,
        )


def _run_llm_batches(candidates: list[dict[str, Any]], existing: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    for batch in _chunks(candidates):
        prompt = _build_user_prompt(batch, existing)
        result = None
        llm_decisions: list[dict[str, Any]] = []
        missing_ids: list[int] = []
        for attempt in range(2):
            result = call_json(
                "judge",
                model=BLOG_MODEL_JUDGE,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prompt if attempt == 0 else prompt + "\n\nThe prior response omitted candidates. Return one decision for every candidate_id.",
            )
            payload = json.loads(result.content)
            llm_decisions = payload.get("decisions") or []
            for decision in llm_decisions:
                _ensure_fit_score(decision)
            missing_ids = _missing_decision_ids(batch, llm_decisions)
            if not missing_ids:
                break
        if missing_ids:
            raise RuntimeError(f"LLM omitted decisions for candidate ids: {missing_ids[:20]}")
        llm_decisions, repair_results = _validate_title_distance(candidates=batch, decisions=llm_decisions, existing=existing)
        decisions.extend(llm_decisions)
        if result:
            usages.append(
                {
                    "model": result.model,
                    "input_tokens": result.input_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                    "output_tokens": result.output_tokens,
                    "cost_usd": str(result.cost_usd),
                    "_result": result,
                }
            )
        for repair_result in repair_results:
            usages.append(
                {
                    "model": repair_result.model,
                    "input_tokens": repair_result.input_tokens,
                    "cached_input_tokens": repair_result.cached_input_tokens,
                    "output_tokens": repair_result.output_tokens,
                    "cost_usd": str(repair_result.cost_usd),
                    "_result": repair_result,
                }
            )
    return decisions, usages


def _before_after_rows(candidates: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for candidate in candidates:
        decision = _decision_for_id(decisions, int(candidate["id"])) or {}
        rows.append(
            {
                "id": candidate["id"],
                "source_title": candidate.get("source_title") or candidate.get("title") or "",
                "our_title": decision.get("our_title") or candidate.get("title") or "",
                "old": str(candidate.get("status") or ""),
                "new": str(decision.get("decision") or (_status_for_decision(decision) if decision else "missing")),
                "source_name": candidate.get("source_name") or "",
                "adapted_from": decision.get("adapted_from") or "",
                "duplicate_of": decision.get("duplicate_of"),
            }
        )
    return rows


def judge(
    *,
    dry_run: bool = False,
    seed: bool = False,
    rejudge_queued: bool = False,
    enrich_seeds: bool = False,
    rejudge_rejected: bool = False,
) -> dict[str, Any]:
    seed_result = seed_topics() if seed and not dry_run else {"inserted": [], "skipped": []}
    conn = get_db()
    try:
        expired = _expire_timely(conn, dry_run=dry_run)
        mode = "enrich_seeds" if enrich_seeds else ("rejudge_rejected" if rejudge_rejected else ("rejudge_queued" if rejudge_queued else "candidate"))
        candidates = _topic_rows(conn, mode=mode)
        exclude_ids = {int(row["id"]) for row in candidates}
        existing = list_existing_articles_and_topics(conn, exclude_topic_ids=exclude_ids)
        remaining, prefiltered = _apply_prefilter(conn, candidates, existing, dry_run=dry_run, preserve_seed=enrich_seeds)
        decisions = list(prefiltered)
        usages: list[dict[str, Any]] = []
        if remaining:
            llm_decisions, usages = _run_llm_batches(remaining, existing)
            decisions.extend(llm_decisions)
            _apply_decisions(
                conn,
                remaining,
                llm_decisions,
                dry_run=dry_run,
                preserve_seed=enrich_seeds,
                rejudge=rejudge_queued or rejudge_rejected,
            )
            if not dry_run:
                for usage in usages:
                    log_usage("judge", usage["_result"])
        if not dry_run:
            conn.commit()
        counts: dict[str, int] = {}
        for decision in decisions:
            status = _status_for_decision(decision) if decision.get("decision") != "duplicate" or decision.get("judge_reason") != "prefilter match" else "rejected_duplicate"
            counts[status] = counts.get(status, 0) + 1
        public_usages = [{key: value for key, value in usage.items() if key != "_result"} for usage in usages]
        return {
            "expired": expired,
            "decisions": decisions,
            "counts": counts,
            "usages": public_usages,
            "usage": public_usages[-1] if public_usages else None,
            "seeds": seed_result,
            "before_after": _before_after_rows(candidates, decisions) if (dry_run and (rejudge_queued or enrich_seeds or rejudge_rejected)) else [],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", action="store_true", help="Insert idempotent seed topics before judging.")
    parser.add_argument("--rejudge-queued", action="store_true", help="Re-judge non-seed queued topics.")
    parser.add_argument("--rejudge-rejected", action="store_true", help="Re-judge rejected competitor_blog/youtube_news topics.")
    parser.add_argument("--enrich-seeds", action="store_true", help="Fill angle/brief on seed topics without changing status or score.")
    args = parser.parse_args()
    result = judge(
        dry_run=args.dry_run,
        seed=args.seed,
        rejudge_queued=args.rejudge_queued,
        enrich_seeds=args.enrich_seeds,
        rejudge_rejected=args.rejudge_rejected,
    )
    print("BLOG_JUDGE_DRY_RUN" if args.dry_run else "BLOG_JUDGE_DONE")
    if result["seeds"]["inserted"] or result["seeds"]["skipped"]:
        print("seeds_inserted=" + json.dumps(result["seeds"]["inserted"]))
        print("seeds_skipped=" + json.dumps(result["seeds"]["skipped"]))
    print("expired=" + str(result["expired"]))
    print("counts=" + json.dumps(result["counts"], sort_keys=True))
    if result["usages"]:
        print("usages=" + json.dumps(result["usages"], sort_keys=True))
    if result["before_after"]:
        print("before_after:")
        print("id | source_name | source_title | old decision | new decision | our_title | adapted_from")
        for row in result["before_after"]:
            print(
                f"{row['id']} | {row['source_name']} | {row['source_title']} | "
                f"{row['old']} | {row['new']} | {row['our_title']} | {row.get('adapted_from') or ''}"
            )
    for decision in result["decisions"][:10]:
        print(json.dumps(decision, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
