from app.video_shorts.services import customer_niche, niches


class _Result:
    def __init__(self, rows, columns=()):
        self._rows = list(rows)
        self.description = [(column,) for column in columns]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _SnapshotConnection:
    def __init__(self, available_dates):
        self.available_dates = list(available_dates)
        self.channel_query_params = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).lower()
        params = list(params or [])
        if "from niches where slug" in normalized:
            return _Result(
                [(7, "turkish_religious_talks", "Turkish talks", "education")],
                ("id", "slug", "name", "category"),
            )
        if normalized.startswith("select max(snapshot_date)"):
            latest = max(self.available_dates) if self.available_dates else None
            return _Result([(latest,)])
        if normalized.startswith("select 1 from niche_channels"):
            return _Result([(1,)] if params[1] in self.available_dates else [])
        if normalized.startswith("select youtube_channel_id"):
            self.channel_query_params = params
            return _Result([])
        if normalized.startswith("select distinct snapshot_date"):
            return _Result([(value,) for value in sorted(self.available_dates, reverse=True)])
        raise AssertionError(normalized)


def test_invalid_requested_snapshot_falls_back_to_latest(monkeypatch):
    monkeypatch.setattr(niches, "table_columns", lambda *_args: set())
    conn = _SnapshotConnection(["2026-11-24", "2026-11-26"])

    selected, rows = niches.load_niche_snapshot(
        conn, "turkish_religious_talks", "2026-11-22"
    )

    assert rows == []
    assert selected["snapshot_date"] == "2026-11-26"
    assert conn.channel_query_params == [7, "2026-11-26"]


def test_valid_requested_snapshot_is_preserved(monkeypatch):
    monkeypatch.setattr(niches, "table_columns", lambda *_args: set())
    conn = _SnapshotConnection(["2026-11-24", "2026-11-26"])

    selected, _rows = niches.load_niche_snapshot(
        conn, "turkish_religious_talks", "2026-11-24"
    )

    assert selected["snapshot_date"] == "2026-11-24"
    assert conn.channel_query_params == [7, "2026-11-24"]


def test_snapshot_options_are_scoped_to_selected_niche():
    conn = _SnapshotConnection(["2026-11-24", "2026-11-26"])

    dates = niches.load_snapshot_dates(conn, 7)

    assert dates == ["2026-11-26", "2026-11-24"]


def test_customer_page_requests_latest_snapshot(monkeypatch):
    captured = []

    class _CustomerConnection:
        def execute(self, sql, params=None):
            if "FROM shorts_brands" in sql:
                return _Result([("brand-1", "Brand", 7, "niche-slug", "Niche")])
            raise AssertionError("unexpected query")

    def fake_load_snapshot(_conn, slug, snapshot_date=""):
        captured.append((slug, snapshot_date))
        return ({"id": 7, "slug": slug, "name": "Niche"}, [])

    monkeypatch.setattr(customer_niche, "load_niche_snapshot", fake_load_snapshot)
    monkeypatch.setattr(customer_niche, "brand_youtube_channel_ids", lambda *_args: set())
    monkeypatch.setattr(customer_niche, "load_latest_niche_outliers", lambda *_args: ("", []))

    page = customer_niche.load_customer_niche_page(
        _CustomerConnection(), owner_user_id="owner-1", brand_id="brand-1"
    )

    assert page is not None
    assert captured == [("niche-slug", "")]
