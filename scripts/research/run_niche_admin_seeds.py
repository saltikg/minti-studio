#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.niche_admin_seeds import add_niche_channels
from app.video_shorts.services.db import get_db_readonly
from app.video_shorts.services.niche_definitions import load_niche_definition
from app.video_shorts.services.niches import load_niche_snapshot

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify_turkish_niche_channel_types import recompute_top_shorts


def main() -> int:
    report = add_niche_channels(
        "turkish_religious_talks",
        ["Herkul", "VideonTR", "Sakirtmen57", "nurnevahizmetten"],
        featured_seed_ids=["UCpZTTbddOVYPvQj02Zu8x_A"],
    )
    conn = get_db_readonly()
    try:
        definition = load_niche_definition(conn, slug="turkish_religious_talks")
        _selected, rows = load_niche_snapshot(conn, "turkish_religious_talks")
    finally:
        conn.close()
    if definition:
        report["top_shorts"] = recompute_top_shorts(definition, rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
