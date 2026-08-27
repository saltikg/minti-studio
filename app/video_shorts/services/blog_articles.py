from __future__ import annotations

import logging
import re
from datetime import date, datetime, time
from typing import Any, Optional

from app.video_shorts.services.blog_content import BLOG_ARTICLES
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns

logger = logging.getLogger(__name__)

BLOG_ARTICLES_TABLE = "blog_articles"
DEFAULT_IMPORT_SOURCE = "hardcoded_blog_articles"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_READING_TIME_RE = re.compile(r"(\d+)")
_DEFAULT_SEED_DONE = False


def _slugify(text: str) -> str:
    normalized = _SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")
    return normalized or "article"


def _normalize_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"draft", "published", "archived"}:
        return normalized
    return "draft"


def _normalize_optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalize_required_text(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _normalize_reading_time(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    match = _READING_TIME_RE.search(str(value))
    if not match:
        return None
    parsed = int(match.group(1))
    return parsed if parsed > 0 else None


def _normalize_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _table_exists(conn) -> bool:
    return bool(table_columns(conn, BLOG_ARTICLES_TABLE))


def _row_to_article(row: Any) -> dict[str, Any]:
    published_at = _normalize_datetime(row[13])
    created_at = _normalize_datetime(row[11])
    updated_at = _normalize_datetime(row[12])
    return {
        "id": int(row[0]),
        "title": row[1] or "",
        "slug": row[2] or "",
        "summary": row[3] or "",
        "content": row[4] or "",
        "cover_image_url": row[5] or "",
        "meta_title": row[6] or "",
        "meta_description": row[7] or "",
        "author_name": row[8] or "",
        "reading_time": row[9],
        "view_count": int(row[10] or 0),
        "created_at": created_at,
        "updated_at": updated_at,
        "published_at": published_at,
        "status": row[14] or "draft",
        "import_source": row[15] or "",
        "import_source_id": row[16] or "",
    }


def _select_columns_sql() -> str:
    return """
        SELECT
            id,
            title,
            slug,
            COALESCE(summary, ''),
            COALESCE(content, ''),
            COALESCE(cover_image_url, ''),
            COALESCE(meta_title, ''),
            COALESCE(meta_description, ''),
            COALESCE(author_name, ''),
            reading_time,
            COALESCE(view_count, 0),
            created_at,
            updated_at,
            published_at,
            status,
            COALESCE(import_source, ''),
            COALESCE(import_source_id, '')
        FROM blog_articles
    """


def _cover_image_url_from_legacy(article: dict[str, Any]) -> Optional[str]:
    for section in article.get("sections") or []:
        for image in section.get("images") or []:
            src = str(image.get("src") or "").strip().lstrip("/")
            if src:
                return f"/video_shorts/static/{src}"
    return None


def _legacy_article_to_markdown(article: dict[str, Any]) -> str:
    parts: list[str] = [f"# {article['title']}"]
    description = str(article.get("description") or "").strip()
    if description:
        parts.extend(["", description])
    for section in article.get("sections") or []:
        heading = str(section.get("heading") or "").strip()
        step_number = str(section.get("step_number") or "").strip()
        if heading:
            if step_number:
                parts.extend(["", f"## Step {step_number}: {heading}"])
            else:
                parts.extend(["", f"## {heading}"])
        for paragraph in section.get("paragraphs") or []:
            text = str(paragraph or "").strip()
            if text:
                parts.extend(["", text])
        bullets = [str(item or "").strip() for item in (section.get("bullets") or []) if str(item or "").strip()]
        if bullets:
            parts.append("")
            parts.extend([f"- {item}" for item in bullets])
        for image in section.get("images") or []:
            src = str(image.get("src") or "").strip().lstrip("/")
            alt = str(image.get("alt") or "").strip()
            if src:
                parts.extend(["", f"![{alt}](/video_shorts/static/{src})"])
    sources = article.get("sources") or []
    if sources:
        parts.extend(["", "## Sources", ""])
        for source in sources:
            label = str(source.get("label") or source.get("url") or "Source").strip()
            url = str(source.get("url") or "").strip()
            if url:
                parts.append(f"- [{label}]({url})")
    cta_title = str(article.get("cta_title") or "").strip()
    cta_body = str(article.get("cta_body") or "").strip()
    if cta_title or cta_body:
        parts.extend(["", "## Next step"])
        if cta_title:
            parts.extend(["", f"**{cta_title}**"])
        if cta_body:
            parts.extend(["", cta_body])
    return "\n".join(parts).strip()


def _legacy_article_seed_payload(article: dict[str, Any]) -> dict[str, Any]:
    published_at = _normalize_datetime(article.get("published_on"))
    return {
        "title": article["title"],
        "slug": _slugify(article.get("slug") or article["title"]),
        "summary": str(article.get("description") or "").strip(),
        "content": _legacy_article_to_markdown(article),
        "cover_image_url": _cover_image_url_from_legacy(article),
        "meta_title": _normalize_optional_text(article.get("meta_title")),
        "meta_description": _normalize_optional_text(article.get("meta_description") or article.get("description")),
        "author_name": _normalize_optional_text(article.get("author_name")) or "MintiStudio Team",
        "reading_time": _normalize_reading_time(article.get("reading_time")),
        "status": "published",
        "published_at": published_at,
        "import_source": DEFAULT_IMPORT_SOURCE,
        "import_source_id": _slugify(article.get("slug") or article["title"]),
    }


def ensure_default_blog_articles_seeded() -> dict[str, int | bool]:
    global _DEFAULT_SEED_DONE
    if _DEFAULT_SEED_DONE:
        return {"seeded": 0, "skipped": len(BLOG_ARTICLES), "table_missing": False}
    conn = None
    inserted = 0
    skipped = 0
    try:
        conn = get_db()
        if not _table_exists(conn):
            return {"seeded": 0, "skipped": 0, "table_missing": True}
        for article in BLOG_ARTICLES:
            payload = _legacy_article_seed_payload(article)
            row = conn.execute(
                """
                INSERT INTO blog_articles (
                    title,
                    slug,
                    summary,
                    content,
                    cover_image_url,
                    meta_title,
                    meta_description,
                    author_name,
                    reading_time,
                    view_count,
                    status,
                    published_at,
                    import_source,
                    import_source_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT (slug) DO NOTHING
                RETURNING id
                """,
                [
                    payload["title"],
                    payload["slug"],
                    payload["summary"],
                    payload["content"],
                    payload["cover_image_url"],
                    payload["meta_title"],
                    payload["meta_description"],
                    payload["author_name"],
                    payload["reading_time"],
                    payload["status"],
                    payload["published_at"],
                    payload["import_source"],
                    payload["import_source_id"],
                ],
            ).fetchone()
            if row:
                inserted += 1
            else:
                skipped += 1
        conn.commit()
        _DEFAULT_SEED_DONE = True
        return {"seeded": inserted, "skipped": skipped, "table_missing": False}
    except Exception:
        if conn is not None:
            conn.rollback()
        logger.exception("Failed to seed default blog articles")
        return {"seeded": inserted, "skipped": skipped, "table_missing": False}
    finally:
        if conn is not None:
            conn.close()


def list_published_blog_articles(*, limit: Optional[int] = None) -> list[dict[str, Any]]:
    conn = None
    try:
        conn = get_db_readonly()
        if not _table_exists(conn):
            return []
        sql = f"""
            {_select_columns_sql()}
            WHERE status = 'published'
            ORDER BY published_at DESC NULLS LAST, created_at DESC
        """
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_article(row) for row in rows]
    except Exception:
        logger.exception("Failed to load published blog articles")
        return []
    finally:
        if conn is not None:
            conn.close()


def get_published_blog_article_by_slug(slug: str) -> Optional[dict[str, Any]]:
    conn = None
    try:
        conn = get_db_readonly()
        if not _table_exists(conn):
            return None
        row = conn.execute(
            f"""
            {_select_columns_sql()}
            WHERE slug = ?
              AND status = 'published'
            LIMIT 1
            """,
            [str(slug or "").strip()],
        ).fetchone()
        return _row_to_article(row) if row else None
    except Exception:
        logger.exception("Failed to load blog article slug=%s", slug)
        return None
    finally:
        if conn is not None:
            conn.close()


def list_admin_blog_articles() -> list[dict[str, Any]]:
    conn = None
    try:
        conn = get_db_readonly()
        if not _table_exists(conn):
            return []
        rows = conn.execute(
            f"""
            {_select_columns_sql()}
            ORDER BY
                CASE status
                    WHEN 'published' THEN 0
                    WHEN 'draft' THEN 1
                    ELSE 2
                END,
                published_at DESC NULLS LAST,
                updated_at DESC
            """
        ).fetchall()
        return [_row_to_article(row) for row in rows]
    except Exception:
        logger.exception("Failed to load admin blog articles")
        return []
    finally:
        if conn is not None:
            conn.close()


def get_blog_article_by_id(article_id: int) -> Optional[dict[str, Any]]:
    conn = None
    try:
        conn = get_db_readonly()
        if not _table_exists(conn):
            return None
        row = conn.execute(
            f"""
            {_select_columns_sql()}
            WHERE id = ?
            LIMIT 1
            """,
            [int(article_id)],
        ).fetchone()
        return _row_to_article(row) if row else None
    except Exception:
        logger.exception("Failed to load blog article id=%s", article_id)
        return None
    finally:
        if conn is not None:
            conn.close()


def _save_payload_from_form(form: Any) -> dict[str, Any]:
    title = _normalize_required_text(form.get("title"), fallback="Untitled article")
    slug = _slugify(form.get("slug") or title)
    status = _normalize_status(form.get("status"))
    published_at = _normalize_datetime(form.get("published_at"))
    if status == "published" and published_at is None:
        published_at = datetime.utcnow()
    return {
        "title": title,
        "slug": slug,
        "summary": _normalize_optional_text(form.get("summary")),
        "content": _normalize_required_text(form.get("content"), fallback=""),
        "cover_image_url": _normalize_optional_text(form.get("cover_image_url")),
        "meta_title": _normalize_optional_text(form.get("meta_title")),
        "meta_description": _normalize_optional_text(form.get("meta_description")),
        "author_name": _normalize_optional_text(form.get("author_name")) or "MintiStudio Team",
        "reading_time": _normalize_reading_time(form.get("reading_time")),
        "status": status,
        "published_at": published_at,
    }


def create_blog_article(form: Any) -> dict[str, Any]:
    payload = _save_payload_from_form(form)
    conn = get_db()
    try:
        row = conn.execute(
            """
            INSERT INTO blog_articles (
                title,
                slug,
                summary,
                content,
                cover_image_url,
                meta_title,
                meta_description,
                author_name,
                reading_time,
                view_count,
                status,
                published_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            RETURNING id
            """,
            [
                payload["title"],
                payload["slug"],
                payload["summary"],
                payload["content"],
                payload["cover_image_url"],
                payload["meta_title"],
                payload["meta_description"],
                payload["author_name"],
                payload["reading_time"],
                payload["status"],
                payload["published_at"],
            ],
        ).fetchone()
        conn.commit()
        return get_blog_article_by_id(int(row[0])) or {}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_blog_article(article_id: int, form: Any) -> Optional[dict[str, Any]]:
    payload = _save_payload_from_form(form)
    conn = get_db()
    try:
        row = conn.execute(
            """
            UPDATE blog_articles
            SET
                title = ?,
                slug = ?,
                summary = ?,
                content = ?,
                cover_image_url = ?,
                meta_title = ?,
                meta_description = ?,
                author_name = ?,
                reading_time = ?,
                status = ?,
                published_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            RETURNING id
            """,
            [
                payload["title"],
                payload["slug"],
                payload["summary"],
                payload["content"],
                payload["cover_image_url"],
                payload["meta_title"],
                payload["meta_description"],
                payload["author_name"],
                payload["reading_time"],
                payload["status"],
                payload["published_at"],
                int(article_id),
            ],
        ).fetchone()
        conn.commit()
        if not row:
            return None
        return get_blog_article_by_id(int(row[0]))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_blog_article_status(article_id: int, status: str) -> bool:
    normalized_status = _normalize_status(status)
    published_at = datetime.utcnow() if normalized_status == "published" else None
    conn = get_db()
    try:
        result = conn.execute(
            """
            UPDATE blog_articles
            SET
                status = ?,
                published_at = CASE
                    WHEN ? = 'published' THEN COALESCE(published_at, ?)
                    WHEN ? IN ('draft', 'archived') THEN published_at
                    ELSE published_at
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [normalized_status, normalized_status, published_at, normalized_status, int(article_id)],
        )
        conn.commit()
        return (result.rowcount or 0) > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def increment_blog_article_view_count(article_id: int) -> None:
    conn = None
    try:
        conn = get_db()
        conn.execute(
            """
            UPDATE blog_articles
            SET view_count = COALESCE(view_count, 0) + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [int(article_id)],
        )
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        logger.exception("Failed to increment blog article view_count id=%s", article_id)
    finally:
        if conn is not None:
            conn.close()
