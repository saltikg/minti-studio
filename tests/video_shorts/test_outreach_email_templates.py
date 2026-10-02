from app.video_shorts.services.outreach_email_templates import (
    LONGFORM_NO_SHORTS_SIGNAL_EN,
    render_bucket_followup_outreach_email,
    render_outreach_email,
    should_include_longform_no_shorts_signal,
)


def test_longform_no_shorts_signal_condition_is_conservative():
    assert should_include_longform_no_shorts_signal(language="EN", longform_last_60d=2, shorts_last_15d=0)
    assert not should_include_longform_no_shorts_signal(language="EN", longform_last_60d=1, shorts_last_15d=0)
    assert not should_include_longform_no_shorts_signal(language="EN", longform_last_60d=2, shorts_last_15d=1)
    assert not should_include_longform_no_shorts_signal(language="EN", longform_last_60d=None, shorts_last_15d=0)
    assert not should_include_longform_no_shorts_signal(language="TR", longform_last_60d=2, shorts_last_15d=0)


def test_first_en_includes_signal_without_counts_when_condition_matches():
    rendered = render_outreach_email(
        stage="first",
        language="EN",
        recipient_name="Alex",
        share_url="https://mintistudio.com/w/test",
        trial_days=30,
        video_title="A useful webinar",
        longform_last_60d=4,
        shorts_last_15d=0,
    )

    assert LONGFORM_NO_SHORTS_SIGNAL_EN in rendered["text"]
    assert "4" not in LONGFORM_NO_SHORTS_SIGNAL_EN
    assert "0" not in LONGFORM_NO_SHORTS_SIGNAL_EN
    assert "[signal]" not in rendered["text"]


def test_first_en_omits_signal_when_data_missing_or_shorts_present():
    rendered = render_outreach_email(
        stage="first",
        language="EN",
        recipient_name="Alex",
        share_url="https://mintistudio.com/w/test",
        trial_days=30,
        video_title="A useful webinar",
        longform_last_60d=4,
        shorts_last_15d=2,
    )
    missing_data = render_outreach_email(
        stage="first",
        language="EN",
        recipient_name="Alex",
        share_url="https://mintistudio.com/w/test",
        trial_days=30,
        video_title="A useful webinar",
    )

    assert LONGFORM_NO_SHORTS_SIGNAL_EN not in rendered["text"]
    assert LONGFORM_NO_SHORTS_SIGNAL_EN not in missing_data["text"]
    assert "[signal]" not in rendered["text"]
    assert "\n\n\n" not in rendered["text"]
    assert "\n\n\n" not in missing_data["text"]


def test_bucket_followup_en_can_include_signal():
    rendered = render_bucket_followup_outreach_email(
        bucket="watched_no_convert",
        sequence_number=2,
        language="EN",
        recipient_name="Alex",
        share_url="https://mintistudio.com/w/test",
        trial_days=30,
        video_title="A useful webinar",
        longform_last_60d=3,
        shorts_last_15d=0,
    )

    assert rendered["key"] == "WATCHED_NO_CONVERT_EN"
    assert LONGFORM_NO_SHORTS_SIGNAL_EN in rendered["text"]


def test_tr_templates_do_not_include_signal():
    rendered = render_outreach_email(
        stage="first",
        language="TR",
        recipient_name="Ali",
        share_url="https://mintistudio.com/w/test",
        trial_days=30,
        video_title="Bir video",
        longform_last_60d=5,
        shorts_last_15d=0,
    )

    assert LONGFORM_NO_SHORTS_SIGNAL_EN not in rendered["text"]
    assert "[signal]" not in rendered["text"]
