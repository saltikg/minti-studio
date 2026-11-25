#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db
from app.video_shorts.services.discovery_blocklist import apply_discovery_blocklist, discovery_blocklist


def main() -> int:
    conn = get_db()
    try:
        result = apply_discovery_blocklist(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(json.dumps({**result, "blocklist": list(discovery_blocklist())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
