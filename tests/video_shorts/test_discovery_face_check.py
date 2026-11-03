import json
from types import SimpleNamespace

from PIL import Image

from app.video_shorts.services.discovery_face_check import check_face_on_camera


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    @property
    def content(self):
        import io

        output = io.BytesIO()
        Image.new("RGB", (32, 18), "blue").save(output, format="JPEG")
        return output.getvalue()


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **_kwargs):
        return self

    def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.payload)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


def _samples(count=6):
    return [
        {"video_id": f"video{index}", "thumbnail_url": f"https://example.test/{index}.jpg"}
        for index in range(count)
    ]


def test_face_check_is_unsure_with_fewer_than_three_videos():
    result = check_face_on_camera(_samples(1))
    assert result["face_check_verdict"] == "face_unsure"
    assert "at least 3" in result["face_check_reason"]


def test_code_derives_pass_from_half_of_usable_videos(monkeypatch):
    monkeypatch.setattr("app.video_shorts.services.discovery_face_check.requests.get", lambda *_args, **_kwargs: _Response())
    result = check_face_on_camera(
        _samples(),
        client=_Client(
            {
                "videos": [
                    {"video_index": 1, "same_presenter_visible": True},
                    {"video_index": 2, "same_presenter_visible": True},
                    {"video_index": 3, "same_presenter_visible": True},
                    {"video_index": 99, "same_presenter_visible": True},
                ],
                "presenter_type": "real_person",
                "reason": "Same presenter appears in three rows.",
            }
        ),
        model="text-only-model",
    )
    assert result["face_check_verdict"] == "face_pass"
    assert result["face_check_same_person_count"] == 3
    assert result["_face_check_model"] == "gpt-4o-mini"


def test_code_clamps_duplicate_and_out_of_range_video_results(monkeypatch):
    monkeypatch.setattr("app.video_shorts.services.discovery_face_check.requests.get", lambda *_args, **_kwargs: _Response())
    result = check_face_on_camera(
        _samples(3),
        client=_Client(
            {
                "videos": [
                    {"video_index": 1, "same_presenter_visible": True},
                    {"video_index": 1, "same_presenter_visible": True},
                    {"video_index": 4, "same_presenter_visible": True},
                ],
                "presenter_type": "mixed",
                "reason": "Mixed imagery.",
            }
        ),
    )
    assert result["face_check_same_person_count"] == 1
    assert result["face_check_verdict"] == "face_unsure"


def test_code_only_fails_allowed_non_presenter_types(monkeypatch):
    monkeypatch.setattr("app.video_shorts.services.discovery_face_check.requests.get", lambda *_args, **_kwargs: _Response())
    result = check_face_on_camera(
        _samples(4),
        client=_Client(
            {
                "videos": [{"video_index": index, "same_presenter_visible": False} for index in range(1, 5)],
                "presenter_type": "text_or_slides",
                "reason": "Only slides are visible.",
            }
        ),
    )
    assert result["face_check_verdict"] == "face_fail"


def test_fewer_than_three_loaded_rows_is_unsure(monkeypatch):
    calls = {"count": 0}

    def response(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] <= 2:
            return _Response()
        return SimpleNamespace(status_code=404)

    monkeypatch.setattr("app.video_shorts.services.discovery_face_check.requests.get", response)
    result = check_face_on_camera(_samples(3), client=_Client({}))
    assert result["face_check_verdict"] == "face_unsure"
    assert "at least 3" in result["face_check_reason"]
