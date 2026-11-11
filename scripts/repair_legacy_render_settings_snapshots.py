#!/usr/bin/env python3
"""Repair legacy pending clips whose only render drift is design/default policy.

Dry-run by default. This intentionally refuses content edits, explicit regenerate
flags, and clips which already have a saved-settings snapshot.
"""
from __future__ import annotations

import argparse
import json

from app import create_app
from app.video_shorts.routes import generation
from app.video_shorts.services.db import get_db_readonly


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    app = create_app()
    repaired = []
    refused = []
    with app.app_context():
        conn = get_db_readonly()
        try:
            rows = conn.execute(
                """SELECT id,user_id,brand_id,payload_json FROM shorts_youtube_schedules
                   WHERE lower(status)='pending' AND youtube_video_id IS NULL"""
            ).fetchall()
        finally:
            conn.close()
        for schedule_id, user_id, brand_id, raw_payload in rows:
            payload = raw_payload if isinstance(raw_payload, dict) else json.loads(raw_payload or "{}")
            video_pk = int(payload.get("video_pk") or 0)
            plan_index = int(payload.get("plan_index") or 0)
            filename = payload.get("filename")
            if not video_pk or not plan_index:
                continue
            fresh = generation._scheduled_clip_render_freshness(
                owner_user_id=str(user_id), brand_id=str(brand_id), video_pk=video_pk,
                plan_index=plan_index, filename=filename,
            )
            content = fresh.get("content_differences") or {}
            design = fresh.get("design_differences") or {}
            source_video_id = str(fresh.get("source_video_id") or payload.get("source_video_id") or "")
            entry = fresh.get("plan_entry") or {}
            generated = fresh.get("generated_record") or {}
            if not design or content or entry.get("edit_requires_regenerate") or entry.get("saved_render_settings_snapshot"):
                if content or entry.get("edit_requires_regenerate"):
                    refused.append({"schedule_id": str(schedule_id), "content": sorted(content), "explicit_regenerate": bool(entry.get("edit_requires_regenerate"))})
                continue
            rendered_options, rendered_entry = generation._render_snapshot_from_generated_record(generated)
            if not rendered_options or not source_video_id:
                refused.append({"schedule_id": str(schedule_id), "reason": "missing render snapshot"})
                continue
            repaired.append({"schedule_id": str(schedule_id), "video_pk": video_pk, "plan_index": plan_index, "fields": sorted(design)})
            if args.apply:
                entries = generation._load_plan_entries(
                    source_video_id, video_pk=video_pk, owner_user_id=str(user_id), brand_id=str(brand_id)
                )
                target = next(item for item in entries if str(item.get("plan_index")) == str(plan_index))
                generation._persist_clip_saved_settings_snapshot(
                    target, options=rendered_options, entry_snapshot=rendered_entry,
                    source="legacy_render_repair", actor="deploy_migration",
                )
                generation._write_plan_entries(
                    source_video_id, entries, video_pk=video_pk,
                    owner_user_id=str(user_id), brand_id=str(brand_id),
                )
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "repaired": repaired, "refused": refused}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
