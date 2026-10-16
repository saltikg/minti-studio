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


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = str(tmp_path / 'schedules.duckdb')
    with duckdb.connect(path) as conn:
        conn.execute(scheduling.SCHEMA_SQL)
        conn.execute('''CREATE TABLE shorts_render_jobs (
            id VARCHAR,user_id VARCHAR,type VARCHAR,status VARCHAR,priority INTEGER,
            payload_json TEXT,attempts INTEGER,max_attempts INTEGER)''')
    monkeypatch.setattr(scheduling, 'get_db', lambda: duckdb.connect(path))
    monkeypatch.setattr(scheduling, 'get_db_readonly', lambda: duckdb.connect(path))
    monkeypatch.setattr(scheduling, 'send_pending_notifications', lambda: None)
    monkeypatch.setattr(scheduling, '_project_upload', lambda *a: None)
    monkeypatch.setattr(scheduling, '_existing_remote_id', lambda **kw: None)
    from app.video_shorts.services import media_access
    monkeypatch.setattr(media_access,'authorize_file',lambda *a,**kw: None)
    monkeypatch.setenv('YOUTUBE_UPLOAD_LEAD_MINUTES', '120')
    monkeypatch.setattr(oauth, 'upload_video_with_refresh_token', lambda **k: pytest.fail('unexpected upload'))
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


@pytest.mark.parametrize('hours,status',[(1,'uploaded'),(7,'missed')])
def test_lateness_boundary_and_notification(db,monkeypatch,tmp_path,hours,status):
    media = tmp_path / 'clip.mp4'; media.write_bytes(b'fake')
    calls=[]
    monkeypatch.setattr(oauth,'upload_video_with_refresh_token',lambda **kw: calls.append(kw) or {'id':'late'})
    first=schedule(video_path=str(media),publish_at=scheduling.iso(scheduling.utc()-timedelta(hours=hours)),async_immediate=True)
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
    assert (payload['title'],payload['description'],payload['filename'])==('edited','new','new.mp4')
    entries=scheduling.overlay_plan([{'plan_index':2}],user_id='owner',brand_id='brand',video_pk=1)
    assert entries[0]['publish_status']=='pending' and entries[0]['upload_due_at']


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


def test_queued_render_blocks_due_upload_until_finished(db):
    first=schedule()
    due=row(db)['upload_due_at']
    with duckdb.connect(db) as conn:
        conn.execute("INSERT INTO shorts_render_jobs(id,user_id,type,status,payload_json) VALUES ('render','owner','render_short','queued',?)",[json.dumps({'video_pk':1,'plan_index':2})])
    assert not scheduling.scheduler_tick(now=due)
    with duckdb.connect(db) as conn:
        conn.execute("UPDATE shorts_render_jobs SET status='done' WHERE id='render'")
    assert scheduling.scheduler_tick(now=due)
