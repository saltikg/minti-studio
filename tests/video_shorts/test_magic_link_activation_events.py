from app.video_shorts.routes.auth import _magic_link_events_to_record


def test_existing_provisioned_user_records_activation_not_signup():
    assert _magic_link_events_to_record(
        created_new_user=False,
        existing_event_names=set(),
    ) == ["magic_link_activated"]


def test_new_magic_link_user_records_signup_and_activation():
    assert _magic_link_events_to_record(
        created_new_user=True,
        existing_event_names=set(),
    ) == ["signup", "magic_link_activated"]


def test_magic_link_events_are_not_recorded_twice():
    assert _magic_link_events_to_record(
        created_new_user=True,
        existing_event_names={"signup", "magic_link_activated"},
    ) == []
