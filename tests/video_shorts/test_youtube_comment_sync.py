from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Dict, Optional


def _load_sync_helpers():
    source_path = Path("app/video_shorts/tasks/sync_short_comments.py")
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    wanted = {
        "YOUTUBE_COMMENT_FETCH_MAX_RESULTS",
        "_normalize_comment_count",
        "_should_fetch_youtube_comments",
        "_youtube_comment_body_target",
        "_should_fetch_youtube_comment_bodies",
    }
    selected = []
    for node in module.body:
        if isinstance(node, ast.Assign):
            target_ids = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if target_ids & wanted:
                selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            selected.append(node)
    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {
        "Dict": Dict,
        "Optional": Optional,
        "logger": logging.getLogger("test_youtube_comment_sync"),
    }
    exec(compile(isolated_module, filename=str(source_path), mode="exec"), namespace)
    return namespace


def _load_sync_function(function_names: set[str], *, extra_namespace: Optional[Dict[str, object]] = None):
    source_path = Path("app/video_shorts/tasks/sync_short_comments.py")
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    selected = []
    for node in module.body:
        if isinstance(node, ast.Assign):
            target_ids = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if target_ids & function_names:
                selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {
        "Dict": Dict,
        "Optional": Optional,
        "List": list,
        "Tuple": tuple,
        "logger": logging.getLogger("test_youtube_comment_sync"),
    }
    if extra_namespace:
        namespace.update(extra_namespace)
    exec(compile(isolated_module, filename=str(source_path), mode="exec"), namespace)
    return namespace


def test_youtube_body_sync_fetches_when_count_unchanged_but_cache_incomplete():
    helpers = _load_sync_helpers()

    should_fetch = helpers["_should_fetch_youtube_comment_bodies"]

    assert should_fetch(
        "wo1IM4Cn4V8",
        5,
        {"last_comment_count": 5},
        3,
    ) is True


def test_youtube_body_sync_caps_completeness_target_at_fetch_window():
    helpers = _load_sync_helpers()

    body_target = helpers["_youtube_comment_body_target"]
    should_fetch = helpers["_should_fetch_youtube_comment_bodies"]

    assert body_target(120) == 50
    assert should_fetch(
        "high-volume-short",
        120,
        {"last_comment_count": 120},
        50,
    ) is False


def test_youtube_count_phase_only_updates_observed_signal():
    captured_upserts = []

    class _FakeDateTime:
        @staticmethod
        def now(_tz):
            return "2026-07-23T19:00:00Z"

    namespace = _load_sync_function(
        {
            "_normalize_comment_count",
            "_sync_youtube_comment_totals",
        },
        extra_namespace={
            "datetime": _FakeDateTime,
            "timezone": type("_FakeTimezone", (), {"utc": object()})(),
            "_upsert_short_comment_platform_total": lambda short_id, total: captured_upserts.append(
                (short_id, total)
            ),
        },
    )

    sync_updates: Dict[str, Dict[str, object]] = {}
    refreshed = namespace["_sync_youtube_comment_totals"](
        ["a", "b", "c"],
        {"a": 4, "b": None, "c": 0},
        sync_updates,
    )

    assert refreshed == 2
    assert captured_upserts == [("a", 4), ("c", 0)]
    assert sync_updates == {
        "a": {
            "observed_comment_count": 4,
            "observed_comment_count_at": "2026-07-23T19:00:00Z",
        },
        "c": {
            "observed_comment_count": 0,
            "observed_comment_count_at": "2026-07-23T19:00:00Z",
        },
    }


def test_youtube_body_skip_does_not_advance_body_synced_count():
    namespace = _load_sync_function(
        {
            "_sync_youtube_comments_for_videos",
        },
        extra_namespace={
            "_normalize_comment_count": lambda value: value,
            "_should_fetch_youtube_comment_bodies": lambda *args, **kwargs: False,
            "datetime": object(),
            "timezone": object(),
            "fetch_video_comments": None,
            "YoutubeApiError": Exception,
            "_merge_youtube_comments": lambda comments: comments,
            "moderate_text_entries": lambda entries, owner_user_id: {},
            "upsert_comment_records": lambda records: None,
        },
    )

    sync_updates: Dict[str, Dict[str, object]] = {
        "stays-dirty": {
            "observed_comment_count": 9,
            "observed_comment_count_at": "2026-07-23T19:00:00Z",
        }
    }
    updated = namespace["_sync_youtube_comments_for_videos"](
        "owner-1",
        ["stays-dirty"],
        {},
        {},
        {"stays-dirty": {"last_comment_count": 7}},
        {"stays-dirty": 9},
        {"stays-dirty": 3},
        sync_updates,
        {"stays-dirty": "oauth-user"},
    )

    assert updated == 0
    assert sync_updates == {
        "stays-dirty": {
            "observed_comment_count": 9,
            "observed_comment_count_at": "2026-07-23T19:00:00Z",
        }
    }
