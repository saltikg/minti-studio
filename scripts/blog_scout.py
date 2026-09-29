#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import parse
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_llm import BLOG_MODEL_REVIEWER, call_json, log_usage  # noqa: E402
from app.video_shorts.services.blog_pipeline import (  # noqa: E402
    BlogSource,
    fetch_url,
    inject_dataimpulse_sessid,
    insert_candidate,
    json_dumps_compact,
    list_blog_sources,
    parse_feed_items,
    parse_sitemap_items,
    robots_allows,
    slug_to_title,
    update_blog_source_check,
)
from app.video_shorts.services.db import get_db  # noqa: E402

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError:  # pragma: no cover
    YouTubeTranscriptApi = None


YOUTUBE_BLOG_KEYWORDS = (
    "shorts",
    "monetiz",
    "partner program",
    "ypp",
    "policy",
    "creator",
    "studio",
    "analytics",
    "made-on-youtube",
    "ads",
    "sponsorship",
    "shopping",
)
CREATOR_STORIES_KEYWORDS = ("shorts", "monetiz", "partner-program", "ypp", "policy", "creator", "studio", "analytics")
TRANSCRIPT_SUMMARY_SYSTEM_PROMPT = """Summarize YouTube creator-advice video captions for MintiStudio's blog topic judge.

Audience: solo educators, coaches, consultants, and podcasters who publish long-form video plus Shorts and have something to sell.
Return compact JSON only with:
{"video_summary":"1-2 sentences","creator_takeaways":["..."],"claims_to_verify":["..."],"mentioned_official_sources":["..."]}
Keep claims conservative. If the transcript is mostly product promo or unrelated, say so clearly.
"""


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self._in_title = False
        self.description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._in_title = True
            return
        if tag.lower() != "meta":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        name = values.get("name", "").lower()
        prop = values.get("property", "").lower()
        if name == "description" or prop == "og:description":
            self.description = values.get("content", "").strip()[:500]

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()


def _fresh_items(items: list[dict[str, Any]], *, days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    fresh = []
    for item in items:
        published_at = item.get("published_at")
        if published_at is not None and published_at < cutoff:
            continue
        fresh.append(item)
    return fresh[:limit]


def _parse_dt(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _keyword_hit(text: str, keywords: tuple[str, ...] = YOUTUBE_BLOG_KEYWORDS) -> bool:
    normalized = re.sub(r"[-_]+", " ", text.lower())
    return any(keyword in normalized for keyword in keywords)


def _youtube_blog_path_allowed(url: str) -> bool:
    path = parse.urlparse(url).path.lower()
    if path.startswith("/news-and-events/") or path.startswith("/inside-youtube/"):
        return True
    if path.startswith("/creator-and-artist-stories/"):
        return _keyword_hit(url, CREATOR_STORIES_KEYWORDS)
    return False


def _fetch_page_metadata(url: str) -> dict[str, str] | None:
    status, _, _, data = fetch_url(url, accept="text/html,application/xhtml+xml")
    if not status or status >= 400:
        return None
    parser = _MetaParser()
    parser.feed(data.decode("utf-8", "ignore"))
    title = parser.title or slug_to_title(url)
    title = re.sub(r"\s+-\s+YouTube Blog\s*$", "", title).strip()
    return {"title": title, "summary": parser.description}


def parse_youtube_blog_sitemap(xml_bytes: bytes, *, days: int = 30, max_items: int = 20) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    candidates: list[tuple[str, datetime | None]] = []
    for node in root.findall(".//sm:url", ns) or root.findall(".//url"):
        loc = (node.findtext("sm:loc", default="", namespaces=ns) or node.findtext("loc") or "").strip()
        lastmod = _parse_dt(node.findtext("sm:lastmod", default="", namespaces=ns) or node.findtext("lastmod"))
        if not loc or not _youtube_blog_path_allowed(loc):
            continue
        if lastmod is None or lastmod < cutoff:
            continue
        if not _keyword_hit(loc):
            continue
        candidates.append((loc, lastmod))
    candidates.sort(key=lambda pair: pair[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    items: list[dict[str, Any]] = []
    for loc, lastmod in candidates[: max_items * 2]:
        metadata = _fetch_page_metadata(loc)
        if not metadata:
            continue
        combined = f"{loc} {metadata['title']}"
        if not _keyword_hit(combined):
            continue
        items.append({"title": metadata["title"], "url": loc, "summary": metadata["summary"], "published_at": lastmod})
        if len(items) >= max_items:
            break
    return items


def _video_id_from_url(url: str) -> str | None:
    parsed = parse.urlparse(url)
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/") or None
    if parsed.path == "/watch":
        return parse.parse_qs(parsed.query).get("v", [None])[0]
    return None


def _youtube_channel_videos(source: BlogSource, *, limit: int = 8) -> list[dict[str, Any]]:
    if yt_dlp is None:
        raise RuntimeError("yt_dlp is not installed")
    opts = {
        "extract_flat": True,
        "quiet": True,
        "skip_download": True,
        "playlistend": limit,
        "ignoreerrors": True,
        "nocheckcertificate": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source.url, download=False)
    videos: list[dict[str, Any]] = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("url") or entry.get("webpage_url") or "").strip()
        video_id = str(entry.get("id") or "").strip()
        if video_id and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={video_id}"
        video_id = video_id or _video_id_from_url(url) or ""
        duration = entry.get("duration")
        if duration is not None:
            try:
                if int(duration) < 180:
                    continue
            except (TypeError, ValueError):
                pass
        timestamp = entry.get("timestamp") or entry.get("release_timestamp")
        published_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc) if timestamp else None
        if title and url:
            videos.append({"title": title, "url": url, "summary": "", "published_at": published_at, "video_id": video_id})
    return _fresh_items(videos, days=14, limit=5)


def _transcript_text(video_id: str, *, use_proxy: bool) -> str:
    if YouTubeTranscriptApi is None:
        raise RuntimeError("youtube_transcript_api is not installed")
    proxy_url = (os.getenv("INGEST_PROXY_URL") or "").strip()
    proxies = None
    if use_proxy:
        if not proxy_url:
            raise RuntimeError("INGEST_PROXY_URL is not configured")
        proxy_url = inject_dataimpulse_sessid(proxy_url)
        proxies = {"http": proxy_url, "https": proxy_url}
    try:
        api = YouTubeTranscriptApi(proxies=proxies) if proxies else YouTubeTranscriptApi()
    except TypeError:
        api = YouTubeTranscriptApi()
        if proxies:
            setattr(api, "proxies", proxies)
    fetched = api.fetch(video_id, languages=["en"])
    pieces = []
    for segment in fetched:
        text = str(getattr(segment, "text", "") or "").strip()
        if text:
            pieces.append(text)
    return re.sub(r"\s+", " ", " ".join(pieces)).strip()


def _summarize_transcript(video: dict[str, Any]) -> tuple[dict[str, Any], str]:
    video_id = str(video.get("video_id") or "")
    if not video_id:
        return {"transcript_status": "skipped", "transcript_error": "missing video id"}, "skipped"
    errors: list[str] = []
    for label, use_proxy in (("direct", False), ("proxy", True)):
        try:
            transcript = _transcript_text(video_id, use_proxy=use_proxy)
            if not transcript:
                raise RuntimeError("empty transcript")
            result = call_json(
                "transcript_summary",
                model=BLOG_MODEL_REVIEWER,
                system_prompt=TRANSCRIPT_SUMMARY_SYSTEM_PROMPT,
                user_prompt=json_dumps_compact({"video_title": video.get("title") or "", "video_url": video.get("url") or "", "transcript": transcript[:12000]}),
            )
            log_usage("transcript_summary", result)
            payload = json.loads(result.content)
            payload["transcript_status"] = label
            return payload, label
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            if label == "direct" and not (os.getenv("INGEST_PROXY_URL") or "").strip():
                break
    return {"transcript_status": "failed", "transcript_error": "; ".join(errors)[:500]}, "failed"


def _with_transcript_summaries(items: list[dict[str, Any]], stats: dict[str, int]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for item in items:
        summary, status = _summarize_transcript(item)
        stats[f"transcript_{status}"] = stats.get(f"transcript_{status}", 0) + 1
        item["source_summary"] = summary
        if summary.get("video_summary"):
            item["summary"] = str(summary["video_summary"])[:400]
        enriched.append(item)
    return enriched


def scout() -> dict[str, int | str]:
    counts: dict[str, int | str] = {}
    conn = get_db()
    try:
        for source in list_blog_sources(conn, enabled_only=True):
            if not source.enabled or not source.url:
                counts[source.name] = "disabled"
                continue
            if source.source_type != "youtube_channel" and not robots_allows(source.url):
                counts[source.name] = "disabled_robots"
                update_blog_source_check(conn, source, error="robots.txt disallows source URL")
                conn.commit()
                continue
            stats: dict[str, int] = {}
            try:
                if source.source_type == "youtube_channel":
                    items = _with_transcript_summaries(_youtube_channel_videos(source), stats)
                else:
                    status, _, _, data = fetch_url(source.url)
                    if not status or status >= 400:
                        raise RuntimeError("unreachable")
                    if source.source_type == "youtube_news" or "blog.youtube/sitemap.xml" in source.url:
                        items = parse_youtube_blog_sitemap(data)
                    elif source.entry_kind == "feed":
                        items = parse_feed_items(data)
                    else:
                        items = parse_sitemap_items(data, source_url=source.url)
                    items = _fresh_items(items)
            except Exception as exc:
                counts[source.name] = f"error:{exc}"
                update_blog_source_check(conn, source, error=str(exc))
                conn.commit()
                continue
            inserted = 0
            for item in items:
                if insert_candidate(conn, source=source, item=item):
                    inserted += 1
            update_blog_source_check(conn, source, error=None)
            conn.commit()
            suffix = f" {json.dumps(stats, sort_keys=True)}" if stats else ""
            counts[source.name] = f"{inserted}{suffix}"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return counts


def main() -> int:
    counts = scout()
    print("BLOG_SCOUT_DONE")
    for name, count in counts.items():
        print(f"{name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
