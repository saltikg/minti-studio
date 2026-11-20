from __future__ import annotations

import html
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.discovery_health import evaluate_discovery_health
from app.video_shorts.services.email_verification import send_resend_email


logger = logging.getLogger(__name__)
PT = ZoneInfo("America/Los_Angeles")
REPORT_HOUR_PT = 18
REPORT_DAYS = 7
REPORT_STATE_PREFIX = "daily_outreach_report:"

OUTREACH_TEST_SHARE_LINK_IDS = (99, 171, 194)
OUTREACH_TEST_DOMAINS = (
    "mintistudio.com",
    "example.com",
    "yoursite.com",
    "slotbeer.com",
)


def outreach_account_filter_sql(
    alias: str,
    *,
    email_column: str,
    name_column: str,
    id_column: str = "",
) -> str:
    """Shared internal/test-account exclusion used by reports and admin funnels."""
    email_expr = f"lower(trim(coalesce({alias}.{email_column}, '')))"
    name_expr = f"lower(trim(coalesce({alias}.{name_column}, '')))"
    clauses = [
        *(
            f"right({email_expr}, {len(domain) + 1}) <> '@{domain}'"
            for domain in OUTREACH_TEST_DOMAINS
        ),
        f"substr({name_expr}, 1, 4) <> 'test'",
        f"strpos({name_expr}, ' test') = 0",
        f"substr({email_expr}, 1, 4) <> 'test'",
        f"strpos({email_expr}, '+test@') = 0",
    ]
    if id_column:
        excluded_ids = ", ".join(str(value) for value in OUTREACH_TEST_SHARE_LINK_IDS)
        clauses.insert(0, f"CAST({alias}.{id_column} AS BIGINT) NOT IN ({excluded_ids})")
    return " AND ".join(clauses)


def _metadata_text_sql(conn, field_name: str, alias: str = "ue") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"NULLIF({alias}.metadata->>'{field_name}', '')"
    return f"NULLIF(json_extract_string({alias}.metadata, '$.{field_name}'), '')"


def share_watch_clean_event_sql(conn, alias: str = "ue") -> str:
    internal = _metadata_text_sql(conn, "is_internal", alias)
    bot = _metadata_text_sql(conn, "is_bot", alias)
    return (
        f"lower(coalesce({internal}, '')) NOT IN ('true', '1', 'yes', 'on') "
        f"AND lower(coalesce({bot}, '')) NOT IN ('true', '1', 'yes', 'on')"
    )


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _date_key(value: Any) -> str:
    parsed = _as_utc(value)
    return parsed.astimezone(PT).date().isoformat() if parsed else ""


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bucket_dates(today: date) -> list[date]:
    return [today - timedelta(days=offset) for offset in range(REPORT_DAYS - 1, -1, -1)]


def _bounds(days: list[date]) -> tuple[datetime, datetime]:
    start = datetime.combine(days[0], time.min, tzinfo=PT).astimezone(timezone.utc)
    end = datetime.combine(days[-1] + timedelta(days=1), time.min, tzinfo=PT).astimezone(timezone.utc)
    return start, end


def _empty_counts(days: Iterable[date]) -> Dict[str, int]:
    return {day.isoformat(): 0 for day in days}


def _count_timestamps(rows: Iterable[Any], counts: Dict[str, int], *, index: int = 0) -> None:
    for row in rows:
        key = _date_key(row[index])
        if key in counts:
            counts[key] += 1


def _origin_sql(conn, alias: str = "j") -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"COALESCE({alias}.payload_json->>'job_origin', '')"
    return f"COALESCE(json_extract_string({alias}.payload_json, '$.job_origin'), '')"


def collect_outreach_report(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The single shared definition for the daily outreach audit/report metrics."""
    now_utc = _as_utc(now) or datetime.now(timezone.utc)
    today = now_utc.astimezone(PT).date()
    days = _bucket_dates(today)
    start_utc, end_utc = _bounds(days)
    counts = {
        "channels_discovered": _empty_counts(days),
        "leads_promoted": _empty_counts(days),
        "demos_ready": _empty_counts(days),
        "first_emails_sent": _empty_counts(days),
        "followups_sent": _empty_counts(days),
        "unique_link_clicks": _empty_counts(days),
        "signups_all": _empty_counts(days),
        "signups_outreach": _empty_counts(days),
    }
    conn = get_db_readonly()
    try:
        _count_timestamps(
            conn.execute(
                "SELECT first_seen_at FROM discovery_leads WHERE first_seen_at >= ? AND first_seen_at < ?",
                [start_utc, end_utc],
            ).fetchall(),
            counts["channels_discovered"],
        )
        _count_timestamps(
            conn.execute(
                """
                SELECT promoted_at
                FROM discovery_leads
                WHERE promoted_at >= ? AND promoted_at < ?
                  AND autopilot_lead_id IS NOT NULL
                """,
                [start_utc, end_utc],
            ).fetchall(),
            counts["leads_promoted"],
        )

        lead_filter = outreach_account_filter_sql(
            "l", email_column="creator_email", name_column="creator_name"
        )
        _count_timestamps(
            conn.execute(
                f"""
                SELECT e.created_at
                FROM lead_pipeline_events e
                JOIN autopilot_leads l ON CAST(l.id AS VARCHAR) = CAST(e.lead_id AS VARCHAR)
                WHERE e.event_type = 'watch_page_created'
                  AND e.created_at >= ? AND e.created_at < ?
                  AND {lead_filter}
                """,
                [start_utc, end_utc],
            ).fetchall(),
            counts["demos_ready"],
        )

        share_filter = outreach_account_filter_sql(
            "sl",
            email_column="recipient_email",
            name_column="recipient_name",
            id_column="id",
        )
        send_rows = conn.execute(
            f"""
            SELECT CAST(sl.id AS VARCHAR), 1, sl.emailed_at
            FROM short_share_links sl
            WHERE sl.emailed_at >= ? AND sl.emailed_at < ? AND {share_filter}
            UNION ALL
            SELECT CAST(sl.id AS VARCHAR), 2, sl.followup_sent_at
            FROM short_share_links sl
            WHERE sl.followup_sent_at >= ? AND sl.followup_sent_at < ? AND {share_filter}
              AND NOT EXISTS (
                SELECT 1
                FROM outreach_scheduled_emails legacy_check
                WHERE legacy_check.share_link_id = sl.id
                  AND lower(COALESCE(legacy_check.status, '')) = 'sent'
                  AND COALESCE(
                    legacy_check.sequence_number,
                    CASE WHEN lower(legacy_check.stage) = 'first' THEN 1 ELSE 2 END
                  ) >= 2
              )
            UNION ALL
            SELECT CAST(ose.share_link_id AS VARCHAR),
                   COALESCE(ose.sequence_number, CASE WHEN lower(ose.stage) = 'first' THEN 1 ELSE 2 END),
                   ose.sent_at
            FROM outreach_scheduled_emails ose
            JOIN short_share_links sl ON sl.id = ose.share_link_id
            WHERE lower(COALESCE(ose.status, '')) = 'sent'
              AND ose.sent_at >= ? AND ose.sent_at < ? AND {share_filter}
            """,
            [start_utc, end_utc, start_utc, end_utc, start_utc, end_utc],
        ).fetchall()
        unique_sends: Dict[tuple[str, int], datetime] = {}
        for row in send_rows:
            sent_at = _as_utc(row[2])
            if not sent_at:
                continue
            key = (str(row[0]), int(row[1] or 0))
            if key not in unique_sends or sent_at < unique_sends[key]:
                unique_sends[key] = sent_at
        for (_link_id, sequence), sent_at in unique_sends.items():
            key = _date_key(sent_at)
            target = "first_emails_sent" if sequence == 1 else "followups_sent"
            if key in counts[target]:
                counts[target][key] += 1

        share_id = _metadata_text_sql(conn, "share_link_id")
        token = _metadata_text_sql(conn, "token")
        clean_click = share_watch_clean_event_sql(conn)
        click_rows = conn.execute(
            f"""
            SELECT
              COALESCE(NULLIF(CAST(sl.autopilot_lead_id AS VARCHAR), ''),
                       lower(NULLIF(sl.recipient_email, '')), CAST(sl.id AS VARCHAR)) AS recipient_key,
              ue.created_at,
              COALESCE(c.channel_name, dl.channel_title, l.creator_name, sl.recipient_name, 'Unknown') AS channel_name,
              COALESCE((
                SELECT dc.name
                FROM keyword_queue kq
                JOIN discovery_categories dc ON dc.id = kq.category_id
                WHERE lower(kq.keyword) = lower(COALESCE(dl.matched_keyword, ''))
                ORDER BY kq.updated_at DESC NULLS LAST, kq.id DESC
                LIMIT 1
              ), 'Uncategorized') AS category
            FROM user_events ue
            JOIN short_share_links sl
              ON ({share_id} IS NOT NULL AND {share_id} = CAST(sl.id AS VARCHAR))
              OR ({share_id} IS NULL AND {token} = sl.token)
            LEFT JOIN autopilot_leads l
              ON CAST(l.id AS VARCHAR) = CAST(sl.autopilot_lead_id AS VARCHAR)
            LEFT JOIN youtube_channels c ON c.channel_id = l.channel_id
            LEFT JOIN discovery_leads dl
              ON CAST(dl.autopilot_lead_id AS VARCHAR) = CAST(l.id AS VARCHAR)
            WHERE ue.event_name = 'share_view'
              AND ue.created_at >= ? AND ue.created_at < ?
              AND {clean_click}
              AND {share_filter}
            """,
            [start_utc, end_utc],
        ).fetchall()
        seen_clicks: set[tuple[str, str]] = set()
        clicked_today: Dict[str, Dict[str, str]] = {}
        for row in click_rows:
            day_key = _date_key(row[1])
            click_key = (str(row[0]), day_key)
            if day_key in counts["unique_link_clicks"] and click_key not in seen_clicks:
                seen_clicks.add(click_key)
                counts["unique_link_clicks"][day_key] += 1
            if day_key == today.isoformat():
                clicked_today.setdefault(
                    str(row[0]),
                    {"channel_name": str(row[2] or "Unknown"), "category": str(row[3] or "Uncategorized")},
                )

        user_filter = outreach_account_filter_sql(
            "u", email_column="email", name_column="name"
        )
        signup_rows = conn.execute(
            f"""
            SELECT CAST(ue.user_id AS VARCHAR), ue.created_at, ue.metadata
            FROM user_events ue
            JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(ue.user_id AS VARCHAR)
            WHERE ue.event_name = 'signup'
              AND ue.created_at >= ? AND ue.created_at < ?
              AND lower(COALESCE(u.role, '')) <> 'admin'
              AND {user_filter}
            ORDER BY ue.created_at ASC
            """,
            [start_utc, end_utc],
        ).fetchall()
        seen_signups: set[str] = set()
        attribution_start = datetime(2026, 11, 15, tzinfo=PT).astimezone(timezone.utc)
        for row in signup_rows:
            user_id = str(row[0])
            if user_id in seen_signups:
                continue
            seen_signups.add(user_id)
            day_key = _date_key(row[1])
            if day_key not in counts["signups_all"]:
                continue
            counts["signups_all"][day_key] += 1
            metadata = _json_dict(row[2])
            landing = str(metadata.get("landing_path") or "").lower()
            source = str(metadata.get("utm_source") or "").lower()
            created_at = _as_utc(row[1])
            if created_at and created_at >= attribution_start and (source == "outreach" or landing.startswith("/w/")):
                counts["signups_outreach"][day_key] += 1

        ready_pool = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM discovery_leads dl
                WHERE dl.icp_fit IS TRUE
                  AND COALESCE(dl.creator_email, '') <> ''
                  AND COALESCE(CAST(dl.autopilot_lead_id AS VARCHAR), '') = ''
                  AND COALESCE(dl.status, '') NOT IN ('already_lead', 'dismissed')
                  AND NOT EXISTS (
                    SELECT 1 FROM autopilot_leads al
                    WHERE al.youtube_channel_id = dl.youtube_channel_id
                  )
                """
            ).fetchone()[0]
            or 0
        )
        awaiting_enrichment = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM discovery_leads
                WHERE status IN ('icp_qualified', 'email_failed')
                  AND COALESCE(creator_email, '') = ''
                """
            ).fetchone()[0]
            or 0
        )
        origin = _origin_sql(conn)
        queue_row = conn.execute(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE j.status = 'queued'),
              COUNT(*) FILTER (WHERE j.status = 'processing')
            FROM shorts_render_jobs j
            WHERE {origin} = 'discovery_demo'
            """
        ).fetchone()
        discovery_queued = int((queue_row[0] if queue_row else 0) or 0)
        discovery_processing = int((queue_row[1] if queue_row else 0) or 0)
        stuck_cutoff = now_utc - timedelta(hours=48)
        legacy_cutoff = now_utc - timedelta(days=30)
        stuck_row = conn.execute(
            f"""
            SELECT
              COUNT(*) FILTER (
                WHERE COALESCE(latest.last_event_at, l.created_at) <= ?
                  AND COALESCE(latest.last_event_at, l.created_at) > ?
              ),
              COUNT(*) FILTER (
                WHERE COALESCE(latest.last_event_at, l.created_at) <= ?
              )
            FROM autopilot_leads l
            LEFT JOIN LATERAL (
              SELECT MAX(e.created_at) AS last_event_at
              FROM lead_pipeline_events e
              WHERE CAST(e.lead_id AS VARCHAR) = CAST(l.id AS VARCHAR)
            ) latest ON TRUE
            WHERE COALESCE(l.pipeline_state, 'new') NOT IN ('sent', 'failed')
              AND {lead_filter}
            """,
            [stuck_cutoff, legacy_cutoff, legacy_cutoff],
        ).fetchone()
        stuck_last_30d = int((stuck_row[0] if stuck_row else 0) or 0)
        legacy_stale = int((stuck_row[1] if stuck_row else 0) or 0)
        first_queue_row = conn.execute(
            """
            SELECT COUNT(*) FROM outreach_scheduled_emails
            WHERE lower(COALESCE(stage, '')) = 'first'
              AND lower(COALESCE(status, '')) IN ('scheduled', 'processing')
            """
        ).fetchone()
        first_email_queue = int((first_queue_row[0] if first_queue_row else 0) or 0)
    finally:
        conn.close()

    health = evaluate_discovery_health(
        gate_reason=None,
        in_night_window=False,
        dry_run=True,
        send_alerts=False,
        now=now_utc,
    )
    active_health_reasons = {
        item["reason"] for item in health.get("conditions", []) if item.get("active")
    }
    warnings: list[str] = []
    if first_email_queue == 0 and ready_pool > 0:
        warnings.append("first-email queue empty")
    if "old_discovery_queue_with_zero_processing" in active_health_reasons:
        warnings.append("discovery queue stalled")
    if stuck_last_30d > 0:
        warnings.append(f"{stuck_last_30d} lead(s) stuck >48h (last 30 days)")
    if int(health.get("disk_usage_pct") or 0) >= 70:
        warnings.append(f"disk {int(health.get('disk_usage_pct') or 0)}%")

    rows = [
        ("Channels discovered", "channels_discovered"),
        ("Leads promoted", "leads_promoted"),
        ("Demos ready (watch pages created)", "demos_ready"),
        ("First emails sent", "first_emails_sent"),
        ("Follow-up emails sent", "followups_sent"),
        ("Unique link clicks", "unique_link_clicks"),
        ("Signups — all", "signups_all"),
        ("Signups — from outreach", "signups_outreach"),
    ]
    return {
        "generated_at": now_utc.isoformat(),
        "today_pt": today.isoformat(),
        "dates": [day.isoformat() for day in days],
        "rows": [
            {
                "label": label,
                "key": key,
                "values": [counts[key][day.isoformat()] for day in days],
                "total": sum(counts[key].values()),
            }
            for label, key in rows
        ],
        "status": {
            "promotable_ready_pool": ready_pool,
            "awaiting_email_enrichment": awaiting_enrichment,
            "discovery_jobs_queued": discovery_queued,
            "discovery_jobs_processing": discovery_processing,
            "leads_stuck_over_48h_last_30d": stuck_last_30d,
            "legacy_stale_over_30d": legacy_stale,
            "root_disk_pct": int(health.get("disk_usage_pct") or 0),
            "first_email_queue": first_email_queue,
        },
        "clicked_today": sorted(clicked_today.values(), key=lambda item: item["channel_name"].lower()),
        "warnings": warnings,
    }


def render_outreach_report(report: Dict[str, Any]) -> Dict[str, str]:
    warnings = list(report.get("warnings") or [])
    suffix = "OK" if not warnings else f"⚠️ {warnings[0]}" + (f" +{len(warnings) - 1}" if len(warnings) > 1 else "")
    subject = f"Minti Outreach — {report['today_pt']} · {suffix}"
    dates = list(report["dates"])
    date_headings = [
        f"{date.fromisoformat(value).strftime('%a')} {date.fromisoformat(value).day}"
        for value in dates
    ]
    heading = [*date_headings, "7 days"]
    text_lines = [subject, "", "Metric | " + " | ".join(heading)]
    for row in report["rows"]:
        text_lines.append(f"{row['label']} | " + " | ".join(str(value) for value in [*row["values"], row["total"]]))
    status = report["status"]
    text_lines.extend(
        [
            "",
            "Current status",
            f"Promotable ready pool: {status['promotable_ready_pool']}",
            f"Awaiting email enrichment: {status['awaiting_email_enrichment']}",
            f"Discovery jobs: {status['discovery_jobs_queued']} queued / {status['discovery_jobs_processing']} processing",
            f"Stuck >48h (last 30 days): {status['leads_stuck_over_48h_last_30d']}",
            f"Legacy stale (state unchanged >30 days): {status['legacy_stale_over_30d']}",
            f"Root disk: {status['root_disk_pct']}%",
            "",
            "Clicked today",
        ]
    )
    clicked = report.get("clicked_today") or []
    text_lines.extend(
        [f"- {item['channel_name']} — {item['category']}" for item in clicked]
        if clicked
        else ["none"]
    )
    if warnings:
        text_lines.extend(["", "Warnings", *[f"- {reason}" for reason in warnings]])

    th_style = "padding:8px;border:1px solid #d7e2df;background:#eefbf7;font-size:12px;white-space:nowrap"
    td_style = "padding:8px;border:1px solid #d7e2df;text-align:center;font-size:13px"
    first_style = td_style + ";text-align:left;font-weight:600;white-space:nowrap"
    header_html = "".join(f"<th style=\"{th_style}\">{html.escape(value)}</th>" for value in ["Metric", *heading])
    row_html = "".join(
        "<tr>"
        f"<td style=\"{first_style}\">{html.escape(row['label'])}</td>"
        + "".join(f"<td style=\"{td_style}\">{int(value)}</td>" for value in [*row["values"], row["total"]])
        + "</tr>"
        for row in report["rows"]
    )
    status_items = "".join(
        f"<li style='margin:4px 0'><strong>{html.escape(label)}:</strong> {html.escape(str(value))}</li>"
        for label, value in (
            ("Promotable ready pool", status["promotable_ready_pool"]),
            ("Awaiting email enrichment", status["awaiting_email_enrichment"]),
            ("Discovery jobs", f"{status['discovery_jobs_queued']} queued / {status['discovery_jobs_processing']} processing"),
            ("Stuck >48h (last 30 days)", status["leads_stuck_over_48h_last_30d"]),
            ("Legacy stale (state unchanged >30 days)", status["legacy_stale_over_30d"]),
            ("Root disk", f"{status['root_disk_pct']}%"),
        )
    )
    clicked_html = (
        "<ul style='margin:0;padding-left:20px'>"
        + "".join(
            f"<li style='margin:4px 0'>{html.escape(item['channel_name'])} — {html.escape(item['category'])}</li>"
            for item in clicked
        )
        + "</ul>"
        if clicked
        else "<p style='margin:0;color:#52615d'>none</p>"
    )
    warning_html = ""
    if warnings:
        warning_html = (
            "<div style='margin:16px 0;padding:12px;border-radius:10px;background:#fff4e5;color:#7a3e00'>"
            "<strong>Warnings</strong><ul style='margin:8px 0 0;padding-left:20px'>"
            + "".join(f"<li>{html.escape(reason)}</li>" for reason in warnings)
            + "</ul></div>"
        )
    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#17211f;line-height:1.45;max-width:760px;margin:0 auto">
      <h2 style="margin:0 0 14px">Minti Outreach — {html.escape(report['today_pt'])}</h2>
      {warning_html}
      <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
        <table role="presentation" style="border-collapse:collapse;width:100%;min-width:720px">
          <thead><tr>{header_html}</tr></thead><tbody>{row_html}</tbody>
        </table>
      </div>
      <h3 style="margin:20px 0 6px">Current status</h3><ul style="margin:0;padding-left:20px">{status_items}</ul>
      <h3 style="margin:20px 0 6px">Clicked today</h3>{clicked_html}
    </div>
    """.strip()
    return {"subject": subject, "html": html_body, "text": "\n".join(text_lines) + "\n"}


def _admin_recipients() -> list[str]:
    recipient = (os.getenv("ADMIN_NOTIFICATION_EMAIL") or "info@mintistudio.com").strip()
    return [recipient] if "@" in recipient else []


def send_outreach_report(report: Dict[str, Any], *, test: bool = False) -> list[Dict[str, Any]]:
    rendered = render_outreach_report(report)
    subject = f"[TEST] {rendered['subject']}" if test else rendered["subject"]
    results = []
    for recipient in _admin_recipients():
        result = send_resend_email(
            to_email=recipient,
            subject=subject,
            html=rendered["html"],
            text=rendered["text"],
            from_display_name="Minti Studio",
            error_message="Daily outreach report could not be sent.",
        )
        results.append({"recipient": recipient, **result})
    return results


def next_report_run_pt(*, now: Optional[datetime] = None) -> datetime:
    now_utc = _as_utc(now) or datetime.now(timezone.utc)
    local = now_utc.astimezone(PT)
    candidate = datetime.combine(local.date(), time(REPORT_HOUR_PT), tzinfo=PT)
    if local >= candidate:
        candidate += timedelta(days=1)
    return candidate


def _claim_report_day(report_date: date, now_utc: datetime) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            """
            INSERT INTO shorts_scheduler_state (
                name, last_tick_at, last_worker_id, last_gap_alerted_at, updated_at
            )
            VALUES (?, ?, 'daily_outreach_report', NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO NOTHING
            RETURNING name
            """,
            [f"{REPORT_STATE_PREFIX}{report_date.isoformat()}", now_utc],
        ).fetchone()
        conn.commit()
        return bool(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_report_day(report_date: date) -> None:
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM shorts_scheduler_state WHERE name = ? AND last_worker_id = 'daily_outreach_report'",
            [f"{REPORT_STATE_PREFIX}{report_date.isoformat()}"],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Could not release failed daily outreach report claim")
    finally:
        conn.close()


def process_scheduled_outreach_report(*, now: Optional[datetime] = None) -> bool:
    now_utc = _as_utc(now) or datetime.now(timezone.utc)
    local = now_utc.astimezone(PT)
    if local.hour < REPORT_HOUR_PT:
        return False
    if not _claim_report_day(local.date(), now_utc):
        return False
    try:
        report = collect_outreach_report(now=now_utc)
        send_outreach_report(report)
        logger.info("Daily outreach report sent report_date_pt=%s", local.date().isoformat())
        return True
    except Exception:
        _release_report_day(local.date())
        raise
