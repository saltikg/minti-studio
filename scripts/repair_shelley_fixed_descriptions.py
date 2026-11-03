#!/usr/bin/env python3
"""Preview or apply Shelley Meyer's narrowly scoped Nov 4/6/8 description repair."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from safe_to_deploy import _connect, _load_env_file  # noqa: E402


BRAND_PREFIX = "3dab32b8-"
TARGET_DATES = {date(2026, 11, 4), date(2026, 11, 6), date(2026, 11, 8)}
PACIFIC = ZoneInfo("America/Los_Angeles")
FIXED_TEXT = "Watch the full video on my YouTube channel — if you need the link, it's in my Linktree/profile!"


def _rows(cur, sql, args=()):
    cur.execute(sql, args)
    names = [column[0] for column in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _payload(value):
    return value if isinstance(value, dict) else json.loads(value or "{}")


def _pacific_date(value, payload=None):
    local_text = str((payload or {}).get("publish_at_local") or "").strip()
    if local_text:
        return date.fromisoformat(local_text[:10])
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PACIFIC).date()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    _load_env_file()

    conn = _connect()
    plan_backups = []
    try:
        cur = conn.cursor()
        if not args.apply:
            cur.execute("SET TRANSACTION READ ONLY")
        brands = _rows(
            cur,
            """
            SELECT b.id AS brand_id, b.owner_user_id, b.name AS brand_name, u.name, u.email
            FROM shorts_brands b
            JOIN shorts_users u ON u.id::text = b.owner_user_id::text
            WHERE b.id::text LIKE %s
            """,
            [BRAND_PREFIX + "%"],
        )
        assert len(brands) == 1, f"Expected one Shelley brand, found {brands}"
        brand = brands[0]
        owner_id, brand_id = str(brand["owner_user_id"]), str(brand["brand_id"])
        assert "shelley" in f"{brand.get('name', '')} {brand.get('email', '')}".lower(), brand

        youtube_candidates = _rows(
            cur,
            """
            SELECT id, status, publish_at, payload_json
            FROM shorts_youtube_schedules
            WHERE user_id = %s AND brand_id = %s AND lower(status) = 'pending'
            ORDER BY publish_at, id
            """,
            [owner_id, brand_id],
        )
        youtube = []
        for row in youtube_candidates:
            row["payload"] = _payload(row["payload_json"])
            if _pacific_date(row["publish_at"], row["payload"]) in TARGET_DATES:
                youtube.append(row)

        instagram_candidates = _rows(
            cur,
            """
            SELECT id, status, publish_at, video_id, plan_index, clip_filename, caption_text
            FROM shorts_instagram_queue
            WHERE user_id = %s AND brand_id = %s AND lower(status) = 'pending'
            ORDER BY publish_at, id
            """,
            [owner_id, brand_id],
        )
        instagram = [row for row in instagram_candidates if _pacific_date(row["publish_at"]) in TARGET_DATES]
        assert len(youtube) == 3, f"Expected 3 pending YouTube schedules, found {len(youtube)}"
        assert len(instagram) == 3, f"Expected 3 pending Instagram schedules, found {len(instagram)}"
        assert {_pacific_date(row["publish_at"], row["payload"]) for row in youtube} == TARGET_DATES
        assert {_pacific_date(row["publish_at"]) for row in instagram} == TARGET_DATES

        targets = []
        for row in youtube:
            payload = row["payload"]
            video_pk = payload.get("video_pk")
            plan_index = payload.get("plan_index")
            clip_filename = payload.get("filename") or payload.get("clip_filename")
            assert video_pk is not None and plan_index is not None and clip_filename, payload
            targets.append((int(video_pk), str(plan_index), str(clip_filename)))
        assert len({item[2] for item in targets}) == 3, targets
        assert {str(row["clip_filename"]) for row in instagram} == {item[2] for item in targets}, (instagram, targets)

        published_nov2 = _rows(
            cur,
            """
            SELECT id, publish_at, youtube_video_id, payload_json
            FROM shorts_youtube_schedules
            WHERE user_id = %s AND brand_id = %s AND lower(status) IN ('published', 'uploaded')
            ORDER BY publish_at
            """,
            [owner_id, brand_id],
        )
        nov2_ids = [str(row["id"]) for row in published_nov2 if _pacific_date(row["publish_at"], _payload(row["payload_json"])) == date(2026, 11, 2)]

        report = {
            "mode": "APPLY" if args.apply else "PREVIEW",
            "brand": brand,
            "youtube": [{"id": str(row["id"]), "date": str(_pacific_date(row["publish_at"], row["payload"])), "clip": targets[index][2]} for index, row in enumerate(youtube)],
            "instagram": [{"id": str(row["id"]), "date": str(_pacific_date(row["publish_at"])), "clip": str(row["clip_filename"])} for row in instagram],
            "published_nov2_untouched": nov2_ids,
        }
        print(json.dumps(report, default=str, ensure_ascii=False, indent=2), flush=True)
        if not args.apply:
            conn.rollback()
            return

        setting_values = {
            f"{owner_id}:description_mode:brand:{brand_id}": "fixed",
            f"{owner_id}:fixed_description_text:brand:{brand_id}": FIXED_TEXT,
            f"{owner_id}:description_languages:brand:{brand_id}": "en",
        }
        for key, value in setting_values.items():
            cur.execute(
                """
                INSERT INTO shorts_prompt_settings (key, value, updated_by, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at
                """,
                [key, value, owner_id],
            )
        for row in youtube:
            payload = row["payload"]
            payload["description"] = FIXED_TEXT
            cur.execute(
                "UPDATE shorts_youtube_schedules SET payload_json=%s, updated_at=now() WHERE id=%s AND status='pending'",
                [json.dumps(payload, ensure_ascii=False), row["id"]],
            )
            assert cur.rowcount == 1
        for row in instagram:
            cur.execute(
                "UPDATE shorts_instagram_queue SET caption_text=%s, updated_at=now() WHERE id=%s AND status='pending'",
                [FIXED_TEXT, row["id"]],
            )
            assert cur.rowcount == 1
        clip_names = [item[2] for item in targets]
        cur.execute(
            """
            UPDATE shorts_generated_videos
            SET generated_description=%s, updated_at=now()
            WHERE user_id=%s AND brand_id=%s AND clip_filename = ANY(%s)
              AND lower(COALESCE(publish_status, '')) <> 'published'
            """,
            [FIXED_TEXT, owner_id, brand_id, clip_names],
        )

        from app.video_shorts.services import tenant_media

        for video_pk, plan_index, clip_filename in targets:
            cur.execute(
                "SELECT video_id FROM youtube_videos WHERE id=%s AND owner_user_id=%s AND brand_id=%s",
                [video_pk, owner_id, brand_id],
            )
            source = cur.fetchone()
            assert source, (video_pk, "source missing")
            video_id = str(source[0])
            state = tenant_media.load_state(video_id, "plan", video_pk=video_pk)
            plan_backups.append((video_id, video_pk, json.loads(json.dumps(state))))
            entries = state.get("plan") or state.get("clips") or []
            matches = [entry for entry in entries if str(entry.get("plan_index")) == plan_index and clip_filename in {entry.get("clip_filename"), entry.get("output_filename")}]
            assert len(matches) == 1, (video_pk, plan_index, clip_filename, len(matches))
            matches[0]["yt_description"] = FIXED_TEXT
            matches[0]["yt_status"] = "ready"
            tenant_media.write_state(video_id, state, "plan", video_pk=video_pk)

        conn.commit()
        print(json.dumps({"result": "APPLIED", "fixed_text": FIXED_TEXT, "clips": clip_names}, ensure_ascii=False), flush=True)
    except BaseException:
        conn.rollback()
        if plan_backups:
            from app.video_shorts.services import tenant_media
            for video_id, video_pk, state in plan_backups:
                tenant_media.write_state(video_id, state, "plan", video_pk=video_pk)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
