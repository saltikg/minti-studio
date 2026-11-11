from datetime import datetime, timedelta

import duckdb

from app.video_shorts.services import scheduler_reachability as reachability


def _db():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE shorts_brands(id VARCHAR, name VARCHAR)")
    conn.execute("CREATE TABLE shorts_users(id VARCHAR, autopilot_billing_paused_at TIMESTAMP)")
    conn.execute("""CREATE TABLE shorts_youtube_schedules(
        id VARCHAR, user_id VARCHAR, brand_id VARCHAR, expected_youtube_channel_id VARCHAR,
        asset_key VARCHAR, payload_json TEXT, status VARCHAR, publish_at TIMESTAMP,
        upload_due_at TIMESTAMP, youtube_video_id VARCHAR)""")
    conn.execute("CREATE TABLE youtube_oauth_tokens(user_id VARCHAR, youtube_channel_id VARCHAR, reauth_required INTEGER)")
    conn.execute("CREATE TABLE instagram_oauth_tokens(user_id VARCHAR, page_access_token VARCHAR, instagram_business_account_id VARCHAR)")
    conn.execute("CREATE TABLE facebook_page_tokens(user_id VARCHAR, page_access_token VARCHAR, page_id VARCHAR)")
    conn.execute("CREATE TABLE tiktok_oauth_tokens(user_id VARCHAR, access_token VARCHAR, open_id VARCHAR)")
    for table, target, uploaded in reachability.SOCIAL_TABLES.values():
        conn.execute(f"""CREATE TABLE {table}(
            id VARCHAR, user_id VARCHAR, brand_id VARCHAR, video_id VARCHAR, plan_index VARCHAR,
            clip_filename VARCHAR, publish_at VARCHAR, status VARCHAR, plan_title VARCHAR,
            {target} VARCHAR, {uploaded} VARCHAR)""")
    conn.execute("INSERT INTO shorts_brands VALUES ('brand-1', ?)", [reachability.CANARY_BRAND_NAME])
    conn.execute("INSERT INTO shorts_users VALUES ('owner-1', NULL)")
    return conn


def _instagram(conn, *, schedule_id="ig-1", publish_at="2026-11-11T17:00:00Z", target="ig-target"):
    conn.execute(
        """INSERT INTO shorts_instagram_queue
           (id,user_id,brand_id,video_id,plan_index,clip_filename,publish_at,status,plan_title,
            instagram_business_account_id,instagram_media_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL)""",
        [schedule_id, "owner-1", "brand-1", "video-1", "1", "clip.mp4", publish_at,
         "pending", "A clip", target],
    )


def test_unreachable_and_blocked_rows_send_one_email(monkeypatch):
    conn = _db()
    _instagram(conn, target="")
    monkeypatch.setattr(reachability, "get_db_readonly", lambda: conn)
    sent = []
    monkeypatch.setattr(reachability, "_send_email", lambda subject, lines: sent.append((subject, lines)))

    rows = reachability.run_reachability_check(trigger="test", now=datetime(2026, 11, 10, 12))

    assert rows[0]["reachable"] is False
    assert rows[0]["reason"] == "missing_instagram_business_account_id"
    assert len(sent) == 1
    assert "missing_instagram_business_account_id" in "\n".join(sent[0][1])


def test_all_reachable_sends_no_email_and_does_not_mutate(monkeypatch):
    conn = _db()
    _instagram(conn)
    conn.execute("INSERT INTO instagram_oauth_tokens VALUES ('owner-1::brand-1','secret','ig-target')")
    before = conn.execute("SELECT * FROM shorts_instagram_queue").fetchall()
    monkeypatch.setattr(reachability.tenant_media, "clip_exists", lambda filename, conn=None: True)
    sent = []
    monkeypatch.setattr(reachability, "_send_email", lambda subject, lines: sent.append(subject))

    rows = reachability.audit_reachability(now=datetime(2026, 11, 10, 12), conn=conn)

    assert rows[0]["reachable"] is True
    assert sent == []
    assert conn.execute("SELECT * FROM shorts_instagram_queue").fetchall() == before


def test_canary_late_emails_with_blocking_reason(monkeypatch):
    conn = _db()
    _instagram(conn, publish_at="2026-11-10T11:30:00Z", target="")
    sent = []
    monkeypatch.setattr(reachability, "_claim_canary_alert", lambda platform, schedule_id: True)
    monkeypatch.setattr(reachability, "_send_email", lambda subject, lines: sent.append((subject, lines)))

    late = reachability.check_canary(now=datetime(2026, 11, 10, 12), conn=conn)

    assert late[0]["reason"] == "missing_instagram_business_account_id"
    assert sent[0][0] == "Scheduler canary failed"
    assert "Blocking reason" in "\n".join(sent[0][1])


def test_daily_check_runs_after_five_pt_once(monkeypatch):
    calls = []
    monkeypatch.setattr(reachability, "run_reachability_check", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(reachability, "check_canary", lambda **kwargs: [])
    monkeypatch.setattr(reachability, "_last_daily_date", None)
    monkeypatch.setattr(reachability, "_last_monitor_at", None)
    now = datetime(2026, 11, 10, 13, 0)  # 05:00 PT

    assert reachability.process_monitors(now=now) is True
    assert reachability.process_monitors(now=now + timedelta(minutes=1)) is False
    assert len(calls) == 1


def test_youtube_query_filtered_row_is_unreachable(monkeypatch):
    candidate = {
        "id": "yt-legacy",
        "status": "pending",
        "youtube_video_id": None,
        "upload_due_at": datetime(2026, 11, 11, 15),
        "payload_json": "{}",
    }
    monkeypatch.setattr(reachability.youtube_scheduling, "_due_candidate_ids", lambda *args, **kwargs: [])

    reason = reachability.youtube_scheduling.evaluate_enqueue_reachability(
        object(), candidate, at_time=candidate["upload_due_at"],
    )

    assert reason == "scheduler query filtered schedule"
