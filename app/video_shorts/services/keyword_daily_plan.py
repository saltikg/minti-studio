from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from app.video_shorts.services.db import get_db


PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
MANUAL_DAILY_TARGET = 20
LLM_DAILY_TARGET = 5
CATEGORY_DAILY_CAP = 3


def _day_bounds(plan_date: date) -> tuple[datetime, datetime]:
    start_pt = datetime.combine(plan_date, time.min, tzinfo=PACIFIC_TZ)
    end_pt = start_pt + timedelta(days=1)
    return (
        start_pt.astimezone(timezone.utc).replace(tzinfo=None),
        end_pt.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _spread_across_categories(
    rows: Iterable[Any],
    needed: int,
    category_counts: Dict[Any, int],
) -> List[Any]:
    groups: Dict[Any, List[Any]] = defaultdict(list)
    category_names: Dict[Any, str] = {}
    for row in rows:
        category_key = row[3] if row[3] is not None else f"uncategorized:{row[0]}"
        groups[category_key].append(row)
        category_names[category_key] = str(row[4] or "")
    ordered_keys = sorted(groups, key=lambda key: (category_counts.get(key, 0), category_names[key].lower(), str(key)))
    selected: List[Any] = []
    while len(selected) < needed:
        progressed = False
        for key in ordered_keys:
            if len(selected) >= needed:
                break
            if category_counts.get(key, 0) >= CATEGORY_DAILY_CAP or not groups[key]:
                continue
            selected.append(groups[key].pop(0))
            category_counts[key] = category_counts.get(key, 0) + 1
            progressed = True
        if not progressed:
            break
    return selected


def plan_daily_keywords(
    *,
    plan_date: Optional[date] = None,
    generate_llm: bool = True,
    apply: bool = True,
) -> Dict[str, Any]:
    """Stage one Pacific day's 20 manual + 5 LLM exploration keywords."""
    target_date = plan_date or datetime.now(PACIFIC_TZ).date()
    start_utc, end_utc = _day_bounds(target_date)

    if generate_llm:
        from app.video_shorts.routes.api import generate_seed_keywords_into_queue

        for _attempt in range(3):
            read_conn = get_db()
            try:
                llm_available = int(
                    read_conn.execute(
                        "SELECT COUNT(*) FROM keyword_queue WHERE status = 'queued' AND source = 'llm'"
                    ).fetchone()[0]
                    or 0
                )
            finally:
                read_conn.close()
            if llm_available >= LLM_DAILY_TARGET:
                break
            generate_seed_keywords_into_queue()

    conn = get_db()
    try:
        planned_rows = conn.execute(
            """
            SELECT q.id, q.keyword, q.source, q.category_id, COALESCE(c.name, '')
            FROM keyword_queue q
            LEFT JOIN discovery_categories c ON c.id = q.category_id
            WHERE q.status = 'queued'
              AND q.source IN ('manual', 'llm')
              AND q.next_run_at >= ? AND q.next_run_at < ?
            ORDER BY q.priority ASC, q.created_at ASC, q.id ASC
            """,
            [start_utc, end_utc],
        ).fetchall()
        selected = list(planned_rows)
        category_counts: Dict[Any, int] = defaultdict(int)
        source_counts = {"manual": 0, "llm": 0}
        for row in selected:
            key = row[3] if row[3] is not None else f"uncategorized:{row[0]}"
            category_counts[key] += 1
            source_counts[str(row[2])] = source_counts.get(str(row[2]), 0) + 1

        for source, target in (("manual", MANUAL_DAILY_TARGET), ("llm", LLM_DAILY_TARGET)):
            needed = max(0, target - source_counts.get(source, 0))
            if not needed:
                continue
            candidates = conn.execute(
                """
                SELECT q.id, q.keyword, q.source, q.category_id, COALESCE(c.name, '')
                FROM keyword_queue q
                LEFT JOIN discovery_categories c ON c.id = q.category_id
                WHERE q.status = 'queued' AND q.source = ?
                  AND (q.next_run_at IS NULL OR NOT (q.next_run_at >= ? AND q.next_run_at < ?))
                ORDER BY COALESCE(q.times_searched, 0) ASC, q.created_at ASC, q.id ASC
                """,
                [source, start_utc, end_utc],
            ).fetchall()
            chosen = _spread_across_categories(candidates, needed, category_counts)
            selected.extend(chosen)
            source_counts[source] += len(chosen)

        selected_ids = [int(row[0]) for row in selected]
        if apply:
            # Keep every non-selected queued term out of the day's eligible queue. This
            # controls allocation without changing discovery's queue-consumption code.
            deferred_until = end_utc + timedelta(days=1)
            if selected_ids:
                placeholders = ",".join("?" for _ in selected_ids)
                conn.execute(
                    f"""
                    UPDATE keyword_queue
                    SET next_run_at = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'queued' AND id NOT IN ({placeholders})
                      AND (next_run_at IS NULL OR next_run_at < ?)
                    """,
                    [deferred_until, *selected_ids, end_utc],
                )
                conn.execute(
                    f"""
                    UPDATE keyword_queue
                    SET next_run_at = ?, priority = CASE WHEN source = 'manual' THEN 0 ELSE 1 END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    [start_utc, *selected_ids],
                )
            conn.commit()
        else:
            conn.rollback()

        return {
            "date": target_date.isoformat(),
            "manual": source_counts.get("manual", 0),
            "llm": source_counts.get("llm", 0),
            "keywords": [
                {"keyword": str(row[1]), "category": str(row[4] or ""), "source": str(row[2])}
                for row in selected
            ],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
