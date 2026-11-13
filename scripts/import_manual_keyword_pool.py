#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app import create_app
from app.video_shorts.routes.api import (
    _insert_category_keyword_items_into_queue,
    _is_close_discovery_keyword_variant,
)
from app.video_shorts.services.db import get_db


DEFAULT_POOL = REPO_ROOT / "app" / "video_shorts" / "data" / "minti_keyword_pool_v1.csv"


def load_pool(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0].strip().lower() != "keyword,category":
        raise RuntimeError("Expected keyword,category header")
    rows = []
    for line_number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        if "," not in line:
            raise RuntimeError(f"Line {line_number} has no category separator")
        keyword, category = (part.strip() for part in line.split(",", 1))
        if not keyword or not category:
            raise RuntimeError(f"Line {line_number} has a blank keyword or category")
        rows.append({"keyword": keyword, "category": category})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    args = parser.parse_args()
    rows = load_pool(args.pool)
    app = create_app()
    with app.app_context():
        conn = get_db()
        try:
            existing = [str(row[0]).strip() for row in conn.execute("SELECT keyword FROM keyword_queue").fetchall()]
            accepted = list(existing)
            keep = []
            exact = 0
            fuzzy = 0
            existing_lower = {item.lower() for item in existing}
            for item in rows:
                keyword = item["keyword"]
                if keyword.lower() in existing_lower:
                    exact += 1
                    continue
                if _is_close_discovery_keyword_variant(keyword, accepted):
                    fuzzy += 1
                    continue
                keep.append(item)
                accepted.append(keyword)
                existing_lower.add(keyword.lower())
            inserted = 0
            if args.apply and keep:
                result = _insert_category_keyword_items_into_queue(
                    conn, keep, source="manual", priority=0, due_now=False,
                    blocked_categories=[], medical_focus=False,
                )
                inserted = int(result["newly_enqueued"])
                conn.commit()
            else:
                conn.rollback()
            print(json.dumps({
                "pool_rows": len(rows), "imported": inserted if args.apply else len(keep),
                "skipped_duplicates": exact + fuzzy, "skipped_exact": exact,
                "skipped_fuzzy": fuzzy, "applied": bool(args.apply),
            }, ensure_ascii=False))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
