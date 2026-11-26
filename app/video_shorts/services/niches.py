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
NICHE_ON_TOPIC_MIN = 0.33
NICHE_CLINICIAN_CREDENTIALS = {"MD_DO", "NP", "PA", "ND"}
NICHE_LANGUAGE_LABELS = {
    "en": {"en", "english"},
    "tr": {"tr", "turkish", "türkçe", "turkce"},
}
# Legacy research scripts still import this while their historical reports remain reproducible.
NICHE_AUDIENCE_COUNTRIES = {"", "US", "CA", "GB", "AU", "NZ", "IE"}


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
    columns = table_columns(conn, "niches")
    optional = [name for name in (
        "language", "target_focus", "focus_options", "status",
        "channel_type_options", "allowed_channel_types",
    ) if name in columns]
    return _dict_rows(conn.execute(
        f"SELECT id, slug, name, category, created_at{''.join(', ' + name for name in optional)} "
        "FROM niches ORDER BY name ASC"
    ))


def load_niche_snapshot(conn, niche_slug: str, snapshot_date: str = "") -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    niche_columns = table_columns(conn, "niches")
    optional = [
        name for name in (
            "description", "core_topics", "adjacent_topics", "excluded_topics",
            "definition_version", "language", "target_focus", "focus_options", "status",
            "search_queries",
            "channel_type_options", "allowed_channel_types",
        ) if name in niche_columns
    ]
    result = conn.execute(
        f"SELECT id, slug, name, category{''.join(', ' + name for name in optional)} "
        "FROM niches WHERE slug = ? LIMIT 1",
        [niche_slug],
    )
    niche_rows = _dict_rows(result)
    niche = niche_rows[0] if niche_rows else None
    if not niche:
        return None, []
    selected = {
        **niche,
        "description": str(niche.get("description") or ""),
        "core_topics": niche.get("core_topics") or [],
        "adjacent_topics": niche.get("adjacent_topics") or [],
        "excluded_topics": niche.get("excluded_topics") or [],
        "definition_version": int(niche.get("definition_version") or 1),
        "language": _clean(niche.get("language")) or "en",
        "target_focus": _clean(niche.get("target_focus")) or "hormones_midlife_health",
        "focus_options": niche.get("focus_options") or [],
        "search_queries": niche.get("search_queries") or [],
        "channel_type_options": niche.get("channel_type_options"),
        "allowed_channel_types": niche.get("allowed_channel_types"),
        "status": _clean(niche.get("status")) or "active",
        "snapshot_date": "",
    }
    requested_date = _clean(snapshot_date)
    latest = conn.execute(
        "SELECT MAX(snapshot_date) FROM niche_channels WHERE niche_id = ?",
        [niche["id"]],
    ).fetchone()
    latest_date = str(latest[0]) if latest and latest[0] else ""
    selected_date = requested_date
    if requested_date:
        exists = conn.execute(
            """
            SELECT 1 FROM niche_channels
            WHERE niche_id = ? AND snapshot_date = ?
            LIMIT 1
            """,
            [niche["id"], requested_date],
        ).fetchone()
        if not exists:
            selected_date = latest_date
    else:
        selected_date = latest_date
    if not selected_date:
        return selected, []

    channel_type_column = (
        ", channel_type" if "channel_type" in table_columns(conn, "niche_channels") else ""
    )
    result = conn.execute(
        f"""
        SELECT youtube_channel_id, snapshot_date, title, channel_url, credential,
               in_niche, presenter, language, subscriber_count, longform_90d,
               shorts_90d, avg_views_long_90d, avg_views_short_90d,
               discovery_lead_id, source, replied, admin_note, updated_at,
               primary_niche, on_topic_share, on_topic_count, entity,
               reclass_reason, views_90d, admin_exclude, admin_include,
               exclude_reason, country, audience_language,
               relevance, relevance_reason, primary_focus, classification_status,
               classification_title_count{channel_type_column}
        FROM niche_channels
        WHERE niche_id = ? AND snapshot_date = ?
        ORDER BY subscriber_count DESC NULLS LAST, lower(title) ASC
        """,
        [niche["id"], selected_date],
    )
    rows = _dict_rows(result)
    selected["snapshot_date"] = selected_date
    return selected, rows


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


def _on_topic_share(row: dict[str, Any]) -> float:
    try:
        return float(row.get("on_topic_share") or 0)
    except (TypeError, ValueError):
        return 0.0


def _language_matches_niche(value: Any, niche_language: str) -> bool | None:
    label = _clean(value).casefold()
    if not label:
        return None
    expected = _clean(niche_language).casefold()
    return label in NICHE_LANGUAGE_LABELS.get(expected, {expected})


def is_base_niche_pool_row(row: dict[str, Any], niche: dict[str, Any] | None = None) -> bool:
    target_focus = _clean((niche or {}).get("target_focus")) or "hormones_midlife_health"
    niche_language = _clean((niche or {}).get("language")) or "en"
    topic_match = (
        _clean(row.get("classification_status")).casefold() == "classified"
        and _clean(row.get("primary_focus")).casefold() == target_focus.casefold()
    )
    language_match = _language_matches_niche(row.get("language"), niche_language)
    allowed_channel_types = (niche or {}).get("allowed_channel_types")
    channel_type_match = (
        True if allowed_channel_types is None
        else _clean(row.get("channel_type")).casefold()
        in {_clean(value).casefold() for value in allowed_channel_types}
    )
    return (
        topic_match
        and _clean(row.get("entity")).lower() == "individual"
        and _clean(row.get("presenter")).lower() == "real_person"
        and language_match is True
        and channel_type_match
    )


def is_final_niche_pool_row(row: dict[str, Any], niche: dict[str, Any] | None = None) -> bool:
    return not bool(row.get("admin_exclude")) and (
        bool(row.get("admin_include")) or is_base_niche_pool_row(row, niche)
    )


def clinician_matches(row: dict[str, Any]) -> bool:
    return _clean(row.get("credential")) in NICHE_CLINICIAN_CREDENTIALS


def final_niche_pool(rows: list[dict[str, Any]], *, clinicians_only: bool, niche: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if is_final_niche_pool_row(row, niche) and (not clinicians_only or clinician_matches(row))
    ]


def excluded_niche_rows(rows: list[dict[str, Any]], *, clinicians_only: bool) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if bool(row.get("admin_exclude")) and (not clinicians_only or clinician_matches(row))
    ]


def near_miss_niche_rows(rows: list[dict[str, Any]], *, clinicians_only: bool, niche: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    target_focus = _clean((niche or {}).get("target_focus")) or "hormones_midlife_health"
    return [
        row
        for row in rows
        if not is_final_niche_pool_row(row, niche)
        and not bool(row.get("admin_exclude"))
        and not bool(row.get("admin_include"))
        and _clean(row.get("primary_focus")).lower() != target_focus.casefold()
        and _on_topic_share(row) >= NICHE_ON_TOPIC_MIN
        and (not clinicians_only or clinician_matches(row))
    ]


def load_niche_run_changes(conn, niche_id: Any) -> dict[str, Any]:
    if not table_columns(conn, "niche_classification_runs") or not table_columns(
        conn, "niche_channel_run_results"
    ):
        return {"current": None, "previous": None, "joined": [], "left": []}
    runs = _dict_rows(
        conn.execute(
            """
            SELECT run_id, run_at, rule_version, notes
            FROM niche_classification_runs
            WHERE niche_id = ?
            ORDER BY run_at DESC, run_id DESC
            LIMIT 2
            """,
            [niche_id],
        )
    )
    if not runs:
        return {"current": None, "previous": None, "joined": [], "left": []}
    current = runs[0]
    previous = runs[1] if len(runs) > 1 else None
    if previous is None:
        return {"current": current, "previous": None, "joined": [], "left": []}
    changed = _dict_rows(
        conn.execute(
            """
            WITH current_run AS (
                SELECT youtube_channel_id, in_pool
                FROM niche_channel_run_results
                WHERE run_id = ?
            ), previous_run AS (
                SELECT youtube_channel_id, in_pool
                FROM niche_channel_run_results
                WHERE run_id = ?
            )
            SELECT COALESCE(c.youtube_channel_id, p.youtube_channel_id) AS youtube_channel_id,
                   COALESCE(c.in_pool, FALSE) AS current_in_pool,
                   COALESCE(p.in_pool, FALSE) AS previous_in_pool,
                   nc.title, nc.channel_url
            FROM current_run c
            FULL OUTER JOIN previous_run p USING (youtube_channel_id)
            LEFT JOIN niche_channels nc
              ON nc.niche_id = ?
             AND nc.youtube_channel_id = COALESCE(c.youtube_channel_id, p.youtube_channel_id)
             AND nc.snapshot_date = (
                 SELECT MAX(snapshot_date) FROM niche_channels WHERE niche_id = ?
             )
            WHERE COALESCE(c.in_pool, FALSE) <> COALESCE(p.in_pool, FALSE)
            ORDER BY lower(COALESCE(nc.title, COALESCE(c.youtube_channel_id, p.youtube_channel_id)))
            """,
            [current["run_id"], previous["run_id"], niche_id, niche_id],
        )
    )
    return {
        "current": current,
        "previous": previous,
        "joined": [row for row in changed if row["current_in_pool"]],
        "left": [row for row in changed if row["previous_in_pool"]],
    }


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
