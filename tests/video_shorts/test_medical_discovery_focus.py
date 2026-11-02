from flask import Flask

from app.video_shorts.routes.api import (
    _filter_medical_category_keyword_items,
    _has_clinical_credential,
    _load_queue_keywords,
)


def test_clinician_credentials_detected_without_making_it_an_icp_gate():
    assert _has_clinical_credential("Dr. Jane Smith", "Women's health physician") is True
    assert _has_clinical_credential("Back Pain Clinic", "Hosted by Alex Doe, DPT") is True
    assert _has_clinical_credential("Healthy Living", "General wellness education") is False


def test_medical_hard_filter_rejects_off_focus_blocked_and_long_queries():
    app = Flask(__name__)
    with app.app_context():
        kept, rejected = _filter_medical_category_keyword_items(
            [
                {"category": "Cardiology & heart health", "keyword": "AFib symptoms and treatment"},
                {"category": "Personal Home Gardening", "keyword": "easy garden soil tips"},
                {"category": "Sleep medicine", "keyword": "why I cannot sleep well every single night"},
                {"category": "Dropped Specialty", "keyword": "doctor explains chronic symptoms"},
            ],
            blocked_categories=["Dropped Specialty"],
        )
    assert kept == [{"category": "Cardiology & heart health", "keyword": "AFib symptoms and treatment"}]
    assert {item["reason"] for item in rejected} == {
        "off_focus:gardening",
        "keyword_word_count:8",
        "blocked_category_status",
    }


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _QueueConn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql):
        return _Rows(self.rows)


def test_queue_spreads_first_pass_across_categories(monkeypatch):
    monkeypatch.setattr(
        "app.video_shorts.routes.api.table_columns",
        lambda _conn, _table: {"category_id"},
    )
    rows = [
        (1, "heart query one", 10),
        (2, "heart query two", 10),
        (3, "sleep query", 20),
        (4, "thyroid query", 30),
    ]
    picked = _load_queue_keywords(_QueueConn(rows), 3)
    assert [item["id"] for item in picked] == [1, 3, 4]


def test_queue_fills_from_existing_order_when_categories_are_insufficient(monkeypatch):
    monkeypatch.setattr(
        "app.video_shorts.routes.api.table_columns",
        lambda _conn, _table: {"category_id"},
    )
    rows = [
        (1, "heart query one", 10),
        (2, "heart query two", 10),
        (3, "sleep query one", 20),
        (4, "sleep query two", 20),
    ]
    picked = _load_queue_keywords(_QueueConn(rows), 4)
    assert [item["id"] for item in picked] == [1, 3, 2, 4]
