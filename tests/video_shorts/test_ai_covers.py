from __future__ import annotations

import base64
import io
import json
import sqlite3
import sys
import time
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from app.video_shorts.services.ai_covers import (
    _api_request,
    _candidate_source_path,
    _candidate_records_from_render_data,
    _history_rows,
    _identity_checked_edit,
    _normalize_ocr,
    _prompt,
    _record_call,
    _select_face_candidate_records,
    ai_cover_allowance,
    ai_cover_enabled,
    assemble_phase2_prompt,
    build_cover_base_image,
    generate_phase2_cover,
    ensure_ai_cover_schema,
    phase2_candidate_frames,
)
from app.video_shorts.services.cover_ai_config import COVER_LOOKS


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
    assert "request_kind='phase2_image'" in queries[0][0]
    assert "COUNT(DISTINCT id)" in queries[1][0]
    assert queries[0][1][2:] == [datetime(2026, 11, 1, 7), datetime(2026, 12, 1, 8)]
    assert queries[1][1][2:] == [datetime(2026, 11, 1, 7), datetime(2026, 12, 1, 8)]


def test_identity_call_schema_records_attempt_scores_and_pass_state():
    conn = sqlite3.connect(":memory:")
    ensure_ai_cover_schema(conn)
    _record_call(
        conn, owner="owner", brand="brand", generated_id=7, model="sface-test",
        request_kind="phase2_image_retry", result_path="identity_pass",
        image_returned=True, latency_ms=123, usage={}, attempt_number=2,
        identity_scores=[0.71, 0.82], identity_passed=True,
    )

    row = conn.execute(
        "SELECT request_kind, attempt_number, identity_scores_json, identity_passed "
        "FROM shorts_cover_ai_calls"
    ).fetchone()
    assert row[:2] == ("phase2_image_retry", 2)
    assert json.loads(row[2]) == [0.71, 0.82]
    assert bool(row[3]) is True
    ensure_ai_cover_schema(conn)


def test_phase2_candidate_selection_supports_one_and_two_people():
    single = [
        {"id": str(index), "person_key": "single", "score": 100 - index}
        for index in range(8)
    ]
    selected_single = _select_face_candidate_records(single)
    assert len(selected_single) == 1
    assert {item["person_label"] for item in selected_single} == {"Person 1"}

    split = [
        {"id": f"left-{index}", "person_key": "left", "score": 100 - index}
        for index in range(4)
    ] + [
        {"id": f"right-{index}", "person_key": "right", "score": 90 - index}
        for index in range(4)
    ]
    selected_split = _select_face_candidate_records(split)
    assert len(selected_split) == 2
    assert [item["person_label"] for item in selected_split] == ["Person 1", "Person 2"]


def test_phase2_candidate_cache_returns_without_downloading_source(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers
    from app.video_shorts.routes import generation

    target = {
        "source_video_id": "source-1", "source_ranges": [(10.0, 20.0)],
        "source_storage_key": "source-key", "title": "Clip B",
    }
    cached = {
        "title": "Clip B", "duration": 10.0,
        "candidates": [{"id": "one", "person_key": "single", "thumbnail_url": "signed:one"}],
        "face_cache_hit": True,
    }

    monkeypatch.setattr(ai_covers, "_cover_target_metadata", lambda **_kwargs: target)
    monkeypatch.setattr(generation, "_face_detection_settings_key", lambda: "detector-v1")
    monkeypatch.setattr(ai_covers, "_load_candidate_cache", lambda **_kwargs: dict(cached))

    class Storage:
        def download_to_temp(self, _key):
            raise AssertionError("cached open must not download or decode the source")

    monkeypatch.setattr(ai_covers, "get_media_storage", lambda: Storage())
    result = phase2_candidate_frames(owner="owner", brand="brand", generated_id=2, video_pk=3)

    assert result["candidate_cache_hit"] is True
    assert result["title"] == "Clip B"
    assert result["candidates"][0]["thumbnail_url"] == "signed:one"


def test_candidate_frames_reuse_render_source_cache(monkeypatch, tmp_path):
    import app.video_shorts.services.ai_covers as ai_covers
    from app.video_shorts.services import media_utils

    cached_path = tmp_path / "source-cache.mp4"
    cached_path.write_bytes(b"video")

    class Storage:
        backend_name = "s3"

        def download_to_temp(self, _key):
            raise AssertionError("render source cache should be reused")

    storage = Storage()
    monkeypatch.setattr(ai_covers, "get_media_storage", lambda: storage)
    monkeypatch.setattr(
        media_utils, "resolve_s3_source_video_cached",
        lambda video_id, source_key, **kwargs: cached_path,
    )

    path, leased = _candidate_source_path({
        "source_video_id": "source-1", "source_storage_key": "sources/source-1.mp4",
    })

    assert path == cached_path
    assert leased is True


def test_candidate_cache_miss_uses_render_data_without_face_detection(monkeypatch, tmp_path):
    import app.video_shorts.services.ai_covers as ai_covers
    from app.video_shorts.routes import generation

    target = {
        "source_video_id": "source-1", "source_storage_key": "source.mp4",
        "source_ranges": [(100.0, 115.0)], "title": "Rendered clip",
        "auto_layout_segments": [{
            "start": 2.0, "end": 12.0, "mode": "split",
            "split_crops": {
                "top": {"x": 0.05, "y": 0.1, "w": 0.4, "h": 0.6, "cx": 0.25, "cy": 0.4},
                "bottom": {"x": 0.55, "y": 0.1, "w": 0.4, "h": 0.6, "cx": 0.75, "cy": 0.4},
            },
        }],
    }
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"video")

    class FakeCapture:
        def __init__(self, _path): pass
        def set(self, *_args): return True
        def read(self): return True, object()
        def release(self): return None

    fake_cv2 = types.SimpleNamespace(
        VideoCapture=FakeCapture,
        CAP_PROP_POS_MSEC=0,
        COLOR_BGR2RGB=1,
        cvtColor=lambda raw, _mode: raw,
    )

    monkeypatch.setattr(ai_covers, "_cover_target_metadata", lambda **_kwargs: target)
    monkeypatch.setattr(ai_covers, "_load_candidate_cache", lambda **_kwargs: None)
    monkeypatch.setattr(ai_covers, "_load_render_face_track", lambda _target: [])
    monkeypatch.setattr(ai_covers, "_candidate_source_path", lambda _target: (source_path, False))
    monkeypatch.setattr(ai_covers, "_cleanup_candidate_source", lambda *_args: None)
    monkeypatch.setattr(ai_covers, "_save_candidate_cache", lambda **_kwargs: None)
    monkeypatch.setattr(ai_covers, "_candidate_urls", lambda candidates: candidates)
    monkeypatch.setattr(ai_covers, "put_file_for_owner", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    monkeypatch.setattr(ai_covers.Image, "fromarray", lambda _raw: Image.new("RGB", (640, 360)))
    monkeypatch.setattr(
        generation, "_sample_face_presence_for_layout",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("face detection must not run")),
    )

    started = time.monotonic()
    result = phase2_candidate_frames(
        owner="owner", brand="brand", generated_id=2, video_pk=3,
    )

    assert time.monotonic() - started < 2.0
    assert result["candidate_cache_hit"] is False
    assert result["render_data_used"] is True
    assert [item["person_key"] for item in result["candidates"]] == ["left", "right"]


def test_candidate_fallback_uses_three_evenly_spaced_frames():
    target = {
        "source_ranges": [(10.0, 22.0)], "auto_layout_segments": [],
    }
    candidates, render_data_used = _candidate_records_from_render_data(target, [])

    assert render_data_used is False
    assert [item["clip_timestamp"] for item in candidates] == [3.0, 6.0, 9.0]


def test_phase2_prompt_safe_zones_and_look_hints_are_configured():
    prompt = assemble_phase2_prompt(
        people_count=1, title="Three Word Title", no_title=False,
        look="minimal",
    )

    assert "Keep the title in the upper third" in prompt
    assert "Keep the bottom 20% and the right edge free" in prompt
    assert "calm, uncluttered, professional" in prompt
    assert COVER_LOOKS["dramatic"]["hint"] == "cinematic, dark, intense"
    assert COVER_LOOKS["yellow_title"]["label"] == "Yellow Title"


def test_phase2_prompt_is_an_edit_instruction_that_forbids_redrawing_people():
    prompt = assemble_phase2_prompt(
        people_count=1, title="Gerçek Yüz", no_title=False, look="yellow_title",
    )

    assert prompt.startswith("Edit this photo into a YouTube Shorts cover.")
    assert "Do NOT redraw, replace, or alter the people" in prompt
    assert "expression, hair, skin, glasses, clothing and pose exactly" in prompt


def test_base_image_builds_one_person_two_shot_and_two_separate_frames():
    face_left = {"cx_ratio": 0.30, "cy_ratio": 0.35, "w_ratio": 0.12, "h_ratio": 0.24}
    face_right = {"cx_ratio": 0.70, "cy_ratio": 0.35, "w_ratio": 0.12, "h_ratio": 0.24}
    one = build_cover_base_image([(Image.new("RGB", (1920, 1080), "red"), face_left)])
    two_shot = build_cover_base_image([
        (Image.new("RGB", (1920, 1080), "green"), face_left),
        (Image.new("RGB", (1920, 1080), "green"), face_right),
    ], same_frame=True)
    separate = build_cover_base_image([
        (Image.new("RGB", (1920, 1080), "red"), face_left),
        (Image.new("RGB", (1920, 1080), "blue"), face_right),
    ], same_frame=False)

    assert one.size == two_shot.size == separate.size == (1080, 1920)
    assert separate.getpixel((100, 960))[0] > separate.getpixel((100, 960))[2]
    assert separate.getpixel((980, 960))[2] > separate.getpixel((980, 960))[0]


def test_identity_check_retries_twice_and_retries_do_not_count_as_allowance(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers

    api_calls = []
    score_values = iter([[0.20], [0.32], [0.51]])
    colors = iter(["red", "green", "blue"])
    monkeypatch.setattr(
        ai_covers, "_phase2_api_request",
        lambda model, prompt, references: (
            api_calls.append((model, prompt, len(references))) or Image.new("RGB", (1080, 1920), next(colors)),
            {"promptTokenCount": 1},
        ),
    )
    monkeypatch.setattr(ai_covers, "identity_similarity_scores", lambda *_args: next(score_values))
    recorded = []

    image, scores, passed, attempts = _identity_checked_edit(
        model="model", prompt="edit", base_image=Image.new("RGB", (1080, 1920), "white"),
        people_count=1, on_attempt=recorded.append,
    )

    assert image.getpixel((0, 0)) == (0, 0, 255)
    assert scores == [0.51] and passed is True and len(attempts) == 3
    assert [entry["request_kind"] for entry in recorded] == [
        "phase2_image", "phase2_image_retry", "phase2_image_retry",
    ]
    assert sum(entry["request_kind"] == "phase2_image" for entry in recorded) == 1
    assert len(api_calls) == 3


def test_identity_check_returns_best_attempt_with_warning_state_after_three_failures(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers

    colors = iter(["red", "green", "blue"])
    scores = iter([[0.21], [0.36], [0.28]])
    monkeypatch.setattr(
        ai_covers, "_phase2_api_request",
        lambda *_args: (Image.new("RGB", (1080, 1920), next(colors)), {}),
    )
    monkeypatch.setattr(ai_covers, "identity_similarity_scores", lambda *_args: next(scores))

    image, best_scores, passed, attempts = _identity_checked_edit(
        model="model", prompt="edit", base_image=Image.new("RGB", (1080, 1920), "white"),
        people_count=1,
    )

    assert image.getpixel((0, 0)) == (0, 128, 0)
    assert best_scores == [0.36] and passed is False and len(attempts) == 3


def test_phase2_prompt_uses_presets_and_advanced_override_verbatim():
    prompt = assemble_phase2_prompt(
        people_count=2,
        title="İşin Özü Şudur",
        no_title=False,
        look="yellow_title",
    )
    assert "both selected people" in prompt
    assert "side by side or facing each other" in prompt
    assert '"İşin Özü Şudur"' in prompt
    assert "bold yellow" in prompt
    assert "Turkish characters" in prompt

    edited = "My exact advanced prompt — ı ş ğ ç ö ü İ"
    assert assemble_phase2_prompt(
        people_count=1, title="ignored", no_title=False,
        look="white_box", advanced_override=edited,
    ) == edited


def test_phase2_no_text_look_omits_title_and_all_six_looks_are_configured():
    prompt = assemble_phase2_prompt(
        people_count=1, title="This Must Not Render", no_title=False, look="no_text",
    )

    assert len(COVER_LOOKS) == 6
    assert "Do not include any title" in prompt
    assert "This Must Not Render" not in prompt


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
            no_title=False, look="curiosity",
        )


def test_phase2_history_returns_only_last_three(monkeypatch):
    import app.video_shorts.services.ai_covers as ai_covers

    class Result:
        def fetchall(self):
            return [
                (f"id-{index}", f"key-{index}", f"prompt-{index}", "model", [], "title", "style", "tone",
                 datetime(2026, 11, 8, 10 + index), f"base-{index}", [0.9], True, 1)
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
