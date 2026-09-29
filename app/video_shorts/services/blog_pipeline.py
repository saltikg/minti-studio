from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any
from urllib import parse, request, robotparser
from xml.etree import ElementTree as ET

from app.video_shorts.services.db import get_db, get_db_readonly, table_columns


BLOG_TOPIC_STATUSES = (
    "candidate",
    "queued",
    "rejected_duplicate",
    "rejected_offtopic",
    "rejected_lowfit",
    "in_production",
    "draft_ready",
    "published",
    "failed",
)
ACTIVE_TOPIC_STATUSES = ("queued", "in_production", "draft_ready", "published")
BLOG_MONTHLY_BUDGET_USD = Decimal(os.getenv("BLOG_MONTHLY_BUDGET_USD", "15") or "15")
BLOG_JUDGE_MIN_SCORE = int(os.getenv("BLOG_JUDGE_MIN_SCORE", "70") or "70")
BLOG_SCOUT_USER_AGENT = os.getenv("BLOG_SCOUT_USER_AGENT", "MintiStudioBlogScout/1.0 (+https://mintistudio.com)")


def normalize_keyword(value: str | None) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text or None


def normalize_title(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9\s-]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def title_similarity(left: str | None, right: str | None) -> float:
    left_norm = normalize_title(left)
    right_norm = normalize_title(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def slug_to_title(url: str) -> str:
    path = parse.urlparse(url).path.strip("/").split("/")[-1]
    stem = re.sub(r"\.(html?|php|aspx?)$", "", path, flags=re.I)
    return re.sub(r"[-_]+", " ", stem).strip().title()


def fetch_url(url: str, *, timeout: int = 15, accept: str | None = None, max_bytes: int = 5_000_000) -> tuple[int | None, str, str, bytes]:
    headers = {
        "User-Agent": BLOG_SCOUT_USER_AGENT,
        "Accept": accept or "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5",
    }
    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.geturl(), resp.headers.get("content-type", ""), resp.read(max_bytes)
    except Exception as exc:
        return None, url, "", str(exc).encode("utf-8", "ignore")


def robots_allows(url: str) -> bool:
    parsed = parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
        return bool(rp.can_fetch(BLOG_SCOUT_USER_AGENT, url))
    except Exception:
        return False


@dataclass(frozen=True)
class BlogSource:
    name: str
    source_type: str
    url: str
    entry_kind: str
    enabled: bool = True
    id: int | None = None
    channel_id: str | None = None


BLOG_SOURCES: tuple[BlogSource, ...] = (
    BlogSource("OpusClip blog", "competitor_blog", "https://www.opus.pro/sitemap.xml", "sitemap"),
    BlogSource("Klap blog", "competitor_blog", "https://klap.app/sitemap.xml", "sitemap"),
    BlogSource("Vizard blog", "competitor_blog", "https://vizard.ai/blog/feed", "feed"),
    BlogSource("Descript blog", "competitor_blog", "https://www.descript.com/sitemap.xml", "sitemap", enabled=False),
    BlogSource("YouTube blog", "youtube_news", "https://blog.youtube/sitemap.xml", "sitemap"),
    BlogSource("Creator Insider RSS", "youtube_news", "", "feed", enabled=False),
)


def _entry_kind_for_source_type(source_type: str, url: str) -> str:
    if source_type == "youtube_channel":
        return "youtube_channel"
    lower = (url or "").lower()
    return "feed" if lower.endswith(".rss") or "/feed" in lower else "sitemap"


def _topic_source_type(fetch_type: str, name: str, url: str) -> str:
    if fetch_type == "youtube_channel":
        return "youtube_channel"
    haystack = f"{name} {url}".lower()
    if "blog.youtube" in haystack or "youtube" in haystack:
        return "youtube_news"
    return "competitor_blog"


def list_blog_sources(conn=None, *, enabled_only: bool = False) -> list[BlogSource]:
    close_conn = conn is None
    conn = conn or get_db_readonly()
    try:
        if not table_columns(conn, "blog_sources"):
            return list(BLOG_SOURCES)
        where = "WHERE enabled = true" if enabled_only else ""
        rows = conn.execute(
            f"""
            SELECT id, type, name, url, channel_id, enabled
            FROM blog_sources
            {where}
            ORDER BY id ASC
            """
        ).fetchall()
        sources: list[BlogSource] = []
        for row in rows:
            source_id, source_type, name, url, channel_id, enabled = row
            source_type = str(source_type or "")
            url = str(url or "")
            name = str(name or "")
            sources.append(
                BlogSource(
                    name=name,
                    source_type=_topic_source_type(source_type, name, url),
                    url=url,
                    entry_kind=_entry_kind_for_source_type(source_type, url),
                    enabled=bool(enabled),
                    id=int(source_id) if source_id is not None else None,
                    channel_id=str(channel_id or "") or None,
                )
            )
        return sources
    finally:
        if close_conn:
            conn.close()


def update_blog_source_check(conn, source: BlogSource, *, error: str | None) -> None:
    if not source.id or not table_columns(conn, "blog_sources"):
        return
    conn.execute(
        """
        UPDATE blog_sources
        SET last_checked_at = CURRENT_TIMESTAMP,
            last_error = ?
        WHERE id = ?
        """,
        [(error or "")[:500] if error else None, source.id],
    )


def admin_blog_sources() -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        if not table_columns(conn, "blog_sources"):
            return []
        rows = conn.execute(
            """
            SELECT id, type, name, url, channel_id, enabled, last_checked_at, last_error, created_at
            FROM blog_sources
            ORDER BY enabled DESC, type ASC, name ASC
            """
        ).fetchall()
        return [row_to_dict(conn.description, row) for row in rows]
    finally:
        conn.close()


def add_blog_source_from_form(form: Any) -> bool:
    source_type = str(form.get("type") or "").strip()
    name = str(form.get("name") or "").strip()
    url = str(form.get("url") or "").strip()
    channel_id = str(form.get("channel_id") or "").strip() or None
    if source_type not in {"rss", "youtube_channel"}:
        raise ValueError("Source type must be rss or youtube_channel.")
    if not name or not url:
        raise ValueError("Name and URL are required.")
    conn = get_db()
    try:
        if not table_columns(conn, "blog_sources"):
            raise RuntimeError("blog_sources table is missing.")
        row = conn.execute(
            """
            INSERT INTO blog_sources (type, name, url, channel_id, enabled)
            VALUES (?, ?, ?, ?, true)
            ON CONFLICT (url) DO UPDATE
            SET type = EXCLUDED.type,
                name = EXCLUDED.name,
                channel_id = EXCLUDED.channel_id,
                enabled = true
            RETURNING id
            """,
            [source_type, name, url, channel_id],
        ).fetchone()
        conn.commit()
        return bool(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_blog_source_enabled(source_id: int, enabled: bool) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            """
            UPDATE blog_sources
            SET enabled = ?
            WHERE id = ?
            RETURNING id
            """,
            [bool(enabled), source_id],
        ).fetchone()
        conn.commit()
        return bool(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def inject_dataimpulse_sessid(proxy_url: str, sessid: str | None = None) -> str:
    text = str(proxy_url or "").strip()
    if not text:
        return text
    sessid = sessid or secrets.token_hex(8)
    parsed = parse.urlsplit(text)
    if not parsed.username or "dataimpulse" not in parsed.netloc.lower():
        return text
    username = parse.unquote(parsed.username)
    if "sessid." not in username:
        username = f"{username};sessid.{sessid}"
    password = parse.unquote(parsed.password or "")
    auth = parse.quote(username, safe=";.")
    if password:
        auth = f"{auth}:{parse.quote(password, safe='')}"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{auth}@{host}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _parse_dt(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strip_html(value: str | None, limit: int = 400) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_feed_items(xml_bytes: bytes, *, max_items: int = 20) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items: list[dict[str, Any]] = []
    if root.tag.lower().endswith("rss"):
        nodes = root.findall(".//item")
        for node in nodes[:max_items]:
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            summary = node.findtext("description") or ""
            published = _parse_dt(node.findtext("pubDate"))
            if title and link:
                items.append({"title": title, "url": link, "summary": _strip_html(summary), "published_at": published})
        return items
    ns = {"atom": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
    for node in root.findall(".//atom:entry", ns)[:max_items]:
        title = (node.findtext("atom:title", default="", namespaces=ns) or "").strip()
        link = ""
        for link_node in node.findall("atom:link", ns):
            href = link_node.attrib.get("href")
            if href:
                link = href
                break
        summary = node.findtext("atom:summary", default="", namespaces=ns) or node.findtext("media:description", default="", namespaces=ns) or ""
        published = _parse_dt(node.findtext("atom:published", default="", namespaces=ns) or node.findtext("atom:updated", default="", namespaces=ns))
        if title and link:
            items.append({"title": title, "url": link, "summary": _strip_html(summary), "published_at": published})
    return items


def parse_sitemap_items(xml_bytes: bytes, *, source_url: str, max_items: int = 20) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items: list[dict[str, Any]] = []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []
    for node in root.findall(".//sm:url", ns) or root.findall(".//url"):
        loc = (node.findtext("sm:loc", default="", namespaces=ns) or node.findtext("loc") or "").strip()
        lastmod = _parse_dt(node.findtext("sm:lastmod", default="", namespaces=ns) or node.findtext("lastmod"))
        if loc:
            urls.append((loc, lastmod))
    if not urls:
        for node in root.findall(".//sm:sitemap", ns) or root.findall(".//sitemap"):
            loc = (node.findtext("sm:loc", default="", namespaces=ns) or node.findtext("loc") or "").strip()
            if loc and ("blog" in loc.lower() or "post" in loc.lower()):
                status, _, _, data = fetch_url(loc)
                if status and 200 <= status < 400:
                    return parse_sitemap_items(data, source_url=loc, max_items=max_items)
    urls.sort(key=lambda pair: pair[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for loc, lastmod in urls:
        lower = loc.lower()
        if any(skip in lower for skip in ("/tag/", "/author/", "/category/", "/page/")):
            continue
        if "blog" not in lower and "youtube" not in lower:
            continue
        title = slug_to_title(loc)
        if not title:
            continue
        items.append({"title": title, "url": loc, "summary": "", "published_at": lastmod})
        if len(items) >= max_items:
            break
    return items


def current_month_spend(conn=None) -> Decimal:
    close_conn = conn is None
    conn = conn or get_db_readonly()
    try:
        if not table_columns(conn, "blog_llm_usage"):
            return Decimal("0")
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM blog_llm_usage
            WHERE created_at >= date_trunc('month', CURRENT_TIMESTAMP)
            """
        ).fetchone()
        return Decimal(str(row[0] or "0"))
    finally:
        if close_conn:
            conn.close()


def list_existing_articles_and_topics(conn=None, *, exclude_topic_ids: set[int] | None = None) -> list[dict[str, str]]:
    close_conn = conn is None
    conn = conn or get_db_readonly()
    rows: list[dict[str, str]] = []
    try:
        if table_columns(conn, "blog_articles"):
            for title, slug in conn.execute("SELECT title, slug FROM blog_articles").fetchall():
                label = str(title or slug or "").strip()
                if label:
                    rows.append({"kind": "article", "title": label, "slug": str(slug or "")})
        if table_columns(conn, "blog_topics"):
            placeholders = ",".join(["?"] * len(ACTIVE_TOPIC_STATUSES))
            for topic_id, title, keyword in conn.execute(
                f"SELECT id, title, primary_keyword FROM blog_topics WHERE status IN ({placeholders})",
                list(ACTIVE_TOPIC_STATUSES),
            ).fetchall():
                if exclude_topic_ids and int(topic_id) in exclude_topic_ids:
                    continue
                label = str(title or keyword or "").strip()
                if label:
                    rows.append({"kind": "topic", "title": label, "slug": str(keyword or "")})
        return rows
    finally:
        if close_conn:
            conn.close()


def best_duplicate_match(title: str, existing: list[dict[str, str]], threshold: float = 0.85) -> str | None:
    normalized = normalize_title(title)
    best_score = 0.0
    best_label = None
    for row in existing:
        for candidate in (row.get("title"), row.get("slug")):
            cand_norm = normalize_title(candidate)
            if not cand_norm:
                continue
            score = SequenceMatcher(None, normalized, cand_norm).ratio()
            if score > best_score:
                best_score = score
                best_label = row.get("title") or candidate
    return best_label if best_score >= threshold else None


def insert_candidate(conn, *, source: BlogSource, item: dict[str, Any]) -> bool:
    source_url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    if not source_url or not title:
        return False
    columns = table_columns(conn, "blog_topics")
    source_summary = item.get("source_summary")
    if "source_summary" in columns:
        row = conn.execute(
            """
            INSERT INTO blog_topics (
                title, source_type, source_name, source_url, source_title, status, brief, source_summary
            )
            VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)
            ON CONFLICT (source_url) WHERE source_url IS NOT NULL DO NOTHING
            RETURNING id
            """,
            [
                title,
                source.source_type,
                source.name,
                source_url,
                title,
                str(item.get("summary") or "")[:400],
                source_summary if isinstance(source_summary, str) else (json_dumps_compact(source_summary) if source_summary else None),
            ],
        ).fetchone()
    else:
        row = conn.execute(
            """
            INSERT INTO blog_topics (
                title, source_type, source_name, source_url, source_title, status, brief
            )
            VALUES (?, ?, ?, ?, ?, 'candidate', ?)
            ON CONFLICT (source_url) WHERE source_url IS NOT NULL DO NOTHING
            RETURNING id
            """,
            [title, source.source_type, source.name, source_url, title, str(item.get("summary") or "")[:400]],
        ).fetchone()
    return bool(row)


def seed_topics() -> dict[str, list[str]]:
    seeds = [
        (95, "done-for-you vs DIY video clipping", "comparison", "commercial"),
        (90, "best OpusClip alternatives", "comparison", "commercial"),
        (80, "Shorts for consultants", "persona", "commercial"),
        (75, "sell your course with YouTube Shorts", "monetization", "commercial"),
        (70, "first sponsorship for small creators", "monetization", "informational"),
        (65, "Shorts for sermons and faith creators", "persona", "informational"),
        (60, "Shorts for webinar and interview hosts", "persona", "informational"),
        (55, "why your Shorts get no views", "craft", "informational"),
        (50, "Shorts vs Reels vs TikTok cross-posting", "craft", "informational"),
    ]
    inserted: list[str] = []
    skipped: list[str] = []
    conn = get_db()
    try:
        existing = list_existing_articles_and_topics(conn)
        for score, title, category, intent in seeds:
            dup = best_duplicate_match(title, existing)
            if dup:
                skipped.append(f"{title} -> {dup}")
                continue
            keyword = normalize_keyword(title)
            row = conn.execute(
                """
                INSERT INTO blog_topics (
                    title, primary_keyword, category, intent, source_type, source_name,
                    fit_score, status, judge_reason
                )
                VALUES (?, ?, ?, ?, 'seed', 'Seed', ?, 'queued', 'manual seed')
                ON CONFLICT (primary_keyword)
                    WHERE status IN ('queued', 'in_production', 'draft_ready', 'published')
                      AND primary_keyword IS NOT NULL
                    DO NOTHING
                RETURNING id
                """,
                [title, keyword, category, intent, score],
            ).fetchone()
            if row:
                inserted.append(title)
                existing.append({"kind": "topic", "title": title, "slug": keyword or ""})
            else:
                skipped.append(f"{title} -> existing active keyword")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"inserted": inserted, "skipped": skipped}


def row_to_dict(description, row) -> dict[str, Any]:
    return {description[index][0]: value for index, value in enumerate(row)}


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def admin_blog_topics(status: str | None = None) -> list[dict[str, Any]]:
    conn = get_db_readonly()
    try:
        columns = table_columns(conn, "blog_topics")
        fit_breakdown_sql = "fit_breakdown" if "fit_breakdown" in columns else "NULL AS fit_breakdown"
        source_summary_sql = "source_summary" if "source_summary" in columns else "NULL AS source_summary"
        adapted_from_sql = "adapted_from" if "adapted_from" in columns else "NULL AS adapted_from"
        where = ""
        params: list[Any] = []
        if status and status in BLOG_TOPIC_STATUSES:
            where = "WHERE status = ?"
            params.append(status)
        rows = conn.execute(
            f"""
            SELECT id, created_at, title, source_title, source_name, source_url,
                   source_type, fit_score, status, judge_reason, brief, primary_keyword,
                   {fit_breakdown_sql}, {source_summary_sql}, {adapted_from_sql}
            FROM blog_topics
            {where}
            ORDER BY
                CASE status
                    WHEN 'queued' THEN 0
                    WHEN 'candidate' THEN 1
                    WHEN 'in_production' THEN 2
                    WHEN 'draft_ready' THEN 3
                    WHEN 'published' THEN 4
                    ELSE 5
                END,
                fit_score DESC NULLS LAST,
                created_at DESC
            LIMIT 300
            """,
            params,
        ).fetchall()
        return [row_to_dict(conn.description, row) for row in rows]
    finally:
        conn.close()


def blog_topics_header_stats() -> dict[str, Any]:
    conn = get_db_readonly()
    try:
        spend = current_month_spend(conn)
        row = None
        if table_columns(conn, "blog_topics"):
            row = conn.execute(
                """
                SELECT MAX(created_at), COUNT(*),
                       SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END)
                FROM blog_topics
                WHERE source_type IN ('competitor_blog', 'youtube_news', 'youtube_channel')
                  AND created_at >= CURRENT_TIMESTAMP - INTERVAL '2 hours'
                """
            ).fetchone()
        return {
            "last_scout_run": row[0] if row else None,
            "recent_candidates": int(row[1] or 0) if row else 0,
            "recent_accepted": int(row[2] or 0) if row else 0,
            "monthly_spend": spend,
            "monthly_budget": BLOG_MONTHLY_BUDGET_USD,
        }
    finally:
        conn.close()


def update_blog_topic_from_form(topic_id: int, form: Any) -> bool:
    title = str(form.get("title") or "").strip()
    brief = str(form.get("brief") or "").strip()
    status = str(form.get("status") or "").strip()
    if status == "rejected":
        status = "rejected_lowfit"
    if status not in {"queued", "rejected_lowfit", "rejected_duplicate", "rejected_offtopic"}:
        status = "queued"
    fit_raw = str(form.get("fit_score") or "").strip()
    fit_score = None
    if fit_raw:
        fit_score = max(0, min(100, int(fit_raw)))
    conn = get_db()
    try:
        row = conn.execute(
            """
            UPDATE blog_topics
            SET title = COALESCE(NULLIF(?, ''), title),
                brief = ?,
                status = ?,
                fit_score = COALESCE(?, fit_score),
                judge_reason = CASE
                    WHEN status <> ? THEN 'manual override'
                    ELSE judge_reason
                END
            WHERE id = ?
            RETURNING id
            """,
            [title, brief, status, fit_score, status, topic_id],
        ).fetchone()
        conn.commit()
        return bool(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
