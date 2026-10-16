#!/usr/bin/env python3
"""Isolated production route verification. No YouTube network operations allowed.

Creates a disposable source/clip for an explicitly selected connected admin brand.
All fixture files and rows are removed in finally; existing clips are never edited.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / '.env')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--admin-id',required=True)
    parser.add_argument('--brand-id',required=True)
    args=parser.parse_args()
    from wsgi import app
    from flask import g
    from app.video_shorts.services.db import get_db,get_db_readonly
    from app.video_shorts.services import youtube_scheduling as scheduling, tenant_media
    from app.video_shorts.services.generated_video_lifecycle import upsert_generated_video_record
    from app.video_shorts.services.youtube_token_store import has_refresh_token
    from app.video_shorts.routes import generation

    conn=get_db_readonly()
    try:
        admin=conn.execute("SELECT id,role,time_zone FROM shorts_users WHERE CAST(id AS VARCHAR)=? AND lower(role)='admin'",[args.admin_id]).fetchone()
        brand=conn.execute('SELECT id,name FROM shorts_brands WHERE id=? AND owner_user_id=?',[args.brand_id,args.admin_id]).fetchone()
        assert admin and brand, 'The fixture must belong to the specified admin.'
        assert has_refresh_token(args.admin_id,args.brand_id), 'The selected brand must already have a YouTube connection.'
        assert not conn.execute("SELECT 1 FROM shorts_youtube_schedules WHERE status='pending' AND upload_due_at<=CURRENT_TIMESTAMP LIMIT 1").fetchone(), 'Due real schedules exist; postpone the no-op tick verification.'
    finally:
        conn.close()
    source='phase2_verify_'+uuid4().hex[:16]
    video_pk=None;filename=None;paths=[];api_calls=[]
    origin='https://mintistudio.com'
    try:
        conn=get_db()
        try:
            video_pk=conn.execute("INSERT INTO youtube_videos(video_id,title,owner_user_id,brand_id,duration_seconds) VALUES (?,?,?,?,?) RETURNING id",[source,'Disposable YouTube scheduling verification',args.admin_id,args.brand_id,30]).fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        with app.test_request_context('/',base_url=origin):
            g.vs_current_user={'id':args.admin_id,'role':'admin'}
            g.vs_current_brand={'id':args.brand_id}
            filename=tenant_media.clip_filename(source,1,video_pk=video_pk)
            media=tenant_media.short_path(filename);media.parent.mkdir(parents=True,exist_ok=True)
            media.write_bytes(b'local route verification only; never upload')
            paths.append(media)
            generation._upsert_storage_asset(file_key=tenant_media.asset_key(filename),file_path=str(media),file_type='short',size_bytes=media.stat().st_size,user_id=args.admin_id,brand_id=args.brand_id)
            entry=dict(plan_index=1,title='Verification',yt_title='Verification',yt_description='Disposable fixture',start=0,end=10,status='created',publish_status='not_ready',clip_filename=filename,output_filename=filename)
            tenant_media.write_state(source,{'plan':[entry]},video_pk=video_pk)
            paths.append(tenant_media.state_path(source,'plan',video_pk=video_pk))
            upsert_generated_video_record(user_id=args.admin_id,brand_id=args.brand_id,source_video_id=source,source_channel_type='youtube',clip_filename=filename,output_filename=filename,storage_file_key=tenant_media.asset_key(filename),generation_status='created',generated_title='Verification',raw_plan_entry=entry)

        def blocked(*a,**kw):
            api_calls.append('blocked')
            raise AssertionError('YouTube network operation attempted during local route verification.')
        import requests,httplib2,googleapiclient.http,googleapiclient.discovery
        original_request=requests.sessions.Session.request
        def guarded_request(self,method,url,*a,**kw):
            if 'google' in str(url) or 'youtube' in str(url):
                return blocked()
            return original_request(self,method,url,*a,**kw)
        with ExitStack() as stack:
            stack.enter_context(patch.object(googleapiclient.http.HttpRequest,'execute',blocked))
            stack.enter_context(patch.object(googleapiclient.http.HttpRequest,'next_chunk',blocked))
            stack.enter_context(patch.object(googleapiclient.discovery,'build',blocked))
            stack.enter_context(patch.object(httplib2.Http,'request',blocked))
            stack.enter_context(patch.object(requests.sessions.Session,'request',guarded_request))
            client=app.test_client()
            with client.session_transaction(base_url=origin) as session:
                session['vs_user_id']=args.admin_id
                session['vs_brand_id']=args.brand_id
            tz=ZoneInfo(admin[2] or generation.DEFAULT_TIME_ZONE)
            tomorrow=(datetime.now(timezone.utc)+timedelta(days=1)).astimezone(tz).replace(second=0,microsecond=0)
            def post(route,data):
                response=client.post('/video_shorts'+route,data=data,headers={'X-Requested-With':'XMLHttpRequest'},base_url=origin)
                return response,response.get_json(silent=True) or {}
            form=dict(video_pk=str(video_pk),plan_index='1',filename=filename,youtube_enabled='1',title='Verification',description='Disposable fixture',publish_at=tomorrow.strftime('%Y-%m-%dT%H:%M'))
            response,payload=post('/youtube/upload_clip',form)
            assert response.status_code==200 and payload.get('success'), (response.status_code,payload)
            conn=get_db_readonly()
            try:
                scheduled=conn.execute('SELECT id,status,publish_at,upload_due_at FROM shorts_youtube_schedules WHERE user_id=? AND brand_id=? AND asset_key=?',[args.admin_id,args.brand_id,scheduling.asset_key(video_pk=video_pk,plan_index=1)]).fetchone()
            finally:
                conn.close()
            assert scheduled[1]=='pending'
            assert scheduled[2]==tomorrow.astimezone(timezone.utc).replace(tzinfo=None)
            assert scheduled[3]==scheduled[2]-timedelta(minutes=scheduling.lead_minutes())
            print('SCHEDULE_OK pending; upload_due_at = publish_at - lead; YouTube calls=0')
            form['publish_at']=(tomorrow+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
            response,payload=post('/youtube/upload_clip',form)
            assert response.status_code==200 and payload.get('success'), (response.status_code,payload)
            conn=get_db_readonly()
            try:
                updated=conn.execute('SELECT upload_due_at FROM shorts_youtube_schedules WHERE id=?',[scheduled[0]]).fetchone()[0]
            finally:
                conn.close()
            assert updated==scheduled[3]+timedelta(hours=1)
            print('RESCHEDULE_OK due updated; YouTube calls=0')
            response,payload=post(f'/generate/{video_pk}/clip/1/unschedule',{'platform':'youtube'})
            assert response.status_code==200 and payload.get('success'), (response.status_code,payload)
            conn=get_db_readonly()
            try:
                assert conn.execute('SELECT status FROM shorts_youtube_schedules WHERE id=?',[scheduled[0]]).fetchone()[0]=='cancelled'
            finally:
                conn.close()
            print('UNSCHEDULE_OK cancelled locally; YouTube calls=0')
            # Put a synthetic remote id only on the disposable fixture. It is never sent to Google.
            conn=get_db()
            try:
                conn.execute("UPDATE shorts_generated_videos SET youtube_video_id='test-lock-only' WHERE user_id=? AND brand_id=? AND clip_filename=?",[args.admin_id,args.brand_id,filename]);conn.commit()
            finally:
                conn.close()
            for route,data in [('/youtube/upload_clip',form),(f'/generate/{video_pk}/clip/1/unschedule',{'platform':'youtube'}),(f'/generate/{video_pk}/clip/1/regenerate',{'force':'1'}),(f'/generate/{video_pk}/update_clip_title',{'plan_index':'1','title':'Rejected edit'})]:
                response,payload=post(route,data)
                assert response.status_code==409 and 'https://studio.youtube.com/video/test-lock-only/edit' in payload.get('message',''), (route,response.status_code,payload)
            print('LOCK_OK reschedule/unschedule/regenerate/title rejected with Studio link; YouTube calls=0')
            with app.app_context():
                assert not scheduling.scheduler_tick()
            assert api_calls==[]
            print('TICK_OK nothing due; YouTube calls=0')
    finally:
        if video_pk:
            conn=get_db()
            try:
                conn.execute('DELETE FROM shorts_youtube_schedules WHERE user_id=? AND brand_id=? AND asset_key=?',[args.admin_id,args.brand_id,scheduling.asset_key(video_pk=video_pk,plan_index=1)])
                if filename:
                    conn.execute('DELETE FROM shorts_generated_videos WHERE user_id=? AND brand_id=? AND clip_filename=?',[args.admin_id,args.brand_id,filename])
                if filename:
                    conn.execute('DELETE FROM shorts_storage_assets WHERE file_key=? AND user_id=? AND brand_id=?',[tenant_media.asset_key(filename),args.admin_id,args.brand_id])
                conn.execute('DELETE FROM youtube_videos WHERE id=? AND owner_user_id=? AND brand_id=?',[video_pk,args.admin_id,args.brand_id])
                conn.commit()
            finally:
                conn.close()
        for path in paths:
            path.unlink(missing_ok=True)
        print('FIXTURE_CLEANED existing clips untouched')


if __name__=='__main__':
    main()
