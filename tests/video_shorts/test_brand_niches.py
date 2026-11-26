from app.video_shorts.services import brand_niches
from app.video_shorts.services.niches import is_final_niche_pool_row


def _row(channel_id, title, subscribers, **overrides):
    row = {
        "youtube_channel_id": channel_id,
        "title": title,
        "subscriber_count": subscribers,
        "primary_niche": "menopause_hormones",
        "primary_focus": "hormones_midlife_health",
        "classification_status": "classified",
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
        _row(
            "UC3", "Near miss", 500,
            primary_focus="fitness_training", on_topic_share=0.8,
        ),
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


def test_primary_focus_drives_pool_without_core_share_threshold():
    rows = [
        _row("UC1", "Strong fit", 2000, primary_focus="hormones_midlife_health"),
        _row("UC2", "Near miss", 1000, primary_focus="hormones_midlife_health", on_topic_share=0.2),
        _row("UC3", "Fitness", 500, primary_focus="fitness_training", relevance=99),
    ]
    suggestions = brand_niches.build_niche_suggestion_index(
        [({"id": 7, "name": "Women's Hormones & Midlife Health"}, rows)]
    )

    assert suggestions["UC1"]["group"] == "pool"
    assert suggestions["UC2"]["group"] == "pool"
    assert suggestions["UC3"]["group"] == "near_miss"


def test_admin_include_and_exclude_override_strict_channel_focus():
    rows = [
        _row("UC1", "Included fitness", 2000, primary_focus="fitness_training", admin_include=True),
        _row("UC2", "Excluded hormones", 1000, primary_focus="hormones_midlife_health", admin_exclude=True),
    ]
    suggestions = brand_niches.build_niche_suggestion_index(
        [({"id": 7, "name": "Women's Hormones & Midlife Health"}, rows)]
    )

    assert suggestions["UC1"]["group"] == "pool"
    assert "UC2" not in suggestions


def test_generic_pool_uses_niche_language_and_ignores_country():
    niche = {"language": "tr", "target_focus": "religious_spiritual_talks"}
    turkish = _row(
        "UC1", "Turkish speaker", 1000,
        primary_focus="religious_spiritual_talks", language="Turkish", country="DE",
    )
    mixed = _row(
        "UC2", "Mixed speaker", 900,
        primary_focus="religious_spiritual_talks", language="mixed", country="TR",
    )

    assert is_final_niche_pool_row(turkish, niche)
    assert not is_final_niche_pool_row(mixed, niche)


def test_missing_language_never_falls_back_to_legacy_status():
    niche = {"language": "en", "target_focus": "hormones_midlife_health"}
    row = _row("UC1", "Known pool member", 1000, language="", in_niche=True, country="IN")

    assert not is_final_niche_pool_row(row, niche)


def test_no_titles_never_uses_preserved_focus_label():
    niche = {"language": "en", "target_focus": "hormones_midlife_health"}
    row = _row(
        "UC1", "Stale focus", 1000,
        classification_status="no_titles", primary_focus="hormones_midlife_health",
    )

    assert not is_final_niche_pool_row(row, niche)


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
