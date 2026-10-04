from types import SimpleNamespace

from app.video_shorts.services import discovery_promote_queue


class _FakeChatCompletions:
    def __init__(self, content):
        self.content = content

    def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                )
            ]
        )


def _fake_client(content):
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions(content)))


def test_infer_outreach_first_name_accepts_clear_person(monkeypatch):
    monkeypatch.setattr(discovery_promote_queue, "_openai_client", _fake_client('{"first_name":"Justin"}'))

    result = discovery_promote_queue.infer_outreach_first_name_for_lead(
        {
            "channel_title": "The Retirement Cafe with Justin King",
            "creator_name": "The Retirement Cafe with Justin King",
            "channel_description": "Hosted by retirement planner Justin King.",
            "creator_email": "jk@example.com",
        }
    )

    assert result == "Justin"


def test_infer_outreach_first_name_marks_non_person(monkeypatch):
    monkeypatch.setattr(discovery_promote_queue, "_openai_client", _fake_client('{"first_name":null}'))

    result = discovery_promote_queue.infer_outreach_first_name_for_lead(
        {
            "channel_title": "Etsy Consultant",
            "creator_name": "Etsy Consultant",
            "channel_description": "Business consulting for Etsy sellers.",
            "creator_email": "enquiries@example.com",
        }
    )

    assert result == discovery_promote_queue.NO_GREETING_NAME_MARKER
