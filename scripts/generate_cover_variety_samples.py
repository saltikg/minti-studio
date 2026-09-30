#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_images import generate_blog_image  # noqa: E402
from app.video_shorts.services.db import get_db  # noqa: E402
from scripts.blog_pipeline import (  # noqa: E402
    BASE_URL,
    COVER_ARCHETYPE_LABELS,
    _ensure_cover_metadata_columns,
    _image_prompt_for_cover,
    _prepare_cover_plan,
)


LATEST_SLUG = "turn-prospect-objections-into-your-next-teaching-video"
SAMPLE_SCENES = {
    "A": {
        "title": "Object still life sample",
        "filename": "cover-archetype-a-object-still-life.png",
        "prompt": "A microphone, teaching cards, a small calendar, and a question mark card arranged as one clear idea.",
    },
    "C": {
        "title": "Metaphor object sample",
        "filename": "cover-archetype-c-metaphor-object.png",
        "prompt": "A magnifier over a video timeline, with the strongest moment glowing and clip cards nearby.",
    },
    "G": {
        "title": "Coach on a video call sample",
        "filename": "cover-archetype-g-coach-video-call.png",
        "prompt": "A coach on a laptop call with a client silhouette and teaching cards beside the screen.",
    },
}


def _absolute(url: str) -> str:
    return url if url.startswith("http") else f"{BASE_URL}{url}"


def _row_to_article(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "title": row[1],
        "slug": row[2],
        "summary": row[3],
        "cover_image_url": row[4],
        "cover": {"prompt": f"Prospect objection cards turning into a helpful teaching video plan for a coach."},
    }


def _regenerate_latest() -> dict[str, Any]:
    conn = get_db()
    try:
        _ensure_cover_metadata_columns(conn)
        row = conn.execute(
            """
            SELECT id, title, slug, summary, cover_image_url
            FROM blog_articles
            WHERE slug = ?
            LIMIT 1
            """,
            [LATEST_SLUG],
        ).fetchone()
        if not row:
            raise RuntimeError(f"article not found: {LATEST_SLUG}")
        article = _prepare_cover_plan(_row_to_article(row), {"title": row[1], "category": "workflow", "brief": row[3]})
        result = generate_blog_image(
            prompt=_image_prompt_for_cover(article),
            slug=str(article["slug"]),
            filename="cover.png",
            kind="cover",
            alt=str(article["title"]),
            overwrite=True,
        )
        article["cover_image_url"] = result.url
        conn.execute(
            """
            UPDATE blog_articles
            SET cover_image_url = ?,
                cover_archetype = ?,
                cover_character_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [
                result.url,
                article.get("cover_archetype"),
                json.dumps(article.get("cover_character") or {}, ensure_ascii=False),
                int(article["id"]),
            ],
        )
        conn.commit()
        return {
            "kind": "latest_article",
            "title": article["title"],
            "slug": article["slug"],
            "url": _absolute(result.url),
            "archetype": article.get("cover_archetype"),
            "archetype_label": COVER_ARCHETYPE_LABELS.get(str(article.get("cover_archetype") or "")),
            "cost_usd": str(result.cost_usd),
        }
    finally:
        conn.close()


def _generate_sample(archetype: str, sample: dict[str, str]) -> dict[str, Any]:
    article = _prepare_cover_plan(
        {
            "title": sample["title"],
            "slug": "style-board",
            "cover": {
                "prompt": sample["prompt"],
                "archetype": archetype,
                "archetypes": [archetype],
            },
        },
        {"title": sample["title"], "brief": sample["prompt"]},
    )
    result = generate_blog_image(
        prompt=_image_prompt_for_cover(article),
        slug="style-board",
        filename=sample["filename"],
        kind="cover",
        alt=sample["title"],
        overwrite=True,
    )
    return {
        "kind": "style_board_sample",
        "title": sample["title"],
        "url": _absolute(result.url),
        "archetype": archetype,
        "archetype_label": COVER_ARCHETYPE_LABELS[archetype],
        "cost_usd": str(result.cost_usd),
    }


def main() -> int:
    outputs = [_regenerate_latest()]
    for archetype, sample in SAMPLE_SCENES.items():
        outputs.append(_generate_sample(archetype, sample))
    total = sum((Decimal(item["cost_usd"]) for item in outputs), Decimal("0"))
    print(json.dumps({"covers": outputs, "total_cost_usd": str(total)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
