from datetime import datetime, timezone

import duckdb
import pytest

from app.video_shorts.routes import api
from app.video_shorts.services import discovery_automation, youtube_quota
from app.video_shorts.services.discovery_backlog_enrichment import next_backlog_run
from app.video_shorts.services.discovery_blocklist import (
    apply_discovery_blocklist,
    discovery_text_is_blocked,
)


def test_discovery_blocklist_matches_requested_hobby_terms():
    assert discovery_text_is_blocked("Sewing for beginners")
    assert discovery_text_is_blocked("Personal creative arts and crafts coaching")
    assert discovery_text_is_blocked("AI tools for creators")
    assert not discovery_text_is_blocked("Cardiology and heart health")


def test_generated_keyword_normalization_hard_filters_blocklist():
    items = api._normalize_category_keyword_items(
        [
            {"keyword": "menopause sleep help", "category": "Menopause"},
            {"keyword": "repair old fabric", "category": "Textile upcycling"},
            {"keyword": "bird habitat advice", "category": "Birdwatching and wildlife"},
        ]
    )
    assert items == [{"keyword": "menopause sleep help", "category": "Menopause"}]


def test_apply_blocklist_marks_rows_excluded_without_deleting():
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE discovery_categories (id INTEGER, name VARCHAR, status VARCHAR, decided_at TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE keyword_queue (id INTEGER, keyword VARCHAR, status VARCHAR, category_id INTEGER, updated_at TIMESTAMP)"
    )
    conn.execute("INSERT INTO discovery_categories VALUES (1, 'Sewing education', 'active', NULL), (2, 'Cardiology', 'active', NULL)")
    conn.execute("INSERT INTO keyword_queue VALUES (1, 'sewing tutorial', 'queued', 1, NULL), (2, 'heart health', 'queued', 2, NULL)")

    apply_discovery_blocklist(conn)

    assert conn.execute("SELECT status FROM discovery_categories WHERE id=1").fetchone()[0] == "excluded"
    assert conn.execute("SELECT status FROM keyword_queue WHERE id=1").fetchone()[0] == "excluded"
    assert conn.execute("SELECT status FROM keyword_queue WHERE id=2").fetchone()[0] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM keyword_queue").fetchone()[0] == 2


def test_discovery_quota_uses_only_reserved_discovery_features(monkeypatch):
    captured = {}
    monkeypatch.setattr(discovery_automation, "ensure_youtube_quota_table", lambda _conn: None)

    def total(_conn, _date, **kwargs):
        captured.update(kwargs)
        return 1200

    monkeypatch.setattr(discovery_automation, "total_units_for_date", total)
    status = discovery_automation._discovery_quota_budget_status(
        object(),
        {
            "youtube_daily_unit_budget": 4000,
            "max_keywords_per_cycle": 5,
            "max_results_per_keyword": 50,
            "max_channels_enriched_per_cycle": 150,
        },
    )

    assert captured["include_features"] == youtube_quota.DISCOVERY_QUOTA_FEATURES
    assert status["used"] == 1200
    assert status["would_exceed"] is False


def test_research_cap_blocks_before_next_request(monkeypatch):
    class Conn:
        def execute(self, _sql, _params=None):
            return self

        def fetchone(self):
            return (1950,)

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setenv("YT_QUOTA_RESEARCH_CAP", "2000")
    monkeypatch.setattr(youtube_quota, "get_db", lambda: Conn())
    monkeypatch.setattr(youtube_quota, "table_columns", lambda *_args: {"units"})
    monkeypatch.setattr(youtube_quota, "ensure_youtube_quota_table", lambda _conn: None)

    with pytest.raises(youtube_quota.YoutubeQuotaCapExceeded):
        youtube_quota.require_youtube_quota_available(
            feature="women_hormones_research", method="search.list"
        )


def test_backlog_next_run_respects_pacific_window():
    # Nov 24 21:30 UTC is 13:30 PT; the next hourly slot is 14:00 PT.
    during = next_backlog_run(datetime(2026, 11, 24, 21, 30, tzinfo=timezone.utc))
    assert during.hour == 14 and during.minute == 0
    # Nov 25 05:30 UTC is Nov 24 21:30 PT; roll to 08:00 the next PT day.
    after = next_backlog_run(datetime(2026, 11, 25, 5, 30, tzinfo=timezone.utc))
    assert after.hour == 8 and after.day == 25
