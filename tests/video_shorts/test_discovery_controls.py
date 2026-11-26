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
    monkeypatch.setattr(discovery_automation, "search_calls_for_date", lambda *_args, **_kwargs: 12)
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
    assert status["search_used"] == 12
    assert status["search_budget"] == 70
    assert status["would_exceed"] is False


def test_quota_day_changes_at_midnight_pacific():
    assert youtube_quota.quota_date_pt(datetime(2026, 11, 25, 7, 59, tzinfo=timezone.utc)) == "2026-11-24"
    assert youtube_quota.quota_date_pt(datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)) == "2026-11-25"


def test_search_calls_do_not_consume_general_query_units():
    assert youtube_quota.unit_cost("search.list") == 0


def test_discovery_catchup_schedule_uses_requested_pacific_hours():
    next_slot = discovery_automation._next_offpeak_slot_utc(
        datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc),
        discovery_automation.DEFAULT_DISCOVERY_RUN_HOURS_PT,
        4,
    )
    assert next_slot == datetime(2026, 11, 24, 21, 0)


def test_search_shares_are_counted_separately(monkeypatch):
    raw_conn = duckdb.connect(":memory:")
    youtube_quota.ensure_youtube_quota_table(raw_conn)
    raw_conn.execute(
        """
        INSERT INTO youtube_data_api_unit_usage
            (date_pt, feature, method, calls, units)
        VALUES
            (?, 'discovery_search', 'search.list', 70, 7000),
            (?, 'research_project', 'search.list', 40, 4000),
            (?, 'women_hormones_research_project', 'search.list', 40, 4000),
            (?, 'publish', 'search.list', 6, 600),
            (?, 'other', 'search.list', 4, 400)
        """,
        [youtube_quota.today_pt()] * 5,
    )

    class Conn:
        def execute(self, sql, params=None):
            return raw_conn.execute(sql, params or [])

        def commit(self):
            raw_conn.commit()

        def close(self):
            pass

    monkeypatch.setattr(youtube_quota, "get_db", Conn)
    monkeypatch.setattr(youtube_quota, "ensure_youtube_quota_table", lambda _conn: None)
    monkeypatch.setattr(
        youtube_quota,
        "table_columns",
        lambda *_args: {"date_pt", "feature", "method", "calls", "units"},
    )

    with pytest.raises(youtube_quota.YoutubeQuotaCapExceeded, match="discovery search quota"):
        youtube_quota.require_youtube_quota_available(feature="discovery_search", method="search.list")
    with pytest.raises(youtube_quota.YoutubeQuotaCapExceeded, match="research search quota"):
        youtube_quota.require_youtube_quota_available(
            feature="new_topic_research_project", method="search.list"
        )
    with pytest.raises(youtube_quota.YoutubeQuotaCapExceeded, match="other search quota"):
        youtube_quota.require_youtube_quota_available(feature="publish", method="search.list")


def test_research_cap_blocks_before_next_request(monkeypatch):
    class Conn:
        value = 0

        def execute(self, sql, _params=None):
            self.value = 0 if "SUM(calls)" in sql else 2000
            return self

        def fetchone(self):
            return (self.value,)

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
            feature="women_hormones_research", method="videos.list"
        )


def test_research_project_key_never_falls_back_to_app_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_RESEARCH_API_KEY", raising=False)
    monkeypatch.setenv("YOUTUBE_API_KEY", "app-key-must-not-be-used")

    with pytest.raises(RuntimeError, match="fallback is disabled"):
        youtube_quota.research_youtube_api_key()


def test_research_project_defaults_to_separate_caps(monkeypatch):
    monkeypatch.delenv("YT_QUOTA_RESEARCH_PROJECT_CAP", raising=False)

    assert youtube_quota.research_project_daily_cap() == 8000
    assert youtube_quota.RESEARCH_PROJECT_SEARCH_DAILY_CAP == 80


def test_search_totals_are_separate_per_api_project():
    conn = duckdb.connect(":memory:")
    youtube_quota.ensure_youtube_quota_table(conn)
    conn.execute(
        """
        INSERT INTO youtube_data_api_unit_usage
            (date_pt, feature, method, calls, units)
        VALUES (?, 'discovery_search', 'search.list', 70, 0),
               (?, 'topic_research_project', 'search.list', 25, 0)
        """,
        [youtube_quota.today_pt(), youtube_quota.today_pt()],
    )

    assert youtube_quota.search_calls_for_api_project(
        conn, research_project=False
    ) == 70
    assert youtube_quota.search_calls_for_api_project(
        conn, research_project=True
    ) == 25


def test_backlog_next_run_respects_pacific_window():
    # Nov 24 21:30 UTC is 13:30 PT; the next hourly slot is 14:00 PT.
    during = next_backlog_run(datetime(2026, 11, 24, 21, 30, tzinfo=timezone.utc))
    assert during.hour == 14 and during.minute == 0
    # Nov 25 05:30 UTC is Nov 24 21:30 PT; roll to 08:00 the next PT day.
    after = next_backlog_run(datetime(2026, 11, 25, 5, 30, tzinfo=timezone.utc))
    assert after.hour == 8 and after.day == 25
