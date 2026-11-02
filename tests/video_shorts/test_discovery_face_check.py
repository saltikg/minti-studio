import json
from types import SimpleNamespace

from PIL import Image

from app.video_shorts.services.discovery_face_check import check_face_on_camera


class _Response:
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


def test_face_check_is_unsure_with_fewer_than_three_thumbnails():
    result = check_face_on_camera(["https://example.test/one.jpg"])
    assert result["face_check_verdict"] == "face_unsure"
    assert "at least 3" in result["face_check_reason"]


def test_face_check_accepts_strict_valid_pass(monkeypatch):
    monkeypatch.setattr("app.video_shorts.services.discovery_face_check.requests.get", lambda *_args, **_kwargs: _Response())
    result = check_face_on_camera(
        [f"https://example.test/{index}.jpg" for index in range(6)],
        client=_Client(
            {
                "verdict": "face_pass",
                "same_person_count": 5,
                "presenter_type": "real_person",
                "reason": "Same presenter appears in five thumbnails.",
            }
        ),
        model="text-only-model",
    )
    assert result["face_check_verdict"] == "face_pass"
    assert result["face_check_same_person_count"] == 5
    assert result["_face_check_model"] == "gpt-4o-mini"


def test_face_check_converts_invalid_pass_to_unsure(monkeypatch):
    monkeypatch.setattr("app.video_shorts.services.discovery_face_check.requests.get", lambda *_args, **_kwargs: _Response())
    result = check_face_on_camera(
        [f"https://example.test/{index}.jpg" for index in range(3)],
        client=_Client(
            {
                "verdict": "face_pass",
                "same_person_count": 2,
                "presenter_type": "real_person",
                "reason": "Invalid pass.",
            }
        ),
    )
    assert result["face_check_verdict"] == "face_unsure"
    assert "requires same_person_count" in result["face_check_reason"]
