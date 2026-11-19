from types import SimpleNamespace

from scripts import blog_judge


class FakeConnection:
    def __init__(self):
        self.closed = False

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_judge_closes_read_connection_before_llm_and_continues_after_write_failure(monkeypatch):
    read_connection = FakeConnection()
    candidates = [
        {"id": 11, "title": "First", "status": "candidate"},
        {"id": 12, "title": "Second", "status": "candidate"},
    ]
    decisions = [
        {"id": 11, "decision": "queue", "fit_score": 90},
        {"id": 12, "decision": "reject", "fit_score": 30},
    ]
    write_calls = []

    monkeypatch.setattr(blog_judge, "get_db", lambda: read_connection)
    monkeypatch.setattr(blog_judge, "_expire_timely", lambda conn, **kwargs: (_ for _ in ()).throw(AssertionError("filtered repair must not expire unrelated topics")))

    def topic_rows(conn, mode, topic_ids, cluster_slug):
        assert topic_ids == [11, 12]
        assert cluster_slug is None
        return candidates

    monkeypatch.setattr(blog_judge, "_topic_rows", topic_rows)
    monkeypatch.setattr(blog_judge, "list_existing_articles_and_topics", lambda conn, exclude_topic_ids: [])
    monkeypatch.setattr(blog_judge, "list_blog_clusters", lambda active_only: [])
    monkeypatch.setattr(blog_judge, "_apply_prefilter", lambda rows, existing: (rows, []))
    monkeypatch.setattr(blog_judge, "log_usage", lambda *args: None)

    def run_llm(rows, existing, *, dry_run, on_batch, clusters):
        assert read_connection.closed
        assert clusters == {}
        on_batch(rows, decisions)
        return decisions, [{"_result": SimpleNamespace(), "input_tokens": 1}]

    def write(candidate, decision, **kwargs):
        write_calls.append(candidate["id"])
        return candidate["id"] == 12

    monkeypatch.setattr(blog_judge, "_run_llm_batches", run_llm)
    monkeypatch.setattr(blog_judge, "_write_topic_decision", write)

    result = blog_judge.judge(topic_ids=[11, 12])

    assert write_calls == [11, 12]
    assert result["write_errors"] == [11]
    assert result["counts"] == {"rejected_lowfit": 1}


def test_topic_write_uses_fresh_connection_and_skips_changed_status(monkeypatch):
    class WriteConnection(FakeConnection):
        def __init__(self):
            super().__init__()
            self.committed = False

        def commit(self):
            self.committed = True

    connection = WriteConnection()
    monkeypatch.setattr(blog_judge, "get_db", lambda: connection)
    monkeypatch.setattr(blog_judge, "_apply_decision", lambda *args, **kwargs: False)

    applied = blog_judge._write_topic_decision(
        {"id": 20, "status": "candidate"}, {"id": 20, "decision": "queue"}
    )

    assert applied is False
    assert connection.committed
    assert connection.closed
