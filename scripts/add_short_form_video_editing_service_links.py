#!/usr/bin/env python3
"""Add contextual links to the short-form editing service page.

This is an explicit, idempotent content update for two existing articles. It is
not part of the automated blog publishing pipeline. Run with ``--apply`` only
after the landing page has been reviewed and is ready to deploy.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from scripts.safe_to_deploy import _load_env_file
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from safe_to_deploy import _load_env_file


SERVICE_PATH = "/services/short-form-video-editing"


@dataclass(frozen=True)
class ArticlePatch:
    slug: str
    old: str
    new: str


PATCHES = (
    ArticlePatch(
        slug="done-for-you-vs-diy-video-clipping",
        old=(
            "[Start free with MintiStudio](https://mintistudio.com/video_shorts/register). "
            "If you want production handled, try Autopilot’s first month free, with no card required."
        ),
        new=(
            "[Explore MintiStudio's done-for-you short form video editing service]"
            f"({SERVICE_PATH}) to see how Autopilot prepares ready-to-review Shorts, or "
            "[try the free AI clipping tool](/tools/youtube-to-shorts) if you want to run the workflow yourself."
        ),
    ),
    ArticlePatch(
        slug="weekly-shorts-workflow-for-busy-creators",
        old=(
            "If you want the outcome without managing every step, MintiStudio Autopilot follows the same workflow for you. "
            "The editor team uses MintiStudio to turn your long videos into Shorts, prepare them for publishing, and keep "
            "your schedule moving, so you can stay focused on creating the original content."
        ),
        new=(
            "If you want the outcome without managing every step, "
            "[MintiStudio's done-for-you short form video editing service]"
            f"({SERVICE_PATH}) uses Autopilot to turn your long videos into ready-to-review Shorts, prepare captions, "
            "vertical framing and titles, and keep the scheduling workflow moving while you focus on the original content."
        ),
    ),
)


def update_article_content(content: str, patch: ArticlePatch) -> tuple[str, bool]:
    text = str(content or "")
    if SERVICE_PATH in text:
        return text, False
    if patch.old not in text:
        raise ValueError(f"Expected insertion text was not found for {patch.slug}")
    return text.replace(patch.old, patch.new, 1), True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the reviewed link updates to the database.")
    args = parser.parse_args()

    _load_env_file()
    from app.video_shorts.services.db import get_db

    conn = get_db()
    changed_slugs: list[str] = []
    try:
        for patch in PATCHES:
            row = conn.execute(
                "SELECT content FROM blog_articles WHERE slug = ? AND status = 'published' LIMIT 1",
                [patch.slug],
            ).fetchone()
            if not row:
                raise RuntimeError(f"Published article not found: {patch.slug}")
            updated, changed = update_article_content(str(row[0] or ""), patch)
            if not changed:
                continue
            changed_slugs.append(patch.slug)
            if args.apply:
                conn.execute(
                    """
                    UPDATE blog_articles
                    SET content = ?, content_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE slug = ? AND status = 'published'
                    """,
                    [updated, patch.slug],
                )
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    mode = "APPLIED" if args.apply else "DRY_RUN"
    print(f"{mode} changed={len(changed_slugs)} slugs={','.join(changed_slugs) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
