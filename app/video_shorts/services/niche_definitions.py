from __future__ import annotations

import json
from typing import Any

from app.video_shorts.services.db import table_columns


def _list_value(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = []
    if not isinstance(value, list):
        return []
    return [" ".join(str(item).split()) for item in value if str(item).strip()]


def _focus_options(value: Any) -> list[dict[str, str]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = []
    if not isinstance(value, list):
        return []
    options = []
    for item in value:
        if not isinstance(item, dict):
            continue
        key = "_".join(str(item.get("key") or "").strip().casefold().split())
        description = " ".join(str(item.get("description") or "").split())
        if key and description:
            options.append({"key": key, "description": description})
    return options


def load_niche_definition(conn, *, niche_id: Any = None, slug: str = "") -> dict[str, Any] | None:
    columns = table_columns(conn, "niches")
    required = {
        "description", "core_topics", "adjacent_topics", "excluded_topics",
        "definition_version",
    }
    if not required.issubset(columns):
        return None
    if niche_id is not None:
        where, value = "id = ?", niche_id
    else:
        where, value = "slug = ?", slug
    config_columns = [name for name in ("language", "target_focus", "focus_options", "search_queries", "status") if name in columns]
    row = conn.execute(
        f"""
        SELECT id, slug, name, description, core_topics, adjacent_topics,
               excluded_topics, definition_version{''.join(', ' + name for name in config_columns)}
        FROM niches WHERE {where} LIMIT 1
        """,
        [value],
    ).fetchone()
    if not row:
        return None
    result = {
        "id": row[0], "slug": str(row[1]), "name": str(row[2]),
        "description": str(row[3] or ""),
        "core_topics": _list_value(row[4]),
        "adjacent_topics": _list_value(row[5]),
        "excluded_topics": _list_value(row[6]),
        "definition_version": int(row[7] or 1),
    }
    offset = 8
    for index, name in enumerate(config_columns):
        result[name] = row[offset + index]
    result["language"] = str(result.get("language") or "en")
    result["target_focus"] = str(result.get("target_focus") or "hormones_midlife_health")
    result["focus_options"] = _focus_options(result.get("focus_options"))
    result["search_queries"] = _list_value(result.get("search_queries"))
    result["status"] = str(result.get("status") or "active")
    return result


def definition_prompt(definition: dict[str, Any]) -> str:
    return json.dumps(
        {
            "name": definition["name"],
            "description": definition["description"],
            "core_topics": definition["core_topics"],
            "adjacent_topics": definition["adjacent_topics"],
            "excluded_topics": definition["excluded_topics"],
            "definition_version": definition["definition_version"],
            "language": definition.get("language"),
            "target_focus": definition.get("target_focus"),
            "focus_options": definition.get("focus_options") or [],
            "search_queries": definition.get("search_queries") or [],
        },
        ensure_ascii=False,
    )


def parse_topic_lines(value: Any) -> list[str]:
    lines = str(value or "").replace(",", "\n").splitlines()
    return list(dict.fromkeys(" ".join(line.split()) for line in lines if line.strip()))


def parse_focus_options(value: Any) -> list[dict[str, str]]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError("Focus options must be valid JSON.") from exc
    options = _focus_options(parsed)
    if not options or len(options) != len(parsed):
        raise ValueError("Each focus option needs a key and short description.")
    if len({item["key"] for item in options}) != len(options):
        raise ValueError("Focus option keys must be unique.")
    return options


def update_niche_definition(
    conn,
    *,
    slug: str,
    name: str,
    description: str,
    core_topics: list[str],
    adjacent_topics: list[str],
    excluded_topics: list[str],
    language: str = "en",
    target_focus: str = "",
    focus_options: list[dict[str, str]] | None = None,
    search_queries: list[str] | None = None,
) -> int:
    if not name.strip() or not 150 <= len(description.split()) <= 250:
        raise ValueError("Name is required and description must contain 150–250 words.")
    if not core_topics or not adjacent_topics or not excluded_topics:
        raise ValueError("Core, adjacent, and excluded topics are required.")
    clean_language = language.strip().casefold()
    clean_target = target_focus.strip().casefold()
    clean_options = focus_options or []
    if not clean_language or not clean_target or clean_target not in {item["key"] for item in clean_options}:
        raise ValueError("Language, target focus, and matching focus options are required.")
    row = conn.execute(
        "SELECT definition_version FROM niches WHERE slug = ? LIMIT 1", [slug]
    ).fetchone()
    if not row:
        raise LookupError("Unknown niche")
    version = int(row[0] or 0) + 1
    conn.execute(
        """
        UPDATE niches
        SET name = ?, description = ?, core_topics = ?, adjacent_topics = ?,
            excluded_topics = ?, language = ?, target_focus = ?, focus_options = ?, search_queries = ?,
            definition_version = ?
        WHERE slug = ?
        """,
        [
            name.strip(), description.strip(), json.dumps(core_topics),
            json.dumps(adjacent_topics), json.dumps(excluded_topics), clean_language,
            clean_target, json.dumps(clean_options), json.dumps(search_queries or []), version, slug,
        ],
    )
    return version
