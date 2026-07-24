from __future__ import annotations

import io
import sys
from types import ModuleType, SimpleNamespace
from uuid import uuid4


def _ensure_module(name: str) -> ModuleType:
    module = sys.modules.get(name)
    if isinstance(module, ModuleType):
        return module
    module = ModuleType(name)
    sys.modules[name] = module
    return module


dotenv_module = _ensure_module("dotenv")
dotenv_module.load_dotenv = lambda *args, **kwargs: None

google_auth_oauthlib_module = _ensure_module("google_auth_oauthlib")
google_auth_oauthlib_flow_module = _ensure_module("google_auth_oauthlib.flow")
google_auth_oauthlib_flow_module.Flow = type("Flow", (), {})
google_auth_oauthlib_module.flow = google_auth_oauthlib_flow_module

google_oauth2_module = _ensure_module("google.oauth2")
google_oauth2_id_token_module = _ensure_module("google.oauth2.id_token")
google_oauth2_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {}
google_oauth2_credentials_module = _ensure_module("google.oauth2.credentials")
google_oauth2_credentials_module.Credentials = type("Credentials", (), {})
google_oauth2_module.id_token = google_oauth2_id_token_module
google_oauth2_module.credentials = google_oauth2_credentials_module

google_auth_module = _ensure_module("google.auth")
google_auth_exceptions_module = _ensure_module("google.auth.exceptions")
google_auth_exceptions_module.RefreshError = type("RefreshError", (Exception,), {})
google_auth_transport_module = _ensure_module("google.auth.transport")
google_auth_transport_requests_module = _ensure_module("google.auth.transport.requests")
google_auth_transport_requests_module.Request = type("Request", (), {})
google_auth_module.exceptions = google_auth_exceptions_module
google_auth_module.transport = google_auth_transport_module
google_auth_transport_module.requests = google_auth_transport_requests_module

googleapiclient_module = _ensure_module("googleapiclient")
googleapiclient_discovery_module = _ensure_module("googleapiclient.discovery")
googleapiclient_discovery_module.build = lambda *args, **kwargs: None
googleapiclient_http_module = _ensure_module("googleapiclient.http")
googleapiclient_http_module.MediaFileUpload = type("MediaFileUpload", (), {})
googleapiclient_module.discovery = googleapiclient_discovery_module
googleapiclient_module.http = googleapiclient_http_module

stripe_module = _ensure_module("stripe")
stripe_module.Customer = type("Customer", (), {"create": staticmethod(lambda *args, **kwargs: None)})
stripe_module.Subscription = type(
    "Subscription",
    (),
    {
        "retrieve": staticmethod(lambda *args, **kwargs: None),
        "modify": staticmethod(lambda *args, **kwargs: None),
    },
)
stripe_module.checkout = SimpleNamespace(
    Session=type(
        "Session",
        (),
        {
            "create": staticmethod(lambda *args, **kwargs: None),
            "retrieve": staticmethod(lambda *args, **kwargs: None),
        },
    )
)
stripe_module.billing_portal = SimpleNamespace(
    Session=type("Session", (), {"create": staticmethod(lambda *args, **kwargs: None)})
)

requests_module = _ensure_module("requests")
requests_module.Response = type("Response", (), {})
requests_module.get = lambda *args, **kwargs: None
requests_module.post = lambda *args, **kwargs: None
requests_module.delete = lambda *args, **kwargs: None
requests_module.request = lambda *args, **kwargs: None

boto3_module = _ensure_module("boto3")
boto3_module.client = lambda *args, **kwargs: None
boto3_module.resource = lambda *args, **kwargs: None

botocore_module = _ensure_module("botocore")
botocore_exceptions_module = _ensure_module("botocore.exceptions")
botocore_exceptions_module.ClientError = type("ClientError", (Exception,), {})
botocore_module.exceptions = botocore_exceptions_module


from app import create_app
from app.video_shorts.routes import auth as auth_routes
from app.video_shorts.routes import quick_short as quick_short_routes
from app.video_shorts.services import db as db_service
from app.video_shorts.services import usage_metering
from app.video_shorts.services.quick_short_flow import create_session, ensure_quick_short_schema


def _configure_duckdb(monkeypatch, tmp_path, filename: str) -> None:
    db_path = tmp_path / filename
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB_BACKEND", "duckdb")
    monkeypatch.setattr(db_service, "VIDEO_SHORTS_DB", str(db_path))


def _insert_user(user_id: str, *, email: str, plan_id: str = "plan_free", google_sub: str | None = None) -> None:
    conn = db_service.get_db()
    try:
        usage_metering.ensure_usage_metering_schema(conn)
        ensure_quick_short_schema(conn)
        conn.execute(
            """
            INSERT INTO shorts_users (id, name, email, username, role, plan_id, google_sub, email_verified)
            VALUES (?, ?, ?, ?, 'member', ?, ?, TRUE)
            """,
            [user_id, "Test User", email, email, plan_id, google_sub],
        )
        conn.commit()
    finally:
        conn.close()


def test_register_page_and_post_are_blocked_when_signups_disabled(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "signup_blocked.duckdb")
    monkeypatch.setenv("SIGNUPS_ENABLED", "false")

    app = create_app()
    app.secret_key = "test-secret"
    client = app.test_client()

    response = client.get("/video_shorts/register")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "We&#39;re at capacity right now" in page
    assert "Create your account" in page
    assert "<form method=\"post\">" not in page

    post_response = client.post(
        "/video_shorts/register",
        data={
            "email": "new@example.com",
            "password": "102030Aa@@",
            "password_confirm": "102030Aa@@",
        },
    )
    assert post_response.status_code == 403
    post_page = post_response.get_data(as_text=True)
    assert "We&#39;re at capacity right now" in post_page


def test_existing_google_user_can_sign_in_when_signups_disabled(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "google_existing_user.duckdb")
    monkeypatch.setenv("SIGNUPS_ENABLED", "no")
    monkeypatch.setattr(auth_routes, "GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setattr(auth_routes, "GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")

    user_id = str(uuid4())
    email = "existing@example.com"
    _insert_user(user_id, email=email, plan_id="plan_free")

    class _FakeFlow:
        def __init__(self):
            self.code_verifier = "verifier"
            self.credentials = SimpleNamespace(id_token="token")

        def fetch_token(self, authorization_response):
            return None

    monkeypatch.setattr(auth_routes, "_build_google_flow", lambda state=None: _FakeFlow())
    monkeypatch.setattr(
        auth_routes.id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {"sub": "google-sub-1", "email": email, "name": "Existing User"},
    )

    app = create_app()
    app.secret_key = "test-secret"
    client = app.test_client()
    with client.session_transaction() as session:
        session["google_oauth_state"] = "state-123"
        session["google_oauth_code_verifier"] = "verifier"
        session["google_login_next"] = "/video_shorts/channels"

    response = client.get("/video_shorts/login/google/callback?state=state-123&code=test-code")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/video_shorts/channels")
    with client.session_transaction() as session:
        assert session["vs_user_id"] == user_id

    conn = db_service.get_db()
    try:
        row = conn.execute("SELECT google_sub FROM shorts_users WHERE id = ?", [user_id]).fetchone()
    finally:
        conn.close()
    assert row[0] == "google-sub-1"


def test_direct_upload_rejects_oversized_file_before_temp_write(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "upload_size_limit.duckdb")
    user_id = str(uuid4())
    _insert_user(user_id, email="upload@example.com", plan_id="plan_free")

    app = create_app()
    app.secret_key = "test-secret"
    client = app.test_client()
    with client.session_transaction() as session:
        session["vs_user_id"] = user_id

    monkeypatch.setattr(quick_short_routes, "disk_guard_triggered", lambda *args, **kwargs: False)
    monkeypatch.setattr(quick_short_routes, "_declared_upload_size_bytes", lambda upload_file: 3 * 1024 ** 3)
    monkeypatch.setattr(quick_short_routes.tempfile, "NamedTemporaryFile", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("temp file should not be created")))

    response = client.post(
        "/video_shorts/shorts/quick/api/upload/direct",
        data={
            "upload_kind": "video",
            "file": (io.BytesIO(b"x"), "oversized.mp4"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    payload = response.get_json()
    assert "Your Free plan allows up to 2 GB per video." in payload["message"]


def test_upload_complete_rejects_over_duration_before_enqueue_and_deletes_object(monkeypatch, tmp_path):
    _configure_duckdb(monkeypatch, tmp_path, "upload_duration_limit.duckdb")
    user_id = str(uuid4())
    _insert_user(user_id, email="duration@example.com", plan_id="plan_free")

    app = create_app()
    app.secret_key = "test-secret"
    client = app.test_client()
    with client.session_transaction() as session:
        session["vs_user_id"] = user_id

    monkeypatch.setattr(quick_short_routes, "disk_guard_triggered", lambda *args, **kwargs: False)

    session_row = create_session(
        user_id=user_id,
        brand_id=None,
        source_type="upload",
        upload_kind="video",
        source_filename="long-video.mp4",
        payload={
            "upload": {
                "video_id": "local_duration_test",
                "source_key": "videos/local_duration_test.mp4",
                "filename": "long-video.mp4",
                "size_bytes": 1024,
                "content_type": "video/mp4",
            }
        },
    )

    deleted_keys: list[str] = []
    enqueued: list[dict] = []

    class _FakeStorage:
        backend_name = "s3"

        def public_url(self, key: str) -> str:
            return f"https://example.test/{key}"

        def delete(self, key: str) -> None:
            deleted_keys.append(key)

        def download_to_temp(self, key: str):
            raise AssertionError("should not download source to EC2 for duration rejection")

    monkeypatch.setattr(quick_short_routes, "get_media_storage", lambda *args, **kwargs: _FakeStorage())
    monkeypatch.setattr(quick_short_routes, "_probe_duration_seconds", lambda *args, **kwargs: 7200)
    monkeypatch.setattr(
        quick_short_routes,
        "enqueue_job",
        lambda *args, **kwargs: enqueued.append({"args": args, "kwargs": kwargs}) or {"job": {"id": "job-1"}},
    )

    response = client.post(
        "/video_shorts/shorts/quick/api/upload/complete",
        json={"session_id": session_row["id"]},
    )
    assert response.status_code == 413
    payload = response.get_json()
    assert "This video is 2h. Your Free plan allows up to 60 minutes per video." == payload["message"]
    assert deleted_keys == ["videos/local_duration_test.mp4"]
    assert enqueued == []
