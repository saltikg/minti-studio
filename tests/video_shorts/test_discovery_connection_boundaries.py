from types import SimpleNamespace

from flask import Flask

from app.video_shorts.routes import api
from app.video_shorts.services import discovery_promote_queue


class _Connection:
    def __init__(self):
        self.closed = False
        self.committed = False

    def execute(self, *_args, **_kwargs):
        return SimpleNamespace(fetchone=lambda: ("profile",))

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _client(assert_connection_closed, content):
    def create(**_kwargs):
        assert_connection_closed()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=None,
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_daily_category_llm_runs_after_read_connection_closes(monkeypatch):
    read_conn = _Connection()
    write_conn = _Connection()
    connections = iter([read_conn, write_conn])
    monkeypatch.setattr(api, "get_db", lambda: next(connections))
    monkeypatch.setattr(api, "ensure_starting_discovery_categories", lambda _conn: None)
    monkeypatch.setattr(api, "_load_discovery_category_metrics", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(api, "_load_discovery_exhaustion_signals", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(api, "_load_top_icp_channels_for_category_discovery", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(api, "_load_discovery_categories", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(api, "_store_discovery_icp_llm_usage", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_get_or_create_discovery_category", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(
        api,
        "_insert_category_keyword_items_into_queue",
        lambda *_args, **_kwargs: {"newly_enqueued": 1, "already_present": 0, "keywords": ["fresh topic"]},
    )
    monkeypatch.setattr(
        api,
        "_openai_client",
        _client(
            lambda: (assert_closed(read_conn), assert_not_closed(write_conn)),
            '{"categories":[{"name":"Fresh","persona":"A creator","queries":["fresh topic"]}]}',
        ),
    )

    with Flask(__name__).app_context():
        result = api.discover_daily_categories_into_queue()

    assert result["newly_enqueued"] == 1
    assert write_conn.committed is True


def assert_closed(connection):
    assert connection.closed is True


def assert_not_closed(connection):
    assert connection.closed is False


def test_keyword_lane_llms_run_after_snapshot_connection_closes(monkeypatch):
    read_conn = _Connection()
    write_conn = _Connection()
    connections = iter([read_conn, write_conn])
    monkeypatch.setattr(api, "get_db", lambda: next(connections))
    monkeypatch.setattr(api, "ensure_starting_discovery_categories", lambda _conn: None)
    monkeypatch.setattr(api, "load_seed_pool", lambda _conn: [{"channel_id": "seed", "transcript_summary": "summary"}])
    monkeypatch.setattr(api, "_load_recent_keyword_queue_terms", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(api, "_load_discovery_exhaustion_signals", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(api, "_load_discovery_category_metrics", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(api, "_load_discovery_categories", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(api, "_store_discovery_icp_llm_usage", lambda **_kwargs: None)
    monkeypatch.setattr(
        api,
        "_filter_generated_keyword_items",
        lambda items, **_kwargs: {"items": items[:1], "dropped_keywords": [], "generated_raw": len(items)},
    )
    monkeypatch.setattr(
        api,
        "_insert_category_keyword_items_into_queue",
        lambda _conn, items, **_kwargs: {
            "newly_enqueued": len(items),
            "already_present": 0,
            "keywords": [item["keyword"] for item in items],
        },
    )
    monkeypatch.setattr(
        api,
        "_openai_client",
        _client(
            lambda: assert_closed(read_conn),
            '{"keywords":[{"keyword":"fresh topic","category":"Fresh"}]}',
        ),
    )

    with Flask(__name__).app_context():
        result = api.generate_seed_keywords_into_queue()

    assert result["generated"] == 3
    assert write_conn.committed is True


def test_greeting_inference_runs_after_read_connection_closes(monkeypatch):
    read_conn = _Connection()
    write_conn = _Connection()
    lead_row = (7, "channel-7", "Jane Teaches", "", 1000, "Jane Doe", "jane@example.com", True, None, False)
    read_conn.execute = lambda *_args, **_kwargs: SimpleNamespace(fetchone=lambda: lead_row)
    connections = iter([read_conn, write_conn])

    monkeypatch.setattr(discovery_promote_queue, "get_db", lambda: next(connections))
    monkeypatch.setattr(
        discovery_promote_queue,
        "table_columns",
        lambda *_args, **_kwargs: {"outreach_first_name", "outreach_first_name_edited_by_admin"},
    )
    monkeypatch.setattr(
        discovery_promote_queue,
        "infer_outreach_first_name_for_lead",
        lambda _lead: (assert_closed(read_conn), "Jane")[1],
    )

    result = discovery_promote_queue.store_outreach_first_name_if_needed(7)

    assert result == "Jane"
    assert write_conn.committed is True
