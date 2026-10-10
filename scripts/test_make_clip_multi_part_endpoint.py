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
        "get_db_readonly": generation.get_db_readonly,
        "_fetch_transcript": generation._fetch_transcript,
        "_schedule_async_clip_title_suggestion": generation._schedule_async_clip_title_suggestion,
    }
    generation._active_editor_context = lambda: {"owner_user_id": "user-1", "brand_id": "brand-1"}
    generation._fetch_scoped_video_row = lambda conn, video_pk, columns: ("video-1", duration)
    generation._load_plan_entries = lambda video_id: list(existing_entries or [])
    generation._write_plan_entries = lambda video_id, entries: writes.append((video_id, json.loads(json.dumps(entries))))
    generation.get_db_readonly = lambda: _FakeConn()
    generation._fetch_transcript = lambda conn, video_id: (
        "one two three four",
        [
            {"start": 0.0, "end": 10.0, "text": "one two"},
            {"start": 20.0, "end": 32.0, "text": "three four"},
        ],
    )
    generation._schedule_async_clip_title_suggestion = lambda **kwargs: None
    try:
        yield writes
    finally:
        for name, value in originals.items():
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

    with app.app_context(), _patched_generation(duration=300.0):
        status, payload = _post(
            app,
            {
                "start_time": "0",
                "end_time": "140",
                "keep_ranges": json.dumps([
                    {"start": 0.0, "end": 50.0},
                    {"start": 60.0, "end": 105.5},
                ]),
            },
        )
        assert status == 400, payload
        assert "90 seconds" in payload["message"]

    print("MAKE_CLIP_MULTI_PART_ENDPOINT_OK")


if __name__ == "__main__":
    main()
