import json
from typing import Dict, List

from app.video_shorts.config import OPENAI_MODEL, _openai_client
from app.video_shorts.services.db import get_db, get_db_readonly


DEFAULT_COMMENT_MODERATION_PROMPT = (
    "You are a strict safety moderator for social media comments. "
    "Flag content that is hateful, harassing, threatening, or demeaning. "
    "Pay special attention to insults, slurs, and death wishes in Turkish and other languages, even with misspellings or spacing. "
    "Flag explicit threats of violence or death, and statements wishing harm. "
    "Flag sexual content, violence, self-harm, illegal activities, and explicit harassment. "
    "Consider emoji-only and emoji-heavy messages; flag if they imply abuse, sexual content, violence, "
    "drugs, or self-harm. "
    "Return JSON only: an array of objects with id, flagged (true/false), reason (short)."
)


def _prompt_key(user_id: str) -> str:
    return f"{user_id}:comment_moderation_system_prompt"


def _load_moderation_prompt(user_id: str) -> str:
    if not user_id:
        user_id = "default"
    conn = None
    try:
        conn = get_db_readonly()
        row = conn.execute(
            """
            SELECT value
            FROM shorts_prompt_settings
            WHERE key = ?
            """
            ,
            [_prompt_key(user_id)],
        ).fetchone()
    except Exception:
        return DEFAULT_COMMENT_MODERATION_PROMPT
    finally:
        if conn:
            conn.close()
    if not row or not row[0]:
        try:
            write_conn = get_db()
            write_conn.execute(
                """
                INSERT INTO shorts_prompt_settings (key, value, updated_by, updated_at)
                VALUES (?, ?, ?, now())
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                [_prompt_key(user_id), DEFAULT_COMMENT_MODERATION_PROMPT, user_id],
            )
            write_conn.commit()
        except Exception:
            pass
        finally:
            try:
                write_conn.close()
            except Exception:
                pass
        return DEFAULT_COMMENT_MODERATION_PROMPT
    return str(row[0]).strip() or DEFAULT_COMMENT_MODERATION_PROMPT


def _keyword_flagged(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    keywords = [
        "fuck",
        "shit",
        "bitch",
        "asshole",
        "bastard",
        "dick",
        "pussy",
        "nude",
        "porn",
        "rape",
        "kill",
        "die",
        "suicide",
        "terror",
        "terrorist",
        "drug",
        "cocaine",
        "heroin",
        "meth",
        "amk",
        "aq",
        "orospu",
        "siktir",
        "yarak",
        "pisc",
        "ibne",
        "kafir",
        "cehennem",
        "geber",
        "serefsiz",
        "namussuz",
        "hain",
        "kahpe",
        "kahbe",
        "ajan",
        "oldurecegim",
        "oldurucem",
        "oldurecem",
        "seni bulup oldurecegim",
    ]
    return any(keyword in lowered for keyword in keywords)


def moderate_text_entries(
    entries: List[Dict[str, str]],
    user_id: str,
) -> Dict[str, Dict[str, object]]:
    if not entries:
        return {}
    if not user_id:
        user_id = "default"
    if not _openai_client:
        return {
            entry["id"]: {
                "flagged": _keyword_flagged(entry["text"]),
                "reason": "keyword",
            }
            for entry in entries
            if entry.get("id") is not None
        }
    try:
        payload = json.dumps(entries, ensure_ascii=True)
        system_prompt = _load_moderation_prompt(user_id)
        response = _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": f"Moderate these comments and return JSON only:\n{payload}",
                },
            ],
            max_tokens=500,
        )
        content = (response.choices[0].message.content or "").strip()
        results = json.loads(content)
        if isinstance(results, list):
            return {
                str(item.get("id")): {
                    "flagged": bool(item.get("flagged")),
                    "reason": item.get("reason") or "ai",
                }
                for item in results
                if item.get("id") is not None
            }
    except Exception:
        pass
    return {
        entry["id"]: {
            "flagged": _keyword_flagged(entry["text"]),
            "reason": "keyword",
        }
        for entry in entries
        if entry.get("id") is not None
    }
