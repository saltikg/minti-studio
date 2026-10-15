#!/usr/bin/env python3
"""Audit or manually repair frozen legacy owners without touching media files.

Earliest source wins unless the earliest legacy clip has direct render-job
creation evidence for another owner. Missing source rows never transfer ownership.
Default is read-only; --apply inserts missing decisions and repairs proven errors.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from safe_to_deploy import _connect, _load_env_file


def audit(cur):
    cur.execute("SELECT id, video_id, owner_user_id, brand_id FROM youtube_videos ORDER BY created_at NULLS LAST, id")
    sources = defaultdict(list)
    by_pk = {}
    for pk, source, owner, brand in cur.fetchall():
        row = {"video_pk": pk, "owner": owner, "brand": brand}
        sources[source].append(row)
        by_pk[pk] = row
    cur.execute("SELECT youtube_id, video_pk, ownership_rule FROM shorts_video_legacy_owners")
    frozen = {source: (pk, rule) for source, pk, rule in cur.fetchall()}
    cur.execute("""SELECT source_video_id, clip_filename, created_at
        FROM shorts_generated_videos
        WHERE clip_filename ~ '^[0-9]+_.+\\.mp4$' AND clip_filename !~ '_v[0-9]+\\.mp4$'
        ORDER BY created_at NULLS LAST, id""")
    earliest_clip = {}
    for source, name, created in cur.fetchall():
        earliest_clip.setdefault(source, (name, created))
    cur.execute("""SELECT id, user_id, payload_json->>'video_pk', payload_json->>'source_video_id',
            payload_json->>'brand_id', result_json->>'clip_filename', created_at
        FROM shorts_render_jobs WHERE type = 'render_short' AND status = 'done'
        ORDER BY created_at, id""")
    jobs = defaultdict(list)
    for job_id, owner, pk, source, brand, filename, created in cur.fetchall():
        jobs[filename].append((job_id, owner, pk, source, brand, created))
    decisions = []
    for source in sorted(set(sources) | set(frozen)):
        old_pk, old_rule = frozen.get(source, (None, None))
        candidates = sources[source]
        if old_pk is not None and old_pk not in by_pk:
            decisions.append(dict(youtube_id=source, old_pk=old_pk, video_pk=old_pk,
                                  rule=old_rule, evidence="frozen_source_missing_preserved"))
            continue
        if old_pk is not None and not any(v["video_pk"] == old_pk for v in candidates):
            decisions.append(dict(youtube_id=source, old_pk=old_pk, video_pk=old_pk,
                                  rule=old_rule, evidence="frozen_source_rehomed_preserved"))
            continue
        if not candidates:
            continue
        chosen = candidates[0]
        rule = "earliest_video_row"
        evidence = None
        if source in earliest_clip:
            filename, created = earliest_clip[source]
            for job_id, owner, raw_pk, job_source, brand, job_created in jobs[filename]:
                if not created or not job_created or job_created > created or job_source != source:
                    continue
                matches = [v for v in candidates if v["owner"] == owner and
                           (not brand or str(v["brand"] or "") == brand) and
                           (not raw_pk or str(v["video_pk"]) == raw_pk)]
                if len(matches) == 1:
                    chosen = matches[0]
                    rule = "creation_render_job"
                    evidence = job_id
                    break
        decisions.append(dict(youtube_id=source, old_pk=old_pk, video_pk=chosen["video_pk"],
                              earliest_pk=candidates[0]["video_pk"], rule=rule, evidence=evidence,
                              owner=chosen["owner"], brand=chosen["brand"]))
    return decisions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    _load_env_file()
    with _connect() as conn, conn.cursor() as cur:
        if args.apply:
            cur.execute("LOCK TABLE youtube_videos, shorts_video_legacy_owners IN SHARE ROW EXCLUSIVE MODE")
        else:
            cur.execute("SET TRANSACTION READ ONLY")
        decisions = audit(cur)
        changes = [d for d in decisions if d["old_pk"] != d["video_pk"]]
        exceptions = [d for d in decisions if d.get("earliest_pk") and d["earliest_pk"] != d["video_pk"]]
        for decision in changes + [d for d in exceptions if d not in changes]:
            print(json.dumps(decision, default=str))
        if args.apply:
            for d in changes:
                cur.execute("""INSERT INTO shorts_video_legacy_owners (youtube_id, video_pk, ownership_rule)
                    VALUES (%s, %s, %s) ON CONFLICT(youtube_id) DO UPDATE SET
                    video_pk = excluded.video_pk, ownership_rule = excluded.ownership_rule""",
                    [d["youtube_id"], d["video_pk"], d["rule"]])
        print(json.dumps({"mode": "APPLIED" if args.apply else "READ_ONLY", "sources": len(decisions),
                          "inserted": sum(d["old_pk"] is None for d in changes),
                          "corrected": sum(d["old_pk"] is not None for d in changes),
                          "supported_exceptions": len(exceptions), "files_changed": 0}))


if __name__ == "__main__":
    main()
