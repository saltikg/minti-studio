import json
from pathlib import Path
from types import SimpleNamespace

from app.video_shorts.services import discovery_frame_review as review


def test_select_review_video_uses_cached_order_and_range():
    lead = {
        "raw_json": json.dumps(
            {
                "face_check_video_samples": [
                    {"video_id": "short", "duration_seconds": 120, "title": "Short"},
                    {"video_id": "too-long", "duration_seconds": 3700, "title": "Too long"},
                    {"video_id": "fits", "duration_seconds": 900, "title": "Newest fitting video"},
                ]
            }
        )
    }
    selected = review.select_review_video(lead)
    assert selected["video_id"] == "fits"
    assert selected["title"] == "Newest fitting video"


def test_select_review_video_fetches_missing_cached_duration(monkeypatch):
    monkeypatch.setattr(
        review,
        "_video_details",
        lambda ids: {"cached": {"video_id": "cached", "duration_seconds": 600, "title": "Cached title"}},
    )
    selected = review.select_review_video(
        {"raw_json": json.dumps({"face_check_video_samples": [{"video_id": "cached"}]})}
    )
    assert selected["video_id"] == "cached"
    assert selected["duration_seconds"] == 600
    assert selected["title"] == "Cached title"


def test_extract_frames_seeks_before_input_and_creates_five(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "_stream_info", lambda _video_id: ("https://stream.test/video", {}, ""))
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(review.subprocess, "run", run)
    frames = review._extract_frames(
        {"video_id": "video-1", "duration_seconds": 1000},
        tmp_path,
        review.time.monotonic() + 90,
    )
    assert [item["percentage"] for item in frames] == [10, 30, 50, 70, 90]
    assert all(command.index("-ss") < command.index("-i") for command in commands)
    assert all("scale=240:-2:force_original_aspect_ratio=decrease" in command for command in commands)
    assert len(commands) == 5


def test_stream_info_uses_ingest_proxy_cookies_and_bgutil(monkeypatch, tmp_path):
    captured = {}
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("cookies")
    monkeypatch.setenv("INGEST_PROXY_URL", "http://user:pass@proxy.test:823")
    monkeypatch.setenv("INGEST_PROXY_COOKIES", str(cookie_file))
    monkeypatch.setattr(review.secrets, "token_hex", lambda _size: "session123")

    class YoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, _url, download=False):
            assert download is False
            return {"url": "https://stream.test/video", "http_headers": {"User-Agent": "test"}}

    monkeypatch.setattr(review, "yt_dlp", SimpleNamespace(YoutubeDL=YoutubeDL))
    stream_url, headers, proxy = review._stream_info("video-1")
    assert stream_url == "https://stream.test/video"
    assert headers == {"User-Agent": "test"}
    assert ";sessid.session123" in proxy
    assert captured["proxy"] == proxy
    assert captured["cookiefile"] == str(cookie_file)
    assert captured["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == ["http://127.0.0.1:4416"]


def test_ai_review_normalizes_dont_promote(monkeypatch, tmp_path):
    paths = []
    for index in range(5):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"jpeg")
        paths.append({"path": path})

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"label":"DONT PROMOTE","reason":"Only slides."}'))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
    )

    class Client:
        request = None

        def with_options(self, **_kwargs):
            return self

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            self.request = kwargs
            return response

    client = Client()
    monkeypatch.setattr(review, "_openai_client", client)
    monkeypatch.setattr(review, "OPENAI_MODEL", "gpt-4.1-mini")
    result = review._ai_review(paths, review.time.monotonic() + 30)
    assert result["label"] == "DON'T PROMOTE"
    assert result["reason"] == "Only slides."
    assert result["cost_usd"] > 0
    content = client.request["messages"][0]["content"]
    assert len([item for item in content if item["type"] == "image_url"]) == 5


def test_safe_error_redacts_stream_and_proxy_urls():
    message = review._safe_error(
        "ffmpeg -http_proxy http://user:secret@proxy.test:823 "
        "-i https://googlevideo.test/videoplayback?sig=secret"
    )
    assert "secret" not in message
    assert "proxy.test" not in message
    assert "googlevideo.test" not in message
    assert message.count("[redacted-url]") == 2


def test_public_errors_are_short_and_categorized():
    assert review._error_code("Sign in to confirm you're not a bot") == "bot_check"
    assert review._error_code("ffmpeg timed out") == "timeout"
    assert review._public_error("bot_check") == "Frames unavailable (bot check)"
    assert review._public_error("timeout") == "Frames unavailable (timeout)"
    assert review._public_error("extraction") == "Frames unavailable"
