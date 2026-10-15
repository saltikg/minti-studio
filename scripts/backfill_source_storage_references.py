#!/usr/bin/env python3
"""Manually persist HEAD-verified legacy source keys before stored-key consumers deploy.

Default is read-only. --apply requires a new external backup directory. Only
video_url changes; media, owners, brands and frozen registry entries are untouched.
The complete original source rows and restore SQL are saved before any UPDATE.
"""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path

from safe_to_deploy import ROOT, _load_env_file, _connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir")
    _load_env_file()
    import boto3
    client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
    bucket = os.environ["S3_BUCKET_NAME"]
    keys = defaultdict(list)
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="videos/"):
        for obj in page.get("Contents", []):
            path = Path(obj["Key"])
            if path.parent.as_posix() == "videos" and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
                keys[path.stem].append(obj["Key"])
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT * FROM youtube_videos WHERE download_status='downloaded' AND COALESCE(video_url,'') NOT LIKE 's3://%'")
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        changes, skipped = [], Counter()
        checked = set()
        for row in rows:
            candidates = keys[row["video_id"]]
            if len(candidates) != 1:
                skipped["no_object" if not candidates else "ambiguous_objects"] += 1
                continue
            key = candidates[0]
            if key not in checked:
                client.head_object(Bucket=bucket, Key=key)
                checked.add(key)
            changes.append(dict(before=row, after="s3://" + key))
        conn.rollback()
        print(json.dumps(dict(mode="APPLY" if args.apply else "READ_ONLY", candidates=len(rows),
                              verified_changes=len(changes), objects=len(checked), skipped=dict(skipped))))
        if not args.apply or not changes:
            return
        backup = Path(args.backup_dir).resolve()
        if backup.is_relative_to(ROOT):
            raise ValueError("Backup must be outside the repository")
        backup.mkdir(mode=0o700, exist_ok=False)
        (backup / "rows.json").write_text(json.dumps(changes, default=str, indent=2))
        restore = ["BEGIN;"]
        for change in changes:
            old = change["before"]
            value = "NULL" if old["video_url"] is None else "'" + old["video_url"].replace("'", "''") + "'"
            restore.append(f"UPDATE main.youtube_videos SET video_url={value} WHERE id={int(old['id'])};")
        (backup / "restore.sql").write_text("\n".join(restore + ["COMMIT;"]) + "\n")
        for path in backup.iterdir():
            os.chmod(path, 0o600)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        cur.execute("SET LOCAL lock_timeout='5s'")
        for change in changes:
            old = change["before"]
            cur.execute("""UPDATE youtube_videos SET video_url=%s WHERE id=%s
                AND video_url IS NOT DISTINCT FROM %s AND owner_user_id IS NOT DISTINCT FROM %s
                AND brand_id IS NOT DISTINCT FROM %s""",
                [change["after"], old["id"], old["video_url"], old["owner_user_id"], old["brand_id"]])
            if cur.rowcount != 1:
                raise RuntimeError("Source changed concurrently; transaction rolled back")
        conn.commit()
        print(json.dumps(dict(updated=len(changes), backup=str(backup))))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
