from app.video_shorts import config
from app.video_shorts.services.export_resolution import resolve_export_resolution


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _ExportResolutionConn:
    def __init__(self, *, plan_id="plan_free", service_mode="self", is_guest=False, override=None):
        self.account = (plan_id, service_mode, is_guest)
        self.override = override

    def execute(self, sql, params):
        if "FROM shorts_users" in sql:
            return _Result(self.account)
        if "FROM shorts_user_preferences" in sql:
            return _Result((self.override,)) if self.override is not None else _Result(None)
        raise AssertionError(f"Unexpected query: {sql}")


def test_export_resolution_account_rules(monkeypatch):
    monkeypatch.setattr(config, "EXPORT_1080_FOR_PAID", False)

    free = resolve_export_resolution("free-user", conn=_ExportResolutionConn())
    assert free.resolution == "720"
    assert free.eligible is False

    overridden = resolve_export_resolution(
        "free-user",
        conn=_ExportResolutionConn(override="1080"),
    )
    assert overridden.resolution == "1080"
    assert overridden.override == "1080"

    paid_conn = _ExportResolutionConn(plan_id="plan_10gb")
    paid_flag_off = resolve_export_resolution("paid-user", conn=paid_conn)
    assert paid_flag_off.resolution == "720"
    assert paid_flag_off.eligible is True

    monkeypatch.setattr(config, "EXPORT_1080_FOR_PAID", True)
    paid_flag_on = resolve_export_resolution("paid-user", conn=paid_conn)
    assert paid_flag_on.resolution == "1080"

    guest = resolve_export_resolution(
        "guest-user",
        conn=_ExportResolutionConn(plan_id="plan_10gb", is_guest=True, override="1080"),
    )
    assert guest.resolution == "720"
    assert guest.override is None
    assert guest.is_guest is True
