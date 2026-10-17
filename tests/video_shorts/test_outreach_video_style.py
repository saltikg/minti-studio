from datetime import datetime, timedelta, timezone

from flask import Flask

from app.video_shorts.config import SUBTITLE_PRESETS
from app.video_shorts.routes import generation
from app.video_shorts.services.autopilot_leads import (
    OUTREACH_SUBTITLE_PRESET,
    _bold_pop_short_editor_defaults,
    _bold_pop_video_style_values,
)


def test_outreach_video_style_is_fixed_and_isolated_from_product_defaults():
    style = _bold_pop_video_style_values()

    assert style == style | {
        "subtitle_font_key": "arimo",
        "subtitle_font_size": 45,
        "subtitle_margin": 520,
        "subtitle_margin_custom": False,
        "caption_anchor": "bottom",
        "subtitle_style": "karaoke",
        "subtitle_preset": "outreach_opus",
        "subtitle_text_color": "#FFFFFF",
        "subtitle_bg_color": "#000000",
        "subtitle_bg_alpha": 85,
        "subtitle_text_alpha": 100,
        "visual_mode": "video",
        "crop_aspect": "portrait",
        "layout_mode": "auto",
    }
    assert OUTREACH_SUBTITLE_PRESET != "opus"
    assert SUBTITLE_PRESETS[OUTREACH_SUBTITLE_PRESET] == SUBTITLE_PRESETS["opus"]


def test_outreach_editor_defaults_match_fixed_video_style():
    style = _bold_pop_video_style_values()
    defaults = _bold_pop_short_editor_defaults()

    assert defaults["sub_font"] == style["subtitle_font_key"]
    assert defaults["sub_font_size"] == style["subtitle_font_size"]
    assert defaults["sub_margin"] == style["subtitle_margin"]
    assert defaults["caption_anchor"] == style["caption_anchor"]
    assert defaults["subtitle_preset"] == style["subtitle_preset"]
    assert defaults["subtitle_text_color"] == style["subtitle_text_color"]
    assert defaults["subtitle_bg_color"] == style["subtitle_bg_color"]
    assert defaults["subtitle_bg_alpha"] == style["subtitle_bg_alpha"]


def test_outreach_auto_layout_uses_face_count_modes():
    assert generation._face_aware_mode_for_count(0) == "fit"
    assert generation._face_aware_mode_for_count(1) == "fill"
    assert generation._face_aware_mode_for_count(2) == "split"
    assert generation._face_aware_mode_for_count(3) == "fit"


def test_auto_layout_builds_face_track_during_render(monkeypatch, tmp_path):
    samples = [
        {
            "t": 0.5,
            "face_present": True,
            "face_count": 1,
            "faces": [
                {"cx_ratio": 0.7, "cy_ratio": 0.3, "w_ratio": 0.2, "h_ratio": 0.25}
            ],
        }
    ]
    captured = {}
    track_path = tmp_path / "face-track.json"

    monkeypatch.setattr(generation, "_face_aware_layout_skip_reason", lambda **_kwargs: None)
    monkeypatch.setattr(generation, "_sample_face_presence_for_layout", lambda **_kwargs: samples)

    def write_track(_video_id, rows, _start, _end):
        captured["rows"] = rows
        track_path.write_text("[]")
        return track_path

    def attach_zoom(segments, *, face_track_smooth_path, **_kwargs):
        captured["track_path"] = face_track_smooth_path
        return segments

    monkeypatch.setattr(generation, "_write_preview_face_track_smooth", write_track)
    monkeypatch.setattr(generation, "_attach_face_aware_zoom_to_segments", attach_zoom)
    monkeypatch.setattr(
        generation,
        "_attach_face_aware_split_crops_to_segments",
        lambda segments, **_kwargs: segments,
    )

    with Flask(__name__).app_context():
        result = generation._compute_face_aware_layout_segments(
            video_id="video-1",
            plan_index=0,
            source_path=tmp_path / "source.mp4",
            start_seconds=10.0,
            end_seconds=15.0,
            owner_user_id="owner-1",
            crop_settings={},
            crop_aspect="portrait",
            visual_mode="video",
            layout_mode="auto",
        )

    assert result["segments"] == [{"start": 0.0, "end": 5.0, "mode": "fill"}]
    assert result["face_track_smooth_path"] == str(track_path)
    assert captured["track_path"] == track_path
    assert captured["rows"][0] == {
        "t": 10.5,
        "cx_ratio": 0.7,
        "cy_ratio": 0.3,
        "w_ratio": 0.2,
        "h_ratio": 0.25,
        "found": True,
    }


def test_transcript_grace_period_does_not_fail_active_ingest():
    now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)

    assert not generation._source_not_ready_is_stuck(
        5,
        now - timedelta(seconds=50),
        now=now,
    )
    assert generation._source_not_ready_is_stuck(
        5,
        now - timedelta(minutes=11),
        now=now,
    )
    assert not generation._source_not_ready_is_stuck(
        4,
        now - timedelta(minutes=30),
        now=now,
    )


def test_outreach_clip_selection_uses_top_scores_when_threshold_has_no_matches():
    entries = [
        {"plan_index": 1, "score": 70, "status": "pending"},
        {"plan_index": 2, "score": 78, "status": "pending"},
        {"plan_index": 3, "score": 75, "status": "pending"},
        {"plan_index": 4, "score": 55, "status": "pending"},
    ]

    selected = generation._select_lead_generation_entries(
        entries,
        min_score=80,
        limit=5,
        fallback_limit=3,
    )

    assert [entry["plan_index"] for entry in selected] == [2, 3, 1]
    assert all(entry["selected_for_generation_reason"] == "top_score_fallback" for entry in selected)
    assert not entries[3]["selected_for_generation"]


def test_outreach_clip_selection_recovers_existing_completed_clips_without_scores():
    entries = [
        {"plan_index": 3, "score": None, "status": "created"},
        {"plan_index": 2, "score": None, "status": "created"},
        {"plan_index": 1, "score": None, "status": "created"},
    ]

    selected = generation._select_lead_generation_entries(
        entries,
        min_score=80,
        limit=5,
        fallback_limit=3,
    )

    assert [entry["plan_index"] for entry in selected] == [3, 2, 1]
    assert all(entry["selected_for_generation_reason"] == "existing_completed_fallback" for entry in selected)
