import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from app.video_shorts.services import transcript_service


class _ProbeClient:
    def __init__(self, languages):
        self._languages = iter(languages)
        self.audio = SimpleNamespace(
            transcriptions=SimpleNamespace(create=self._create),
        )

    def _create(self, **_kwargs):
        return SimpleNamespace(language=next(self._languages), text="", segments=[])


@pytest.mark.parametrize("language", ["tr", "en"])
def test_known_language_probe_remains_stable(monkeypatch, tmp_path, language):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"")
    (tmp_path / "ffprobe").write_bytes(b"")
    counter = {"value": 0}

    def extract(*_args):
        counter["value"] += 1
        probe = tmp_path / f"probe-{counter['value']}.mp3"
        probe.write_bytes(b"audio")
        return probe

    monkeypatch.setattr(transcript_service, "_openai_client", _ProbeClient([language] * 3))
    monkeypatch.setattr(transcript_service, "_resolve_ffmpeg", lambda: str(ffmpeg))
    monkeypatch.setattr(transcript_service, "_extract_audio_segment", extract)
    monkeypatch.setattr(
        transcript_service,
        "run_media_subprocess",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="120", stderr=""),
    )

    assert transcript_service._detect_primary_audio_language(source) == language


def test_probe_preserves_hindi_and_disagreement_uses_auto(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"")
    (tmp_path / "ffprobe").write_bytes(b"")

    def run(languages):
        counter = {"value": 0}

        def extract(*_args):
            counter["value"] += 1
            path = tmp_path / f"probe-{counter['value']}.mp3"
            path.write_bytes(b"audio")
            return path

        monkeypatch.setattr(transcript_service, "_openai_client", _ProbeClient(languages))
        monkeypatch.setattr(transcript_service, "_resolve_ffmpeg", lambda: str(ffmpeg))
        monkeypatch.setattr(transcript_service, "_extract_audio_segment", extract)
        monkeypatch.setattr(
            transcript_service,
            "run_media_subprocess",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="120", stderr=""),
        )
        return transcript_service._detect_primary_audio_language(source)

    assert run(["hindi", "hindi", "hindi"]) == "hi"
    assert run(["hindi", "turkish", "hindi"]) == ""


def test_english_translation_keeps_segment_timestamps_and_builds_word_timings(monkeypatch):
    source = [
        {"start": 1.25, "end": 3.75, "duration": 2.5, "lang": "hi", "text": "नमस्ते दुनिया"},
        {"start": 4.0, "end": 7.0, "duration": 3.0, "lang": "hi", "text": "यह एक परीक्षण है"},
    ]

    class _TranslationClient:
        def create(self, **kwargs):
            request = json.loads(kwargs["messages"][1]["content"])
            values = ["Hello world", "This is a test"]
            payload = {
                "segments": [
                    {"id": item["id"], "text": values[index]}
                    for index, item in enumerate(request["segments"])
                ]
            }
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])

    monkeypatch.setattr(
        transcript_service,
        "_openai_client",
        SimpleNamespace(chat=SimpleNamespace(completions=_TranslationClient())),
    )
    full_text, translated = transcript_service.translate_segments_to_english(source)

    assert full_text == "Hello world This is a test"
    for original, english in zip(source, translated):
        assert (english["start"], english["end"], english["duration"]) == (
            original["start"], original["end"], original["duration"]
        )
        assert english["lang"] == "en"
        assert english["words"][0]["start"] == original["start"]
        assert english["words"][-1]["end"] == original["end"]


def test_fetch_transcript_selects_brand_english_track():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE shorts_brands(id VARCHAR, caption_language VARCHAR)")
    conn.execute("CREATE TABLE youtube_videos(id INTEGER, video_id VARCHAR, brand_id VARCHAR, deleted_at TIMESTAMP)")
    conn.execute(
        """CREATE TABLE youtube_transcripts(
             video_id VARCHAR, full_text TEXT, segments_json TEXT, whisper_segments_json TEXT,
             detected_language VARCHAR, segments_en_json TEXT, full_text_en TEXT, translated_to VARCHAR)"""
    )
    conn.execute("CREATE TABLE shorts_transcript_overrides(video_pk INTEGER, full_text TEXT, segments_json TEXT)")
    original = [{"start": 0, "end": 1, "duration": 1, "lang": "hi", "text": "नमस्ते"}]
    english = [{"start": 0, "end": 1, "duration": 1, "lang": "en", "text": "Hello"}]
    conn.execute("INSERT INTO shorts_brands VALUES ('brand', 'en')")
    conn.execute("INSERT INTO youtube_videos VALUES (1, 'video', 'brand', NULL)")
    conn.execute(
        "INSERT INTO youtube_transcripts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["video", "नमस्ते", json.dumps(original), json.dumps(original), "hi", json.dumps(english), "Hello", "en"],
    )

    full_text, segments = transcript_service._fetch_transcript(conn, "video", video_pk=1)

    assert full_text == "Hello"
    assert segments[0]["text"] == "Hello"
    assert segments[0]["lang"] == "en"
