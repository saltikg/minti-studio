#!/usr/bin/env python3
"""Manual, idempotent private-state migration and frozen legacy compilation registration."""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / '.env')
from app.video_shorts.config import SHORTS_DIR, PRIVATE_STATE_DIR
from app.video_shorts.services import compilation_media, tenant_media
from app.video_shorts.services.db import get_db, get_db_readonly, table_columns


def legacy_identity(conn, path, kind):
    if kind == 'long_from_shorts':
        match = re.fullmatch(r'long_from_shorts_(.+)_\d{8}_\d{6}\.mp4', path.name)
        source_ids = [match[1]] if match else []
        brand = None
    else:
        name = path.with_suffix('.meta.json').name
        meta_path = next((p for p in (SHORTS_DIR / name, PRIVATE_STATE_DIR / 'legacy' / name,
                                      PRIVATE_STATE_DIR / 'compilations' / name) if p.is_file()), None)
        if not meta_path:
            return None
        meta = json.loads(meta_path.read_text())
        source_ids = meta.get('video_ids') or []
        brand = meta.get('brand_id')
        if not brand:
            return None
    matches = []
    for source_id in source_ids:
        rows = conn.execute('''SELECT DISTINCT v.id,v.video_id,v.owner_user_id,v.brand_id
            FROM shorts_video_legacy_owners l JOIN youtube_videos v ON v.id=l.video_pk
            LEFT JOIN shorts_generated_videos g ON g.source_video_id=l.youtube_id
             AND g.user_id=v.owner_user_id AND g.brand_id IS NOT DISTINCT FROM v.brand_id
            WHERE l.youtube_id=? OR g.youtube_video_id=? OR g.instagram_media_id=?
               OR g.facebook_video_id=? OR g.tiktok_video_id=?''', [str(source_id)] * 5).fetchall()
        if not rows:
            return None
        matches.extend(rows)
    if not matches or len({(str(r[2]), r[3]) for r in matches}) != 1:
        return None
    row = sorted(matches, key=lambda r: int(r[0]))[0]
    if brand and str(row[3]) != str(brand):
        return None
    return tenant_media.VideoIdentity(int(row[0]), str(row[1]), str(row[2]), row[3])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    counts = dict(state_files=0, moved=0, already_archived=0, compilations=0, registered=0, already_registered=0, unresolved=0)
    conn = get_db() if args.apply else get_db_readonly()
    try:
        if args.apply:
            compilation_media.create_schema(conn)
            conn.commit()
        has_registry = bool(table_columns(conn, "shorts_compilations"))
        for kind in ('long_from_shorts', 'monthly_top'):
            for path in SHORTS_DIR.glob(kind + '_*.mp4'):
                counts['compilations'] += 1
                if has_registry and conn.execute("SELECT 1 FROM shorts_compilations WHERE filename = ?", [path.name]).fetchone():
                    counts['already_registered'] += 1
                    continue
                video = legacy_identity(conn, path, kind)
                if video is None:
                    counts['unresolved'] += 1
                    print(json.dumps({'unresolved_compilation': path.name}), flush=True)
                elif args.apply:
                    compilation_media.register_compilation(path, video, kind, conn=conn)
                    counts['registered'] += 1
        if args.apply:
            conn.commit()
        for path in SHORTS_DIR.glob('*.json'):
            counts['state_files'] += 1
            target = PRIVATE_STATE_DIR / 'legacy' / path.name
            if args.apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(path, target)
                    counts['moved'] += 1
                except FileExistsError:
                    # Refuse to discard different data from a previous interrupted run.
                    if target.read_bytes() != path.read_bytes():
                        raise RuntimeError(f'Private state conflict: {path.name}')
                    counts['already_archived'] += 1
                path.unlink(missing_ok=True)
        print(json.dumps({'apply': args.apply, **counts}), flush=True)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
