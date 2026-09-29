#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_pipeline import (  # noqa: E402
    BLOG_SOURCES,
    fetch_url,
    insert_candidate,
    parse_feed_items,
    parse_sitemap_items,
    robots_allows,
)
from app.video_shorts.services.db import get_db  # noqa: E402


def _fresh_items(items: list[dict], *, days: int = 30) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    fresh = []
    for item in items:
        published_at = item.get("published_at")
        if published_at is not None and published_at < cutoff:
            continue
        fresh.append(item)
    return fresh[:20]


def scout() -> dict[str, int | str]:
    counts: dict[str, int | str] = {}
    conn = get_db()
    try:
        for source in BLOG_SOURCES:
            if not source.enabled or not source.url:
                counts[source.name] = "disabled"
                continue
            if not robots_allows(source.url):
                counts[source.name] = "disabled_robots"
                continue
            status, _, _, data = fetch_url(source.url)
            if not status or status >= 400:
                counts[source.name] = "unreachable"
                continue
            try:
                if source.entry_kind == "feed":
                    items = parse_feed_items(data)
                else:
                    items = parse_sitemap_items(data, source_url=source.url)
            except Exception as exc:
                counts[source.name] = f"parse_error:{exc}"
                continue
            inserted = 0
            for item in _fresh_items(items):
                if insert_candidate(conn, source=source, item=item):
                    inserted += 1
            conn.commit()
            counts[source.name] = inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return counts


def main() -> int:
    counts = scout()
    print("BLOG_SCOUT_DONE")
    for name, count in counts.items():
        print(f"{name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
