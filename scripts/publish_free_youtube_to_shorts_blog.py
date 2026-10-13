#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_articles import _slugify  # noqa: E402
from app.video_shorts.services.db import get_db, table_columns  # noqa: E402


POST_SOURCE = ROOT / "app" / "video_shorts" / "blog_pipeline" / "manual_posts" / "free-youtube-to-shorts-tool.md"
COVER_URL = "/video_shorts/static/img/blog/free-youtube-to-shorts-tool/cover.png"
EXISTING_SLUG = "how-to-turn-youtube-videos-into-shorts"
NEW_POST_URL = "https://mintistudio.com/video_shorts/blog/free-youtube-to-shorts-tool/"
NEW_POST_TITLE = "Free YouTube to Shorts: Paste a Link, Get an Edited Short in Minutes"
TIP_TEXT = (
    ":::tip\n"
    "Want to skip the manual steps? [Paste your link into our free YouTube to Shorts tool]"
    "(/tools/youtube-to-shorts) and get an edited Short in minutes.\n"
    ":::\n"
)
RELATED_LINK = f"- [{NEW_POST_TITLE}]({NEW_POST_URL})"


def _parse_front_matter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{POST_SOURCE} is missing YAML-style front matter")
    try:
        _, raw_meta, body = text.split("---\n", 2)
    except ValueError as exc:
        raise ValueError(f"{POST_SOURCE} front matter is not closed") from exc
    meta: dict[str, object] = {}
    for raw_line in raw_meta.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        try:
            meta[key] = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            meta[key] = value.strip('"').strip("'")
    return meta, body.strip()


def _reading_time_minutes(markdown_text: str) -> int:
    words = re.findall(r"\b[\w'-]+\b", markdown_text)
    return max(1, round(len(words) / 220))


def _insert_tip_near_top(content: str) -> tuple[str, bool]:
    if "free YouTube to Shorts tool" in content:
        return content, False
    lines = content.splitlines()
    insert_at = 0
    seen_text = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if seen_text:
                insert_at = index + 1
                break
            continue
        if stripped.startswith("#"):
            continue
        seen_text = True
    else:
        insert_at = min(len(lines), 2)
    updated = lines[:insert_at] + [TIP_TEXT.rstrip(), ""] + lines[insert_at:]
    return "\n".join(updated).strip(), True


def _add_related_guide(content: str) -> tuple[str, bool]:
    if NEW_POST_URL in content:
        return content, False
    heading = re.search(r"(?im)^##\s+Related guides\s*$", content)
    if heading:
        line_end = content.find("\n", heading.end())
        if line_end == -1:
            return f"{content.rstrip()}\n\n{RELATED_LINK}\n", True
        return f"{content[:line_end + 1]}{RELATED_LINK}\n{content[line_end + 1:]}", True
    return f"{content.rstrip()}\n\n## Related guides\n\n{RELATED_LINK}\n", True


def _patch_existing_guide(conn) -> tuple[bool, bool]:
    row = conn.execute(
        "SELECT id, content FROM blog_articles WHERE slug = ? LIMIT 1",
        [EXISTING_SLUG],
    ).fetchone()
    if not row:
        return False, False
    article_id = int(row[0])
    content = str(row[1] or "")
    content, tip_added = _insert_tip_near_top(content)
    content, related_added = _add_related_guide(content)
    if tip_added or related_added:
        conn.execute(
            """
            UPDATE blog_articles
            SET content = ?, content_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [content, article_id],
        )
    return tip_added, related_added


def main() -> int:
    meta, body = _parse_front_matter(POST_SOURCE.read_text(encoding="utf-8"))
    title = str(meta.get("title") or NEW_POST_TITLE)
    slug = _slugify(meta.get("slug") or title)
    meta_description = str(meta.get("meta_description") or "").strip()
    published_at = datetime.utcnow()
    reading_time = _reading_time_minutes(body)
    summary = meta_description

    conn = get_db()
    try:
        if not table_columns(conn, "blog_articles"):
            raise RuntimeError("blog_articles table is missing")
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
                content_updated_at,
                import_source,
                import_source_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'MintiStudio Team', ?, 0, 'published', ?, CURRENT_TIMESTAMP, 'manual_blog_post', ?)
            ON CONFLICT (slug) DO UPDATE SET
                title = EXCLUDED.title,
                summary = EXCLUDED.summary,
                content = EXCLUDED.content,
                cover_image_url = EXCLUDED.cover_image_url,
                meta_title = EXCLUDED.meta_title,
                meta_description = EXCLUDED.meta_description,
                author_name = EXCLUDED.author_name,
                reading_time = EXCLUDED.reading_time,
                status = 'published',
                published_at = COALESCE(blog_articles.published_at, EXCLUDED.published_at),
                content_updated_at = CURRENT_TIMESTAMP,
                import_source = EXCLUDED.import_source,
                import_source_id = EXCLUDED.import_source_id,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """,
            [
                title,
                slug,
                summary,
                body,
                COVER_URL,
                title,
                meta_description,
                reading_time,
                published_at,
                slug,
            ],
        ).fetchone()
        tip_added, related_added = _patch_existing_guide(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"published_slug={slug}")
    print(f"article_id={int(row[0]) if row else 'UNKNOWN'}")
    print(f"reading_time={reading_time}")
    print(f"existing_tip_added={tip_added}")
    print(f"existing_related_added={related_added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
