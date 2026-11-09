from __future__ import annotations

import inspect
import io
from pathlib import Path

import pytest
from flask import Flask, g
from PIL import Image
from werkzeug.exceptions import Forbidden

from app.video_shorts import worker
from app.video_shorts.routes import generation
from app.video_shorts.services.clip_covers import _uploaded_cover_image


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "app/video_shorts/templates/generate_short.html"


def test_non_admin_cover_access_is_forbidden_and_routes_check_first():
    app = Flask(__name__)
    with app.test_request_context("/"):
        g.vs_current_user = {"role": "customer"}
        with pytest.raises(Forbidden):
            generation._require_cover_admin()

    for view in (
        generation.clip_cover,
        generation.upload_clip_cover,
        generation.remove_clip_cover,
        generation.restore_generated_clip_cover,
        generation.download_clip_cover,
        generation.clip_cover_ai_candidates,
        generation.clip_cover_ai_frame,
        generation.clip_cover_ai_prompt,
        generation.clip_cover_ai_suggest,
        generation.clip_cover_ai_generate,
        generation.clip_cover_ai_use,
    ):
        source = inspect.getsource(view)
        assert source.index("_require_cover_admin()") < source.index("_scoped_generated_clip_for_cover")


def test_cover_ui_is_admin_only_and_uses_one_stable_delegated_listener():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert '{% if is_admin %}<button\n                          type="button"\n                          class="btn btn-sm generate-v2-tile generate-v2-cover-btn"' in template
    assert '{% if is_admin %}\n<style>' in template
    assert '<div class="modal fade" id="clipCoverModal"' in template
    cover_js = template[template.index('const stableRoot = document.body;'):template.index('window.__coverModalTestHooks =')]
    assert 'stableRoot.dataset.clipCoverDelegated === "1"' in cover_js
    assert cover_js.count('stableRoot.addEventListener("click"') == 1
    assert 'event.target.closest?.(".generate-v2-cover-btn")' in cover_js
    assert 'document.querySelectorAll(".generate-v2-cover-btn")' not in cover_js
    assert "clipPanel.replaceWith(newClipPanel)" in template


def test_successful_render_no_longer_enqueues_an_automatic_cover():
    source = inspect.getsource(worker._process_next_job)

    assert "enqueue_cover_job" not in source
    assert "auto_cover_followup" not in source


def test_uploaded_cover_output_is_exactly_1080_by_1920_jpeg():
    original = io.BytesIO()
    Image.new("RGB", (1600, 900), "#336699").save(original, "PNG")

    cover = _uploaded_cover_image(
        original.getvalue(), fit_mode="fill", add_text=False,
        kicker="", main="", highlight="",
    )
    encoded = io.BytesIO()
    cover.save(encoded, "JPEG", quality=92)
    encoded.seek(0)

    with Image.open(encoded) as saved:
        assert saved.format == "JPEG"
        assert saved.size == (1080, 1920)


def test_cover_modal_uses_one_best_card_per_person_and_inline_frame_picker():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="clip-cover-frame-player" controls playsinline' in template
    assert 'id="clip-cover-ai-scrubber"' not in template
    assert 'const bestFramesByPerson = (frames) =>' in template
    assert 'if (!best.has(key)) best.set(key, frame);' in template
    assert 'return [...best.values()].slice(0, 2);' in template
    assert 'candidateFrames.push(frame);' in template
    assert 'framePicker.classList.add("d-none");' in template
    assert 'aiGrab.addEventListener("click"' in template


def test_cover_modal_generation_state_disables_controls_and_prevents_double_submit():
    template = TEMPLATE.read_text(encoding="utf-8")
    cover_js = template[template.index('const setGeneratingState = (busy) =>'):]

    assert 'modalEl.querySelectorAll("button, input, textarea, video")' in cover_js
    assert 'control.disabled = true;' in cover_js
    assert 'control.disabled = false;' in cover_js
    assert 'if (isGenerating) return;' in cover_js
    assert 'setGeneratingState(true);' in cover_js
    assert 'finally {\n      setGeneratingState(false);' in cover_js
    assert 'id="clip-cover-generating"' in template
    assert 'id="clip-cover-elapsed"' in template


def test_cover_modal_no_text_style_disables_title_and_uses_no_title_prompt():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert 'data-style="no_text"' in template
    assert 'const noTextSelected = () => selectedStyle() === "no_text";' in template
    assert 'aiTitle.disabled = isGenerating || noTextSelected();' in template
    assert 'no_title: noTextSelected(),' in template
