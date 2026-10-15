"""Stored ownership for long and monthly compilations; no filename-based listing."""
from __future__ import annotations
import json
import re
from pathlib import Path
from app.video_shorts.config import PRIVATE_STATE_DIR, SHORTS_DIR
from app.video_shorts.services import tenant_media
from app.video_shorts.services.db import get_db, get_db_readonly


def create_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS shorts_compilations (
        filename VARCHAR PRIMARY KEY, video_pk BIGINT NOT NULL,
        owner_user_id VARCHAR NOT NULL, brand_id VARCHAR, kind VARCHAR NOT NULL,
        storage_file_key VARCHAR NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")


def compilation_filename(video, kind, label):
    if kind not in {"long_from_shorts", "monthly_top"}:
        raise ValueError("Unknown compilation kind")
    # Reuse the canonical clip identity, including video_pk.
    stem = Path(tenant_media.clip_filename(video, 1)).stem.removeprefix("1_")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        raise ValueError("Invalid compilation label")
    return f"{kind}_{stem}_{label}.mp4"


def metadata_path(output_path, kind):
    name = tenant_media.safe_filename(Path(output_path).name)
    suffix = ".long.meta.json" if kind == "long_from_shorts" else ".meta.json"
    basename = Path(name).with_suffix(suffix).name
    path = PRIVATE_STATE_DIR / "compilations" / basename
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        for root in (PRIVATE_STATE_DIR / "legacy", SHORTS_DIR):
            old = root / basename
            if old.is_file():
                import os
                try:
                    os.link(old, path)
                except FileExistsError:
                    pass
                old.unlink(missing_ok=True)
                break
    return path


def register_compilation(path, video, kind, *, conn=None):
    own_connection = conn is None
    conn = conn or get_db()
    try:
        name = tenant_media.safe_filename(Path(path).name)
        key = tenant_media.asset_key(name)
        row = conn.execute("""INSERT INTO shorts_compilations
            (filename, video_pk, owner_user_id, brand_id, kind, storage_file_key)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(filename) DO UPDATE SET kind=excluded.kind
            WHERE shorts_compilations.video_pk=excluded.video_pk
              AND shorts_compilations.owner_user_id=excluded.owner_user_id
              AND shorts_compilations.brand_id IS NOT DISTINCT FROM excluded.brand_id
            RETURNING filename""", [name, video.video_pk, video.owner_user_id, video.brand_id, kind, key]).fetchone()
        if not row:
            raise tenant_media.TenantMediaConflict("Compilation already belongs to another tenant")
        tenant_media.upsert_asset(conn, file_key=key, file_path=str(Path(path).resolve()), file_type="short",
                                  size_bytes=Path(path).stat().st_size, user_id=video.owner_user_id, brand_id=video.brand_id)
        if own_connection:
            conn.commit()
    finally:
        if own_connection:
            conn.close()


def list_compilation_paths(kind, *, video_pk=None, limit=12):
    owner, brand = tenant_media.current_scope()
    if not owner:
        return []
    conn = get_db_readonly()
    try:
        sql = "SELECT filename FROM shorts_compilations WHERE owner_user_id = ? AND brand_id IS NOT DISTINCT FROM ? AND kind = ?"
        params = [owner, brand, kind]
        if video_pk is not None:
            sql += " AND video_pk = ?"
            params.append(int(video_pk))
        rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", [*params, max(1, int(limit))]).fetchall()
        return [tenant_media.short_path(r[0]) for r in rows if tenant_media.short_path(r[0]).is_file()]
    finally:
        conn.close()


def authorize_compilation(filename, *, video_pk=None, kind=None):
    owner, brand = tenant_media.current_scope()
    conn = get_db_readonly()
    try:
        row = conn.execute("SELECT video_pk, kind FROM shorts_compilations WHERE filename = ? AND owner_user_id = ? AND brand_id IS NOT DISTINCT FROM ?",
                           [tenant_media.safe_filename(filename), owner, brand]).fetchone()
        if not row or (video_pk is not None and int(row[0]) != int(video_pk)) or (kind and row[1] != kind):
            raise tenant_media.TenantMediaConflict("Compilation belongs to another tenant or video")
        from app.video_shorts.services.media_access import authorize_file
        return authorize_file(filename, owner_user_id=owner, brand_id=brand, video_pk=row[0])
    finally:
        conn.close()


def monthly_source_identity(rows):
    owner, brand = tenant_media.current_scope()
    conn = get_db_readonly()
    try:
        for item in rows:
            source_id = str(item.get("video_id") or "")
            row = conn.execute("""SELECT DISTINCT v.id FROM youtube_videos v
                LEFT JOIN shorts_generated_videos s ON s.source_video_id=v.video_id
                 AND s.user_id=v.owner_user_id AND s.brand_id IS NOT DISTINCT FROM v.brand_id
                WHERE v.owner_user_id=? AND v.brand_id IS NOT DISTINCT FROM ?
                 AND (v.video_id=? OR s.youtube_video_id=? OR s.instagram_media_id=? OR s.facebook_video_id=? OR s.tiktok_video_id=?)
                ORDER BY v.id LIMIT 1""", [owner, brand, *([source_id]*5)]).fetchone()
            if row:
                return tenant_media.resolve_video(video_pk=row[0], owner_user_id=owner, brand_id=brand)
    finally:
        conn.close()
    raise tenant_media.TenantMediaConflict("Monthly compilation needs a source owned by this tenant")
