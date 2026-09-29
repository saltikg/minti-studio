from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.video_shorts.services.db import get_db, get_db_readonly, table_columns


RUN_STATUSES = ("running", "draft_ready", "needs_you", "failed")
STAGE_ORDER = ("writer", "checks", "reviewer_1", "revision_1", "reviewer_2", "revision_2", "designer", "images")


def _row_to_dict(description, row) -> dict[str, Any]:
    return {description[index][0]: value for index, value in enumerate(row)}


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def create_run(conn, *, topic_id: int, dry_run: bool = False) -> int | None:
    if dry_run:
        return None
    row = conn.execute(
        """
        INSERT INTO blog_pipeline_runs (topic_id, status, current_stage, total_cost_usd)
        VALUES (?, 'running', 'writer', 0)
        RETURNING id
        """,
        [int(topic_id)],
    ).fetchone()
    return int(row[0]) if row else None


def finish_run(conn, run_id: int | None, *, status: str, article_id: int | None = None, final_review_score: int | None = None, error: str | None = None) -> None:
    if not run_id:
        return
    conn.execute(
        """
        UPDATE blog_pipeline_runs
        SET status = ?,
            article_id = COALESCE(?, article_id),
            final_review_score = COALESCE(?, final_review_score),
            error = ?,
            finished_at = CURRENT_TIMESTAMP,
            total_cost_usd = COALESCE((SELECT SUM(COALESCE(cost_usd, 0)) FROM blog_pipeline_stages WHERE run_id = ?), 0)
        WHERE id = ?
        """,
        [status, article_id, final_review_score, error, run_id, run_id],
    )


def set_current_stage(conn, run_id: int | None, stage: str) -> None:
    if not run_id:
        return
    conn.execute(
        "UPDATE blog_pipeline_runs SET current_stage = ? WHERE id = ?",
        [stage, run_id],
    )


def record_stage(
    conn,
    *,
    run_id: int | None,
    seq: int,
    stage: str,
    status: str,
    model: str | None = None,
    output: Any = None,
    score: int | None = None,
    notes: str | None = None,
    cost_usd: Decimal | float | str | None = None,
) -> None:
    if not run_id:
        return
    conn.execute(
        """
        INSERT INTO blog_pipeline_stages (
            run_id, seq, stage, status, model, output, score, notes, cost_usd, finished_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [
            run_id,
            int(seq),
            stage,
            status,
            model,
            json.dumps(output, ensure_ascii=False) if output is not None else None,
            score,
            notes,
            cost_usd,
        ],
    )


def list_admin_runs(limit: int = 100) -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        if not table_columns(conn, "blog_pipeline_runs"):
            return []
        article_slug_sql = "a.slug" if table_columns(conn, "blog_articles") else "NULL"
        rows = conn.execute(
            f"""
            SELECT r.id, r.topic_id, r.article_id, r.status, r.current_stage, r.final_review_score,
                   r.total_cost_usd, r.error, r.started_at, r.finished_at,
                   t.title AS topic_title,
                   {article_slug_sql} AS article_slug
            FROM blog_pipeline_runs r
            LEFT JOIN blog_topics t ON t.id = r.topic_id
            LEFT JOIN blog_articles a ON a.id = r.article_id
            ORDER BY r.started_at DESC
            LIMIT ?
            """,
            [int(limit)],
        ).fetchall()
        runs = [_row_to_dict(conn.description, row) for row in rows]
        if not runs:
            return []
        run_ids = [int(run["id"]) for run in runs]
        placeholders = ",".join(["?"] * len(run_ids))
        stage_rows = conn.execute(
            f"""
            SELECT run_id, stage, status, score, cost_usd
            FROM blog_pipeline_stages
            WHERE run_id IN ({placeholders})
            ORDER BY seq ASC, id ASC
            """,
            run_ids,
        ).fetchall()
        stages_by_run: dict[int, list[dict[str, Any]]] = {}
        for row in stage_rows:
            item = _row_to_dict(conn.description, row)
            stages_by_run.setdefault(int(item["run_id"]), []).append(item)
        for run in runs:
            run["stages"] = stages_by_run.get(int(run["id"]), [])
        return runs
    finally:
        conn.close()


def get_admin_run_detail(run_id: int) -> dict[str, Any] | None:
    conn = get_db_readonly()
    try:
        if not table_columns(conn, "blog_pipeline_runs"):
            return None
        row = conn.execute(
            """
            SELECT r.id, r.topic_id, r.article_id, r.status, r.current_stage, r.final_review_score,
                   r.total_cost_usd, r.error, r.started_at, r.finished_at,
                   t.title AS topic_title, t.brief AS topic_brief,
                   a.title AS article_title, a.slug AS article_slug, a.content AS article_content,
                   a.cover_image_url AS article_cover_image_url
            FROM blog_pipeline_runs r
            LEFT JOIN blog_topics t ON t.id = r.topic_id
            LEFT JOIN blog_articles a ON a.id = r.article_id
            WHERE r.id = ?
            LIMIT 1
            """,
            [int(run_id)],
        ).fetchone()
        if not row:
            return None
        run = _row_to_dict(conn.description, row)
        stage_rows = conn.execute(
            """
            SELECT id, seq, stage, status, model, output, score, notes, cost_usd, started_at, finished_at
            FROM blog_pipeline_stages
            WHERE run_id = ?
            ORDER BY seq ASC, id ASC
            """,
            [int(run_id)],
        ).fetchall()
        stages = [_row_to_dict(conn.description, stage_row) for stage_row in stage_rows]
        for stage in stages:
            stage["output"] = _json_value(stage.get("output"))
        run["stages"] = stages
        return run
    finally:
        conn.close()


def publish_pipeline_topic_for_article(conn, *, article_id: int) -> None:
    if not table_columns(conn, "blog_pipeline_runs"):
        return
    row = conn.execute(
        """
        SELECT topic_id
        FROM blog_pipeline_runs
        WHERE article_id = ?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        [int(article_id)],
    ).fetchone()
    if not row:
        return
    conn.execute(
        """
        UPDATE blog_topics
        SET status = 'published'
        WHERE id = ?
          AND status IN ('draft_ready', 'in_production', 'queued')
        """,
        [int(row[0])],
    )
