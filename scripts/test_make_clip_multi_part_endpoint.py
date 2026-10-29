#!/usr/bin/env python3
import json
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.video_shorts.routes import generation


class _FakeConn:
    def close(self):
        pass


@contextmanager
def _patched_generation(existing_entries=None, duration=120.0):
    writes = []
    originals = {
        "_active_editor_context": generation._active_editor_context,
        "_fetch_scoped_video_row": generation._fetch_scoped_video_row,
        "_load_plan_entries": generation._load_plan_entries,
        "_write_plan_entries": generation._write_plan_entries,
        "_resolve_video_clip_ranges": generation._resolve_video_clip_ranges,
        "get_db_readonly": generation.get_db_readonly,
        "_fetch_transcript": generation._fetch_transcript,
        "_schedule_async_clip_title_suggestion": generation._schedule_async_clip_title_suggestion,
        "tenant_clip_filename": generation.tenant_media.clip_filename,
    }
    generation._active_editor_context = lambda: {"owner_user_id": "user-1", "brand_id": "brand-1"}
    generation._fetch_scoped_video_row = lambda conn, video_pk, columns: ("video-1", duration)
    generation._load_plan_entries = lambda video_id, **kwargs: list(existing_entries or [])
    generation._write_plan_entries = lambda video_id, entries, **kwargs: writes.append((video_id, json.loads(json.dumps(entries))))
    generation._resolve_video_clip_ranges = lambda **kwargs: (
        generation.ClipRangeResolution(
            True,
            float((kwargs.get("keep_ranges") or [{"start": kwargs["start"]}])[0]["start"]),
            float((kwargs.get("keep_ranges") or [{"end": kwargs["end"]}])[-1]["end"]),
            list(kwargs.get("keep_ranges") or [{"start": kwargs["start"], "end": kwargs["end"]}]),
            "",
        ),
        duration,
    )
    generation.get_db_readonly = lambda: _FakeConn()
    generation._fetch_transcript = lambda conn, video_id, **kwargs: (
        "one two three four",
        [
            {"start": 0.0, "end": 10.0, "text": "one two"},
            {"start": 20.0, "end": 32.0, "text": "three four"},
        ],
    )
    generation._schedule_async_clip_title_suggestion = lambda **kwargs: None
    generation.tenant_media.clip_filename = lambda video_id, plan_index, **kwargs: f"{plan_index}_{video_id}.mp4"
    try:
        yield writes
    finally:
        for name, value in originals.items():
            if name == "tenant_clip_filename":
                generation.tenant_media.clip_filename = value
            else:
                setattr(generation, name, value)


def _post(app, data):
    with app.test_request_context(
        "/video_shorts/generate/1/add_clip_section",
        method="POST",
        data=data,
        headers={"X-Requested-With": "XMLHttpRequest"},
    ):
        response = generation.add_clip_section(1)
    if isinstance(response, tuple):
        flask_response, status = response[0], response[1]
    else:
        flask_response, status = response, response.status_code
    return status, flask_response.get_json()


class _TranscriptConn:
    def __init__(self, segments):
        self.segments = segments
        self.params = None
        self.committed = False

    def execute(self, sql, params):
        self.params = params
        return self

    def fetchone(self):
        return (1,)

    def commit(self):
        self.committed = True

    def close(self):
        pass


@contextmanager
def _patched_word_edit():
    segments = [
        {
            "start": 0.0,
            "end": 2.0,
            "text": "hello world",
            "tr_text": "hello world",
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.4},
                {"word": "world", "start": 0.5, "end": 0.9},
            ],
        }
    ]
    conn = _TranscriptConn(segments)
    originals = {
        "get_db": generation.get_db,
        "_ensure_transcript_schema": generation._ensure_transcript_schema,
        "_fetch_scoped_video_row": generation._fetch_scoped_video_row,
        "_fetch_transcript": generation._fetch_transcript,
        "_active_editor_context": generation._active_editor_context,
        "_load_plan_entries": generation._load_plan_entries,
        "clear_done_job_cache_for_plan": generation.clear_done_job_cache_for_plan,
    }
    generation.get_db = lambda: conn
    generation._ensure_transcript_schema = lambda db: None
    generation._fetch_scoped_video_row = lambda db, video_pk, columns: ("video-1",)
    generation._fetch_transcript = lambda db, video_id, **kwargs: ("hello world", segments)
    generation._active_editor_context = lambda: {"owner_user_id": "user-1"}
    generation._load_plan_entries = lambda video_id, **kwargs: []
    generation.clear_done_job_cache_for_plan = lambda **kwargs: None
    try:
        yield conn
    finally:
        for name, value in originals.items():
            setattr(generation, name, value)


def main():
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context(), _patched_generation() as writes:
        status, payload = _post(
            app,
            {
                "start_time": "0",
                "end_time": "32",
                "keep_ranges": json.dumps([
                    {"start": 1.0, "end": 8.0},
                    {"start": 20.0, "end": 28.0},
                ]),
            },
        )
        assert status == 200, payload
        entry = writes[-1][1][0]
        assert entry["start"] == 1.0
        assert entry["end"] == 28.0
        assert entry["edit_keep_ranges"] == [{"start": 1.0, "end": 8.0}, {"start": 20.0, "end": 28.0}]
        assert payload["edit_keep_ranges"] == entry["edit_keep_ranges"]

    with app.app_context(), _patched_generation() as writes:
        status, payload = _post(
            app,
            {
                "start_time": "1",
                "end_time": "8",
                "keep_ranges": json.dumps([{"start": 1.0, "end": 8.0}]),
            },
        )
        assert status == 200, payload
        entry = writes[-1][1][0]
        assert entry["start"] == 1.0
        assert entry["end"] == 8.0
        assert "edit_keep_ranges" not in entry
        assert payload["edit_keep_ranges"] == []

    with app.app_context(), _patched_generation() as writes:
        indexed_range = {
            "start": 1.0,
            "end": 8.0,
            "start_word_index": 1429,
            "end_word_index": 1614,
        }
        status, payload = _post(
            app,
            {
                "start_time": "1",
                "end_time": "8",
                "selection_source": "transcript_words",
                "keep_ranges": json.dumps([indexed_range]),
            },
        )
        assert status == 200, payload
        entry = writes[-1][1][0]
        assert entry["edit_keep_ranges"] == [indexed_range]
        assert payload["edit_keep_ranges"] == [indexed_range]

    with app.app_context(), _patched_generation(duration=300.0) as writes:
        status, payload = _post(
            app,
            {
                "start_time": "0",
                "end_time": "150",
                "keep_ranges": json.dumps([
                    {"start": 0.0, "end": 60.0},
                    {"start": 90.0, "end": 150.0},
                ]),
            },
        )
        assert status == 200, payload
        assert writes[-1][1][0]["edit_keep_ranges"] == [
            {"start": 0.0, "end": 60.0},
            {"start": 90.0, "end": 150.0},
        ]

    with app.app_context(), _patched_generation(duration=300.0):
        status, payload = _post(
            app,
            {
                "start_time": "0",
                "end_time": "241",
                "keep_ranges": json.dumps([
                    {"start": 0.0, "end": 90.0},
                    {"start": 150.0, "end": 241.0},
                ]),
            },
        )
        assert status == 400, payload
        assert "180 seconds" in payload["message"]

    with app.app_context(), _patched_word_edit() as conn:
        with app.test_request_context(
            "/video_shorts/generate/1/segment_edit",
            method="POST",
            json={"word_index": 1, "text": "earth"},
        ):
            response = generation.edit_segment_text(1)
        if isinstance(response, tuple):
            flask_response, status = response[0], response[1]
        else:
            flask_response, status = response, response.status_code
        payload = flask_response.get_json()
        assert status == 200, payload
        assert payload["word_text"] == "earth"
        assert payload["segment_text"] == "hello earth"
        assert conn.committed
        updated_segments = json.loads(conn.params[2])
        updated_word = updated_segments[0]["words"][1]
        assert updated_word["word"] == "earth"
        assert updated_word["start"] == 0.5
        assert updated_word["end"] == 0.9

    print("MAKE_CLIP_MULTI_PART_ENDPOINT_OK")


if __name__ == "__main__":
    main()
