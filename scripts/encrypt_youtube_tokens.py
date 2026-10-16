#!/usr/bin/env python3
"""Idempotently encrypt YouTube refresh tokens; output aggregate counts only."""
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


def main() -> int:
    from app.video_shorts.services.youtube_token_store import migrate_plaintext_refresh_tokens

    try:
        counts = migrate_plaintext_refresh_tokens()
    except Exception:
        print("TOKEN_MIGRATION_FAILED: check YOUTUBE_TOKEN_ENC_KEY and database access; no token values logged.", file=sys.stderr)
        return 1
    for table, count in counts.items():
        print(f"{table}: encrypted_rows={count}")
    print("TOKEN_MIGRATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
