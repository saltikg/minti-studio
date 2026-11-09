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


def test_successful_render_enqueues_low_priority_candidate_precompute():
    enqueue_source = inspect.getsource(worker._enqueue_cover_candidate_precompute)
    process_source = inspect.getsource(worker._process_next_job)

    assert 'job_type=JOB_TYPE_PREPARE_COVER_CANDIDATES' in enqueue_source
    assert '"job_origin": render_jobs.AUTO_RENDER_JOB_ORIGIN' in enqueue_source
    assert 'priority=-100' in enqueue_source
    assert '_enqueue_cover_candidate_precompute(job, result)' in process_source


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
    assert 'return [...best.values()].slice(0, 3);' in template
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


def test_cover_modal_looks_use_real_images_and_no_text_disables_title():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert template.count('class="cover-look-card') == 6
    assert template.count("filename='cover_looks/") == 6
    assert 'data-look="no_text"' in template
    assert 'const noTextSelected = () => selectedLook() === "no_text";' in template
    assert 'aiTitle.disabled = isGenerating || noTextSelected();' in template
    assert 'no_title: noTextSelected(),' in template
    assert 'id="clip-cover-ai-tones"' not in template
    assert 'Short titles read better.' in template
    assert '<span class="ms-1">Suggest</span>' in template


def test_cover_is_used_by_rail_and_player_and_updates_without_reload():
    template = TEMPLATE.read_text(encoding="utf-8")
    cover_js = template[template.index("const applyCoverToEditor = (coverUrl"):]

    assert 'data-current-cover-url="{{ row.cover_url|e }}"' in template
    assert 'data-frame-thumbnail="{{ (row.poster_url or \'\')|e }}"' in template
    assert 'data-cover-thumbnail' in template
    assert '<span class="clip-rail-cover-badge">Cover</span>' in template
    assert 'poster="{{ row.cover_url }}"' in template
    assert 'row.cover_url or row.poster_url or short_bg_url' not in template
    assert 'railItem.dataset.currentCoverUrl = url;' in cover_js
    assert 'player.setAttribute("poster", url);' in cover_js
    assert 'player.removeAttribute("poster");' in cover_js
    assert 'applyCoverToEditor(coverUrl);' in cover_js
    assert 'applyCoverToEditor("");' in cover_js
    assert 'bootstrap.Modal.getOrCreateInstance(modalEl).hide();' in cover_js
    assert 'showGenerateV2TextToast("Cover saved")' in cover_js


def test_cover_modal_resets_clip_state_and_ignores_late_previous_responses():
    template = TEMPLATE.read_text(encoding="utf-8")
    cover_js = template[template.index('const resetModalForClip = (card) =>'):]
    full_cover_js = template[template.index('const requestIsCurrent = (token, card) =>'):]

    assert 'openToken += 1;' in cover_js
    assert 'openController?.abort();' in cover_js
    assert 'aiTitle.value = card?.dataset.title || "";' in cover_js
    assert 'selectedFrames = [];' in cover_js
    assert 'candidateFrames = [];' in cover_js
    assert 'aiPrompt.value = "";' in cover_js
    assert 'renderHistory([]);' in cover_js
    assert 'showCurrentCover("");' in cover_js
    assert 'renderCandidateSkeletons();' in cover_js
    assert 'Promise.allSettled([loadCover(token, card), loadAiCandidates(token, card)])' in cover_js
    assert full_cover_js.count('if (!requestIsCurrent(token, card)) return;') >= 3
    assert 'aiTitle.value = payload.title' not in cover_js


def test_cover_modal_renders_six_static_visual_look_cards():
    template = TEMPLATE.read_text(encoding="utf-8")
    assets = ROOT / "app/video_shorts/static/cover_looks"

    assert template.count('class="cover-look-card') == 6
    for filename in (
        "yellow-title.webp", "white-box.webp", "minimal.webp",
        "dramatic.webp", "curiosity.webp", "no-text.webp",
    ):
        assert filename in template
        assert (assets / filename).is_file()
    assert 'id="clip-cover-look-hint"' in template
    assert 'payload?.ai_presets?.looks || {}' in template
    assert 'id="clip-cover-ai-tones"' not in template


def test_cover_candidates_keep_picker_visible_and_fail_to_retry():
    template = TEMPLATE.read_text(encoding="utf-8")
    skeleton_js = template[
        template.index('const renderCandidateSkeletons = () =>'):
        template.index('const renderCandidateCards = () =>')
    ]

    assert 'aiCandidates.append(pickFrameNode());' in skeleton_js
    assert 'window.setTimeout(() =>' in template
    assert '}, 8000);' in template
    assert 'Couldn\'t load frames — ' in template
    assert 'retry.textContent = "Retry";' in template
    assert 'renderCandidateFailure(token, card);' in template
    assert 'const grabbedSelected = selectedFrames.filter' in template
    assert 'selectedFrames = grabbedSelected.length ? grabbedSelected.slice(0, 2)' in template
