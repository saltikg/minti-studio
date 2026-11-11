from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
from flask import Flask

from app.video_shorts.routes import generation
from app.video_shorts.services import scheduled_clip_customer_actions as actions


def test_saved_vs_render_uses_actual_fields_and_render_persist_clears_diff():
    rendered_options = {
        "title": "Rendered", "caption_anchor": "bottom", "subtitle_font_key": "font-a",
        "edit_keep_ranges": [{"start": 1, "end": 5}], "subtitle_text": "same",
    }
    rendered_entry = {
        "start": 1, "end": 5, "title": "Rendered",
        "render_settings_snapshot": rendered_options,
    }
    plan_entry = {
        "start": 1, "end": 5, "title": "Saved title", "edit_requires_regenerate": False,
        "saved_render_settings_snapshot": {**rendered_options, "title": "Saved title", "caption_anchor": "top"},
        "saved_render_entry_snapshot": {"start": 1, "end": 5, "title": "Saved title", "edit_keep_ranges": [{"start": 1, "end": 5}]},
    }
    changes = generation._clip_unapplied_changes(
        plan_entry=plan_entry, current_options={},
        generated_record={"raw_plan_entry_json": json.dumps(rendered_entry)},
    )
    assert set(changes["content"]) == {"title"}
    assert set(changes["design"]) == {"caption_anchor"}
    assert "edit_requires_regenerate" not in changes["content"]

    generation._persist_clip_saved_settings_snapshot(
        plan_entry, options=rendered_options, entry_snapshot=rendered_entry,
        source="render_complete", actor="worker",
    )
    changes = generation._clip_unapplied_changes(
        plan_entry=plan_entry, current_options={},
        generated_record={"raw_plan_entry_json": json.dumps(rendered_entry)},
    )
    assert changes == {"content": {}, "design": {}}


def test_template_only_warns_scheduled_changed_clips_and_old_copy_is_gone():
    template = (Path(__file__).resolve().parents[2] / "app/video_shorts/templates/generate_short.html").read_text()
    assert "{% if generate_v2 and row.scheduled_unapplied_changes %}" in template
    assert "Changes not in scheduled post ·" in template
    assert "The scheduled post uses the last generated video." in template
    assert 'showGenerateV2TextToast("Saved · regenerate to update the post")' in template
    assert "Edited — regenerate to apply" not in template
    assert "Design changed — regenerate to apply" not in template


def test_customer_actions_are_batched_once_after_ten_minutes(tmp_path, monkeypatch):
    db_path = str(tmp_path / "actions.duckdb")
    with duckdb.connect(db_path) as conn:
        conn.execute((Path(__file__).resolve().parents[2] / "scripts/create_scheduled_clip_customer_actions.sql").read_text())
    monkeypatch.setattr(actions, "get_db", lambda: duckdb.connect(db_path))
    monkeypatch.setattr(actions, "get_db_readonly", lambda: duckdb.connect(db_path))
    for action in ("edit+save", "regenerate"):
        assert actions.record_customer_action(
            owner_user_id="owner", brand_id="brand", video_pk=1, plan_index=2,
            brand_name="Test Brand", clip_title="A clip", action=action,
            changed_fields=["title"] if action == "edit+save" else [],
            schedules=[{"platform": "youtube", "publish_at": "2026-11-14T17:02:00Z"}],
            clip_url="https://example.test/clip", actor_user_id="owner",
        )
    sent = []
    assert actions.process_due_batches(
        now=datetime.now(timezone.utc) + timedelta(minutes=11),
        sender=lambda **kwargs: sent.append(kwargs),
    ) == 1
    assert len(sent) == 1
    assert "edit+save, regenerate" in sent[0]["text"]
    assert actions.process_due_batches(
        now=datetime.now(timezone.utc) + timedelta(minutes=12),
        sender=lambda **kwargs: sent.append(kwargs),
    ) == 0


def test_admin_action_never_records(monkeypatch):
    app = Flask(__name__)
    called = []
    monkeypatch.setattr(generation, "_pending_schedule_contexts", lambda **kwargs: called.append(kwargs) or [{}])
    with app.test_request_context("/video_shorts/generate/1"):
        generation._record_scheduled_customer_action(
            editor_context={"owner_user_id": "customer", "brand_id": "brand"},
            current_user={"id": "admin", "role": "admin"}, video_pk=1,
            source_video_id="source", plan_index=2, clip_title="Clip",
            action="edit+save", changed_fields=["title"],
        )
    assert called == []
