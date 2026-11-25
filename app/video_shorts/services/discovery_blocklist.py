from __future__ import annotations

import os
import re
from typing import Any, Iterable


DEFAULT_DISCOVERY_BLOCKLIST = (
    "textile",
    "upcycling",
    "sewing",
    "craft",
    "arts and crafts",
    "birdwatching",
    "wildlife",
    "journaling",
    "beekeeping",
    "woodworking",
    "cooking",
    "gardening",
    "ai tools",
)


def discovery_blocklist() -> tuple[str, ...]:
    configured = str(os.getenv("DISCOVERY_BLOCKLIST") or "").strip()
    values = re.split(r"[,;\n]+", configured) if configured else DEFAULT_DISCOVERY_BLOCKLIST
    normalized: list[str] = []
    seen = set()
    for value in values:
        clean = " ".join(str(value or "").strip().lower().split())
        if clean and clean not in seen:
            seen.add(clean)
            normalized.append(clean)
    return tuple(normalized)


def discovery_blocklist_prompt_text() -> str:
    return ", ".join(discovery_blocklist())


def discovery_text_is_blocked(*values: Any) -> bool:
    haystack = " ".join(str(value or "").lower() for value in values)
    haystack = " ".join(re.sub(r"[^a-z0-9]+", " ", haystack).split())
    padded = f" {haystack} "
    for term in discovery_blocklist():
        normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", term).split())
        if normalized and f" {normalized} " in padded:
            return True
        # The configured singular "craft" intentionally covers craft/crafts/crafting.
        if normalized == "craft" and re.search(r"\bcraft(?:s|ing)?\b", haystack):
            return True
    return False


def discovery_blocklist_sql(expressions: Iterable[str]) -> str:
    """Return a safe SQL predicate that is true when any expression is blocked."""
    checks: list[str] = []
    for expression in expressions:
        clean_expression = str(expression or "").strip()
        if not clean_expression:
            continue
        for term in discovery_blocklist():
            escaped = term.replace("'", "''").replace("%", "%%")
            checks.append(f"lower(COALESCE({clean_expression}, '')) LIKE '%%{escaped}%%'")
        checks.append(f"lower(COALESCE({clean_expression}, '')) LIKE '%%craft%%'")
    return "(" + " OR ".join(checks) + ")" if checks else "FALSE"


def discovery_lead_blocked_sql(alias: str = "discovery_leads") -> str:
    return f"{alias}.is_blocklisted IS TRUE"


def refresh_discovery_lead_categories(conn) -> dict[str, int]:
    """Materialize keyword category and blocklist eligibility on discovery leads."""
    from app.video_shorts.services.db import table_columns

    lead_columns = table_columns(conn, "discovery_leads")
    if not {"category", "is_blocklisted"}.issubset(lead_columns):
        return {"lead_categories_updated": 0, "leads_blocklisted": 0}

    category_result = conn.execute(
        """
        UPDATE discovery_leads AS d
        SET category = COALESCE(
            NULLIF((
                SELECT dc.name
                FROM keyword_queue kq
                JOIN discovery_categories dc ON dc.id = kq.category_id
                WHERE lower(kq.keyword) = lower(COALESCE(d.matched_keyword, ''))
                ORDER BY kq.updated_at DESC NULLS LAST, kq.id DESC
                LIMIT 1
            ), ''),
            NULLIF(d.category, ''),
            NULLIF(d.raw_json->>'category', ''),
            NULLIF(d.raw_json->>'niche', ''),
            ''
        )
        """
    )
    blocked_predicate = discovery_blocklist_sql(["d.matched_keyword", "d.category"])
    conn.execute(
        f"""
        UPDATE discovery_leads AS d
        SET is_blocklisted = {blocked_predicate}
        WHERE is_blocklisted IS DISTINCT FROM ({blocked_predicate})
        """
    )
    blocked_row = conn.execute(
        "SELECT COUNT(*) FROM discovery_leads WHERE is_blocklisted IS TRUE"
    ).fetchone()
    return {
        "lead_categories_updated": max(0, int(getattr(category_result, "rowcount", 0) or 0)),
        "leads_blocklisted": int((blocked_row or [0])[0] or 0),
    }


def apply_discovery_blocklist(conn) -> dict[str, int]:
    """Mark matching categories/queued terms excluded without deleting history."""
    category_predicate = discovery_blocklist_sql(["name"])
    keyword_predicate = discovery_blocklist_sql(["q.keyword", "c.name"])
    category_result = conn.execute(
        f"""
        UPDATE discovery_categories
        SET status = 'excluded', decided_at = COALESCE(decided_at, CURRENT_TIMESTAMP)
        WHERE {category_predicate}
          AND COALESCE(status, '') <> 'excluded'
        """
    )
    keyword_result = conn.execute(
        f"""
        UPDATE keyword_queue AS q
        SET status = 'excluded', updated_at = CURRENT_TIMESTAMP
        FROM discovery_categories AS c
        WHERE c.id = q.category_id
          AND q.status = 'queued'
          AND {keyword_predicate}
        """
    )
    uncategorized_result = conn.execute(
        f"""
        UPDATE keyword_queue AS q
        SET status = 'excluded', updated_at = CURRENT_TIMESTAMP
        WHERE q.status = 'queued'
          AND q.category_id IS NULL
          AND {discovery_blocklist_sql(['q.keyword'])}
        """
    )
    lead_result = refresh_discovery_lead_categories(conn)
    return {
        "categories_excluded": max(0, int(getattr(category_result, "rowcount", 0) or 0)),
        "keywords_excluded": max(0, int(getattr(keyword_result, "rowcount", 0) or 0))
        + max(0, int(getattr(uncategorized_result, "rowcount", 0) or 0)),
        **lead_result,
    }
