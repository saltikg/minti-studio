#!/usr/bin/env python3
"""Recompute discovery lead clinician labels with the strict credential matcher."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app import create_app
from app.video_shorts.routes.api import _clinical_credential_match
from app.video_shorts.services.db import get_db


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist recomputed labels.")
    parser.add_argument("--sample-size", type=int, default=20)
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT id, channel_title, channel_description FROM discovery_leads ORDER BY id"
            ).fetchall()
            matched = []
            for row in rows:
                match = _clinical_credential_match(row[1], row[2])
                if match:
                    matched.append(
                        {
                            "id": str(row[0]),
                            "channel": str(row[1] or ""),
                            "token": match["token"],
                            "context": match["context"],
                        }
                    )
                if args.apply:
                    conn.execute(
                        "UPDATE discovery_leads SET is_clinician = ? WHERE id = ?",
                        [bool(match), row[0]],
                    )
            if args.apply:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()

    random.Random(20261102).shuffle(matched)
    print(
        json.dumps(
            {
                "applied": bool(args.apply),
                "total_discovery_leads": len(rows),
                "is_clinician_true": len(matched),
                "sample": matched[: max(0, args.sample_size)],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
