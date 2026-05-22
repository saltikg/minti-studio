# app/admin/youtube_trend_agent.py

import json
from typing import Any

from langchain.tools import tool
from langchain_openai import ChatOpenAI

from app.db import connect_ro


def _get_db(read_only: bool = True):
    return connect_ro()


@tool("search_past_ideas", return_direct=False)
def search_past_ideas(query: str, limit: int = 20) -> str:
    """
    Search previously generated youtube trend ideas by keyword.

    Returns a JSON list with fields: idea_text, channel_title, video_title, created_at.
    """
    q = (query or "").strip().lower()
    if not q:
        return json.dumps([])

    con = _get_db(True)
    rows = con.execute(
        """
        SELECT
          ti.idea_text,
          c.channel_title,
          v.video_title,
          ti.created_at
        FROM youtube_trend_ideas ti
        JOIN youtube_videos v ON ti.video_id = v.id
        JOIN youtube_channels c ON ti.channel_id = c.id
        WHERE lower(ti.idea_text) LIKE ?
        ORDER BY ti.created_at DESC
        LIMIT ?
        """,
        [f"%{q}%", limit],
    ).fetchall()
    cols = [d[0] for d in con.description]
    con.close()

    data = [dict(zip(cols, r)) for r in rows]
    return json.dumps(data, default=str)


def _build_llm():
    return ChatOpenAI(model="gpt-4o-mini", temperature=0.2)


def _parse_ideas(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, str):
        text = raw.strip()
    elif isinstance(raw, dict):
        text = json.dumps(raw)
    else:
        text = str(raw or "").strip()

    if not text:
        return []

    # Prefer a JSON array if present inside a markdown block or extra text.
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        payload = json.loads(text)
    except Exception:
        return []

    if not isinstance(payload, list):
        return []

    ideas: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        idea_text = (item.get("idea_text") or "").strip()
        if not idea_text:
            continue
        ideas.append(
            {
                "idea_text": idea_text,
                "reason": (item.get("reason") or "").strip(),
            }
        )
    return ideas[:5]


def run_youtube_trend_agent(video_meta: dict[str, Any], caption_text: str) -> list[dict[str, str]]:
    """
    Generate a few shopping-oriented trend ideas from a YouTube caption.

    This keeps the existing admin workflow working without relying on DuckDB.
    """
    text = (caption_text or "").strip()
    if not text:
        return []
    if len(text) > 8000:
        text = text[:8000]

    title = (video_meta.get("video_title") or "").strip()
    channel = (video_meta.get("channel_title") or "").strip()
    published_at = video_meta.get("published_at")

    related = []
    for token in [title, channel]:
        if token:
            related.extend(json.loads(search_past_ideas.invoke({"query": token, "limit": 5})))
    related_json = json.dumps(related[:10], default=str)

    prompt = f"""
You are a shopping trend analyst.
Read the YouTube transcript and extract up to 3 ecommerce- or affiliate-friendly content ideas.
Avoid duplicates or ideas that are too close to these prior ideas: {related_json}

Video metadata:
- Channel: {channel}
- Title: {title}
- Published at: {published_at}

Transcript:
{text}

Return ONLY valid JSON:
[
  {{
    "idea_text": "short concrete idea",
    "reason": "why it is commercially useful"
  }}
]
If nothing is relevant, return [].
"""

    llm = _build_llm()
    response = llm.invoke(prompt)
    return _parse_ideas(getattr(response, "content", response))
