from datetime import timedelta
import json
from pathlib import Path

import duckdb
import httplib2
import pytest
from flask import Flask
from googleapiclient.errors import HttpError

from app.video_shorts.services import youtube_scheduling as scheduling
from app.video_shorts.services import youtube_oauth as oauth
from app.video_shorts.services import youtube_token_store

REAL_SCHEDULER_HEALTH_CHECK = scheduling.scheduler_health_check


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = str(tmp_path / 'schedules.duckdb')
    with duckdb.connect(path) as conn:
        conn.execute(scheduling.SCHEMA_SQL)
        conn.execute(scheduling.MUTATION_LEASE_SCHEMA_SQL)
        conn.execute(scheduling.HISTORY_SCHEMA_SQL)
        conn.execute('''CREATE TABLE shorts_schedule_alerts (
            alert_key VARCHAR PRIMARY KEY,schedule_id VARCHAR,platform VARCHAR,
            alert_type VARCHAR,message VARCHAR,created_at TIMESTAMP)''')
        conn.execute('''CREATE TABLE shorts_scheduler_state (
            name VARCHAR PRIMARY KEY,last_tick_at TIMESTAMP,last_worker_id VARCHAR,
            last_gap_alerted_at TIMESTAMP,updated_at TIMESTAMP)''')
        conn.execute('''CREATE TABLE shorts_users (
            id VARCHAR PRIMARY KEY,autopilot_billing_paused_at TIMESTAMP,email VARCHAR,role VARCHAR)''')
        conn.execute("INSERT INTO shorts_users VALUES ('owner',NULL,'admin@example.com','admin')")
        conn.execute('''CREATE TABLE shorts_render_jobs (
            id VARCHAR,user_id VARCHAR,type VARCHAR,status VARCHAR,priority INTEGER,
            payload_json TEXT,attempts INTEGER,max_attempts INTEGER)''')
    monkeypatch.setattr(scheduling, 'get_db', lambda: duckdb.connect(path))
    monkeypatch.setattr(scheduling, 'get_db_readonly', lambda: duckdb.connect(path))
    monkeypatch.setattr(scheduling, 'send_pending_notifications', lambda: None)
    monkeypatch.setattr(scheduling, 'scheduler_health_check', lambda **kw: None)
    monkeypatch.setattr(scheduling, '_file_exists', lambda payload: True)
    monkeypatch.setattr(
        scheduling, '_render_freshness',
        lambda candidate, payload: {'ready': True, 'reason': None},
    )
    monkeypatch.setattr(scheduling, '_alert', lambda *a, **kw: False)
    monkeypatch.setattr(scheduling, '_project_upload', lambda *a: None)
    monkeypatch.setattr(scheduling, '_existing_remote_id', lambda **kw: None)
    from app.video_shorts.services import media_access
    monkeypatch.setattr(media_access,'authorize_file',lambda *a,**kw: None)
    monkeypatch.setenv('YOUTUBE_UPLOAD_LEAD_MINUTES', '120')
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **k: pytest.fail('unexpected upload'))
    monkeypatch.setattr(oauth, 'find_exact_title_video', lambda *a, **kw: None)
    monkeypatch.setattr(oauth, 'verify_uploaded_video', lambda *a, **kw: {'ok': True})
    monkeypatch.setattr(
        youtube_token_store,
        'get_youtube_connection',
        lambda user_id, brand_id: {
            'user_id': f'{user_id}::{brand_id}',
            'youtube_channel_id': 'channel-a',
            'channel_title': 'Channel A',
        },
    )
    monkeypatch.setattr(
        oauth,
        'get_connected_channel_info',
        lambda user_id, brand_id: ({'id': 'channel-a', 'title': 'Channel A'}, None),
    )
    return path


def schedule(**kw):
    args = dict(video_path='clip.mp4', title='title', description='description',
                publish_at=scheduling.iso(scheduling.utc()+timedelta(days=1)),
                user_id='owner',brand_id='brand',video_pk=1,plan_index='2')
    args.update(kw)
    return scheduling.schedule_youtube_upload(**args)


def row(db):
    with duckdb.connect(db) as conn:
        cur = conn.execute('SELECT * FROM shorts_youtube_schedules')
        return dict(zip([c[0] for c in cur.description],cur.fetchone()))


def test_far_future_reschedule_cancel_all_local_and_tenant_isolated(db):
    first = schedule()
    assert first['status'] == 'pending' and first['id'] is None
    stored = row(db)
    assert stored['publish_at']-stored['upload_due_at'] == timedelta(hours=2)
    new_time = scheduling.utc()+timedelta(days=2)
    second = schedule(publish_at=scheduling.iso(new_time))
    assert first['schedule_id'] == second['schedule_id']
    assert row(db)['upload_due_at'] == new_time-timedelta(hours=2)
    scheduling.cancel_schedule(user_id='different',brand_id='brand',video_pk=1,plan_index='2')
    assert row(db)['status'] == 'pending'
    scheduling.cancel_schedule(user_id='owner',brand_id='brand',video_pk=1,plan_index='2')
    assert row(db)['status'] == 'cancelled'
    with duckdb.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM shorts_render_jobs').fetchone()[0] == 0


def test_atomic_tick_noop_then_claim_once(db):
    first = schedule()
    assert scheduling.scheduler_tick() is False
    due = row(db)['upload_due_at']
    assert scheduling.scheduler_tick(now=due) is True
    assert scheduling.scheduler_tick(now=due) is False
    assert row(db)['status'] == 'queued'
    with duckdb.connect(db) as conn:
        jobs = conn.execute('SELECT payload_json FROM shorts_render_jobs').fetchall()
    assert len(jobs) == 1 and json.loads(jobs[0][0])['youtube_schedule_id'] == first['schedule_id']
    with pytest.raises(scheduling.YouTubeScheduleLocked):
        schedule()
    with pytest.raises(scheduling.YouTubeScheduleLocked):
        scheduling.cancel_schedule(user_id='owner',brand_id='brand',video_pk=1,plan_index='2')


def test_success_replayed_job_never_uploads_twice(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls = []
    monkeypatch.setattr(oauth,'upload_video_with_refresh_token',lambda **kw: calls.append(kw) or {'id':'remote'})
    first = schedule(video_path=str(media),publish_at=scheduling.iso(scheduling.utc()+timedelta(hours=1)),async_immediate=True)
    assert first['status'] == 'queued'
    assert scheduling.execute_scheduled_upload(first['schedule_id'],video_path=media)['id'] == 'remote'
    assert scheduling.execute_scheduled_upload(first['schedule_id'],video_path=media)['id'] == 'remote'
    assert len(calls) == 1 and calls[0]['privacy_status'] == 'private'
    with pytest.raises(scheduling.YouTubeScheduleLocked,match='studio.youtube.com/video/remote/edit'):
        schedule()


def test_reconnected_channel_blocks_without_provider_call(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls = []
    alerts = []
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: calls.append(kw))
    monkeypatch.setattr(
        oauth,
        'get_connected_channel_info',
        lambda user_id, brand_id: ({'id': 'channel-b', 'title': 'Channel B'}, None),
    )
    monkeypatch.setattr(scheduling, 'send_target_mismatch_alert', alerts.append)
    first = schedule(video_path=str(media), publish_at=None, async_immediate=True)
    result = scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)
    assert result['status'] == 'blocked_target_mismatch'
    assert calls == []
    assert len(alerts) == 1


def test_immediate_upload_is_public_without_publish_at(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls = []
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: calls.append(kw) or {'id': 'public-now'})
    first = schedule(video_path=str(media), publish_at=None, async_immediate=True)
    scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)
    assert len(calls) == 1
    assert calls[0]['privacy_status'] == 'public'
    assert calls[0]['publish_at'] is None


def test_pending_publish_now_reuses_shared_path_and_rejects_uploaded(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls = []
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: calls.append(kw) or {'id': 'public-pending'})
    schedule(video_path=str(media))
    immediate = scheduling.publish_pending_now(
        video_path=str(media), title='title', description='description',
        user_id='owner', brand_id='brand', video_pk=1, plan_index='2',
        async_immediate=True,
    )
    assert immediate['status'] == 'queued'
    scheduling.execute_scheduled_upload(immediate['schedule_id'], video_path=media)
    assert calls[0]['privacy_status'] == 'public' and calls[0]['publish_at'] is None
    with pytest.raises(scheduling.YouTubeScheduleLocked, match='studio.youtube.com/video/public-pending/edit'):
        scheduling.publish_pending_now(
            video_path=str(media), title='title', description='description',
            user_id='owner', brand_id='brand', video_pk=1, plan_index='2',
            async_immediate=True,
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    'status,resumable_uri',
    [('queued', None), ('uploading', None), ('uncertain', 'https://upload.example/session'), ('pending', 'https://upload.example/session')],
)
def test_pending_publish_now_refuses_started_upload_states(db, monkeypatch, tmp_path, status, resumable_uri):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls = []
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: calls.append(kw) or {'id': 'unexpected'})
    schedule(video_path=str(media))
    with duckdb.connect(db) as conn:
        conn.execute(
            'UPDATE shorts_youtube_schedules SET status=?,resumable_uri=?',
            [status, resumable_uri],
        )
    with pytest.raises(scheduling.YouTubeScheduleLocked, match='already queued or in progress'):
        scheduling.publish_pending_now(
            video_path=str(media), title='title', description='description',
            user_id='owner', brand_id='brand', video_pk=1, plan_index='2',
            async_immediate=True,
        )
    assert calls == []


@pytest.mark.parametrize('hours,status',[(1,'uploaded'),(25,'missed')])
def test_lateness_boundary_and_notification(db,monkeypatch,tmp_path,hours,status):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls=[]
    monkeypatch.setattr(oauth,'upload_video_with_refresh_token',lambda **kw: calls.append(kw) or {'id':'late'})
    first=schedule(video_path=str(media),publish_at=scheduling.iso(scheduling.utc()+timedelta(hours=1)),async_immediate=True)
    late_publish = scheduling.utc()-timedelta(hours=hours)
    with duckdb.connect(db) as conn:
        conn.execute(
            'UPDATE shorts_youtube_schedules SET publish_at=?,created_at=?',
            [late_publish, late_publish-timedelta(hours=1)],
        )
    scheduling.execute_scheduled_upload(first['schedule_id'],video_path=media)
    assert row(db)['status'] == status
    assert row(db)['notification'] == ('late' if hours==1 else 'missed')
    if hours==1:
        assert calls[0]['publish_at'] is None and calls[0]['privacy_status']=='public'
    else:
        assert calls==[]


def test_transient_failure_backoff_retains_session_and_blocks_edits(db,monkeypatch,tmp_path):
    media=tmp_path/'clip.mp4';media.write_bytes(b'fake')
    def fail(**kw):
        kw['save_resumable_uri']('https://upload.example/session')
        raise HttpError(httplib2.Response({'status':'503'}),b'private provider response')
    monkeypatch.setattr(oauth,'upload_video_with_refresh_token',fail)
    first=schedule(video_path=str(media),publish_at=scheduling.iso(scheduling.utc()+timedelta(hours=1)),async_immediate=True)
    scheduling.execute_scheduled_upload(first['schedule_id'],video_path=media)
    stored=row(db)
    assert stored['status']=='pending' and stored['upload_due_at']>scheduling.utc()
    assert stored['resumable_uri']=='https://upload.example/session'
    assert 'provider' not in stored['error']
    with pytest.raises(scheduling.YouTubeScheduleLocked):
        schedule()
    calls=[]
    monkeypatch.setattr(oauth,'upload_video_with_refresh_token',lambda **kw: calls.append(kw) or {'id':'resumed'})
    assert scheduling.scheduler_tick(now=stored['upload_due_at'])
    scheduling.execute_scheduled_upload(first['schedule_id'],video_path=media)
    assert calls[0]['resumable_uri']==stored['resumable_uri']
    assert row(db)['youtube_video_id']=='resumed'


def test_expired_resumable_session_never_starts_another_upload(db,monkeypatch,tmp_path):
    media=tmp_path/'clip.mp4';media.write_bytes(b'fake')
    def fail(**kw):
        kw['save_resumable_uri']('https://upload.example/session')
        raise HttpError(httplib2.Response({'status':'404'}),b'expired')
    monkeypatch.setattr(oauth,'upload_video_with_refresh_token',fail)
    first=schedule(video_path=str(media),publish_at=None,async_immediate=True)
    scheduling.execute_scheduled_upload(first['schedule_id'],video_path=media)
    assert row(db)['status']=='uncertain'
    assert not scheduling.scheduler_tick(now=scheduling.utc()+timedelta(days=1))


def test_metadata_regenerate_sync_only_pending(db):
    schedule()
    scheduling.sync_plan_metadata([dict(plan_index=2,yt_title='edited',yt_description='new',clip_filename='new.mp4')],user_id='owner',brand_id='brand',video_pk=1)
    payload=json.loads(row(db)['payload_json'])
    assert (payload['title'],payload['description'],payload['filename'])==('title','description','clip.mp4')
    scheduling.sync_completed_render(
        user_id='owner', brand_id='brand', video_pk=1, plan_index=2,
        rendered_entry=dict(yt_title='edited',yt_description='new',clip_filename='new.mp4'),
    )
    payload=json.loads(row(db)['payload_json'])
    assert (payload['title'],payload['description'],payload['filename'])==('edited','new','new.mp4')
    entries=scheduling.overlay_plan([{'plan_index':2}],user_id='owner',brand_id='brand',video_pk=1)
    assert entries[0]['publish_status']=='pending' and entries[0]['upload_due_at']
    assert entries[0]['youtube_publish_now_eligible'] is True
    with duckdb.connect(db) as conn:
        conn.execute("UPDATE shorts_youtube_schedules SET resumable_uri='https://upload.example/session'")
    entries=scheduling.overlay_plan([{'plan_index':2}],user_id='owner',brand_id='brand',video_pk=1)
    assert entries[0]['youtube_publish_now_eligible'] is False


def test_shared_upload_persists_session_before_media_and_resumes_without_new_insert(monkeypatch,tmp_path):
    from googleapiclient.http import HttpRequest
    from types import SimpleNamespace
    media=tmp_path/'clip.mp4';media.write_bytes(b'fake-media')
    saved=[]; calls=[]
    class Http:
        def request(self,uri,method='GET',**kwargs):
            calls.append((uri,method,kwargs.get('headers',{})))
            if method=='POST':
                return httplib2.Response({'status':'200','location':'https://upload.example/session'}),b''
            assert saved==['https://upload.example/session']
            return httplib2.Response({'status':'201'}),b'{"id":"same-video"}'
    def insert(**kw):
        return HttpRequest(Http(),lambda resp,content:json.loads(content),
            'https://youtube.example/insert',method='POST',body=json.dumps(kw['body']),resumable=kw['media_body'])
    api=SimpleNamespace(videos=lambda:SimpleNamespace(insert=insert))
    monkeypatch.setattr(oauth,'build_authenticated_youtube',lambda *a,**k:api)
    monkeypatch.setattr(oauth,'record_youtube_data_api_call',lambda **k:None)
    assert oauth.upload_video_with_refresh_token(str(media),'title','description',save_resumable_uri=saved.append)['id']=='same-video'
    assert [call[1] for call in calls]==['POST','PUT']
    calls.clear()
    assert oauth.upload_video_with_refresh_token(str(media),'title','description',resumable_uri=saved[0],save_resumable_uri=saved.append)['id']=='same-video'
    assert [call[1] for call in calls]==['PUT']
    assert calls[0][2]['Content-Range'].startswith('bytes */')


def test_queued_render_at_due_uses_last_finished_render(db, monkeypatch):
    alerts=[]
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append(kind))
    first=schedule()
    due=row(db)['upload_due_at']
    with duckdb.connect(db) as conn:
        conn.execute("INSERT INTO shorts_render_jobs(id,user_id,type,status,payload_json) VALUES ('render','owner','render_short','queued',?)",[json.dumps({'video_pk':1,'plan_index':2})])
    assert scheduling.scheduler_tick(now=due)
    assert 'rendering_at_upload_due' in alerts


def test_stale_render_pending_clears_while_active_render_uses_last_finished(db):
    first = schedule()
    due = row(db)['upload_due_at']
    with duckdb.connect(db) as conn:
        payload = json.loads(conn.execute(
            'SELECT payload_json FROM shorts_youtube_schedules'
        ).fetchone()[0])
        payload['render_pending'] = True
        conn.execute('UPDATE shorts_youtube_schedules SET payload_json=?', [json.dumps(payload)])
        conn.execute(
            "INSERT INTO shorts_render_jobs(id,user_id,type,status,payload_json) "
            "VALUES ('render','owner','render_short','processing',?)",
            [json.dumps({'video_pk': 1, 'plan_index': '2'})],
        )
    assert scheduling.scheduler_tick(now=due + timedelta(minutes=16)) is True
    assert json.loads(row(db)['payload_json'])['render_pending'] is False


def test_legacy_future_row_enqueues_on_time_and_legacy_past_only_alerts(db, monkeypatch, tmp_path):
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append(kind))
    schedule()
    due = row(db)['upload_due_at']
    with duckdb.connect(db) as conn:
        conn.execute('UPDATE shorts_youtube_schedules SET created_at=NULL')
    assert scheduling.scheduler_tick(now=due) is True
    assert row(db)['status'] == 'queued'
    with duckdb.connect(db) as conn:
        conn.execute("DELETE FROM shorts_render_jobs WHERE type='publish_short'")
        conn.execute("UPDATE shorts_youtube_schedules SET status='pending',job_id=NULL,publish_at=?", [due - timedelta(minutes=1)])
    assert scheduling.scheduler_tick(now=due) is False
    assert row(db)['status'] == 'pending'
    assert 'legacy_past_no_catchup' in alerts


def test_legacy_future_row_executes_existing_render_exactly_once(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls = []
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: calls.append(kw) or {'id': 'legacy-on-time'})
    first = schedule(video_path=str(media))
    due = row(db)['upload_due_at']
    with duckdb.connect(db) as conn:
        conn.execute('UPDATE shorts_youtube_schedules SET created_at=NULL')
    assert scheduling.scheduler_tick(now=due) is True
    assert scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)['id'] == 'legacy-on-time'
    assert scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)['id'] == 'legacy-on-time'
    assert len(calls) == 1


def test_legacy_past_queued_job_alerts_without_upload(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls = []
    alerts = []
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: calls.append(kw))
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append(kind))
    first = schedule(video_path=str(media))
    with duckdb.connect(db) as conn:
        conn.execute(
            "UPDATE shorts_youtube_schedules SET status='queued',created_at=NULL,publish_at=?",
            [scheduling.utc() - timedelta(minutes=1)],
        )
    scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)
    assert calls == []
    assert row(db)['status'] == 'queued'
    assert 'legacy_past_no_catchup' in alerts


def test_already_posted_row_is_never_enqueued(db):
    schedule()
    due = row(db)['upload_due_at']
    with duckdb.connect(db) as conn:
        conn.execute(
            "UPDATE shorts_youtube_schedules SET created_at=?,youtube_video_id='existing'",
            [due - timedelta(days=1)],
        )
    assert scheduling.scheduler_tick(now=due + timedelta(hours=1)) is False
    with duckdb.connect(db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM shorts_render_jobs WHERE type='publish_short'"
        ).fetchone()[0] == 0


def test_new_past_row_within_catchup_enqueues(db):
    schedule()
    now = scheduling.utc()
    with duckdb.connect(db) as conn:
        conn.execute(
            'UPDATE shorts_youtube_schedules SET publish_at=?,upload_due_at=?,created_at=?',
            [now - timedelta(hours=1), now - timedelta(hours=3), now - timedelta(days=1)],
        )
    assert scheduling.scheduler_tick(now=now) is True
    assert row(db)['status'] == 'queued'


def test_saved_content_change_still_uploads_frozen_render(db, monkeypatch):
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append((kind, message)))
    monkeypatch.setattr(
        scheduling, '_render_freshness',
        lambda candidate, payload: {
            'ready': False,
            'reason': 'customer edited clip, needs regenerate (content changed: title)',
        },
    )
    schedule()
    due = row(db)['upload_due_at']
    with duckdb.connect(db) as conn:
        payload = json.loads(conn.execute('SELECT payload_json FROM shorts_youtube_schedules').fetchone()[0])
        payload['render_pending'] = True
        conn.execute('UPDATE shorts_youtube_schedules SET payload_json=?', [json.dumps(payload)])
    assert scheduling.scheduler_tick(now=due + timedelta(minutes=16)) is True
    assert json.loads(row(db)['payload_json'])['render_pending'] is False


def test_content_vs_design_render_difference_classification():
    from app.video_shorts.routes import generation

    rendered = {
        'title': 'Original title',
        'edit_keep_ranges': [{'start': 10.0, 'end': 20.0}],
        'subtitle_text': 'same words',
        'caption_anchor': 'bottom',
        'subtitle_font_key': 'font-a',
        'title_text_color': '#000000',
        'layout_mode': 'fill',
        'background_visual_key': 'bg-a',
        'overlay_shapes': [{'id': 'one', 'x': 1}],
    }
    current = {
        **rendered,
        'caption_anchor': 'top',
        'subtitle_font_key': 'font-b',
        'title_text_color': '#ffffff',
        'layout_mode': 'auto',
        'background_visual_key': 'bg-b',
        'overlay_shapes': [{'id': 'one', 'x': 2}],
    }
    design_only = generation._schedule_render_difference_classification(
        rendered_options=rendered,
        current_options=current,
        rendered_entry={'start': 10.0, 'end': 20.0},
        current_entry={'start': 10.0, 'end': 20.0},
    )
    assert design_only['content'] == {}
    assert set(design_only['design']) == {
        'caption_anchor', 'subtitle_font_key', 'title_text_color',
        'layout_mode', 'background_visual_key', 'overlay_shapes',
    }

    current['title'] = 'Edited title'
    current['subtitle_text'] = 'different words'
    content_change = generation._schedule_render_difference_classification(
        rendered_options=rendered,
        current_options=current,
        rendered_entry={'start': 10.0, 'end': 20.0},
        current_entry={
            'start': 11.0,
            'end': 20.0,
            'edit_requires_regenerate': True,
        },
    )
    assert {'start', 'title', 'subtitle_text'} <= set(content_change['content'])
    assert 'edit_requires_regenerate' not in content_change['content']


def test_design_only_difference_allows_enqueue_and_sends_info_alert(db, monkeypatch):
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append((kind, message)))
    monkeypatch.setattr(
        scheduling, '_render_freshness',
        lambda candidate, payload: {
            'ready': True,
            'reason': None,
            'content_differences': {},
            'design_differences': {
                'caption_anchor': {'rendered': 'bottom', 'current': 'top'},
            },
        },
    )
    schedule()
    due = row(db)['upload_due_at']
    assert scheduling.scheduler_tick(now=due) is True
    assert row(db)['status'] == 'queued'
    assert any(kind == 'design_only_render_difference' and 'caption_anchor' in message for kind, message in alerts)


def test_legacy_warnings_apply_and_warning_scan_does_not_mutate(db, monkeypatch):
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append(kind))
    first = schedule(publish_at=scheduling.iso(scheduling.utc() + timedelta(hours=5)))
    stored = row(db)
    with duckdb.connect(db) as conn:
        payload = json.loads(stored['payload_json'])
        payload['render_pending'] = True
        conn.execute(
            'UPDATE shorts_youtube_schedules SET created_at=NULL,payload_json=?',
            [json.dumps(payload)],
        )
    scheduling.scheduler_tick(now=stored['upload_due_at'] - timedelta(hours=2, minutes=59))
    assert 'preflight_failed' in alerts
    assert json.loads(row(db)['payload_json'])['render_pending'] is True
    scheduling._alert_due_not_enqueued(
        duckdb.connect(db), stored['upload_due_at'] + timedelta(minutes=16)
    )
    assert 'enqueue_late' in alerts
    assert json.loads(row(db)['payload_json'])['render_pending'] is True
    assert first['schedule_id'] == row(db)['id']


def test_duplicate_title_blocks_before_upload(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls = []
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append(kind))
    monkeypatch.setattr(oauth, 'find_exact_title_video', lambda *a, **kw: {'id': 'duplicate'})
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: calls.append(kw))
    first = schedule(video_path=str(media), publish_at=None, async_immediate=True)
    result = scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)
    assert result['status'] == 'blocked_duplicate'
    assert calls == []
    assert alerts == ['duplicate_title']


def test_scheduler_alerts_preflight_enqueue_due_and_missed(db, monkeypatch):
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append((sid, kind, message)))
    first = schedule(publish_at=scheduling.iso(scheduling.utc() + timedelta(hours=5)))
    stored = row(db)
    monkeypatch.setattr(scheduling, '_file_exists', lambda payload: False)
    preflight_at = stored['upload_due_at'] - timedelta(hours=2, minutes=59)
    scheduling.scheduler_tick(now=preflight_at)
    scheduling.scheduler_tick(now=stored['upload_due_at'] + timedelta(minutes=16))
    scheduling.scheduler_tick(now=stored['publish_at'])
    scheduling.scheduler_tick(now=stored['publish_at'] + timedelta(hours=25))
    kinds = {kind for _, kind, _ in alerts}
    assert {'preflight_failed', 'enqueue_late', 'publish_time_not_uploaded', 'missed'} <= kinds
    assert first['schedule_id'] == row(db)['id']


def test_post_upload_verification_alerts(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append(kind))
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: {'id': 'remote'})
    monkeypatch.setattr(oauth, 'verify_uploaded_video', lambda *a, **kw: {'ok': False, 'reason': 'not visible'})
    first = schedule(video_path=str(media), publish_at=None, async_immediate=True)
    scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)
    assert 'post_upload_verification_failed' in alerts


def test_permanent_upload_failure_alerts_with_sanitized_error(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append((kind, message)))
    monkeypatch.setattr(
        oauth,
        'upload_video_with_refresh_token',
        lambda **kw: (_ for _ in ()).throw(ValueError('token=secret https://private.example/path')),
    )
    first = schedule(video_path=str(media), publish_at=None, async_immediate=True)
    scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)
    assert row(db)['status'] == 'failed'
    assert alerts[0][0] == 'upload_failed'
    assert 'secret' not in alerts[0][1]
    assert 'private.example' not in alerts[0][1]


def test_scheduler_heartbeat_gap_alert(db, monkeypatch):
    alerts = []
    monkeypatch.setattr(scheduling, '_alert', lambda sid, kind, message: alerts.append((kind, message)))
    now = scheduling.utc()
    with duckdb.connect(db) as conn:
        conn.execute(
            "INSERT INTO shorts_scheduler_state(name,last_tick_at,last_worker_id,updated_at) VALUES ('youtube',?,'old',?)",
            [now - timedelta(minutes=11), now - timedelta(minutes=11)],
        )
    REAL_SCHEDULER_HEALTH_CHECK(now=now)
    assert alerts and alerts[0][0].startswith('heartbeat_gap:')


def test_status_history_records_created_enqueued_uploading_uploaded(db, monkeypatch, tmp_path):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **kw: {'id': 'remote'})
    first = schedule(video_path=str(media), publish_at=None, async_immediate=True)
    scheduling.execute_scheduled_upload(first['schedule_id'], video_path=media)
    with duckdb.connect(db) as conn:
        statuses = [r[0] for r in conn.execute(
            'SELECT status FROM shorts_schedule_status_history ORDER BY created_at,id'
        ).fetchall()]
    assert statuses == ['created', 'enqueued', 'uploading', 'uploaded']


def test_generated_video_guard_uses_authenticated_owner_and_returns_record(monkeypatch):
    from types import SimpleNamespace
    from app.video_shorts.routes import generation
    closed=[];checks=[]
    conn=SimpleNamespace(execute=lambda *a,**kw:SimpleNamespace(fetchone=lambda:('record','remote','scheduled',None,'description')),close=lambda:closed.append(True))
    monkeypatch.setattr(generation,'get_db_readonly',lambda:conn)
    monkeypatch.setattr(generation,'table_columns',lambda *a:{'generated_description'})
    monkeypatch.setattr(generation,'_active_editor_context',lambda:{'owner_user_id':'owner'})
    monkeypatch.setattr(generation.tenant_media,'assert_clip_write_owner',lambda *a,**kw:checks.append((a,kw)))
    result=generation._load_generated_video_publish_guard_row(source_video_id='source',clip_filename='1_source_v1.mp4',brand_id='brand')
    assert result['youtube_video_id']=='remote'
    assert checks[0][0][2:] == ('owner','brand')
    assert checks[0][1] == {'source_id':'source'}
    assert closed==[True]
