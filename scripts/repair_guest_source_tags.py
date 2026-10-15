#!/usr/bin/env python3
"""Manually remove guest expiration tags from current non-guest source references."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / '.env')
from app.video_shorts.services.db import get_db, get_db_readonly
from app.video_shorts.services.guest_objects import non_guest_source_references
from app.video_shorts.services.storage import get_media_storage, GUEST_S3_TAG_KEY
from app.video_shorts.services.tenant_media import lock_source_references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Remove only the guest expiration tag; default is read-only.')
    args = parser.parse_args()
    storage = get_media_storage()
    if storage.backend_name != 's3':
        raise RuntimeError('This repair requires S3 storage')
    counts = dict(scanned=0, non_guest_referenced=0, tagged=0, repaired=0, skipped=0)
    conn = get_db() if args.apply else get_db_readonly()
    try:
        for page in storage.client.get_paginator('list_objects_v2').paginate(Bucket=storage.bucket_name, Prefix='videos/'):
            for obj in page.get('Contents', []):
                key = obj['Key']
                counts['scanned'] += 1
                if not non_guest_source_references(conn, key=key):
                    continue
                counts['non_guest_referenced'] += 1
                tags = storage.client.get_object_tagging(Bucket=storage.bucket_name, Key=key).get('TagSet', [])
                if not any(t['Key'] == GUEST_S3_TAG_KEY for t in tags):
                    continue
                counts['tagged'] += 1
                print(json.dumps({'key': key, 'action': 'remove_guest_expiration_tag' if args.apply else 'would_remove_guest_expiration_tag'}), flush=True)
                if args.apply:
                    lock_source_references(conn)
                    if non_guest_source_references(conn, key=key) and storage.delete_tag(key, GUEST_S3_TAG_KEY):
                        counts['repaired'] += 1
                    else:
                        counts['skipped'] += 1
                    conn.commit()
        print(json.dumps({'apply': args.apply, **counts}), flush=True)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
