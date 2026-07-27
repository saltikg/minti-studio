from __future__ import annotations

import ast
import copy
import logging
from pathlib import Path
from typing import Any, Dict, Optional


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
        "Any": Any,
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


def _load_videos_function(function_names: set[str], *, extra_namespace: Optional[Dict[str, object]] = None):
    source_path = Path("app/video_shorts/routes/videos.py")
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    selected = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            cloned = copy.deepcopy(node)
            cloned.decorator_list = []
            selected.append(cloned)
    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "Optional": Optional,
        "List": list,
        "Tuple": tuple,
    }
    if extra_namespace:
        namespace.update(extra_namespace)
    exec(compile(isolated_module, filename=str(source_path), mode="exec"), namespace)
    return namespace


def test_tracked_short_comment_total_includes_pending_and_rejected():
    namespace = _load_videos_function(
        {
            "_normalize_nonnegative_int",
            "_tracked_short_comment_total",
        },
    )

    tracked_total = namespace["_tracked_short_comment_total"]

    assert (
        tracked_total(
            platform_comment_count=99,
            published_comment_count=5,
            pending_comment_count=2,
            rejected_comment_count=1,
        )
        == 8
    )
    assert (
        tracked_total(
            platform_comment_count=None,
            published_comment_count=3,
            pending_comment_count=2,
            rejected_comment_count=0,
            fallback_comment_count=3,
        )
        == 5
    )
    assert (
        tracked_total(
            platform_comment_count=11,
            published_comment_count=0,
            pending_comment_count=0,
            rejected_comment_count=0,
            fallback_comment_count=7,
        )
        == 11
    )


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


def test_youtube_live_fetch_persists_platform_total_for_badge():
    captured_totals = []
    captured_summaries = []

    class _Args(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    namespace = _load_videos_function(
        {
            "shorts_comments",
        },
        extra_namespace={
            "g": type("_G", (), {"vs_current_user": {"role": "member", "id": "owner-1"}})(),
            "request": type("_Req", (), {"args": _Args({"status": "all", "refresh": "auto"})})(),
            "jsonify": lambda **kwargs: kwargs,
            "current_brand_id": lambda: None,
            "_load_brand_scope_context": lambda active_brand_id: (None, None),
            "_load_video_scope": lambda video_id: ("Clip title", "owner-1", None, None),
            "_resolve_owner_for_short_id": lambda video_id: "owner-1",
            "_preferred_brand_channel_ids": lambda owner_user_id, active_brand_id: [],
            "_parse_refresh_mode": lambda value: "auto",
            "_load_short_comment_sync_state_entry": lambda video_id: {"last_synced_at": None},
            "_should_allow_live_comment_fetch": lambda last_synced_at: True,
            "_sync_youtube_comments_for_video": lambda owner_user_id, video_id, video_title=None: 1,
            "_load_youtube_platform_comment_total": lambda video_id: 12,
            "_upsert_short_comment_platform_total": lambda video_id, total: captured_totals.append((video_id, total)),
            "fetch_comment_records_for_video": lambda *args, **kwargs: [{"comment_id": "c1", "status": "published", "is_reply": False}],
            "_build_short_title_map": lambda: {},
            "_apply_short_title_fallback": lambda comments, title_map: None,
            "_thread_youtube_comment_rows": lambda comments: comments,
            "_summarize_comment_counts_for_entries": lambda comments: {"pending": 0, "published": 1, "rejected": 0},
            "_upsert_short_comment_counts": lambda video_id, summary: captured_summaries.append((video_id, summary)),
            "_load_short_comment_cache": lambda short_video_ids: {
                "short-1": {
                    "platform_comment_count": 12,
                    "last_seen_comment_count": 4,
                }
            },
            "current_app": type(
                "_App",
                (),
                {
                    "logger": type(
                        "_Logger",
                        (),
                        {
                            "info": staticmethod(lambda *args, **kwargs: None),
                            "exception": staticmethod(lambda *args, **kwargs: None),
                        },
                    )()
                },
            )(),
        },
    )

    payload = namespace["shorts_comments"]("short-1")

    assert captured_totals == [("short-1", 12)]
    assert captured_summaries == [("short-1", {"pending": 0, "published": 1, "rejected": 0})]
    assert payload["platform_comment_count"] == 12
    assert payload["last_seen_comment_count"] == 4
    assert payload["live_fetch_attempted"] is True


def test_mark_seen_uses_platform_total_not_pending_plus_published():
    captured = {
        "counts": [],
        "platform": [],
        "seen": [],
    }

    class _FakeRequest:
        @staticmethod
        def get_json(silent=True):
            return {
                "pending": 2,
                "published": 3,
                "rejected": 1,
                "mark_seen": True,
                "platform_comment_count": 11,
            }

    namespace = _load_videos_function(
        {
            "_normalize_nonnegative_int",
            "shorts_comment_cache_counts",
        },
        extra_namespace={
            "request": _FakeRequest(),
            "jsonify": lambda payload=None, **kwargs: payload if payload is not None else kwargs,
            "_parse_bool": lambda value, default=False: bool(value),
            "_upsert_short_comment_counts": lambda video_id, summary: captured["counts"].append((video_id, summary)),
            "_upsert_short_comment_platform_total": lambda video_id, total: captured["platform"].append((video_id, total)),
            "_update_short_comment_last_seen_count": lambda video_id, total: captured["seen"].append((video_id, total)),
        },
    )

    payload = namespace["shorts_comment_cache_counts"]("short-1")

    assert captured["counts"] == [("short-1", {"pending": 2, "published": 3, "rejected": 1})]
    assert captured["platform"] == [("short-1", 11)]
    assert captured["seen"] == [("short-1", 11)]
    assert payload["platform_comment_count"] == 11
    assert payload["last_seen_comment_count"] == 11


def test_instagram_cache_read_does_not_advance_last_seen():
    captured_seen = []

    class _Args(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    namespace = _load_videos_function(
        {
            "instagram_media_comments",
        },
        extra_namespace={
            "_require_instagram_media_entry": lambda queue_id: (
                {"id": queue_id, "instagram_media_id": "ig-1", "comment_count": 7, "last_seen_comment_count": 2},
                None,
            ),
            "request": type("_Req", (), {"args": _Args({})})(),
            "_parse_refresh_mode": lambda value: "cache",
            "_normalize_instagram_limit": lambda value, default: default,
            "_load_instagram_comment_cache_sync_timestamp": lambda media_id: None,
            "_should_allow_live_comment_fetch": lambda last_synced_at: False,
            "refresh_instagram_media": lambda queue_id, comments_limit=25: None,
            "get_instagram_queue_entry": lambda queue_id: None,
            "_build_instagram_media_payload": lambda entry: {
                "comment_count": entry.get("comment_count"),
                "comments": [{"id": "c1"}],
                "last_seen_comment_count": entry.get("last_seen_comment_count"),
            },
            "update_instagram_last_seen_comment_count": lambda queue_id, count: captured_seen.append((queue_id, count)),
            "jsonify": lambda **kwargs: kwargs,
            "current_app": type(
                "_App",
                (),
                {
                    "logger": type(
                        "_Logger",
                        (),
                        {
                            "info": staticmethod(lambda *args, **kwargs: None),
                            "warning": staticmethod(lambda *args, **kwargs: None),
                        },
                    )()
                },
            )(),
            "COMMENT_LIVE_FETCH_MIN_INTERVAL_SECONDS": 60,
            "InstagramActionError": Exception,
        },
    )

    payload = namespace["instagram_media_comments"]("queue-1")

    assert captured_seen == []
    assert payload["platform_comment_count"] == 7
    assert payload["live_fetch_attempted"] is False
