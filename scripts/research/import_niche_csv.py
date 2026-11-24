#!/usr/bin/env python3
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db, table_columns


REQUIRED_COLUMNS = {
    "youtube_channel_id",
    "title",
    "channel_url",
    "credential",
    "in_niche",
    "presenter",
    "language",
    "subscriber_count",
    "longform_90d",
    "shorts_90d",
    "avg_views_long_90d",
    "avg_views_short_90d",
    "discovery_lead_id",
    "source",
}


def _snapshot_date(path: Path) -> str:
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", path.name)
    if not match:
        raise ValueError("CSV filename must include a YYYY-MM-DD snapshot date")
    return match.group(1)


def _integer(value: object, *, default: int | None = None) -> int | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return default
    return int(float(text))


def import_csv(csv_path: Path, niche_slug: str) -> tuple[int, str]:
    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)
    snapshot = _snapshot_date(csv_path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing columns: {', '.join(sorted(missing))}")
        source_rows = list(reader)

    conn = get_db()
    try:
        if not table_columns(conn, "niches") or not table_columns(conn, "niche_channels"):
            raise RuntimeError("niches and niche_channels must be created manually before import")
        niche = conn.execute("SELECT id FROM niches WHERE slug = ? LIMIT 1", [niche_slug]).fetchone()
        if not niche:
            raise ValueError(f"Unknown niche slug: {niche_slug}")
        niche_id = niche[0]
        imported = 0
        seen: set[str] = set()
        for row in source_rows:
            channel_id = str(row.get("youtube_channel_id") or "").strip()
            if not channel_id or channel_id in seen:
                continue
            seen.add(channel_id)
            conn.execute(
                """
                INSERT INTO niche_channels (
                    niche_id, youtube_channel_id, snapshot_date, title, channel_url,
                    credential, in_niche, presenter, language, subscriber_count,
                    longform_90d, shorts_90d, avg_views_long_90d,
                    avg_views_short_90d, discovery_lead_id, source, country, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
                ON CONFLICT (niche_id, youtube_channel_id, snapshot_date) DO UPDATE SET
                    title = EXCLUDED.title,
                    channel_url = EXCLUDED.channel_url,
                    credential = EXCLUDED.credential,
                    in_niche = EXCLUDED.in_niche,
                    presenter = EXCLUDED.presenter,
                    language = EXCLUDED.language,
                    subscriber_count = EXCLUDED.subscriber_count,
                    longform_90d = EXCLUDED.longform_90d,
                    shorts_90d = EXCLUDED.shorts_90d,
                    avg_views_long_90d = EXCLUDED.avg_views_long_90d,
                    avg_views_short_90d = EXCLUDED.avg_views_short_90d,
                    discovery_lead_id = EXCLUDED.discovery_lead_id,
                    source = EXCLUDED.source,
                    country = EXCLUDED.country,
                    updated_at = now()
                """,
                [
                    niche_id,
                    channel_id,
                    snapshot,
                    str(row.get("title") or "").strip(),
                    str(row.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}").strip(),
                    str(row.get("credential") or "").strip(),
                    str(row.get("in_niche") or "").strip(),
                    str(row.get("presenter") or "").strip(),
                    str(row.get("language") or "").strip(),
                    _integer(row.get("subscriber_count")),
                    _integer(row.get("longform_90d"), default=0),
                    _integer(row.get("shorts_90d"), default=0),
                    _integer(row.get("avg_views_long_90d")),
                    _integer(row.get("avg_views_short_90d")),
                    str(row.get("discovery_lead_id") or "").strip() or None,
                    str(row.get("source") or "").strip(),
                    str(row.get("country") or "").strip().upper(),
                ],
            )
            imported += 1
        conn.commit()
        return imported, snapshot
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: scripts/research/import_niche_csv.py <csv> <niche_slug>", file=sys.stderr)
        return 2
    count, snapshot = import_csv(Path(sys.argv[1]).expanduser().resolve(), sys.argv[2].strip())
    print(f"IMPORTED niche={sys.argv[2].strip()} snapshot={snapshot} rows={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
