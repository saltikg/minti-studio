from __future__ import annotations

from typing import Optional, Tuple

from app.video_shorts.services.db import ensure_prompt_settings_schema, get_db, get_db_readonly

DEFAULT_DESCRIPTION_PROMPT = (
    "You are an expert multilingual YouTube description writer for long-form video content.\n\n"
    "Analyze the transcript below and write a description based on it. NEVER copy sentences from the transcript verbatim into the description.\n\n"
    "LANGUAGE INSTRUCTION: {language_instruction}\n\n"
    "TASK:\nFill in the TEMPLATE below completely and in order.\n\n"
    "RULES:\n"
    "- Output only the template, no extra headers.\n"
    "- Description: 2-3 paragraphs summarizing the key ideas and value of the video for the viewer, in your own words.\n"
    "- Hashtags: at least 10 relevant hashtags on a single line, derived from the video's topics.\n"
    "- Full Transcript: include the clip transcript exactly as provided.\n"
    "- If the transcript contains a date, use it naturally; otherwise do not mention a date.\n"
    "{date_note_line}\n\n"
    "{template_block}\n\n"
    "TRANSCRIPT\n{transcripts_source}"
)

DEFAULT_DESCRIPTION_LANGUAGES = "en"
DESCRIPTION_CONTEXT_PADDING_SECONDS = 60


def _normalize_user_id(user_id: Optional[str]) -> str:
    value = str(user_id or "").strip()
    return value or "default"


def _brand_scoped_key(base_key: str, brand_id: Optional[str]) -> str:
    scoped_brand_id = str(brand_id or "").strip()
    return f"{base_key}:brand:{scoped_brand_id}" if scoped_brand_id else base_key


def description_prompt_key(user_id: Optional[str], brand_id: Optional[str] = None) -> str:
    return _brand_scoped_key(f"{_normalize_user_id(user_id)}:description_prompt", brand_id)


def description_languages_key(user_id: Optional[str], brand_id: Optional[str] = None) -> str:
    return _brand_scoped_key(f"{_normalize_user_id(user_id)}:description_languages", brand_id)


def load_prompt_setting(key: str) -> Optional[str]:
    if not key:
        return None
    conn = get_db_readonly()
    try:
        ensure_prompt_settings_schema(conn)
        row = conn.execute(
            "SELECT value FROM shorts_prompt_settings WHERE key = ?",
            [key],
        ).fetchone()
        return str(row[0]).strip() if row and row[0] else None
    finally:
        conn.close()


def save_prompt_setting(key: str, value: str, updated_by: Optional[str]) -> None:
    if not key:
        return
    conn = get_db()
    try:
        ensure_prompt_settings_schema(conn)
        conn.execute(
            """
            INSERT INTO shorts_prompt_settings (key, value, updated_by, updated_at)
            VALUES (?, ?, ?, now())
            ON CONFLICT (key) DO UPDATE SET
                value = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            [key, value, updated_by],
        )
        conn.commit()
    finally:
        conn.close()


def delete_prompt_setting(key: str) -> None:
    if not key:
        return
    conn = get_db()
    try:
        ensure_prompt_settings_schema(conn)
        conn.execute("DELETE FROM shorts_prompt_settings WHERE key = ?", [key])
        conn.commit()
    finally:
        conn.close()


def load_description_prompt(user_id: Optional[str], brand_id: Optional[str] = None) -> Optional[str]:
    return load_prompt_setting(description_prompt_key(user_id, brand_id))


def save_description_prompt(user_id: Optional[str], brand_id: Optional[str], value: str, updated_by: Optional[str]) -> None:
    save_prompt_setting(description_prompt_key(user_id, brand_id), value, updated_by)


def delete_description_prompt(user_id: Optional[str], brand_id: Optional[str]) -> None:
    delete_prompt_setting(description_prompt_key(user_id, brand_id))


def normalize_description_languages(raw: Optional[str]) -> str:
    values = []
    seen = set()
    for item in str(raw or "").split(","):
        candidate = item.strip().lower()
        if candidate not in {"en", "tr"} or candidate in seen:
            continue
        seen.add(candidate)
        values.append(candidate)
    if not values:
        return DEFAULT_DESCRIPTION_LANGUAGES
    ordered = [lang for lang in ("en", "tr") if lang in seen]
    return ",".join(ordered) if ordered else DEFAULT_DESCRIPTION_LANGUAGES


def load_description_languages(user_id: Optional[str], brand_id: Optional[str] = None) -> Optional[str]:
    raw = load_prompt_setting(description_languages_key(user_id, brand_id))
    return normalize_description_languages(raw) if raw else None


def save_description_languages(user_id: Optional[str], brand_id: Optional[str], value: str, updated_by: Optional[str]) -> None:
    save_prompt_setting(
        description_languages_key(user_id, brand_id),
        normalize_description_languages(value),
        updated_by,
    )


def delete_description_languages(user_id: Optional[str], brand_id: Optional[str]) -> None:
    delete_prompt_setting(description_languages_key(user_id, brand_id))


def load_description_settings(user_id: Optional[str], brand_id: Optional[str] = None) -> Tuple[str, str]:
    prompt_text = load_description_prompt(user_id, brand_id) or DEFAULT_DESCRIPTION_PROMPT
    languages = load_description_languages(user_id, brand_id) or DEFAULT_DESCRIPTION_LANGUAGES
    return prompt_text, normalize_description_languages(languages)
