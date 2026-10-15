#!/usr/bin/env python3
"""Manually reconcile existing legacy media; default is a read-only inventory.

Only insert missing metadata when a frozen registry or exact, unambiguous
successful render establish ownership. Never overwrite metadata,
reassign a registry, resurrect a deleted source, or infer publishing success.
--apply requires a new backup directory outside the repository.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import sys

from safe_to_deploy import ROOT, _connect, _load_env_file


def query(cur, sql, args=()):
    cur.execute(sql, args)
    return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]


def inventory(cur, storage, local):
    sources = {str(r["id"]): r for r in query(cur, "SELECT id,video_id,owner_user_id,brand_id FROM youtube_videos")}
    frozen = {r["youtube_id"]: str(r["video_pk"]) for r in query(cur, "SELECT youtube_id,video_pk FROM shorts_video_legacy_owners")}
    brands = {r["id"]: r["name"] for r in query(cur, "SELECT id,name FROM shorts_brands")}
    lifecycle = {n: r for r in query(cur, "SELECT * FROM shorts_generated_videos") for n in (r["clip_filename"], r["output_filename"]) if n}
    assets = {r["file_key"]: r for r in query(cur, "SELECT * FROM shorts_storage_assets")}
    jobs = defaultdict(list)
    for row in query(cur, "SELECT id,user_id,payload_json,result_json,finished_at FROM shorts_render_jobs WHERE status='done'"):
        jobs[(row["result_json"] or {}).get("clip_filename")].append(row)
    plans = {}
    for root in (ROOT / "app/video_shorts/private_state", ROOT / "app/video_shorts/private_state/legacy", ROOT / "app/video_shorts/static/shorts"):
        for path in root.glob("*_plan.json"):
            data = json.loads(path.read_text())
            for entry in data.get("plan", data.get("clips", [])):
                name = entry.get("output_filename") or entry.get("clip_filename")
                if name:
                    plans.setdefault(name, entry)
    objects = {}
    for backend in (storage, local) if storage.backend_name != "local" else (local,):
        for obj in backend.list_prefix("shorts/"):
            name = Path(obj.key).name
            match = re.fullmatch(r"\d+_([A-Za-z0-9_-]+)\.mp4", name)
            if not match or re.search(r"_v\d+\.mp4$", name) or name.endswith("_trimmed.mp4"):
                continue
            objects.setdefault(name, (backend, obj, match[1]))
    # Stored locations can differ from the logical clip filename.
    for name in set(plans) | set(lifecycle):
        match = re.fullmatch(r"\d+_([A-Za-z0-9_-]+)\.mp4", name)
        if not match or re.search(r"_v\d+\.mp4$", name):
            continue
        asset = assets.get((lifecycle.get(name) or {}).get("storage_file_key") or "short:" + name)
        if asset and str(asset["file_path"]).startswith("s3://"):
            from app.video_shorts.services.storage import StorageEntry
            objects[name] = (storage, StorageEntry(key=asset["file_path"][5:], backend="s3", exists=False), match[1])
    results = []
    for name, (backend, obj, source_id) in sorted(objects.items()):
        life = lifecycle.get(name)
        asset_key = (life or {}).get("storage_file_key") or "short:" + name
        asset = assets.get(asset_key)
        if life and asset:
            continue
        if not backend.exists(obj.key):  # S3 HEAD, never trust listing alone.
            continue
        source = sources.get(frozen.get(source_id))
        evidence = "registry" if source else None
        creators = {(j["user_id"], (j["payload_json"] or {}).get("brand_id"), str((j["payload_json"] or {}).get("video_pk"))) for j in jobs[name]}
        if not source and len(creators) == 1:
            owner, brand, pk = next(iter(creators))
            candidate = sources.get(pk)
            if candidate and (candidate["owner_user_id"], candidate["brand_id"]) == (owner, brand):
                source, evidence = candidate, "successful_render"
            elif (not candidate and pk.isdecimal() and asset
                  and (str(asset["user_id"]), asset["brand_id"]) == (owner, brand)
                  and all((j["payload_json"] or {}).get("source_video_id") == source_id for j in jobs[name])):
                # A deleted source must not erase exact creation evidence. Recover
                # only the clip metadata; never recreate its source or registry.
                source = dict(id=int(pk), video_id=source_id, owner_user_id=owner,
                              brand_id=brand, deleted_source=True)
                evidence = "successful_render_and_matching_existing_asset"
        reason = None
        if not source:
            reason = "no_registry_or_unambiguous_creation_evidence"
        else:
            scope = (source["owner_user_id"], source["brand_id"])
            if any((u, b) != scope for u, b, _ in creators):
                reason = "conflicting_render_owners"
            for record in (life, asset):
                if record and (str(record["user_id"] or ""), record["brand_id"]) != scope:
                    reason = "existing_metadata_owner_conflict"
        entry = dict(plans.get(name) or {})
        creation = min(jobs[name], key=lambda j: str(j["finished_at"] or "")) if jobs[name] else None
        if creation:
            entry.setdefault("render_job_id", creation["id"])
        brand = source["brand_id"] if source else (asset or life or {}).get("brand_id")
        size = obj.size_bytes
        if size is None:
            size = (backend.client.head_object(Bucket=backend.bucket_name, Key=obj.key)["ContentLength"]
                    if backend.backend_name == "s3" else backend._key_path(obj.key).stat().st_size)
        results.append(dict(filename=name, source=source, brand_name=brands.get(brand), evidence=evidence,
                            skip_reason=reason, missing_lifecycle=life is None, missing_asset=asset is None,
                            file_key=asset_key, file_path="s3://" + obj.key if backend.backend_name == "s3" else str(backend._key_path(obj.key)),
                            size_bytes=size, entry=entry, created_at=creation["finished_at"] if creation else None))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir")
    _load_env_file()
    sys.path.insert(0, str(ROOT))
    from app.video_shorts.services.storage import get_media_storage
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        candidates = inventory(cur, get_media_storage(), get_media_storage("local"))
        conn.rollback()
        for row in candidates:
            print(json.dumps({k: v for k, v in row.items() if k != "entry"}, default=str), flush=True)
        eligible = [r for r in candidates if not r["skip_reason"]]
        print(json.dumps(dict(mode="APPLY" if args.apply else "READ_ONLY", missing=len(candidates),
                              per_brand=dict(Counter(r["brand_name"] or "UNKNOWN" for r in candidates)),
                              eligible=len(eligible), skipped=len(candidates)-len(eligible))))
        if not args.apply or not eligible:
            print("inserted_lifecycle=0 inserted_assets=0")
            return
        backup = Path(args.backup_dir).resolve()
        if backup.is_relative_to(ROOT):
            raise ValueError("Backup must be outside the repository")
        backup.mkdir(mode=0o700, exist_ok=False)
        (backup / "planned_inserts.json").write_text(json.dumps(eligible, default=str, indent=2))
        os.chmod(backup / "planned_inserts.json", 0o600)
        cur.execute("SET LOCAL lock_timeout='5s'")
        cur.execute("LOCK TABLE shorts_generated_videos,shorts_storage_assets,youtube_videos,shorts_video_legacy_owners IN SHARE ROW EXCLUSIVE MODE")
        inserted = []
        for row in eligible:
            name, source, entry = row["filename"], row["source"], row["entry"]
            # Refuse a concurrent source ownership change since inventory.
            current = query(cur, "SELECT owner_user_id,brand_id FROM youtube_videos WHERE id=%s", [source["id"]])
            if source.get("deleted_source"):
                if current:
                    raise RuntimeError("Deleted source was replaced during reconciliation")
            elif not current or (current[0]["owner_user_id"], current[0]["brand_id"]) != (source["owner_user_id"], source["brand_id"]):
                raise RuntimeError("Source changed during reconciliation")
            for table, clause, params in (
                ("shorts_generated_videos", "clip_filename=%s OR output_filename=%s", [name, name]),
                ("shorts_storage_assets", "file_key=%s", [row["file_key"]]),
            ):
                existing = query(cur, f"SELECT user_id,brand_id FROM {table} WHERE {clause}", params)
                if any((str(r["user_id"]), r["brand_id"]) != (source["owner_user_id"], source["brand_id"]) for r in existing):
                    raise RuntimeError("Metadata ownership changed during reconciliation")
            if row["missing_asset"]:
                cur.execute("""INSERT INTO shorts_storage_assets(file_key,file_path,file_type,size_bytes,user_id,brand_id,status)
                    VALUES (%s,%s,'short',%s,%s,%s,'active') ON CONFLICT DO NOTHING RETURNING file_key""",
                    [row["file_key"], row["file_path"], row["size_bytes"], source["owner_user_id"], source["brand_id"]])
                result = cur.fetchone()
                if result:
                    inserted.append(dict(table="shorts_storage_assets", key="file_key", value=result[0]))
            if row["missing_lifecycle"]:
                # Preserve any explicit publication evidence from the retained plan.
                cur.execute("""INSERT INTO shorts_generated_videos(user_id,brand_id,source_video_id,source_channel_type,
                    clip_filename,output_filename,storage_file_key,generation_status,publish_status,youtube_video_id,raw_plan_entry_json,generated_title,created_at)
                    VALUES (%s,%s,%s,'youtube',%s,%s,%s,'created',%s,%s,%s::jsonb,%s,COALESCE(%s,CURRENT_TIMESTAMP))
                    ON CONFLICT DO NOTHING RETURNING id""", [source["owner_user_id"], source["brand_id"], source["video_id"],
                    name, name, row["file_key"], entry.get("publish_status") or "not_ready",
                    entry.get("yt_video_id") or entry.get("short_video_id"), json.dumps(entry), entry.get("title"), row["created_at"]])
                result = cur.fetchone()
                if result:
                    inserted.append(dict(table="shorts_generated_videos", key="id", value=result[0]))
        # Persist exact inserted IDs before committing; these are the restore manifest.
        path = backup / "inserted_rows.json"
        path.write_text(json.dumps(inserted, indent=2))
        os.chmod(path, 0o600)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        restore = ["BEGIN;"]
        for item in reversed(inserted):
            value = "'" + str(item["value"]).replace("'", "''") + "'"
            restore.append(f"DELETE FROM main.{item['table']} WHERE {item['key']}={value};")
        restore_path = backup / "restore.sql"
        restore_path.write_text("\n".join(restore + ["COMMIT;"]) + "\n")
        os.chmod(restore_path, 0o600)
        with restore_path.open("rb") as handle:
            os.fsync(handle.fileno())
        conn.commit()
        print(json.dumps(dict(inserted=dict(Counter(r["table"] for r in inserted)), backup=str(backup))))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
