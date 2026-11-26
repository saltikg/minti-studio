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
    row = conn.execute(
        f"""
        SELECT id, slug, name, description, core_topics, adjacent_topics,
               excluded_topics, definition_version
        FROM niches WHERE {where} LIMIT 1
        """,
        [value],
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "slug": str(row[1]), "name": str(row[2]),
        "description": str(row[3] or ""),
        "core_topics": _list_value(row[4]),
        "adjacent_topics": _list_value(row[5]),
        "excluded_topics": _list_value(row[6]),
        "definition_version": int(row[7] or 1),
    }


def definition_prompt(definition: dict[str, Any]) -> str:
    return json.dumps(
        {
            "name": definition["name"],
            "description": definition["description"],
            "core_topics": definition["core_topics"],
            "adjacent_topics": definition["adjacent_topics"],
            "excluded_topics": definition["excluded_topics"],
            "definition_version": definition["definition_version"],
        },
        ensure_ascii=False,
    )


def parse_topic_lines(value: Any) -> list[str]:
    lines = str(value or "").replace(",", "\n").splitlines()
    return list(dict.fromkeys(" ".join(line.split()) for line in lines if line.strip()))


def update_niche_definition(
    conn,
    *,
    slug: str,
    name: str,
    description: str,
    core_topics: list[str],
    adjacent_topics: list[str],
    excluded_topics: list[str],
) -> int:
    if not name.strip() or not 150 <= len(description.split()) <= 250:
        raise ValueError("Name is required and description must contain 150–250 words.")
    if not core_topics or not adjacent_topics or not excluded_topics:
        raise ValueError("Core, adjacent, and excluded topics are required.")
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
            excluded_topics = ?, definition_version = ?
        WHERE slug = ?
        """,
        [
            name.strip(), description.strip(), json.dumps(core_topics),
            json.dumps(adjacent_topics), json.dumps(excluded_topics), version, slug,
        ],
    )
    return version
