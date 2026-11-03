from types import SimpleNamespace
from pathlib import Path

from flask import Flask
from markupsafe import escape

from app.video_shorts.routes import generation
from app.video_shorts.routes import settings


FIXED_TEXT = "Watch the full video on my YouTube channel — if you need the link, it's in my Linktree/profile!"


def _generation_kwargs():
    return {
        "current_user": {"id": "owner", "plan_id": "pro"},
        "brand_id": "brand",
        "video_id": "source",
        "video_title": "Title",
        "video_date_text": None,
        "duration_seconds": 30,
        "plan_entry": {"plan_index": 1, "start": 1, "end": 2, "transcript_full": "clip"},
        "segments": [],
    }


def test_fixed_mode_returns_verbatim_without_openai(monkeypatch):
    monkeypatch.setattr(generation, "fixed_description_for_brand", lambda user_id, brand_id: FIXED_TEXT)
    monkeypatch.setattr(
        generation,
        "_openai_client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: (_ for _ in ()).throw(AssertionError("LLM called"))))),
    )

    assert generation._generate_description_for_plan_entry(**_generation_kwargs()) == FIXED_TEXT


def test_fixed_mode_overwrites_existing_render_description(monkeypatch):
    monkeypatch.setattr(generation, "fixed_description_for_brand", lambda user_id, brand_id: FIXED_TEXT)
    kwargs = _generation_kwargs()
    kwargs["plan_entry"]["yt_description"] = "old AI text"

    assert generation._maybe_autogenerate_description_for_plan_entry(**kwargs) is True
    assert kwargs["plan_entry"]["yt_description"] == FIXED_TEXT


def test_ai_mode_still_calls_openai(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="AI description"))])

    monkeypatch.setattr(generation, "fixed_description_for_brand", lambda user_id, brand_id: None)
    monkeypatch.setattr(generation, "load_description_settings", lambda user_id, brand_id: ("{transcripts_source}", "en"))
    monkeypatch.setattr(generation, "build_transcript_for_range", lambda *args, **kwargs: "transcript")
    monkeypatch.setattr(generation, "_openai_client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))

    assert generation._generate_description_for_plan_entry(**_generation_kwargs()) == "AI description"
    assert len(calls) == 1


def test_shared_component_renders_both_modes():
    template_dir = Path(__file__).resolve().parents[2] / "app" / "video_shorts" / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    with app.test_request_context("/"):
        template = app.jinja_env.get_template("partials/_description_prompt_component.html")
        html = template.module.description_prompt_component(
            "/settings/prompts/description",
            "prompt",
            "en",
            "fixed",
            FIXED_TEXT,
            "default prompt",
            "en",
            "ai",
            False,
        )

    assert "AI-generated (prompt template)" in html
    assert "Fixed text (no AI)" in html
    assert str(escape(FIXED_TEXT)) in html


def test_admin_description_save_uses_workspace_owner_and_brand(monkeypatch):
    class Connection:
        def close(self):
            pass

    saved = []
    app = Flask(__name__)
    monkeypatch.setattr(settings, "get_db", Connection)
    monkeypatch.setattr(settings, "ensure_brand_schema", lambda conn: None)
    monkeypatch.setattr(settings, "_static_asset_scope", lambda conn, current_user: ("shelley-owner", "shelley-brand", {"admin_operation_brand": "shelley-brand"}))
    monkeypatch.setattr(settings, "save_description_mode", lambda user_id, brand_id, value, updated_by: saved.append(("mode", user_id, brand_id, value, updated_by)))
    monkeypatch.setattr(settings, "save_fixed_description_text", lambda user_id, brand_id, value, updated_by: saved.append(("text", user_id, brand_id, value, updated_by)))
    monkeypatch.setattr(settings, "save_description_languages", lambda user_id, brand_id, value, updated_by: saved.append(("languages", user_id, brand_id, value, updated_by)))
    monkeypatch.setattr(settings, "load_description_settings", lambda user_id, brand_id: ("prompt", "en"))
    monkeypatch.setattr(settings, "load_description_mode", lambda user_id, brand_id: "fixed")
    monkeypatch.setattr(settings, "load_fixed_description_text", lambda user_id, brand_id: FIXED_TEXT)

    with app.test_request_context("/settings/prompts/description?admin_operation_brand=shelley-brand&admin_operation_kind=customer", method="POST", json={"description_mode": "fixed", "fixed_description_text": FIXED_TEXT, "description_languages": "en"}):
        from flask import g
        g.vs_current_user = {"id": "admin-owner", "brand_id": "admin-brand"}
        response = settings.description_prompt_settings_api()

    assert response.get_json()["brand_id"] == "shelley-brand"
    assert {item[1] for item in saved} == {"shelley-owner"}
    assert {item[2] for item in saved} == {"shelley-brand"}
    assert {item[4] for item in saved} == {"admin-owner"}
