#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.niche_admin_seeds import SeedRunner, _clean_name
from app.video_shorts.services.niches import load_niche_snapshot


SLUGS = ("menopause_hormones", "turkish_religious_talks")


def _affected_admin_rows() -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        result = conn.execute(
            """
            SELECT n.slug, nc.youtube_channel_id, nc.title,
                   COALESCE(nc.on_topic_count, 0) AS old_core,
                   nc.classification_title_count
            FROM niche_channels nc
            JOIN niches n ON n.id = nc.niche_id
            WHERE n.slug IN (?, ?)
              AND nc.snapshot_date = (
                  SELECT MAX(x.snapshot_date)
                  FROM niche_channels x
                  WHERE x.niche_id = nc.niche_id
              )
              AND nc.admin_include = TRUE
              AND (
                  COALESCE(nc.on_topic_count, 0) = 0
                  OR COALESCE(nc.classification_title_count, 0) = 0
              )
            ORDER BY n.slug, lower(nc.title)
            """,
            list(SLUGS),
        )
        columns = [str(item[0]) for item in result.description or []]
        return [dict(zip(columns, row)) for row in result.fetchall()]
    finally:
        conn.close()


def _persist_classification(
    runner: SeedRunner,
    channel_id: str,
    enriched: dict[str, Any],
    result: dict[str, Any],
) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE niche_channels
            SET title=?, presenter=?, language=?, subscriber_count=?,
                longform_90d=?, shorts_90d=?, avg_views_long_90d=?,
                avg_views_short_90d=?, primary_niche=?, on_topic_share=?,
                on_topic_count=?, entity=?, reclass_reason=?, views_90d=?,
                country=?, audience_language=?, primary_focus=?,
                classification_status=?, classification_title_count=?,
                channel_type=?, updated_at=CURRENT_TIMESTAMP
            WHERE niche_id=? AND snapshot_date=? AND youtube_channel_id=?
            """,
            [
                enriched["channel"]["title"] or channel_id,
                result["presenter"],
                "Turkish" if result["language"] == "tr" else result["language"],
                enriched["channel"]["subscriber_count"],
                enriched["longform_90d"], enriched["shorts_90d"],
                enriched["avg_views_long_90d"], enriched["avg_views_short_90d"],
                runner.niche_slug, result["core_share"], result["core_count"],
                result["entity"], result["reason"], enriched["views_90d"],
                enriched["channel"]["country"].upper(), result["language"],
                result["primary_focus"], result["classification_status"],
                result["title_count"], result["channel_type"],
                runner.definition["id"], runner.selected["snapshot_date"], channel_id,
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def backfill_admin_rows(runners: dict[str, SeedRunner]) -> list[dict[str, Any]]:
    output = []
    for row in _affected_admin_rows():
        runner = runners[row["slug"]]
        enriched = runner.enrich(row["youtube_channel_id"], force_refresh=True)
        result = runner.classify(enriched)
        _persist_classification(runner, row["youtube_channel_id"], enriched, result)
        output.append({
            "niche": row["slug"], "channel": enriched["channel"]["title"],
            "channel_id": row["youtube_channel_id"], "old_core": int(row["old_core"] or 0),
            "new_core": result["core_count"], "title_count": result["title_count"],
            "primary_focus": result["primary_focus"], "channel_type": result["channel_type"],
        })
    return output


def audit_big_zero_view_channels(
    runners: dict[str, SeedRunner],
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    output = []
    for slug, runner in runners.items():
        conn = get_db_readonly()
        try:
            _selected, rows = load_niche_snapshot(conn, slug)
        finally:
            conn.close()
        targets = [
            row for row in rows
            if int(row.get("subscriber_count") or 0) >= 100_000
            and int(row.get("views_90d") or 0) == 0
        ]
        for row in targets:
            channel_id = str(row.get("youtube_channel_id") or "")
            try:
                item = runner._channels_lookup(channel_id=channel_id, force_refresh=True)
                enriched = runner.enrich(channel_id, item, force_refresh=True)
                fresh_title = enriched["channel"]["title"]
                title_matches = _clean_name(fresh_title) == _clean_name(row.get("title"))
                latest_text = str(enriched.get("latest_upload_at") or "")
                try:
                    latest = datetime.fromisoformat(latest_text.replace("Z", "+00:00"))
                except ValueError:
                    latest = None
                if not title_matches:
                    verdict = "wrong id"
                elif int(enriched["views_90d"] or 0) > 0:
                    verdict = "active; stale cache fixed"
                elif latest is None or latest < cutoff:
                    verdict = "truly inactive"
                else:
                    verdict = "active uploads with zero public views"
                if title_matches:
                    conn = get_db()
                    try:
                        conn.execute(
                            """
                            UPDATE niche_channels
                            SET views_90d=?, longform_90d=?, shorts_90d=?,
                                avg_views_long_90d=?, avg_views_short_90d=?,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE niche_id=? AND snapshot_date=? AND youtube_channel_id=?
                            """,
                            [
                                enriched["views_90d"], enriched["longform_90d"],
                                enriched["shorts_90d"], enriched["avg_views_long_90d"],
                                enriched["avg_views_short_90d"], runner.definition["id"],
                                runner.selected["snapshot_date"], channel_id,
                            ],
                        )
                        conn.commit()
                    finally:
                        conn.close()
                output.append({
                    "niche": slug, "channel": row.get("title"), "channel_id": channel_id,
                    "fresh_title": fresh_title, "handle": str((item.get("snippet") or {}).get("customUrl") or ""),
                    "subscribers": enriched["channel"]["subscriber_count"],
                    "latest_upload": latest_text, "views_90d": enriched["views_90d"],
                    "longform_90d": enriched["longform_90d"], "shorts_90d": enriched["shorts_90d"],
                    "verdict": verdict,
                })
            except Exception as exc:
                output.append({
                    "niche": slug, "channel": row.get("title"), "channel_id": channel_id,
                    "latest_upload": "", "views_90d": 0,
                    "verdict": f"fetch error: {str(exc)[:180]}",
                })
    return output


def main() -> int:
    runners = {slug: SeedRunner(slug) for slug in SLUGS}
    try:
        backfill = backfill_admin_rows(runners)
        audit = audit_big_zero_view_channels(runners)
        youtube = {
            "units_used": sum(runner.units_used for runner in runners.values()),
            "calls": {
                method: sum(runner.calls.get(method, 0) for runner in runners.values())
                for method in {method for runner in runners.values() for method in runner.calls}
            },
        }
        print(json.dumps({"backfill": backfill, "audit": audit, "youtube": youtube}, ensure_ascii=False, indent=2))
    finally:
        for runner in runners.values():
            runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
