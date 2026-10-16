from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import duckdb
import pytest
from cryptography.fernet import Fernet
from flask import Flask, g, session
from googleapiclient.errors import HttpError
import httplib2

from app.video_shorts.services import youtube_token_store as store
from app.video_shorts.services import youtube_oauth as oauth
from app.video_shorts.routes import generation, auth


@pytest.fixture
def token_db(monkeypatch, tmp_path):
    path = str(tmp_path / "tokens.duckdb")
    with duckdb.connect(path) as conn:
        conn.execute(store.TOKEN_TABLE_SQL)
        conn.execute(store.LEGACY_TOKEN_TABLE_SQL)
        conn.execute("CREATE TABLE shorts_users(id VARCHAR, role VARCHAR)")
    monkeypatch.setenv("YOUTUBE_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(store, "get_db", lambda: duckdb.connect(path))
    monkeypatch.setattr(store, "get_db_readonly", lambda: duckdb.connect(path, read_only=True))
    monkeypatch.setattr(store, "current_brand_id", lambda: None)
    return path


def test_encrypted_roundtrip_all_read_paths_and_exact_scope_storage(token_db):
    store.store_refresh_token("fake-refresh-secret", ["actual-grant"], user_id="owner", brand_id="brand")
    with duckdb.connect(token_db) as conn:
        value, scopes = conn.execute("SELECT refresh_token, scopes FROM youtube_oauth_tokens_v2").fetchone()
    assert value.startswith("enc:v1:") and "fake-refresh-secret" not in value
    assert scopes == "actual-grant"
    assert store.get_stored_refresh_token("owner", "brand") == "fake-refresh-secret"
    assert store.get_stored_refresh_token() == "fake-refresh-secret"  # default-row path
    assert store.list_stored_refresh_tokens()[0]["refresh_token"] == "fake-refresh-secret"
    store.store_refresh_token("replacement", [], user_id="owner", brand_id="brand")
    with duckdb.connect(token_db) as conn:
        assert conn.execute("SELECT scopes FROM youtube_oauth_tokens_v2").fetchone()[0] == ""


def test_legacy_write_read_and_reauth_revocation(token_db):
    store.store_refresh_token("legacy-secret", ["grant"])
    assert store.get_stored_refresh_token() == "legacy-secret"
    with duckdb.connect(token_db) as conn:
        assert conn.execute("SELECT refresh_token FROM youtube_oauth_tokens").fetchone()[0].startswith("enc:v1:")
    store.store_refresh_token("owner-secret", ["grant"], user_id="owner", brand_id="brand")
    store.mark_refresh_token_invalid("owner", "brand")
    assert store.get_stored_refresh_token("owner", "brand") is None
    assert store.get_refresh_token_for_revocation("owner", "brand") == "owner-secret"


@pytest.mark.parametrize("key", [None, "bad-key"])
def test_missing_or_invalid_key_never_saves_plaintext(token_db, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("YOUTUBE_TOKEN_ENC_KEY")
    else:
        monkeypatch.setenv("YOUTUBE_TOKEN_ENC_KEY", key)
    with pytest.raises(store.YouTubeTokenStoreError, match="YOUTUBE_TOKEN_ENC_KEY"):
        store.store_refresh_token("do-not-leak", user_id="owner")
    with duckdb.connect(token_db) as conn:
        assert conn.execute("SELECT count(*) FROM youtube_oauth_tokens_v2").fetchone()[0] == 0


def test_migration_both_tables_idempotent_and_plaintext_warning(token_db, caplog):
    store.store_refresh_token("already-encrypted", ["grant"], user_id="existing")
    with duckdb.connect(token_db) as conn:
        conn.execute("INSERT INTO youtube_oauth_tokens_v2 VALUES ('plain', 'legacy-secret', 'old-grant', 'old-date', 1)")
        conn.execute("INSERT INTO youtube_oauth_tokens VALUES (1, 'legacy-table-secret', 'legacy-grant', 'old-date', 0)")
        encrypted_before = conn.execute("SELECT refresh_token FROM youtube_oauth_tokens_v2 WHERE user_id='existing'").fetchone()[0]
    assert store.get_refresh_token_for_revocation("plain") == "legacy-secret"
    assert "plaintext" in caplog.text and "legacy-secret" not in caplog.text
    assert store.migrate_plaintext_refresh_tokens() == {store.TOKEN_TABLE: 1, store.LEGACY_TOKEN_TABLE: 1}
    assert store.migrate_plaintext_refresh_tokens() == {store.TOKEN_TABLE: 0, store.LEGACY_TOKEN_TABLE: 0}
    with duckdb.connect(token_db) as conn:
        for table in (store.TOKEN_TABLE, store.LEGACY_TOKEN_TABLE):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE refresh_token NOT LIKE 'enc:v1:%'").fetchone()[0] == 0
        assert conn.execute("SELECT refresh_token FROM youtube_oauth_tokens_v2 WHERE user_id='existing'").fetchone()[0] == encrypted_before
        assert conn.execute("SELECT scopes, updated_at, reauth_required FROM youtube_oauth_tokens_v2 WHERE user_id='plain'").fetchone() == ('old-grant', 'old-date', 1)
    assert store.get_refresh_token_for_revocation("plain") == "legacy-secret"


def test_migration_rolls_back_both_tables_on_failure(token_db):
    with duckdb.connect(token_db) as conn:
        conn.execute("INSERT INTO youtube_oauth_tokens_v2 VALUES ('owner', 'plain-secret', '', '', 0)")
        conn.execute("INSERT INTO youtube_oauth_tokens VALUES (1, 'enc:v2:unsupported', '', '', 0)")
    with pytest.raises(store.YouTubeTokenStoreError, match="rolled back"):
        store.migrate_plaintext_refresh_tokens()
    with duckdb.connect(token_db) as conn:
        assert conn.execute("SELECT refresh_token FROM youtube_oauth_tokens_v2").fetchone()[0] == 'plain-secret'


@pytest.mark.parametrize("value", ["enc:v1:corrupted-secret", "enc:v2:secret"])
def test_encrypted_errors_do_not_fall_back_to_plaintext(token_db, value):
    with pytest.raises(store.YouTubeTokenStoreError) as exc:
        store._decode_refresh_token(value)
    assert value not in str(exc.value)


def test_wrong_key_fails_closed(token_db, monkeypatch):
    store.store_refresh_token("secret", user_id="owner")
    monkeypatch.setenv("YOUTUBE_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(store.YouTubeTokenStoreError, match="decrypt"):
        store.get_stored_refresh_token("owner")


@pytest.mark.parametrize("outcome", [200, 400, 503, "timeout"])
def test_disconnect_revokes_then_deletes_only_target_even_on_failure(token_db, monkeypatch, caplog, outcome):
    store.store_refresh_token("disconnect-secret", user_id="owner", brand_id="brand")
    store.store_refresh_token("keep-secret", user_id="owner", brand_id="other")
    store.mark_refresh_token_invalid("owner", "brand")
    calls = []
    def revoke(url, **kwargs):
        calls.append(url)
        assert store.get_refresh_token_for_revocation("owner", "brand") == "disconnect-secret"
        assert kwargs["data"] == {"token": "disconnect-secret"}
        assert kwargs["allow_redirects"] is False
        if outcome == "timeout":
            raise oauth.requests.Timeout("disconnect-secret")
        return SimpleNamespace(status_code=outcome)
    monkeypatch.setattr(oauth.requests, "post", revoke)
    assert oauth.disconnect_youtube("owner", "brand") is (outcome == 200)
    assert calls == ["https://oauth2.googleapis.com/revoke"]
    assert store.get_refresh_token_for_revocation("owner", "brand") is None
    assert store.get_stored_refresh_token("owner", "other") == "keep-secret"
    assert "disconnect-secret" not in caplog.text


@pytest.mark.parametrize("granted,returned,expected", [
    (["actual"], "other", ["actual"]),
    (None, "b a b", ["a", "b"]),
    (None, ["b", "a"], ["a", "b"]),
    (None, None, []),
    ([], "must-not-fallback", []),
])
def test_granted_scope_extraction(granted, returned, expected):
    flow = SimpleNamespace(credentials=SimpleNamespace(granted_scopes=granted), oauth2session=SimpleNamespace(token={"scope": returned}))
    assert oauth.granted_scopes_from_flow(flow) == expected


@pytest.mark.parametrize("module", [generation, auth])
def test_both_oauth_callbacks_persist_actual_grants(monkeypatch, module):
    app = Flask(__name__)
    app.secret_key = "test"
    for endpoint in ["social_connect", "channels_page", "login"]:
        app.add_url_rule('/' + endpoint, 'video_shorts_bp.' + endpoint, lambda: '')
    flow = SimpleNamespace(
        credentials=SimpleNamespace(refresh_token="callback-secret", granted_scopes=None),
        oauth2session=SimpleNamespace(token={"scope": "returned-only"}),
        fetch_token=lambda **kwargs: None,
    )
    saved = []
    monkeypatch.setattr(module, "build_oauth_flow", lambda **kwargs: flow)
    monkeypatch.setattr(module, "store_refresh_token", lambda token, **kwargs: saved.append((token, kwargs)))
    monkeypatch.setattr(module, "track_event", lambda *args, **kwargs: None)
    with app.test_request_context('/callback?state=state'):
        g.vs_current_user = {"id": "owner"}
        session['yt_oauth_state'] = {"nonce": "state", "user_id": "owner", "code_verifier": "verifier"}
        response = module.youtube_oauth_callback() if module is generation else module.google_oauth_callback()
    assert response.status_code == 302
    assert saved == [("callback-secret", {"scopes": ["returned-only"], "user_id": "owner"})]


class VideoAPI:
    def __init__(self, states, delete_error=None):
        self.states = iter(states)
        self.delete_error = delete_error
        self.calls = []
    def videos(self):
        return self
    def list(self, **kwargs):
        self.calls.append('list')
        state = next(self.states)
        def execute():
            if isinstance(state, Exception):
                raise state
            return {'items': [] if state is None else [{'id': 'video', 'status': state}]}
        return SimpleNamespace(execute=execute)
    def delete(self, **kwargs):
        self.calls.append('delete')
        def execute():
            if self.delete_error:
                raise self.delete_error
            return {}
        return SimpleNamespace(execute=execute)
    def update(self, **kwargs):
        self.calls.append('update')
        assert kwargs['body']['status'] == {'privacyStatus': 'private'}
        return SimpleNamespace(execute=lambda: {})


def future_status():
    return {'privacyStatus': 'private', 'publishAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}


@pytest.mark.parametrize('state', [
    {'privacyStatus': 'public'}, {'privacyStatus': 'unlisted'}, None,
    {'privacyStatus': 'private'}, {'privacyStatus': 'private', 'publishAt': 'invalid'},
    {'privacyStatus': 'private', 'publishAt': '2000-01-01T00:00:00Z'}, RuntimeError('read failed'),
])
def test_unsafe_unschedule_never_mutates_youtube(monkeypatch, state):
    api = VideoAPI([state])
    monkeypatch.setattr(oauth, 'build_authenticated_youtube', lambda *args, **kwargs: api)
    monkeypatch.setattr(oauth, 'record_youtube_data_api_call', lambda **kwargs: None)
    with Flask(__name__).app_context():
        result = oauth.unschedule_youtube_video_if_safe(video_id='video', expected_video_ids=['video'], scheduled_publish_at=future_status()['publishAt'], title='Title', description='Description')
    assert not result['unscheduled']
    assert api.calls == []  # Compatibility helper is inert for all uploaded clips.


def test_unschedule_compatibility_helper_never_calls_youtube(monkeypatch):
    monkeypatch.setattr(oauth, "build_authenticated_youtube", lambda *a, **k: pytest.fail("YouTube called"))
    result = oauth.unschedule_youtube_video_if_safe(video_id="video",title="Title",description="Description")
    assert result == {"unscheduled": False, "deleted": False, "blocked": True, "reason": "uploaded_locked"}


def test_key_rotation_both_tables_new_key_only_and_idempotent(token_db, monkeypatch, caplog):
    import os
    old = os.environ["YOUTUBE_TOKEN_ENC_KEY"]
    store.store_refresh_token("v2-token", ["actual-grant"], user_id="owner", brand_id="brand")
    store.store_refresh_token("legacy-token", ["legacy-grant"])
    new = Fernet.generate_key().decode()
    monkeypatch.setenv("YOUTUBE_TOKEN_ENC_PREVIOUS_KEY", old)
    monkeypatch.setenv("YOUTUBE_TOKEN_ENC_KEY", new)
    assert store.get_stored_refresh_token("owner", "brand") == "v2-token"
    assert store.rotate_refresh_token_encryption() == {store.TOKEN_TABLE: 1, store.LEGACY_TOKEN_TABLE: 1}
    assert store.rotate_refresh_token_encryption() == {store.TOKEN_TABLE: 0, store.LEGACY_TOKEN_TABLE: 0}
    monkeypatch.delenv("YOUTUBE_TOKEN_ENC_PREVIOUS_KEY")
    assert store.get_stored_refresh_token("owner", "brand") == "v2-token"
    with duckdb.connect(token_db) as conn:
        for table in [store.TOKEN_TABLE,store.LEGACY_TOKEN_TABLE]:
            for (stored,) in conn.execute(f"SELECT refresh_token FROM {table}").fetchall():
                assert Fernet(new.encode()).decrypt(stored[7:].encode())
    assert new not in caplog.text and old not in caplog.text


def test_refresh_provider_error_does_not_leak_token(monkeypatch, caplog):
    from google.auth.exceptions import RefreshError
    monkeypatch.setattr(oauth, 'get_stored_refresh_token', lambda **kwargs: 'refresh-secret')
    class Credentials:
        def __init__(self, **kwargs):
            pass
        def refresh(self, request):
            raise RefreshError('provider echoed refresh-secret')
    monkeypatch.setattr(oauth, 'Credentials', Credentials)
    with pytest.raises(RefreshError) as exc:
        oauth.get_access_token(user_id='owner')
    assert 'refresh-secret' not in str(exc.value) + caplog.text


def test_disconnect_with_unreadable_token_still_deletes_locally(token_db, monkeypatch):
    store.store_refresh_token('secret', user_id='owner', brand_id='brand')
    monkeypatch.setenv('YOUTUBE_TOKEN_ENC_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr(oauth.requests, 'post', lambda *args, **kwargs: pytest.fail('Unreadable token must not be sent'))
    assert oauth.disconnect_youtube('owner', 'brand') is False
    assert store.get_refresh_token_for_revocation('owner', 'brand') is None


def test_comment_delete_failure_does_not_call_nonexistent_thread_api(monkeypatch):
    from app.video_shorts.routes import videos
    app = Flask(__name__)
    api = SimpleNamespace(comments=lambda: SimpleNamespace(delete=lambda **kwargs: SimpleNamespace(execute=lambda: (_ for _ in ()).throw(RuntimeError('processingFailure')))))
    monkeypatch.setattr(videos, '_ensure_comment_owner_access', lambda *args: (None, None))
    monkeypatch.setattr(videos, '_require_youtube_client', lambda: api)
    monkeypatch.setattr(videos, 'delete_comment_record', lambda *args: pytest.fail('Remote failure must preserve local record'))
    with app.test_request_context('/comment', method='DELETE', json={'thread_id': 'thread'}):
        response, status = videos.shorts_comment_delete('comment')
    assert status == 500 and response.json['message'] == 'processingFailure'
