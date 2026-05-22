import os
import re
import json
import traceback
from datetime import date, datetime
from flask import current_app
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from .rag_seasons_library import EVERGREEN_RULES, CANONICAL_FAMILIES
from app.db import connect_ro

# ---------- helpers ----------
def _kebab(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s

def _lim(arr, n):
    return (arr or [])[:n]

def _today_iso():
    return date.today().isoformat()

def _iso(d):
    try:
        return datetime.fromisoformat(d).date().isoformat()
    except Exception:
        return None

def _parse_json_safe(raw: str):
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
        return data if isinstance(data, list) else []
    except Exception:
        m = re.search(r"(\[[\s\S]+?\])", s)
        if m:
            try:
                data = json.loads(m.group(1))
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []


def _families_from_db():
    con = connect_ro()
    rows = con.execute("""
        SELECT DISTINCT
               substring(lower(season_name) FROM '^([a-z0-9-]+)-\\d{4}$') AS fam
        FROM seasons
        WHERE season_name ~ '-\\d{4}$'
    """).fetchall()
    con.close()
    return sorted({r[0] for r in rows if r and r[0]})




def _existing_all_and_active(locale="US"):
    con = connect_ro()
    rows_all = con.execute("SELECT lower(season_name) FROM seasons").fetchall()
    rows_active = con.execute("""
        SELECT lower(season_name)
        FROM seasons
        WHERE start_date IS NOT NULL AND end_date IS NOT NULL
          AND start_date <= current_date AND end_date >= current_date
          AND (locale IS NULL OR locale = ?)
    """, [locale]).fetchall()
    con.close()
    existing_all = sorted({r[0] for r in rows_all})
    active_now = sorted({r[0] for r in rows_active})
    return existing_all, active_now


def _db_minisummary_for_families(families):
    if not families:
        return ""

    def _to_list(v):
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            try:
                data = json.loads(v)
                if isinstance(data, list):
                    return [str(x) for x in data]
            except Exception:
                return [v]
        return [str(v)]

    con = connect_ro()

    # DİKKAT: \d{4} içeren yerlerde çift süslü parantez kullandık
    base_sql = """
        WITH base AS (
          SELECT
            substring(lower(season_name) FROM '^([a-z0-9-]+)-\\d{{4}}$') AS fam,
            seeds_json,
            theme_tokens,
            type_tokens
          FROM seasons
          WHERE season_name ~ '-\\d{{4}}$'
        )
        SELECT fam, seeds_json, theme_tokens, type_tokens
        FROM base
        WHERE fam IN ({placeholders})
        LIMIT 300
    """
    placeholders = ",".join(["?"] * len(families))
    q = base_sql.replace("{placeholders}", placeholders)

    rows = con.execute(q, families).fetchall()
    con.close()

    blocks = []
    for fam, seeds, themes, types in rows:
        seeds_l  = _to_list(seeds)[:6]
        themes_l = _to_list(themes)[:4]
        types_l  = _to_list(types)[:3]
        blocks.append(
            f"name: {fam or ''}\n"
            f"seeds: {', '.join(seeds_l)}\n"
            f"themes: {', '.join(themes_l)}\n"
            f"types: {', '.join(types_l)}\n"
        )
    return "\n---\n".join(blocks)



# ---------- prompt ----------
PROMPT_DB_FIRST = ChatPromptTemplate.from_template(
"""
You are an ecommerce season planner.

Today is {today}.
Locale is {locale}.

Allowed season families:
{allowed_families}

Optional DB mini summaries for some families:
{db_context}

Already in DB (names with year, kebab-case):
{existing_all}

Already active today in DB (subset):
{active_now}

Task:
Suggest evergreen seasons that are ACTIVE today, or START within the next {window_days} days, but NOT in "active_now".
Use only the families listed in "Allowed season families". Do not invent new family names.

For each suggestion:
- Use the chosen family and add the current year suffix to form season_name
- Compute the exact active window for THIS YEAR as ISO dates (yyyy-mm-dd) inclusive
- Keep lists short: seeds max 6, theme max 4, type max 3
- Provide a brief reason citing calendar or retail behavior

Output strictly as a JSON array, no prose, no code fences.
Item schema:
{{

  "season_name": "<kebab-case>-<year>",
  "season_group": "seasonal",
  "start_date": "yyyy-mm-dd",
  "end_date": "yyyy-mm-dd",
  "seeds_json": ["..."],
  "theme_tokens": ["..."],
  "type_tokens": ["..."],
  "reason": "..."
}}
Limit to at most 10 items. If nothing is active today, return items that start within {window_days} days.
"""
)



# ---------- main ----------
def research_seasons_lite(locale="US", window_days=14):

    today = _today_iso()

    # 1) İzinli aile seti DB’den
    db_families = _families_from_db()

    # Birleşim: DB ∪ Kanonik
    families = sorted({*db_families, *CANONICAL_FAMILIES})

    # DB boşsa fallback olarak EVERGREEN_RULES aileleri
    if not families:
        families = [r.get("name","") for r in EVERGREEN_RULES if r.get("name")]
        current_app.logger.info("[LITE] DB has no families, falling back to EVERGREEN_RULES")

    # 2) DB context kisa özet
    db_ctx = _db_minisummary_for_families(db_families)  # yalnız DB için mini özet

    # 3) Var olanlar ve bugün aktif olanlar
    existing_all, active_now = _existing_all_and_active(locale=locale)


    # 4) LLM çağrısı
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.0, timeout=30)
    chain = PROMPT_DB_FIRST | llm | StrOutputParser()
    try:
        raw = chain.invoke({
            "today": today,
            "locale": locale,
            "allowed_families": "\n".join(families),
            "db_context": db_ctx or "(none)",
            "existing_all": "\n".join(existing_all),
            "active_now": "\n".join(active_now),
            "window_days": window_days
        })
        current_app.logger.info("[LITE/DB-FIRST] raw_first=%s", (raw or "")[:300].replace("\n", " "))
    except Exception as e:
        current_app.logger.error("[LITE/DB-FIRST] LLM error -> []: %s\n%s", e, traceback.format_exc())
        raw = "[]"

    items = _parse_json_safe(raw)

    # 5) Normalize, doğrula, dup at
    out = []
    seen = set(existing_all)
    for c in items:
        name = _kebab(c.get("season_name",""))
        if not name or name in seen:
            continue
        sd = _iso(c.get("start_date",""))
        ed = _iso(c.get("end_date",""))
        if not sd or not ed:
            continue
        out.append({
            "season_name": name,
            "season_group": c.get("season_group","seasonal"),
            "start_date": sd,
            "end_date": ed,
            "seeds_json": _lim(c.get("seeds_json"), 6),
            "theme_tokens": _lim(c.get("theme_tokens"), 4),
            "type_tokens": _lim(c.get("type_tokens"), 3),
            "reason": c.get("reason",""),
            "locale": locale
        })
        seen.add(name)

    current_app.logger.info("[LITE/DB-FIRST] candidates=%d", len(out))
    return out
