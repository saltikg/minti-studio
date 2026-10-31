from app.video_shorts.services.email_sequence import (
    process_due_signup_nurture_email,
    render_sequence_email,
    render_sequence_placeholders,
    rewrite_nurture_links,
    sequence_email_headers,
)
from app.video_shorts.services import outreach_email_send


def test_sequence_placeholders_and_footer():
    rendered = render_sequence_email(
        subject="Hi {first_name}",
        body_text="Welcome, {name}.",
        recipient_name="Alex Morgan",
        unsubscribe_link="https://mintistudio.com/email/unsubscribe/token",
        postal_address="123 Main St, San Francisco, CA",
    )

    assert rendered["subject"] == "Hi Alex"
    assert "Welcome, Alex Morgan." in rendered["body_text"]
    assert "Unsubscribe: https://mintistudio.com/email/unsubscribe/token" in rendered["body_text"]
    assert rendered["body_text"].endswith("123 Main St, San Francisco, CA")
    assert render_sequence_placeholders("Hi {name} / {first_name}") == "Hi there / there"


def test_sequence_headers_include_one_click():
    headers = sequence_email_headers(unsubscribe_link="https://mintistudio.com/email/unsubscribe/token")

    assert headers["List-Unsubscribe"] == (
        "<https://mintistudio.com/email/unsubscribe/token>, "
        "<mailto:info@mintistudio.com?subject=unsubscribe>"
    )
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_nurture_link_rewrite_excludes_unsubscribe():
    unsubscribe = "https://mintistudio.com/email/unsubscribe/abc"
    body, links = rewrite_nurture_links(
        f"Read https://example.com/post.\n\nUnsubscribe: {unsubscribe}",
        send_token="send-token",
        unsubscribe_link=unsubscribe,
    )

    assert links == ["https://example.com/post"]
    assert "https://mintistudio.com/e/c/send-token/1." in body
    assert unsubscribe in body


def test_nurture_flag_off_returns_without_database(monkeypatch):
    monkeypatch.delenv("NURTURE_ENABLED", raising=False)

    assert process_due_signup_nurture_email() is False


def test_shared_zoho_sender_plain_text_and_outreach_html(monkeypatch):
    sent_messages = []

    class FakeSmtp:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            return None

        def send_message(self, message, **_kwargs):
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
        text="Body",
        extra_headers={"List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    )
    outreach_email_send.send_zoho_outreach_email(
        to_email="lead@example.com",
        subject="Outreach",
        text="Text body",
        html="<p>HTML body</p>",
    )

    assert sent_messages[0].is_multipart() is False
    assert sent_messages[0]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert sent_messages[1].is_multipart() is True
    assert sent_messages[1].get_body(preferencelist=("html",)).get_content().strip() == "<p>HTML body</p>"
