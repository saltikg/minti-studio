import json

import pytest

from app.video_shorts.services import niche_short_ideas


def test_validate_generated_idea_enforces_shape_and_original_wording():
    payload = {
        "why_it_worked": "It opens with a sharp question, builds tension through three clear steps, and closes with a practical takeaway.",
        "format": "Tip",
        "your_version": {
            "title": "A safer question to ask",
            "first_line": "What changes when you pause first?",
            "beats": ["Name the common assumption", "Show the hidden tradeoff", "Offer one next step"],
            "ideal_length": "35–45 seconds",
        },
    }
    result = niche_short_ideas._validate_generated_idea(
        payload, "This transcript uses completely different language throughout the source video."
    )
    assert result["format_tag"] == "Tip"
    assert len(result["beats"]) == 3
    assert result["ideal_length"] == "35–45 seconds"

    payload["your_version"]["first_line"] = "one two three four five six seven"
    with pytest.raises(niche_short_ideas.NicheShortIdeasError, match="copied"):
        niche_short_ideas._validate_generated_idea(
            payload, "zero one two three four five six seven eight"
        )


def test_generate_idea_uses_one_gpt_41_call_and_niche_language():
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content = json.dumps({
                "why_it_worked": "The direct hook creates curiosity, then a compact progression delivers a useful payoff without detours.",
                "format": "Story",
                "your_version": {
                    "title": "Sabırla gelen beklenmedik cevap",
                    "first_line": "Cevabın geciktiğini düşündüğünüz oldu mu?",
                    "beats": ["Bekleyişi kur", "Yanlış beklentiyi göster", "Dersi kısa bağla"],
                    "ideal_length": "45–60 saniye",
                },
            }, ensure_ascii=False)
            message = type("Message", (), {"content": content})()
            choice = type("Choice", (), {"message": message})()
            usage = type("Usage", (), {"prompt_tokens": 200, "completion_tokens": 100})()
            return type("Response", (), {"choices": [choice], "usage": usage})()

    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    result = niche_short_ideas._generate_idea(
        {"title": "Source", "duration_s": 58, "outlier_ratio": 12.4},
        {"transcript": "Kaynak metin burada tamamen farklı ifadelerle devam ediyor."},
        "tr",
        client,
    )

    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-4.1"
    assert json.loads(calls[0]["messages"][1]["content"])["niche_language"] == "tr"
    assert result["format_tag"] == "Story"
    assert result["ideal_length"] == "45–60 seconds"
    assert result["llm_cost_usd"] == pytest.approx(0.0012)


def test_non_english_niche_still_requires_english_analysis():
    payload = {
        "why_it_worked": "Güçlü bir açılış ile izleyicinin ilgisini hemen çekti.",
        "format": "Tip",
        "your_version": {
            "title": "Yeni bir başlık", "first_line": "İlk cümle burada",
            "beats": ["Birinci adım", "İkinci adım", "Son adım"],
            "ideal_length": "40",
        },
    }
    with pytest.raises(niche_short_ideas.NicheShortIdeasError, match="must be English"):
        niche_short_ideas._validate_generated_idea(
            payload, "Tamamen farklı kaynak metni", niche_language="tr"
        )


def test_customer_loader_never_selects_transcript(monkeypatch):
    queries = []

    class Result:
        description = [(name,) for name in (
            "source_video_id", "source_channel_title", "source_title", "source_views",
            "source_ratio", "source_url", "source_duration_s", "selected_rank",
            "why_it_worked", "idea_title", "first_line", "beats_json", "ideal_length", "format_tag",
        )]

        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params=None):
            queries.append(sql)
            if "MAX(as_of)" in sql:
                return Result([("2026-11-28",)])
            return Result([("vid", "Channel", "Source", 1000, 5.0, "url", 42, 1,
                            "Why", "Idea", "Hook", '["a","b","c"]', "35–45 seconds", "Tip")])

    monkeypatch.setattr(niche_short_ideas, "table_columns", lambda *_args: {"source_video_id"})
    _as_of, rows = niche_short_ideas.load_niche_short_ideas(Connection(), 1)

    assert rows[0]["beats"] == ["a", "b", "c"]
    assert all("transcript" not in query.casefold() for query in queries)


def test_transcription_rotates_proxy_session_before_whisper(monkeypatch, tmp_path):
    audio = tmp_path / "source.m4a"
    audio.write_bytes(b"audio")
    attempts = []

    def download(_url, _video_id):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("temporary bot check")
        return audio, True, False

    monkeypatch.setattr("app.video_shorts.worker._download_youtube_audio_with_proxy", download)
    monkeypatch.setattr(
        "app.video_shorts.services.transcript_service._transcribe_with_whisper",
        lambda *_args, **_kwargs: ("Original transcript", [{"lang": "en"}]),
    )

    result = niche_short_ideas._transcribe_source("video-1", 60, "en")

    assert len(attempts) == 3
    assert result["transcript"] == "Original transcript"
    assert result["whisper_cost_usd"] == pytest.approx(0.006)
