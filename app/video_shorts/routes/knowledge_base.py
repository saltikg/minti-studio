import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from flask import flash, g, jsonify, redirect, render_template, request, url_for

from app.video_shorts import video_shorts_bp
from app.video_shorts.config import SHORTS_DIR
from app.video_shorts.routes.videos import _collect_short_broadcast_entries
from app.video_shorts.services.brands import current_brand_id
from app.video_shorts.services.db import (
    ensure_channel_owner_schema,
    ensure_knowledge_base_schema,
    get_db,
    get_db_readonly,
)
from app.video_shorts.services.knowledge_base import (
    KnowledgeBaseGenerationError,
    extract_hashtags,
    find_duplicate_question,
    generate_short_qa_payload,
)


def _require_admin():
    current_user = getattr(g, "vs_current_user", None) or {}
    if not current_user:
        return None, redirect(url_for("video_shorts_bp.login", next=request.url))
    if (current_user.get("role") or "").lower() != "admin":
        flash("Admin access required.", "warning")
        return None, redirect(url_for("video_shorts_bp.channels_page"))
    return current_user, None


def _preview_text(value: str | None, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _video_public_url(video_id: str | None, video_url: str | None) -> str:
    if video_url:
        return video_url
    if not video_id:
        return ""
    return f"https://www.youtube.com/watch?v={video_id}"


def _source_entry_key(entry: dict) -> str:
    short_video_id = str(entry.get("short_video_id") or "").strip()
    if short_video_id:
        return short_video_id
    video_id = str(entry.get("video_id") or "").strip()
    plan_index = str(entry.get("plan_index") or "").strip()
    return f"{video_id}:{plan_index}"


def _source_anchor_id(source_entry_key: str) -> str:
    return "kb-entry-" + "".join(ch if ch.isalnum() else "-" for ch in source_entry_key)


def _safe_json_loads(value: object) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_sort_datetime(value):
    if value is None or isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _load_matching_plan_entry(source_entry: dict) -> dict:
    video_id = str(source_entry.get("video_id") or "").strip()
    if not video_id:
        return {}
    plan_path = SHORTS_DIR / f"{video_id}_plan.json"
    if not plan_path.exists():
        return {}
    try:
        payload = json.loads(plan_path.read_text())
    except Exception:
        return {}
    entries = payload.get("plan") or payload.get("clips") or []
    plan_index = str(source_entry.get("plan_index") or "").strip()
    short_video_id = str(source_entry.get("short_video_id") or "").strip()
    for entry in entries:
        entry_short_id = str(entry.get("yt_video_id") or entry.get("short_video_id") or "").strip()
        if short_video_id and entry_short_id and short_video_id == entry_short_id:
            return entry
    for entry in entries:
        entry_plan_index = str(entry.get("plan_index") or "").strip()
        if plan_index and entry_plan_index and plan_index == entry_plan_index:
            return entry
    return {}


def _build_generation_source(source_entry: dict, base_row: tuple) -> dict:
    plan_entry = _load_matching_plan_entry(source_entry)
    primary_short_text = (
        plan_entry.get("transcript_full_custom")
        or plan_entry.get("transcript_full")
        or source_entry.get("excerpt")
        or plan_entry.get("excerpt")
        or ""
    )
    description = (
        plan_entry.get("yt_description")
        or source_entry.get("description")
        or plan_entry.get("description")
        or ""
    )
    source_title = (
        source_entry.get("plan_title")
        or plan_entry.get("title")
        or plan_entry.get("yt_title")
        or base_row[2]
        or ""
    )
    hashtags = extract_hashtags(source_title, description)
    return {
        "source_title": source_title,
        "primary_short_text": primary_short_text,
        "description": description,
        "hashtags": hashtags,
        "support_transcript": base_row[4] or "",
        "video_url": _video_public_url(source_entry.get("short_video_id") or base_row[1], None),
        "plan_entry": plan_entry,
    }


def _is_json_request() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    requested_with = (request.headers.get("X-Requested-With") or "").lower()
    return "application/json" in accept or requested_with == "xmlhttprequest"


def _row_to_json(item: dict) -> dict:
    payload = deepcopy(item)
    published_at = payload.get("published_at")
    generation_updated_at = payload.get("generation_updated_at")
    if isinstance(published_at, datetime):
        payload["published_at"] = published_at.isoformat()
    if isinstance(generation_updated_at, datetime):
        payload["generation_updated_at"] = generation_updated_at.isoformat()
    return payload


def _load_generator_items(sort_dir: str = "desc") -> list[dict]:
    conn = get_db()
    ensure_channel_owner_schema(conn)
    ensure_knowledge_base_schema(conn)
    conn.close()

    conn = get_db_readonly()
    try:
        overview_entries = _collect_short_broadcast_entries(brand_id=current_brand_id())
        video_ids = sorted({entry.get("video_id") for entry in overview_entries if entry.get("video_id")})
        video_meta_rows = []
        transcript_rows = []
        if video_ids:
            placeholders = ", ".join("?" for _ in video_ids)
            video_meta_rows = conn.execute(
                f"""
                SELECT
                    v.video_id,
                    v.id AS video_pk,
                    v.title,
                    v.video_url,
                    v.published_at
                FROM youtube_videos v
                WHERE v.video_id IN ({placeholders})
                """,
                video_ids,
            ).fetchall()
            transcript_rows = conn.execute(
                f"""
                SELECT
                    video_id,
                    COALESCE(full_text, '') AS full_text
                FROM youtube_transcripts
                WHERE video_id IN ({placeholders})
                """,
                video_ids,
            ).fetchall()
        generation_rows = conn.execute(
            f"""
            SELECT
                g.id AS generation_id,
                g.source_entry_key,
                g.source_video_pk,
                g.source_video_id,
                g.source_plan_index,
                g.source_short_video_id,
                g.source_title,
                g.source_published_at,
                g.generation_status,
                g.main_question,
                g.short_answer,
                g.transcript_summary,
                g.raw_payload_json,
                g.updated_at AS generation_updated_at
            FROM shorts_kb_generations g
            ORDER BY
                COALESCE(g.source_published_at, g.updated_at) {sort_dir.upper()} NULLS LAST,
                g.updated_at DESC,
                g.id DESC
            """
        ).fetchall()
        similar_rows = conn.execute(
            """
            SELECT
                generation_id,
                id,
                question,
                sort_order,
                decision,
                page_id
            FROM shorts_kb_similar_questions
            ORDER BY generation_id, sort_order, created_at
            """
        ).fetchall()
        review_rows = conn.execute(
            """
            SELECT source_entry_key, COALESCE(is_relevant, true)
            FROM shorts_kb_source_reviews
            """
        ).fetchall()
    except Exception as exc:
        conn.close()
        raise exc
    conn.close()

    similar_map = {}
    for row in similar_rows:
        generation_id = str(row[0]) if row[0] is not None else ""
        if not generation_id:
            continue
        similar_map.setdefault(generation_id, []).append(
            {
                "id": str(row[1]),
                "question": row[2] or "",
                "sort_order": row[3] or 0,
                "decision": row[4] or "pending",
                "page_id": str(row[5]) if row[5] else None,
            }
        )

    generation_map = {}
    for row in generation_rows:
        generation_id = str(row[0]) if row[0] else None
        source_key = row[1] or ""
        generation_map[source_key] = {
            "generation_id": generation_id,
            "source_video_pk": row[2],
            "source_video_id": row[3] or "",
            "source_plan_index": row[4] or "",
            "source_short_video_id": row[5] or "",
            "source_title": row[6] or "",
            "source_published_at": row[7],
            "generation_status": row[8] or "generated",
            "main_question": row[9] or "",
            "short_answer": row[10] or "",
            "transcript_summary": row[11] or "",
            "generation_meta": _safe_json_loads(row[12]),
            "generation_updated_at": row[13],
            "similar_questions": similar_map.get(generation_id or "", []),
        }

    relevance_map = {str(row[0]): bool(row[1]) for row in review_rows if row and row[0]}
    video_meta_map = {
        row[0]: {
            "video_pk": row[1],
            "video_title": row[2] or "",
            "video_url": row[3] or "",
            "published_at": row[4],
        }
        for row in video_meta_rows
    }
    transcript_map = {row[0]: row[1] or "" for row in transcript_rows}

    items = []
    for entry in overview_entries:
        source_key = _source_entry_key(entry)
        base_meta = video_meta_map.get(entry.get("video_id"), {})
        generation = generation_map.get(source_key, {})
        transcript_full = transcript_map.get(entry.get("video_id"), "")
        short_video_id = entry.get("short_video_id")
        short_url = _video_public_url(short_video_id, None) if short_video_id else ""
        published_at = entry.get("publish_at_iso") or generation.get("source_published_at") or base_meta.get("published_at")
        title = entry.get("plan_title") or generation.get("source_title") or base_meta.get("video_title") or ""
        items.append(
            {
                "source_entry_key": source_key,
                "anchor_id": _source_anchor_id(source_key),
                "video_pk": base_meta.get("video_pk"),
                "video_id": entry.get("video_id"),
                "short_video_id": short_video_id,
                "title": title,
                "video_url": short_url or _video_public_url(entry.get("video_id"), base_meta.get("video_url")),
                "published_at": published_at,
                "source_status": entry.get("publish_status") or "unknown",
                "transcript_preview": _preview_text(transcript_full, 200),
                "transcript_full": transcript_full,
                "generation_id": generation.get("generation_id"),
                "generation_status": generation.get("generation_status") or "not_generated",
                "main_question": generation.get("main_question") or "",
                "short_answer": generation.get("short_answer") or "",
                "transcript_summary": generation.get("transcript_summary") or "",
                "generation_updated_at": generation.get("generation_updated_at"),
                "existing_match": (generation.get("generation_meta") or {}).get("existing_match"),
                "similar_questions": generation.get("similar_questions") or [],
                "is_relevant": relevance_map.get(source_key, True),
            }
        )
    items.sort(
        key=lambda item: (
            _normalize_sort_datetime(item.get("published_at")) is None,
            _normalize_sort_datetime(item.get("published_at")) or datetime.min,
        ),
        reverse=sort_dir == "desc",
    )
    return items


def _find_item_by_key(source_entry_key: str, sort_dir: str = "desc") -> dict | None:
    items = _load_generator_items(sort_dir=sort_dir)
    return next((item for item in items if item.get("source_entry_key") == source_entry_key), None)


def _upsert_page_for_decision(
    conn,
    *,
    owner_user_id: str | None,
    source_video_pk: int,
    generation_id: str,
    similar_question_id: str | None,
    page_type: str,
    question: str,
    answer: str,
    transcript_summary: str,
) -> str:
    row = None
    if page_type == "main":
        row = conn.execute(
            """
            SELECT id
            FROM shorts_kb_pages
            WHERE generation_id = ? AND page_type = 'main'
            """,
            [generation_id],
        ).fetchone()
    elif similar_question_id:
        row = conn.execute(
            """
            SELECT id
            FROM shorts_kb_pages
            WHERE similar_question_id = ?
            """,
            [similar_question_id],
        ).fetchone()
    if row:
        page_id = str(row[0])
        conn.execute(
            """
            UPDATE shorts_kb_pages
            SET owner_user_id = ?,
                generation_id = ?,
                similar_question_id = ?,
                question = ?,
                answer = ?,
                transcript_summary = ?,
                updated_at = now()
            WHERE id = ?
            """,
            [
                owner_user_id,
                generation_id,
                similar_question_id,
                question,
                answer,
                transcript_summary,
                page_id,
            ],
        )
        return page_id
    page_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO shorts_kb_pages (
            id,
            owner_user_id,
            source_video_pk,
            generation_id,
            similar_question_id,
            page_type,
            status,
            question,
            answer,
            transcript_summary
        )
        VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)
        """,
        [
            page_id,
            owner_user_id,
            source_video_pk,
            generation_id,
            similar_question_id,
            page_type,
            question,
            answer,
            transcript_summary,
        ],
    )
    return page_id


def _delete_similar_page(conn, similar_question_id: str) -> None:
    row = conn.execute(
        "SELECT id FROM shorts_kb_pages WHERE similar_question_id = ?",
        [similar_question_id],
    ).fetchone()
    if not row:
        return
    conn.execute("DELETE FROM shorts_kb_pages WHERE id = ?", [row[0]])


def _missing_schema_response(exc: Exception):
    message = str(exc).lower()
    if "shorts_kb_" not in message:
        raise exc
    flash("Knowledge Base tables are missing. Run db/migrations_20260322_shorts_kb.sql first.", "warning")
    return redirect(url_for("video_shorts_bp.channels_page"))


@video_shorts_bp.route("/knowledge-base/short-qa-generator")
def short_qa_generator():
    current_user, denial = _require_admin()
    if denial:
        return denial
    sort_key = (request.args.get("sort") or "published_at").strip().lower()
    sort_dir = (request.args.get("dir") or "desc").strip().lower()
    if sort_key not in {"published_at"}:
        sort_key = "published_at"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"
    published_at_sort_dir = "asc" if sort_dir == "desc" else "desc"

    try:
        items = _load_generator_items(sort_dir=sort_dir)
    except Exception as exc:
        return _missing_schema_response(exc)
    return render_template(
        "short_kb_generator.html",
        items=items,
        active_items=[item for item in items if item.get("is_relevant", True)],
        inactive_items=[item for item in items if not item.get("is_relevant", True)],
        current_user=current_user,
        sort_key=sort_key,
        sort_dir=sort_dir,
        published_at_sort_dir=published_at_sort_dir,
    )


@video_shorts_bp.route("/knowledge-base/short-qa-generator/generate", methods=["POST"])
def generate_short_qa():
    current_user, denial = _require_admin()
    if denial:
        return denial
    source_entry_key = (request.form.get("source_entry_key") or "").strip()
    if not source_entry_key:
        flash("Source short key missing.", "warning")
        return redirect(url_for("video_shorts_bp.short_qa_generator"))

    conn = get_db()
    ensure_channel_owner_schema(conn)
    ensure_knowledge_base_schema(conn)
    overview_entries = _collect_short_broadcast_entries(brand_id=current_brand_id())
    source_entry = next((entry for entry in overview_entries if _source_entry_key(entry) == source_entry_key), None)
    if not source_entry:
        conn.close()
        flash("Source short not found.", "warning")
        return redirect(url_for("video_shorts_bp.short_qa_generator"))
    try:
        row = conn.execute(
            """
            SELECT
                v.id,
                v.video_id,
                v.title,
                v.video_url,
                COALESCE(t.full_text, '')
            FROM youtube_videos v
            LEFT JOIN youtube_transcripts t ON t.video_id = v.video_id
            WHERE v.video_id = ?
            ORDER BY v.published_at DESC NULLS LAST, v.id DESC
            LIMIT 1
            """,
            [source_entry.get("video_id")],
        ).fetchone()
    except Exception as exc:
        conn.close()
        return _missing_schema_response(exc)
    if not row:
        conn.close()
        flash("Source short not found.", "warning")
        return redirect(url_for("video_shorts_bp.short_qa_generator"))

    source_payload = _build_generation_source(source_entry, row)
    try:
        payload = generate_short_qa_payload(source_payload)
    except KnowledgeBaseGenerationError as exc:
        conn.close()
        if _is_json_request():
            return jsonify(success=False, message=str(exc)), 400
        flash(str(exc), "danger")
        return redirect(url_for("video_shorts_bp.short_qa_generator", focus=source_entry_key))

    generation_row = conn.execute(
        "SELECT id FROM shorts_kb_generations WHERE source_entry_key = ?",
        [source_entry_key],
    ).fetchone()
    video_pk = int(row[0])
    generation_id = str(generation_row[0]) if generation_row else str(uuid4())
    duplicate_rows = conn.execute(
        """
        SELECT id, question, page_type, status
        FROM shorts_kb_pages
        WHERE question IS NOT NULL
          AND generation_id <> ?
        ORDER BY updated_at DESC
        LIMIT 500
        """,
        [generation_id],
    ).fetchall()
    duplicate_match = find_duplicate_question(
        payload["main_question"],
        [
            {
                "id": row[0],
                "question": row[1],
                "page_type": row[2],
                "status": row[3],
            }
            for row in duplicate_rows
        ],
    )
    raw_payload = {
        **payload,
        "existing_match": duplicate_match,
    }
    if generation_row:
        conn.execute(
            """
            UPDATE shorts_kb_generations
            SET owner_user_id = ?,
                source_video_pk = ?,
                source_entry_key = ?,
                source_video_id = ?,
                source_plan_index = ?,
                source_short_video_id = ?,
                source_title = ?,
                source_published_at = ?,
                generation_status = 'generated',
                main_question = ?,
                short_answer = ?,
                transcript_summary = ?,
                source_video_url = ?,
                generated_with_model = ?,
                raw_payload_json = ?,
                updated_at = now()
            WHERE id = ?
            """,
            [
                current_user.get("id"),
                video_pk,
                source_entry_key,
                source_entry.get("video_id"),
                str(source_entry.get("plan_index") or ""),
                source_entry.get("short_video_id"),
                source_entry.get("plan_title") or row[2] or "",
                source_entry.get("publish_at_iso"),
                payload["main_question"],
                payload["short_answer"],
                payload["transcript_summary"],
                payload.get("source_video_url") or "",
                payload.get("model") or "",
                json.dumps(raw_payload, ensure_ascii=False),
                generation_id,
            ],
        )
    else:
        conn.execute(
            """
            INSERT INTO shorts_kb_generations (
                id,
                owner_user_id,
                source_video_pk,
                source_entry_key,
                source_video_id,
                source_plan_index,
                source_short_video_id,
                source_title,
                source_published_at,
                generation_status,
                main_question,
                short_answer,
                transcript_summary,
                source_video_url,
                generated_with_model,
                raw_payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'generated', ?, ?, ?, ?, ?, ?)
            """,
            [
                generation_id,
                current_user.get("id"),
                video_pk,
                source_entry_key,
                source_entry.get("video_id"),
                str(source_entry.get("plan_index") or ""),
                source_entry.get("short_video_id"),
                source_entry.get("plan_title") or row[2] or "",
                source_entry.get("publish_at_iso"),
                payload["main_question"],
                payload["short_answer"],
                payload["transcript_summary"],
                payload.get("source_video_url") or "",
                payload.get("model") or "",
                json.dumps(raw_payload, ensure_ascii=False),
            ],
        )
    conn.execute("DELETE FROM shorts_kb_similar_questions WHERE generation_id = ?", [generation_id])
    for index, question in enumerate(payload.get("similar_questions") or [], start=1):
        conn.execute(
            """
            INSERT INTO shorts_kb_similar_questions (
                id,
                generation_id,
                question,
                sort_order,
                decision
            )
            VALUES (?, ?, ?, ?, 'pending')
            """,
            [
                str(uuid4()),
                generation_id,
                str(question),
                index,
            ],
        )

    _upsert_page_for_decision(
        conn,
        owner_user_id=current_user.get("id"),
        source_video_pk=video_pk,
        generation_id=generation_id,
        similar_question_id=None,
        page_type="main",
        question=str(payload["main_question"]),
        answer=str(payload["short_answer"]),
        transcript_summary=str(payload["transcript_summary"]),
    )
    conn.commit()
    conn.close()
    if duplicate_match:
        flash(
            f"Similar existing page found: {duplicate_match.get('question')} "
            f"({duplicate_match.get('page_type')}, similarity={duplicate_match.get('similarity')}).",
            "warning",
        )
    item = _find_item_by_key(source_entry_key)
    if _is_json_request():
        return jsonify(
            success=True,
            message="Q&A taslagi olusturuldu.",
            item=_row_to_json(item or {"source_entry_key": source_entry_key}),
        )
    flash("Q&A draft generated.", "success")
    return redirect(url_for("video_shorts_bp.short_qa_generator", focus=source_entry_key))


@video_shorts_bp.route("/knowledge-base/generations/<generation_id>/save", methods=["POST"])
def save_short_qa_generation(generation_id: str):
    current_user, denial = _require_admin()
    if denial:
        return denial

    conn = get_db()
    ensure_channel_owner_schema(conn)
    ensure_knowledge_base_schema(conn)
    try:
        generation = conn.execute(
            """
            SELECT id, source_video_pk, source_entry_key
            FROM shorts_kb_generations
            WHERE id = ?
            """,
            [generation_id],
        ).fetchone()
    except Exception as exc:
        conn.close()
        return _missing_schema_response(exc)
    if not generation:
        conn.close()
        flash("Generation not found.", "warning")
        return redirect(url_for("video_shorts_bp.short_qa_generator"))

    source_video_pk = int(generation[1])
    source_entry_key = generation[2] or str(source_video_pk)
    main_question = (request.form.get("main_question") or "").strip()
    short_answer = (request.form.get("short_answer") or "").strip()
    transcript_summary = request.form.get("transcript_summary")
    if transcript_summary is None:
        existing_summary_row = conn.execute(
            "SELECT transcript_summary FROM shorts_kb_generations WHERE id = ?",
            [generation_id],
        ).fetchone()
        transcript_summary = (existing_summary_row[0] or "") if existing_summary_row else ""
    else:
        transcript_summary = transcript_summary.strip()
    if not main_question or not short_answer:
        conn.close()
        if _is_json_request():
            return jsonify(success=False, message="Ana soru ve kisa cevap zorunlu."), 400
        flash("Main question and short answer are required.", "warning")
        return redirect(url_for("video_shorts_bp.short_qa_generator", focus=source_entry_key))

    conn.execute(
        """
        UPDATE shorts_kb_generations
        SET main_question = ?,
            short_answer = ?,
            transcript_summary = ?,
            generation_status = 'reviewed',
            updated_at = now()
        WHERE id = ?
        """,
        [main_question, short_answer, transcript_summary, generation_id],
    )
    _upsert_page_for_decision(
        conn,
        owner_user_id=current_user.get("id"),
        source_video_pk=source_video_pk,
        generation_id=generation_id,
        similar_question_id=None,
        page_type="main",
        question=main_question,
        answer=short_answer,
        transcript_summary=transcript_summary,
    )

    similar_rows = conn.execute(
        """
        SELECT id
        FROM shorts_kb_similar_questions
        WHERE generation_id = ?
        ORDER BY sort_order, created_at
        """,
        [generation_id],
    ).fetchall()
    for row in similar_rows:
        similar_id = str(row[0])
        question_value = (request.form.get(f"similar_question_{similar_id}") or "").strip()
        decision_value = (request.form.get(f"decision_{similar_id}") or "pending").strip().lower()
        if decision_value not in {"pending", "standalone", "related", "rejected"}:
            decision_value = "pending"
        if not question_value:
            question_value = "Untitled related question"
        if decision_value == "standalone":
            page_id = _upsert_page_for_decision(
                conn,
                owner_user_id=current_user.get("id"),
                source_video_pk=source_video_pk,
                generation_id=generation_id,
                similar_question_id=similar_id,
                page_type="standalone",
                question=question_value,
                answer=short_answer,
                transcript_summary=transcript_summary,
            )
        else:
            _delete_similar_page(conn, similar_id)
            page_id = None
        conn.execute(
            """
            UPDATE shorts_kb_similar_questions
            SET question = ?,
                decision = ?,
                page_id = ?,
                updated_at = now()
            WHERE id = ?
            """,
            [question_value, decision_value, page_id, similar_id],
        )

    conn.commit()
    conn.close()
    item = _find_item_by_key(source_entry_key)
    if _is_json_request():
        return jsonify(success=True, message="Taslak kaydedildi.", item=_row_to_json(item or {}))
    flash("Q&A draft saved.", "success")
    return redirect(url_for("video_shorts_bp.short_qa_generator", focus=source_entry_key))


@video_shorts_bp.route("/knowledge-base/short-qa-generator/item")
def short_qa_generator_item():
    _, denial = _require_admin()
    if denial:
        return denial
    source_entry_key = (request.args.get("source_entry_key") or "").strip()
    if not source_entry_key:
        return jsonify(success=False, message="source_entry_key missing"), 400
    item = _find_item_by_key(source_entry_key)
    if not item:
        return jsonify(success=False, message="Kayit bulunamadi."), 404
    return jsonify(success=True, item=_row_to_json(item))


@video_shorts_bp.route("/knowledge-base/short-qa-generator/relevance", methods=["POST"])
def set_short_qa_relevance():
    current_user, denial = _require_admin()
    if denial:
        return denial
    source_entry_key = (request.form.get("source_entry_key") or "").strip()
    value = (request.form.get("is_relevant") or "").strip().lower()
    if not source_entry_key:
        return jsonify(success=False, message="source_entry_key missing"), 400
    is_relevant = value in {"1", "true", "yes", "on"}
    conn = get_db()
    ensure_knowledge_base_schema(conn)
    row = conn.execute(
        "SELECT 1 FROM shorts_kb_source_reviews WHERE source_entry_key = ?",
        [source_entry_key],
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE shorts_kb_source_reviews
            SET owner_user_id = ?, is_relevant = ?, updated_at = now()
            WHERE source_entry_key = ?
            """,
            [current_user.get("id"), is_relevant, source_entry_key],
        )
    else:
        conn.execute(
            """
            INSERT INTO shorts_kb_source_reviews (source_entry_key, owner_user_id, is_relevant)
            VALUES (?, ?, ?)
            """,
            [source_entry_key, current_user.get("id"), is_relevant],
        )
    conn.commit()
    conn.close()
    item = _find_item_by_key(source_entry_key)
    return jsonify(
        success=True,
        message="Kayit guncellendi.",
        item=_row_to_json(item or {"source_entry_key": source_entry_key, "is_relevant": is_relevant}),
    )


@video_shorts_bp.route("/knowledge-base/generated-pages")
def generated_kb_pages():
    _, denial = _require_admin()
    if denial:
        return denial
    brand_id = current_brand_id()

    conn = get_db()
    ensure_channel_owner_schema(conn)
    ensure_knowledge_base_schema(conn)
    conn.close()

    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT
                p.id,
                p.question,
                v.title AS source_short_title,
                p.page_type,
                p.status,
                p.updated_at
            FROM shorts_kb_pages p
            LEFT JOIN youtube_videos v ON v.id = p.source_video_pk
            LEFT JOIN shorts_kb_generations g ON g.id = p.generation_id
            LEFT JOIN shorts_kb_source_reviews r ON r.source_entry_key = g.source_entry_key
            WHERE COALESCE(r.is_relevant, true) = true
              AND (? IS NULL OR v.brand_id = ?)
            ORDER BY p.updated_at DESC, p.created_at DESC
            LIMIT 200
            """
            ,
            [brand_id, brand_id],
        ).fetchall()
    except Exception as exc:
        conn.close()
        return _missing_schema_response(exc)
    conn.close()
    pages = [
        {
            "id": str(row[0]),
            "question": row[1] or "",
            "source_short_title": row[2] or "",
            "page_type": row[3] or "",
            "status": row[4] or "",
            "updated_at": row[5],
        }
        for row in rows
    ]
    return render_template("generated_kb_pages.html", pages=pages)
