from __future__ import annotations

import base64
import io
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from app.video_shorts.services.ai_covers import (
    _api_request,
    _history_rows,
    _normalize_ocr,
    _prompt,
    _select_face_candidate_records,
    ai_cover_allowance,
    ai_cover_enabled,
    assemble_phase2_prompt,
    generate_phase2_cover,
)


def test_ai_prompt_is_versioned_and_exact_copy_is_preserved():
    prompt = _prompt("METABOLİZMA", "KİLO ENGELİ", "İNSÜLİN", text_free=False)
    assert '"METABOLİZMA"' in prompt
    assert '"KİLO ENGELİ"' in prompt
    assert '"İNSÜLİN"' in prompt
    assert "Keep the face EXACTLY the same" in prompt


def test_text_free_fallback_bans_letters():
    prompt = _prompt("", "", "", text_free=True)
    assert "NO text, no letters, no logos" in prompt


def test_turkish_ocr_case_normalization_keeps_dotless_i_distinct():
    assert _normalize_ocr("KİLO ENGELİ", turkish=True) == _normalize_ocr("kilo engeli", turkish=True)
    assert _normalize_ocr("IŞIK", turkish=True) == _normalize_ocr("ışık", turkish=True)
    assert _normalize_ocr("I", turkish=True) != _normalize_ocr("İ", turkish=True)
    assert _normalize_ocr("PEPTIDE") == _normalize_ocr("peptide")


def test_image_request_sends_style_first_frame_second_and_requests_4k(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers

    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    png = io.BytesIO()
    Image.new("RGB", (9, 16), "red").save(png, "PNG")
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, timeout):
        observed.update(json.loads(request.data))
        assert timeout == 60
        return Response()

    monkeypatch.setattr(ai_covers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ai_covers.json, "load", lambda _response: {
        "candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "image/png", "data": base64.b64encode(png.getvalue()).decode(),
        }}]}}], "usageMetadata": {"promptTokenCount": 10},
    })
    image, usage = _api_request("gemini-3.1-flash-image", "sample prompt", b"style", b"frame")
    parts = observed["contents"][0]["parts"]
    assert base64.b64decode(parts[1]["inlineData"]["data"]) == b"style"
    assert base64.b64decode(parts[2]["inlineData"]["data"]) == b"frame"
    assert observed["generationConfig"]["imageConfig"] == {"aspectRatio": "9:16", "imageSize": "4K"}
    assert image.size == (9, 16) and usage["promptTokenCount"] == 10


def test_ai_requires_global_flag_key_and_brand_opt_in(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers

    monkeypatch.setattr(ai_covers, "load_user_bool_preference", lambda *_args: True)
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    monkeypatch.setenv("AI_COVERS_ENABLED", "0")
    assert not ai_cover_enabled("owner", "brand")
    monkeypatch.setenv("AI_COVERS_ENABLED", "1")
    assert ai_cover_enabled("owner", "brand")
    monkeypatch.setattr(ai_covers, "load_user_bool_preference", lambda *_args: False)
    assert not ai_cover_enabled("owner", "brand")


def test_monthly_allowance_counts_ai_covered_and_rendered_shorts_in_pacific_month(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers

    queries = []

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class Conn:
        def execute(self, sql, params):
            queries.append((sql, params))
            return Result(2 if "shorts_cover_ai_calls" in sql else 5)

        def close(self):
            pass

    monkeypatch.setattr(ai_covers, "get_db_readonly", Conn)
    monkeypatch.setattr(ai_covers, "table_columns", lambda _conn, table: {"id"} if table == "shorts_cover_ai_calls" else set())

    used, allowed = ai_cover_allowance(
        "owner", "brand",
        now=datetime(2026, 11, 8, 12, tzinfo=ZoneInfo("America/Los_Angeles")),
    )

    assert (used, allowed) == (2, 5)
    assert "COUNT(*)" in queries[0][0]
    assert "COUNT(DISTINCT id)" in queries[1][0]
    assert queries[0][1][2:] == [datetime(2026, 11, 1, 7), datetime(2026, 12, 1, 8)]
    assert queries[1][1][2:] == [datetime(2026, 11, 1, 7), datetime(2026, 12, 1, 8)]


def test_phase2_candidate_selection_supports_one_and_two_people():
    single = [
        {"id": str(index), "person_key": "single", "score": 100 - index}
        for index in range(8)
    ]
    selected_single = _select_face_candidate_records(single)
    assert len(selected_single) == 6
    assert {item["person_label"] for item in selected_single} == {"Person 1"}

    split = [
        {"id": f"left-{index}", "person_key": "left", "score": 100 - index}
        for index in range(4)
    ] + [
        {"id": f"right-{index}", "person_key": "right", "score": 90 - index}
        for index in range(4)
    ]
    selected_split = _select_face_candidate_records(split)
    assert len(selected_split) == 6
    assert [item["person_label"] for item in selected_split].count("Person 1") == 3
    assert [item["person_label"] for item in selected_split].count("Person 2") == 3


def test_phase2_prompt_uses_presets_and_advanced_override_verbatim():
    prompt = assemble_phase2_prompt(
        people_count=2,
        title="İşin Özü Şudur",
        no_title=False,
        title_style="big_bold_yellow",
        tone="interview_podcast",
    )
    assert "both selected people" in prompt
    assert '"İşin Özü Şudur"' in prompt
    assert "bold yellow" in prompt
    assert "Turkish characters" in prompt

    edited = "My exact advanced prompt — ı ş ğ ç ö ü İ"
    assert assemble_phase2_prompt(
        people_count=1, title="ignored", no_title=False,
        title_style="white_box", tone="dramatic", advanced_override=edited,
    ) == edited


def test_phase2_allowance_blocks_before_reference_or_api_work(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers

    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    monkeypatch.setattr(ai_covers, "ai_cover_allowance", lambda *_args: (3, 3))
    monkeypatch.setattr(
        ai_covers,
        "extract_reference_frame",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("reference extraction must not run")),
    )
    with pytest.raises(ai_covers.AiCoverLimitReached):
        generate_phase2_cover(
            owner="owner", brand="brand", generated_id=1, video_pk=2,
            selected_frames=[{"clip_timestamp": 1.0}], title="Three Word Title",
            no_title=False, title_style="minimal_clean", tone="curiosity",
        )


def test_phase2_history_returns_only_last_three(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers

    class Result:
        def fetchall(self):
            return [
                (f"id-{index}", f"key-{index}", f"prompt-{index}", "model", [], "title", "style", "tone", datetime(2026, 11, 8, 10 + index))
                for index in range(3)
            ]

    class Conn:
        def execute(self, sql, params):
            assert "LIMIT 3" in sql
            return Result()

    class Storage:
        def public_url(self, key):
            return f"signed:{key}"

    monkeypatch.setattr(ai_covers, "get_media_storage", lambda: Storage())
    history = _history_rows(Conn(), owner="owner", brand="brand", generated_id=1)
    assert len(history) == 3
    assert history[0]["url"] == "signed:key-0"
