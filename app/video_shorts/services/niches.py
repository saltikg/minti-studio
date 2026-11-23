from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any

from app.video_shorts.services.db import table_columns


OUTREACH_LABELS = {
    "customer": "Customer",
    "signed_up": "Signed up",
    "clicked": "Clicked",
    "emailed": "Emailed",
    "lead_not_emailed": "Lead not emailed",
    "not_a_lead": "Not a lead",
}


def _dict_rows(result) -> list[dict[str, Any]]:
    columns = [str(item[0]) for item in (result.description or [])]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _placeholders(values: list[Any]) -> str:
    return ", ".join("?" for _ in values)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _latest_datetime(*values: Any) -> Any:
    present = [value for value in values if value is not None]
    if not present:
        return None

    def comparable(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    return max(present, key=comparable)


def _language_is_english(value: Any) -> bool:
    language = _clean(value).lower().replace("_", "-")
    return language == "english" or language == "en" or language.startswith("en-")


def list_niches(conn) -> list[dict[str, Any]]:
    if not table_columns(conn, "niches"):
        return []
    return _dict_rows(
        conn.execute(
            """
            SELECT id, slug, name, category, created_at
            FROM niches
            ORDER BY name ASC
            """
        )
    )


def load_niche_snapshot(conn, niche_slug: str, snapshot_date: str = "") -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    niche = conn.execute(
        "SELECT id, slug, name, category FROM niches WHERE slug = ? LIMIT 1",
        [niche_slug],
    ).fetchone()
    if not niche:
        return None, []
    selected_date = _clean(snapshot_date)
    if not selected_date:
        latest = conn.execute(
            "SELECT MAX(snapshot_date) FROM niche_channels WHERE niche_id = ?",
            [niche[0]],
        ).fetchone()
        selected_date = str(latest[0]) if latest and latest[0] else ""
    if not selected_date:
        return {"id": niche[0], "slug": niche[1], "name": niche[2], "category": niche[3], "snapshot_date": ""}, []

    result = conn.execute(
        """
        SELECT youtube_channel_id, snapshot_date, title, channel_url, credential,
               in_niche, presenter, language, subscriber_count, longform_90d,
               shorts_90d, avg_views_long_90d, avg_views_short_90d,
               discovery_lead_id, source, replied, admin_note, updated_at
        FROM niche_channels
        WHERE niche_id = ? AND snapshot_date = ?
        ORDER BY subscriber_count DESC NULLS LAST, lower(title) ASC
        """,
        [niche[0], selected_date],
    )
    rows = _dict_rows(result)
    return {
        "id": niche[0],
        "slug": niche[1],
        "name": niche[2],
        "category": niche[3],
        "snapshot_date": selected_date,
    }, rows


def load_snapshot_dates(conn, niche_id: Any) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT snapshot_date FROM niche_channels WHERE niche_id = ? ORDER BY snapshot_date DESC",
        [niche_id],
    ).fetchall()
    return [str(row[0]) for row in rows]


def _metadata_text_sql(conn, field_name: str) -> str:
    if getattr(conn, "backend_name", "") == "postgres":
        return f"NULLIF(ue.metadata->>'{field_name}', '')"
    return f"NULLIF(json_extract_string(ue.metadata, '$.{field_name}'), '')"


def attach_outreach_statuses(conn, rows: list[dict[str, Any]]) -> None:
    channel_ids = sorted({_clean(row.get("youtube_channel_id")) for row in rows if _clean(row.get("youtube_channel_id"))})
    if not channel_ids:
        return
    statuses: dict[str, dict[str, Any]] = {
        channel_id: {"key": "not_a_lead", "label": OUTREACH_LABELS["not_a_lead"], "emailed_at": None}
        for channel_id in channel_ids
    }
    for row in rows:
        channel_id = _clean(row.get("youtube_channel_id"))
        if _clean(row.get("discovery_lead_id")):
            statuses[channel_id] = {
                "key": "lead_not_emailed",
                "label": OUTREACH_LABELS["lead_not_emailed"],
                "emailed_at": None,
            }

    required = {"id", "youtube_channel_id", "user_id", "brand_id", "converted_at"}
    if not required.issubset(table_columns(conn, "autopilot_leads")):
        for row in rows:
            row["outreach"] = statuses[_clean(row.get("youtube_channel_id"))]
        return

    user_columns = table_columns(conn, "shorts_users")
    user_event_columns = table_columns(conn, "user_events")
    signup_evidence = []
    if "password_hash" in user_columns:
        signup_evidence.append("COALESCE(u.password_hash, '') <> ''")
    if "google_sub" in user_columns:
        signup_evidence.append("COALESCE(u.google_sub, '') <> ''")
    if "last_login_at" in user_columns:
        signup_evidence.append("u.last_login_at IS NOT NULL")
    if {"user_id", "event_name"}.issubset(user_event_columns):
        signup_evidence.append(
            """
            EXISTS (
                SELECT 1
                FROM user_events signup_event
                WHERE CAST(signup_event.user_id AS VARCHAR) = CAST(u.id AS VARCHAR)
                  AND signup_event.event_name = 'signup'
            )
            """
        )
    signup_evidence_sql = " OR ".join(signup_evidence) or "FALSE"

    lead_rows = conn.execute(
        f"""
        SELECT CAST(l.id AS VARCHAR) AS lead_id, l.youtube_channel_id,
               CAST(l.user_id AS VARCHAR) AS user_id,
               CAST(l.brand_id AS VARCHAR) AS brand_id, l.converted_at,
               lower(COALESCE(u.service_mode, '')) AS service_mode,
               CASE WHEN b.id IS NULL THEN FALSE ELSE TRUE END AS brand_exists,
               CASE
                   WHEN u.id IS NULL THEN FALSE
                   WHEN ({signup_evidence_sql}) THEN TRUE
                   ELSE FALSE
               END AS has_real_signup
        FROM autopilot_leads l
        LEFT JOIN shorts_users u ON CAST(u.id AS VARCHAR) = CAST(l.user_id AS VARCHAR)
        LEFT JOIN shorts_brands b ON CAST(b.id AS VARCHAR) = CAST(l.brand_id AS VARCHAR)
        WHERE l.youtube_channel_id IN ({_placeholders(channel_ids)})
        """,
        channel_ids,
    ).fetchall()
    lead_to_channel: dict[str, str] = {}
    for lead in lead_rows:
        lead_id, channel_id = _clean(lead[0]), _clean(lead[1])
        if not lead_id or not channel_id:
            continue
        lead_to_channel[lead_id] = channel_id
        if statuses[channel_id]["key"] == "not_a_lead":
            statuses[channel_id] = {
                "key": "lead_not_emailed",
                "label": OUTREACH_LABELS["lead_not_emailed"],
                "emailed_at": None,
            }
        if bool(lead[7]) and _clean(lead[2]) and statuses[channel_id]["key"] != "customer":
            statuses[channel_id] = {"key": "signed_up", "label": OUTREACH_LABELS["signed_up"], "emailed_at": None}
        if bool(lead[6]) and lead[4] is not None and _clean(lead[5]) == "autopilot":
            statuses[channel_id] = {"key": "customer", "label": OUTREACH_LABELS["customer"], "emailed_at": None}

    lead_ids = sorted(lead_to_channel)
    if lead_ids and table_columns(conn, "short_share_links"):
        email_rows = conn.execute(
            f"""
            SELECT CAST(sl.autopilot_lead_id AS VARCHAR) AS lead_id,
                   MAX(sl.emailed_at) AS link_emailed_at,
                   MAX(CASE WHEN lower(COALESCE(ose.status, '')) = 'sent' THEN ose.sent_at ELSE NULL END) AS scheduled_sent_at
            FROM short_share_links sl
            LEFT JOIN outreach_scheduled_emails ose ON ose.share_link_id = sl.id
            WHERE CAST(sl.autopilot_lead_id AS VARCHAR) IN ({_placeholders(lead_ids)})
            GROUP BY CAST(sl.autopilot_lead_id AS VARCHAR)
            """,
            lead_ids,
        ).fetchall()
        for item in email_rows:
            channel_id = lead_to_channel.get(_clean(item[0]))
            if not channel_id or statuses[channel_id]["key"] in {"customer", "signed_up"}:
                continue
            sent_at = _latest_datetime(item[1], item[2])
            if sent_at:
                statuses[channel_id] = {"key": "emailed", "label": OUTREACH_LABELS["emailed"], "emailed_at": sent_at}

        clicked_lead_ids: set[str] = set()
        if user_event_columns:
            share_expr = _metadata_text_sql(conn, "share_link_id")
            token_expr = _metadata_text_sql(conn, "token")
            lead_expr = _metadata_text_sql(conn, "autopilot_lead_id")
            clicked = conn.execute(
                f"""
                SELECT DISTINCT CAST(sl.autopilot_lead_id AS VARCHAR)
                FROM short_share_links sl
                JOIN user_events ue ON ue.event_name = 'share_view'
                 AND (
                      {share_expr} = CAST(sl.id AS VARCHAR)
                      OR ({share_expr} IS NULL AND {token_expr} = sl.token)
                      OR {lead_expr} = CAST(sl.autopilot_lead_id AS VARCHAR)
                 )
                WHERE CAST(sl.autopilot_lead_id AS VARCHAR) IN ({_placeholders(lead_ids)})
                """,
                lead_ids,
            ).fetchall()
            clicked_lead_ids.update(_clean(item[0]) for item in clicked if _clean(item[0]))

        onboarding_columns = table_columns(conn, "onboarding_magic_links")
        if {"share_link_id", "used_at"}.issubset(onboarding_columns):
            used_onboarding = conn.execute(
                f"""
                SELECT DISTINCT CAST(sl.autopilot_lead_id AS VARCHAR)
                FROM short_share_links sl
                JOIN onboarding_magic_links oml
                  ON CAST(oml.share_link_id AS VARCHAR) = CAST(sl.id AS VARCHAR)
                WHERE CAST(sl.autopilot_lead_id AS VARCHAR) IN ({_placeholders(lead_ids)})
                  AND oml.used_at IS NOT NULL
                """,
                lead_ids,
            ).fetchall()
            clicked_lead_ids.update(_clean(item[0]) for item in used_onboarding if _clean(item[0]))

        for lead_id in clicked_lead_ids:
            channel_id = lead_to_channel.get(lead_id)
            if channel_id and statuses[channel_id]["key"] not in {"customer", "signed_up"}:
                statuses[channel_id] = {"key": "clicked", "label": OUTREACH_LABELS["clicked"], "emailed_at": statuses[channel_id].get("emailed_at")}

    for row in rows:
        row["outreach"] = statuses[_clean(row.get("youtube_channel_id"))]


def default_pool(rows: list[dict[str, Any]], include_partial: bool = False) -> list[dict[str, Any]]:
    allowed = {"yes", "partial"} if include_partial else {"yes"}
    return [
        row
        for row in rows
        if _clean(row.get("in_niche")).lower() in allowed
        and _clean(row.get("presenter")).lower() == "real_person"
        and _language_is_english(row.get("language"))
    ]


def pool_kpis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    subscribers = [int(row["subscriber_count"]) for row in rows if row.get("subscriber_count") is not None]
    shorts = [int(row.get("shorts_90d") or 0) for row in rows]
    posting = sum(1 for value in shorts if value > 0)
    return {
        "channels": len(rows),
        "median_subscribers": int(median(subscribers)) if subscribers else 0,
        "shorts_posting_pct": round((posting / len(rows)) * 100) if rows else 0,
        "median_shorts_90d": round(float(median(shorts)), 1) if shorts else 0,
    }


def apply_table_filters(rows: list[dict[str, Any]], subscriber_band: str, outreach_status: str) -> list[dict[str, Any]]:
    def subscriber_matches(row: dict[str, Any]) -> bool:
        subscribers = int(row.get("subscriber_count") or 0)
        if subscriber_band == "1k_10k":
            return 1_000 <= subscribers < 10_000
        if subscriber_band == "10k_100k":
            return 10_000 <= subscribers < 100_000
        if subscriber_band == "100k_plus":
            return subscribers >= 100_000
        return True

    return [
        row
        for row in rows
        if subscriber_matches(row)
        and (not outreach_status or row.get("outreach", {}).get("key") == outreach_status)
    ]
