from app.video_shorts.services import brand_niches


def _row(channel_id, title, subscribers, **overrides):
    row = {
        "youtube_channel_id": channel_id,
        "title": title,
        "subscriber_count": subscribers,
        "primary_niche": "menopause_hormones",
        "on_topic_share": 0.8,
        "entity": "individual",
        "presenter": "real_person",
        "language": "English",
        "country": "US",
        "audience_language": "English",
        "admin_exclude": False,
        "admin_include": False,
    }
    row.update(overrides)
    return row


def test_suggestion_index_ranks_pool_and_near_misses():
    rows = [
        _row("UC1", "Larger", 2000),
        _row("UC2", "Shelley", 1000),
        _row("UC3", "Near miss", 500, on_topic_share=0.2),
    ]
    suggestions = brand_niches.build_niche_suggestion_index(
        [({"id": 7, "name": "Menopause hormones"}, rows)]
    )

    assert suggestions["UC2"] == {
        "niche_id": "7",
        "niche_name": "Menopause hormones",
        "rank": 2,
        "total": 2,
        "group": "pool",
    }
    assert suggestions["UC3"]["rank"] == 1
    assert suggestions["UC3"]["total"] == 1
    assert suggestions["UC3"]["group"] == "near_miss"


class _UpdateConnection:
    def __init__(self, brand_exists=True, niche_exists=True):
        self.brand_exists = brand_exists
        self.niche_exists = niche_exists
        self.updates = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).lower()
        if normalized.startswith("select cast(b.id as varchar)"):
            return _Result([("brand-1",)] if self.brand_exists else [])
        if normalized.startswith("select cast(id as varchar) from niches"):
            return _Result([("7",)] if self.niche_exists else [])
        if normalized.startswith("update shorts_brands"):
            self.updates.append(list(params or []))
            return _Result([])
        raise AssertionError(normalized)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_update_brand_niche_revalidates_owner_and_niche(monkeypatch):
    monkeypatch.setattr(brand_niches, "table_columns", lambda *_args: {"niche_id"})
    conn = _UpdateConnection()

    selected = brand_niches.update_brand_niche(
        conn,
        owner_user_id="user-1",
        brand_id="brand-1",
        niche_id="7",
    )

    assert selected == "7"
    assert conn.updates == [["7", "brand-1", "user-1"]]


def test_update_brand_niche_rejects_wrong_owner(monkeypatch):
    monkeypatch.setattr(brand_niches, "table_columns", lambda *_args: {"niche_id"})

    try:
        brand_niches.update_brand_niche(
            _UpdateConnection(brand_exists=False),
            owner_user_id="wrong-user",
            brand_id="brand-1",
            niche_id="7",
        )
    except LookupError:
        pass
    else:
        raise AssertionError("Expected ownership revalidation to reject the update")
