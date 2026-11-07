#!/usr/bin/env python3
"""Prepare or apply the reviewed 2026 AI clipping tools comparison.

The script migrates the existing comparison row to the new keyword-focused
slug, preserving its database ID and publication date. It is deliberately
separate from the automated blog pipeline and defaults to a transactional dry
run. Use ``--apply`` only for manual publication.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # noqa: E402
    from scripts.safe_to_deploy import _load_env_file
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from safe_to_deploy import _load_env_file


POST_SOURCE = (
    ROOT
    / "app"
    / "video_shorts"
    / "blog_pipeline"
    / "manual_posts"
    / "best-ai-video-clipping-tools-2026.md"
)
OLD_SLUG = "best-opusclip-alternatives-for-busy-experts-who-need-less-clipping-work"
TARGET_SLUG = "best-ai-video-clipping-tools-2026"
OLD_PATH = f"/video_shorts/blog/{OLD_SLUG}/"
TARGET_PATH = f"/video_shorts/blog/{TARGET_SLUG}/"
RELATED_LINK = f"- [Best AI Video Clipping Tools in 2026: 6 Tools Compared]({TARGET_PATH})"


@dataclass(frozen=True)
class ArticleSource:
    title: str
    slug: str
    meta_title: str
    meta_description: str
    body: str
    reading_time: int


BACKLINK_SLUGS = (
    "free-youtube-to-shorts-tool",
    "how-to-repurpose-long-videos-into-short-clips",
)


def parse_post_source(text: str) -> ArticleSource:
    if not text.startswith("---\n"):
        raise ValueError("Manual post is missing front matter")
    try:
        _, raw_meta, body = text.split("---\n", 2)
    except ValueError as exc:
        raise ValueError("Manual post front matter is not closed") from exc

    meta: dict[str, object] = {}
    for raw_line in raw_meta.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        try:
            meta[key.strip()] = ast.literal_eval(value.strip())
        except (ValueError, SyntaxError):
            meta[key.strip()] = value.strip().strip('"').strip("'")

    body = body.strip()
    title = str(meta.get("title") or "").strip()
    slug = str(meta.get("slug") or "").strip()
    meta_title = str(meta.get("meta_title") or title).strip()
    meta_description = str(meta.get("meta_description") or "").strip()
    if not title or not meta_description or not body:
        raise ValueError("Manual post requires title, meta_description, and body")
    if slug != TARGET_SLUG:
        raise ValueError(f"Manual post must use the canonical slug: {TARGET_SLUG}")
    if re.search(r"(?m)^#\s+", body):
        raise ValueError("The Markdown body must not contain an H1; the template renders the title as H1")

    words = re.findall(r"\b[\w'-]+\b", body)
    return ArticleSource(
        title=title,
        slug=slug,
        meta_title=meta_title,
        meta_description=meta_description,
        body=body,
        reading_time=max(1, round(len(words) / 220)),
    )


def add_related_link(content: str) -> tuple[str, bool]:
    text = str(content or "").strip()
    if OLD_PATH in text:
        return text.replace(OLD_PATH, TARGET_PATH), True
    if TARGET_PATH in text:
        return text, False

    heading = re.search(r"(?im)^##\s+Related guides\s*$", text)
    if heading:
        line_end = text.find("\n", heading.end())
        if line_end == -1:
            return f"{text}\n\n{RELATED_LINK}\n", True
        return f"{text[:line_end + 1]}\n{RELATED_LINK}\n{text[line_end + 1:]}", True
    return f"{text}\n\n## Related guides\n\n{RELATED_LINK}\n", True


def update_comparison_article(conn, source: ArticleSource) -> int:
    old_row = conn.execute(
        "SELECT id, status FROM blog_articles WHERE slug = ? LIMIT 1",
        [OLD_SLUG],
    ).fetchone()
    new_row = conn.execute(
        "SELECT id, status FROM blog_articles WHERE slug = ? LIMIT 1",
        [TARGET_SLUG],
    ).fetchone()
    if old_row and new_row and int(old_row[0]) != int(new_row[0]):
        raise RuntimeError("Both old and new comparison slugs exist; refusing to create duplicate content")

    target = new_row or old_row
    if not target:
        raise RuntimeError(f"Existing comparison article not found: {OLD_SLUG}")
    if str(target[1] or "").lower() != "published":
        raise RuntimeError("Existing comparison article is not currently published")

    article_id = int(target[0])
    conn.execute(
        """
        UPDATE blog_articles
        SET title = ?,
            slug = ?,
            summary = ?,
            content = ?,
            meta_title = ?,
            meta_description = ?,
            author_name = 'MintiStudio Team',
            reading_time = ?,
            content_updated_at = CURRENT_TIMESTAMP,
            import_source = 'manual_blog_post',
            import_source_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        [
            source.title,
            source.slug,
            source.meta_description,
            source.body,
            source.meta_title,
            source.meta_description,
            source.reading_time,
            source.slug,
            article_id,
        ],
    )
    return article_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Publish the reviewed update to the existing article.")
    args = parser.parse_args()

    source = parse_post_source(POST_SOURCE.read_text(encoding="utf-8"))
    _load_env_file()
    from app.video_shorts.services.db import get_db

    conn = get_db()
    backlink_changes: list[str] = []
    try:
        published_rows = conn.execute(
            "SELECT slug, content FROM blog_articles WHERE status = 'published'"
        ).fetchall()
        published_content = {str(row[0]): str(row[1] or "") for row in published_rows}
        backlink_updates: dict[str, str] = {}

        for slug, content in published_content.items():
            if slug == OLD_SLUG:
                continue
            if OLD_PATH not in content:
                continue
            updated = content.replace(OLD_PATH, TARGET_PATH)
            backlink_changes.append(slug)
            backlink_updates[slug] = updated

        for slug in BACKLINK_SLUGS:
            if slug not in published_content:
                raise RuntimeError(f"Backlink article not found: {slug}")
            base_content = backlink_updates.get(slug, published_content[slug])
            updated, changed = add_related_link(base_content)
            if changed:
                if slug not in backlink_changes:
                    backlink_changes.append(slug)
                backlink_updates[slug] = updated

        if args.apply:
            update_comparison_article(conn, source)
            for slug, content in backlink_updates.items():
                conn.execute(
                    """
                    UPDATE blog_articles
                    SET content = ?, content_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE slug = ? AND status = 'published'
                    """,
                    [content, slug],
                )
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    mode = "APPLIED" if args.apply else "DRY_RUN"
    print(f"{mode} target={source.slug}")
    print(f"title={source.title}")
    print(f"reading_time={source.reading_time}")
    print(f"backlinks={','.join(backlink_changes) or 'already-present'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
