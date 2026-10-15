#!/usr/bin/env python3
"""Manual, idempotent repair of the ten approved legacy clips (2026-10-15).

Default: preview only. --apply requires a new, private --backup-dir outside the
repository. --audit compares all lifecycle/assets with exact render outputs and
retained plan ownership, without changing data. No app startup or state migration
helpers are used. Backups contain complete rows, the original plan, and restore SQL.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import json
import os
from pathlib import Path
import re
import tempfile

from safe_to_deploy import ROOT, _connect, _load_env_file

ADMIN = ("f97df4cb-93de-4761-9c39-62d303261b0a", "63f772f8-2d31-4416-9239-c546949bfa98")
CEVHERI = ("0a5183ea-1927-43cb-a0ed-9eb84f028abe", "41c2d07b-d74d-4fdd-b60a-2b5dafcdc4f4")
ISMAIL = ("4603cb8c-2422-4015-86f2-e5db2e874509", "82c6e48f-74c2-4973-9137-d046a240c3eb")
GUEST = ("a84992f6-f9b6-4d5b-bd37-a8f6d7f68aa5", "d0b8179e-fbc1-41ad-902a-56e1b3663047")
TARGETS = [
    ("16_EJi84qd5Wqo.mp4", 6488, CEVHERI),
    ("17_EJi84qd5Wqo.mp4", 6488, CEVHERI),
    *[(f"{i}_local_8b0b5dfdf2584fc7bd7f0f02b3a80b60.mp4", 7245, ISMAIL) for i in (5, 6, 7, 8)],
    ("12_local_d9d811e7e470425aa7c932855c3ce400.mp4", 7246, ISMAIL),
    *[(f"{i}_local_62868533c86e4eb7a598cf18e3655fad.mp4", 7267, ISMAIL) for i in (11, 12)],
]
GUEST_NAME = "4_IV1vIJ-OVyo.mp4"
PLAN = ROOT / "app/video_shorts/private_state/7748_plan.json"


def rows(cur, sql, args=()):
    cur.execute(sql, args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def identity(row):
    return str(row.get("user_id") or row.get("owner_user_id") or ""), str(row.get("brand_id") or "")


def emit(value):
    print(json.dumps(value, default=str, ensure_ascii=False), flush=True)


def atomic_write(path, contents, mode):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".repair-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def repair(conn, args):
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '5s'")
    if not args.apply:
        cur.execute("SET TRANSACTION READ ONLY")
    suffix = " FOR UPDATE" if args.apply else ""
    changes, report = [], []
    for name, pk, target in TARGETS + [(GUEST_NAME, 7753, GUEST)]:
        source = rows(cur, "SELECT id, video_id, owner_user_id, brand_id FROM youtube_videos WHERE id=%s", [pk])
        assert len(source) == 1 and identity(source[0]) == target, (name, "source changed")
        frozen = rows(cur, "SELECT video_pk FROM shorts_video_legacy_owners WHERE youtube_id=%s", [source[0]["video_id"]])
        assert len(frozen) == 1 and frozen[0]["video_pk"] == (7748 if name == GUEST_NAME else pk), (name, "registry changed")
        jobs = rows(cur, """SELECT user_id, payload_json, result_json FROM shorts_render_jobs
            WHERE status='done' AND result_json->>'clip_filename'=%s""", [name])
        assert jobs and all((str(j["user_id"]), str(j["payload_json"].get("brand_id") or "")) == target
                            and str(j["payload_json"].get("video_pk")) == str(pk) for j in jobs), (name, "creation evidence changed")
        lifecycle = rows(cur, "SELECT * FROM shorts_generated_videos WHERE clip_filename=%s OR output_filename=%s" + suffix, [name, name])
        assets = rows(cur, "SELECT * FROM shorts_storage_assets WHERE file_key=%s" + suffix, ["short:" + name])
        assert len(lifecycle) == len(assets) == 1, (name, "missing or ambiguous records")
        assert lifecycle[0]["source_video_id"] == source[0]["video_id"]
        assert lifecycle[0]["storage_file_key"] == "short:" + name
        for table, records, key in (("shorts_generated_videos", lifecycle, "id"), ("shorts_storage_assets", assets, "file_key")):
            row = records[0]
            assert identity(row) in ((target,) if name == GUEST_NAME else (ADMIN, target)), (name, "unexpected owner")
            if identity(row) != target:
                changes.append(dict(table=table, key=key, before=row, target=target))
        report.append(dict(filename=name, video_pk=pk, lifecycle_before=identity(lifecycle[0]),
                           asset_before=identity(assets[0]), after=target,
                           publish_status=lifecycle[0]["publish_status"], frozen_pk=frozen[0]["video_pk"]))

    original = PLAN.read_bytes()
    data = json.loads(original)
    assert isinstance(data.get("plan"), list), "Unexpected admin plan format"
    proposed = copy.deepcopy(data)
    removed = []
    for entry in data["plan"]:
        if GUEST_NAME in (entry.get("clip_filename"), entry.get("output_filename")):
            assert entry.get("plan_index") == 4 and entry.get("render_job_id") == "47cb4264-baf0-42f6-9535-99adc4c5293c"
            removed.append(entry)
    assert len(removed) <= 1, "Duplicate guest entries"
    proposed["plan"] = [entry for entry in data["plan"] if entry not in removed]
    assert [e for e in proposed["plan"] if e.get("plan_index") == 1] == [e for e in data["plan"] if e.get("plan_index") == 1]
    for item in report:
        emit(item)
    emit(dict(mode="APPLY" if args.apply else "PREVIEW", lifecycle_changes=sum(c["table"] == "shorts_generated_videos" for c in changes),
              asset_changes=sum(c["table"] == "shorts_storage_assets" for c in changes), plan_entries_removed=len(removed)))
    if not args.apply or not (changes or removed):
        conn.rollback()
        return

    backup = Path(args.backup_dir).resolve()
    assert not backup.is_relative_to(ROOT), "Backup must be outside repository"
    backup.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = dict(changes=changes, plan_path=str(PLAN), plan_changed=bool(removed), report=report)
    (backup / "rows.json").write_text(json.dumps(manifest, default=str, ensure_ascii=False, indent=2))
    if removed:
        (backup / "7748_plan.json.before").write_bytes(original)
    restore = ["-- Restore only the changed owner/brand fields; complete original rows are in rows.json.", "BEGIN;"]
    for change in changes:
        table, key, row = change["table"], change["key"], change["before"]
        encoded = json.dumps(row, default=str).replace("'", "''")
        restore.append(f"UPDATE {table} t SET user_id=b.user_id, brand_id=b.brand_id FROM jsonb_populate_record(NULL::{table}, '{encoded}'::jsonb) b WHERE t.{key}=b.{key};")
    restore.append("COMMIT;")
    (backup / "restore.sql").write_text("\n".join(restore) + "\n")
    (backup / "README.txt").write_text("Restore ownership with restore.sql using search_path main,public.\n"
        "If plan_changed in rows.json, restore 7748_plan.json.before to plan_path only after checking for newer plan edits.\n"
        "rows.json contains every complete original row changed by this run. No publication fields were updated.\n"
        "The existing lifecycle database trigger advances updated_at on repair and restore.\n")
    for path in backup.iterdir():
        os.chmod(path, 0o600)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    plan_written = False
    try:
        for change in changes:
            table, key, before = change["table"], change["key"], change["before"]
            cur.execute(f"UPDATE {table} SET user_id=%s, brand_id=%s WHERE {key}=%s", [*change["target"], before[key]])
            after = rows(cur, f"SELECT * FROM {table} WHERE {key}=%s", [before[key]])[0]
            assert identity(after) == change["target"]
            # Production's lifecycle trigger always advances updated_at.
            allowed = {"user_id", "brand_id"} | ({"updated_at"} if table == "shorts_generated_videos" else set())
            assert {k: v for k, v in before.items() if k not in allowed} == {k: v for k, v in after.items() if k not in allowed}, "Non-ownership fields changed"
        if removed:
            assert PLAN.read_bytes() == original, "Plan changed concurrently; retry"
            atomic_write(PLAN, (json.dumps(proposed, ensure_ascii=False, indent=2) + "\n").encode(), PLAN.stat().st_mode & 0o777)
            plan_written = True
        conn.commit()
    except BaseException:
        conn.rollback()
        if plan_written:
            atomic_write(PLAN, original, PLAN.stat().st_mode & 0o777)
        raise
    emit(dict(result="APPLIED", backup_dir=str(backup), changed_rows=len(changes), changed_files=int(bool(removed))))


def audit(conn):
    cur = conn.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    sources = {str(r["id"]): r for r in rows(cur, "SELECT id, video_id, owner_user_id, brand_id FROM youtube_videos")}
    frozen = {r["youtube_id"]: str(r["video_pk"]) for r in rows(cur, "SELECT youtube_id,video_pk FROM shorts_video_legacy_owners")}
    users = {str(r["id"]): r for r in rows(cur, "SELECT id,name,username,role,is_guest,plan_id,subscription_status FROM shorts_users")}
    brands = {r["id"]: r["name"] for r in rows(cur, "SELECT id,name FROM shorts_brands")}

    def scope(pair):
        owner, brand = pair
        user = users.get(owner, {})
        internal = owner == ADMIN[0] or user.get("role") == "admin" or str(user.get("name", "")).lower() == "minti studio"
        return dict(owner=owner, name=user.get("name") or user.get("username"), brand=brand, brand_name=brands.get(brand),
                    classification="internal" if internal else "external_guest" if user.get("is_guest") else "external_registered" if user else "unknown",
                    subscription_status=user.get("subscription_status"))

    jobs = defaultdict(list)
    for job in rows(cur, "SELECT id,user_id,type,payload_json,result_json,created_at,finished_at FROM shorts_render_jobs WHERE status='done' ORDER BY created_at,id"):
        result = job["result_json"] or {}
        for name in {result.get("clip_filename"), result.get("output_filename")} - {None, ""}:
            jobs[name].append(job)
    plans = defaultdict(list)
    plan_errors = []
    plan_count = 0
    for root in (ROOT / "app/video_shorts/private_state", ROOT / "app/video_shorts/private_state/legacy", ROOT / "app/video_shorts/static/shorts"):
        if not root.exists():
            continue
        for path in sorted(root.glob("*_plan*.json")):
            match = re.fullmatch(r"(.+)_plan(?:_v\d+)?\.json", path.name)
            if not match:
                continue
            key = match[1]
            pk = key if key.isdecimal() else frozen.get(key)
            source = sources.get(pk)
            try:
                data = json.loads(path.read_text())
                entries = data.get("plan") or data.get("clips") or []
                plan_count += 1
                for entry in entries:
                    for name in {entry.get("clip_filename"), entry.get("output_filename")} - {None, ""}:
                        plans[name].append(dict(path=str(path.relative_to(ROOT)), video_pk=pk,
                                                owner=identity(source) if source else None, render_job_id=entry.get("render_job_id")))
            except (ValueError, AttributeError, TypeError) as exc:
                plan_errors.append(dict(path=str(path), error=str(exc)))
    counts, groups = Counter(), defaultdict(list)
    for table, query in (
        ("lifecycle", "SELECT id,user_id,brand_id,clip_filename,output_filename,raw_plan_entry_json,created_at,publish_status FROM shorts_generated_videos"),
        ("asset", "SELECT file_key,file_path,file_type,user_id,brand_id,status FROM shorts_storage_assets"),
    ):
        for row in rows(cur, query):
            counts[table + "_total"] += 1
            if table == "lifecycle":
                names = {row.get("clip_filename"), row.get("output_filename")} - {None, ""}
            else:
                names = {row["file_key"][6:]} if row["file_key"].startswith("short:") else {Path(row.get("file_path") or "").name}
            matched = {j["id"]: j for name in names for j in jobs[name]}
            refs = [p for name in names for p in plans[name]]
            recorded = identity(row)
            job_scopes = set()
            evidence = []
            for job in matched.values():
                payload = job["payload_json"] or {}
                owner = str(job["user_id"] or "")
                embedded_brand = owner.split("::", 1)[1] if "::" in owner else ""
                owner = owner.split("::", 1)[0]
                pk = str(payload.get("video_pk") or "")
                source = sources.get(pk)
                brand = str(payload.get("brand_id") or embedded_brand or (source or {}).get("brand_id") or "")
                pair = (owner, brand)
                job_scopes.add(pair)
                evidence.append(dict(job_id=job["id"], owner=pair, video_pk=pk, source_owner=identity(source) if source else None,
                                     created_at=job["created_at"], finished_at=job["finished_at"]))
            if not matched:
                counts[table + "_no_exact_successful_job"] += 1
            elif len(job_scopes) > 1:
                counts[table + "_ambiguous_job_owners"] += 1
            else:
                counts[table + "_exact_job_unambiguous"] += 1
            foreign_jobs = job_scopes - {recorded}
            foreign_plans = [p for p in refs if p["owner"] and p["owner"] != recorded]
            source_conflicts = [j for j in evidence if j["source_owner"] and j["source_owner"] != j["owner"]]
            if foreign_jobs or foreign_plans or source_conflicts:
                counts[table + "_flagged"] += 1
                item = dict(table=table, row_id=row.get("id", row.get("file_key")), filenames=sorted(names),
                            recorded=scope(recorded), creator_candidates=[scope(pair) for pair in sorted(job_scopes)],
                            jobs=evidence, plans=refs, status=row.get("publish_status", row.get("status")),
                            ambiguous=len(job_scopes) > 1, job_mismatch=bool(foreign_jobs), plan_mismatch=bool(foreign_plans),
                            job_source_mismatch=bool(source_conflicts))
                emit(dict(mismatch=item))
                for pair in foreign_jobs:
                    groups[(recorded, pair)].append((table, tuple(sorted(names))))
    for (recorded, creator), entries in sorted(groups.items()):
        emit(dict(group=dict(recorded=scope(recorded), creator=scope(creator), row_counts=dict(Counter(t for t, _ in entries)),
                             filenames=sorted({name for _, names in entries for name in names}))))
    emit(dict(audit_summary=dict(counts), plans_read=plan_count, plan_errors=plan_errors))
    conn.rollback()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--audit", action="store_true")
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir (new directory outside repository)")
    _load_env_file()
    conn = _connect()
    try:
        audit(conn) if args.audit else repair(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
