from __future__ import annotations

import base64
import io
import json

from PIL import Image

from app.video_shorts.services.ai_covers import _api_request, _normalize_ocr, _prompt, ai_cover_enabled


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
