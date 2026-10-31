from email import policy
from email.parser import BytesParser

import duckdb

from app.video_shorts.services.email_sequence import (
    ensure_email_sequence_schema,
    load_signup_sequence_steps,
    process_due_signup_nurture_email,
    render_sequence_email,
    render_sequence_placeholders,
    rewrite_nurture_links,
)
from app.video_shorts.services import outreach_email_send


def test_bilingual_sequence_schema_seeds_synced_languages():
    conn = duckdb.connect(":memory:")
    ensure_email_sequence_schema(conn)
    ensure_email_sequence_schema(conn)

    steps = load_signup_sequence_steps(conn)
    assert len(steps) == 14
    assert {step["language"] for step in steps} == {"en", "tr"}
    for step_number in range(1, 8):
        versions = [step for step in steps if step["step"] == step_number]
        assert len(versions) == 2
        assert len({version["delay_days"] for version in versions}) == 1
        assert all(version["active"] is False for version in versions)


def test_sequence_placeholders_without_footer():
    rendered = render_sequence_email(
        subject="Hi {first_name}",
        body_text="Welcome, {name}.\nFrom time to time\n",
        recipient_name="Alex Morgan",
    )

    assert rendered["subject"] == "Hi Alex"
    assert rendered["body_text"] == "Welcome, Alex Morgan.\nFrom time to time\n"
    assert "Unsubscribe" not in rendered["body_text"]
    assert render_sequence_placeholders("Hi {name} / {first_name}") == "Hi there / there"


def test_nurture_link_rewrite_remains_enabled():
    body, links = rewrite_nurture_links(
        "Read https://example.com/post.",
        send_token="send-token",
        unsubscribe_link="",
    )

    assert links == ["https://example.com/post"]
    assert "https://mintistudio.com/e/c/send-token/1." in body


def test_nurture_flag_off_returns_without_database(monkeypatch):
    monkeypatch.delenv("NURTURE_ENABLED", raising=False)

    assert process_due_signup_nurture_email() is False


def test_shared_zoho_sender_plain_text_and_outreach_html(monkeypatch):
    sent_messages: list[bytes] = []

    class FakeSmtp:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            return None

        def sendmail(self, _from_addr, _to_addrs, message, *_args, **_kwargs):
            sent_messages.append(message)
            return {}

    monkeypatch.setattr(
        outreach_email_send,
        "_zoho_smtp_settings",
        lambda: {"host": "smtp.example", "port": 465, "user": "info@mintistudio.com", "password": "secret"},
    )
    monkeypatch.setattr(outreach_email_send.smtplib, "SMTP_SSL", FakeSmtp)

    outreach_email_send.send_zoho_email(
        to_email="admin@example.com",
        subject="Plain",
        text="Body\nFrom time to time\nDone",
    )
    outreach_email_send.send_zoho_outreach_email(
        to_email="lead@example.com",
        subject="Outreach",
        text="Text body",
        html="<p>HTML body</p>",
    )

    plain_message = BytesParser(policy=policy.default).parsebytes(sent_messages[0])
    outreach_message = BytesParser(policy=policy.default).parsebytes(sent_messages[1])
    assert plain_message.is_multipart() is False
    assert plain_message.get_content_type() == "text/plain"
    assert plain_message["List-Unsubscribe"] is None
    assert plain_message["List-Unsubscribe-Post"] is None
    assert "\nFrom time to time\n" in plain_message.get_content().replace("\r\n", "\n")
    assert b"\r\n>From time to time\r\n" not in sent_messages[0]
    assert b"\r\nFrom time to time\r\n" in sent_messages[0]
    assert outreach_message.is_multipart() is True
    assert outreach_message.get_body(preferencelist=("html",)).get_content().strip() == "<p>HTML body</p>"
