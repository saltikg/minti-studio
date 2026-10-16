"""Canonical identities for tenant-owned state and generated media.

YouTube IDs identify shared inputs. Only youtube_videos.id identifies a tenant's
editable state. Callers outside a request must supply that primary key.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from flask import g, has_request_context, request

from app.video_shorts.config import SHORTS_DIR, PRIVATE_STATE_DIR, VIDEOS_DIR
from app.video_shorts.services.db import get_db_readonly

log = logging.getLogger(__name__)
SOURCE_VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


class TenantMediaConflict(ValueError):
    pass


@dataclass(frozen=True)
class VideoIdentity:
    video_pk: int
    youtube_id: str
    owner_user_id: str
    brand_id: str | None


def current_scope():
    if not has_request_context():
        return None, None
    from app.video_shorts.services.admin_operation_scope import request_admin_operation_scope
    from app.video_shorts.services.brands import current_brand_id
    scope = request_admin_operation_scope() or {}
    return (scope.get("owner_user_id") or (getattr(g, "vs_current_user", None) or {}).get("id"),
            scope.get("brand_id") or current_brand_id())


def resolve_video(video_id=None, *, video_pk=None, owner_user_id=None, brand_id=None):
    """Resolve an explicit row or an authenticated, unambiguous tenant scope."""
    if isinstance(video_id, VideoIdentity):
        return video_id
    if video_pk is None and owner_user_id is None and has_request_context():
        owner_user_id, brand_id = current_scope()
    if video_pk is None and not owner_user_id:
        raise TenantMediaConflict("A video_pk or authenticated owner/brand is required")
    conn = get_db_readonly()
    try:
        sql = "SELECT id, video_id, owner_user_id, brand_id FROM youtube_videos WHERE "
        if video_pk is not None:
            sql += "id = ?"
            params = [int(video_pk)]
            if video_id:
                sql += " AND video_id = ?"
                params.append(str(video_id))
        elif owner_user_id:
            sql += "video_id = ? AND owner_user_id = ? AND brand_id IS NOT DISTINCT FROM ?"
            params = [str(video_id), str(owner_user_id), brand_id]
        else:
            raise TenantMediaConflict("A video_pk or authenticated owner/brand is required")
        rows = conn.execute(sql, params).fetchall()
        if len(rows) != 1:
            raise TenantMediaConflict("Video identity is missing or ambiguous")
        row = rows[0]
        if owner_user_id is not None and (str(row[2] or "") != str(owner_user_id) or str(row[3] or "") != str(brand_id or "")):
            raise TenantMediaConflict("Video does not belong to the requested tenant")
        return VideoIdentity(int(row[0]), str(row[1]), str(row[2] or ""), str(row[3]) if row[3] else None)
    finally:
        conn.close()


def _legacy_owner(conn, video):
    # Frozen at cutover. No foreign key: deleting the legacy owner must never
    # transfer its state to the next tenant with the same YouTube ID.
    row = conn.execute("SELECT video_pk FROM shorts_video_legacy_owners WHERE youtube_id = ?", [video.youtube_id]).fetchone()
    return int(row[0]) if row else None


def state_path(video_id=None, kind="plan", *, video_pk=None, read=False, migrate=True):
    if kind not in {"suggestions", "plan", "plan_v2", "plan_v3", "plan_v4", "auto_suggest_render", "non_speech_overrides", "debug", "debug_v2", "debug_v3", "debug_v4"}:
        raise ValueError("Unknown video state kind")
    video = resolve_video(video_id, video_pk=video_pk)
    path = PRIVATE_STATE_DIR / f"{video.video_pk}_{kind}.json"
    if not path.exists():
        names = [f"{video.video_pk}_{kind}.json"]
        conn = get_db_readonly()
        try:
            if kind != "suggestions" and _legacy_owner(conn, video) == video.video_pk:
                names.append(f"{video.youtube_id}_{kind}.json")
        finally:
            conn.close()
        for name in names:
            for root in (PRIVATE_STATE_DIR / "legacy", SHORTS_DIR):
                legacy = root / name
                if legacy.is_file():
                    if not migrate:
                        return legacy
                    path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        # Link without replacing a concurrent reader/writer's result.
                        import os
                        os.link(legacy, path)
                    except FileExistsError:
                        pass
                    except FileNotFoundError:
                        if not path.exists():
                            continue
                    legacy.unlink(missing_ok=True)
                    return path
    return path


def clip_filename(video_id, plan_index, *, video_pk=None):
    video = resolve_video(video_id, video_pk=video_pk)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video.youtube_id) or int(plan_index) < 1:
        raise ValueError("Invalid clip identity")
    return f"{int(plan_index)}_{video.youtube_id}_v{video.video_pk}.mp4"


def safe_filename(filename):
    value = str(filename or "")
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError("Invalid media filename")
    return value


def short_key(filename):
    location = clip_location(filename)
    # Only a new output (or an unindexed legacy file) needs a generated name.
    return location["storage_key"] if location else f"shorts/{safe_filename(filename)}"


def asset_key(filename):
    return f"short:{safe_filename(filename)}"


def short_path(filename):
    location = clip_location(filename)
    return location["local_path"] if location else SHORTS_DIR / safe_filename(filename)


def storage_location(reference):
    """Decode a persisted storage reference without deriving it from a media ID."""
    from app.video_shorts.services.storage import get_media_storage
    raw = str(reference or "").strip()
    if not raw:
        raise TenantMediaConflict("Stored media location is missing")
    root = get_media_storage("local").root_dir
    if raw.startswith("s3://"):
        key = raw[5:]
    else:
        path = Path(raw)
        if path.is_absolute():
            try:
                key = path.resolve().relative_to(root).as_posix()
            except ValueError:
                if path.resolve().is_relative_to(VIDEOS_DIR.resolve()):
                    key = "videos/" + path.resolve().relative_to(VIDEOS_DIR.resolve()).as_posix()
                elif "/app/video_shorts/videos/" in raw:
                    key = "videos/" + raw.split("/app/video_shorts/videos/", 1)[1]
                elif "/app/video_shorts/static/" in raw:
                    key = raw.split("/app/video_shorts/static/", 1)[1]
                else:
                    raise TenantMediaConflict("Stored path is outside media storage")
        else:
            key = raw
    if not key or key.startswith("/") or ".." in Path(key).parts or "://" in key:
        raise TenantMediaConflict("Invalid stored media key")
    return {"storage_key": key, "local_path": (VIDEOS_DIR / key[7:] if key.startswith("videos/") else root / key)}


def clip_location(filename, *, conn=None):
    name = safe_filename(filename)
    own = conn is None
    conn = conn or get_db_readonly()
    try:
        keys = conn.execute("""SELECT DISTINCT storage_file_key FROM shorts_generated_videos
            WHERE (clip_filename=? OR output_filename=?) AND storage_file_key IS NOT NULL""", [name, name]).fetchall()
        if len(keys) > 1:
            raise TenantMediaConflict("Ambiguous stored clip keys")
        key = keys[0][0] if keys else asset_key(name)
        row = conn.execute("SELECT file_path FROM shorts_storage_assets WHERE file_key=?", [key]).fetchone()
        if not row:
            return None
        return {**storage_location(row[0]), "file_key": key}
    finally:
        if own:
            conn.close()


def clip_exists(filename, *, conn=None):
    from app.video_shorts.services.storage import get_media_storage
    location = clip_location(filename, conn=conn)
    if location is None:
        location = storage_location(f"shorts/{safe_filename(filename)}")
    if location["local_path"].is_file():
        return True
    storage = get_media_storage()
    return storage.backend_name == "s3" and storage.exists(location["storage_key"])


def source_locations(video_id, *, video_pk=None, conn=None):
    """Resolve stored source URLs/assets; unscoped workers may only share one key."""
    own = conn is None
    conn = conn or get_db_readonly()
    try:
        sql = "SELECT id, video_url, owner_user_id, brand_id FROM youtube_videos WHERE video_id=?"
        params = [str(video_id)]
        if video_pk is not None:
            sql += " AND id=?"
            params.append(int(video_pk))
        elif has_request_context():
            owner, brand = current_scope()
            if owner:
                sql += " AND owner_user_id=? AND brand_id IS NOT DISTINCT FROM ?"
                params.extend([owner, brand])
        locations = {}
        for pk, url, owner, brand in conn.execute(sql, params).fetchall():
            refs = [url] if str(url or "").startswith("s3://") else []
            if not refs:
                assets = conn.execute("""SELECT file_key,file_path FROM shorts_storage_assets
                    WHERE file_type='downloaded' AND CAST(user_id AS VARCHAR)=?
                    AND brand_id IS NOT DISTINCT FROM ?""", [owner, brand]).fetchall()
                refs = [path for key, path in assets if Path(str(key).removeprefix("downloaded:")).stem == str(video_id)]
            for ref in refs:
                location = storage_location(ref)
                locations[location["storage_key"]] = location
        if video_pk is None and len(locations) > 1:
            raise TenantMediaConflict("Source has multiple stored locations; video_pk required")
        return list(locations.values())
    finally:
        if own:
            conn.close()


def poster_key(filename):
    return f"shorts/posters/{safe_filename(filename)}"


def assert_asset_owner(conn, file_key, user_id, brand_id):
    row = conn.execute("SELECT user_id, brand_id FROM shorts_storage_assets WHERE file_key = ?", [file_key]).fetchone()
    if row and (str(row[0] or "") != str(user_id or "") or str(row[1] or "") != str(brand_id or "")):
        log.error("Tenant asset collision key=%s existing_owner=%s existing_brand=%s requested_owner=%s requested_brand=%s", file_key, row[0], row[1], user_id, brand_id)
        raise TenantMediaConflict("Media key belongs to another tenant")


def assert_clip_write_owner(conn, filename, owner_user_id, brand_id, *, video_pk=None, source_id=None):
    """Authorize a clip mutation independently of mutable lifecycle ownership."""
    name = safe_filename(filename)
    scoped = re.fullmatch(r"\d+_([A-Za-z0-9_-]+)_v(\d+)\.mp4", name)
    legacy = re.fullmatch(r"\d+_([A-Za-z0-9_-]+)\.mp4", name)
    if not scoped and not legacy:
        return  # AI videos and compilations have their own ownership records.
    source = (scoped or legacy)[1]
    if scoped:
        pk = int(scoped[2])
    else:
        row = conn.execute(
            "SELECT video_pk FROM shorts_video_legacy_owners WHERE youtube_id = ?", [source],
        ).fetchone()
        if not row:
            raise TenantMediaConflict("Legacy clip has no frozen owner")
        pk = int(row[0])
    row = conn.execute(
        "SELECT video_id, owner_user_id, brand_id FROM youtube_videos WHERE id = ?", [pk],
    ).fetchone()
    # A source can be rehomed from YouTube to a local upload without renaming
    # its old clips. The frozen/scoped PK remains authoritative in that case.
    if (not row or not owner_user_id
            or (source_id is not None and str(row[0]) != str(source_id))
            or str(row[1] or "") != str(owner_user_id)
            or str(row[2] or "") != str(brand_id or "")
            or (video_pk is not None and int(video_pk) != pk)):
        raise TenantMediaConflict("Clip is reserved for its original video owner and brand")


def upsert_asset(conn, *, file_key, file_path, file_type, size_bytes, user_id, brand_id=None):
    if file_key.startswith("short:"):
        assert_clip_write_owner(conn, file_key.removeprefix("short:"), user_id, brand_id)
    row = conn.execute(
        """INSERT INTO shorts_storage_assets
           (file_key, file_path, file_type, size_bytes, user_id, brand_id, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'active', now())
           ON CONFLICT(file_key) DO UPDATE SET
             file_path = excluded.file_path, file_type = excluded.file_type,
             size_bytes = excluded.size_bytes, status = 'active', updated_at = now()
           WHERE shorts_storage_assets.user_id IS NOT DISTINCT FROM excluded.user_id
             AND shorts_storage_assets.brand_id IS NOT DISTINCT FROM excluded.brand_id
           RETURNING file_key""",
        [file_key, file_path, file_type, size_bytes, user_id, brand_id],
    ).fetchone()
    if not row:
        log.error("Tenant asset upsert collision key=%s requested_owner=%s requested_brand=%s", file_key, user_id, brand_id)
        raise TenantMediaConflict("Asset key belongs to another tenant")


def other_source_references(conn, youtube_id, *, excluding_pks=()):
    sql = "SELECT 1 FROM youtube_videos WHERE video_id = ?"
    params = [youtube_id]
    if excluding_pks:
        sql += " AND id NOT IN (" + ",".join("?" for _ in excluding_pks) + ")"
        params.extend(int(pk) for pk in excluding_pks)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def lock_source_references(conn):
    # Hold through reference check, filesystem deletion, and row removal. This
    # prevents an import from inserting a new reference between check/delete.
    if getattr(conn, "backend_name", "") == "postgres":
        conn.execute("LOCK TABLE youtube_videos IN SHARE ROW EXCLUSIVE MODE")


def delete_source_media(conn, youtube_id, *, excluding_pks=()):
    from app.video_shorts.services.storage import get_media_storage
    from app.video_shorts.services.media_utils import purge_source_cache_for_video
    lock_source_references(conn)
    if other_source_references(conn, youtube_id, excluding_pks=excluding_pks):
        return False
    purge_source_cache_for_video(youtube_id)
    storage = get_media_storage()
    local = get_media_storage("local")
    for location in source_locations(youtube_id, conn=conn):
        key = location["storage_key"]
        params = [f"s3://{key}"]
        sql = "SELECT 1 FROM youtube_videos WHERE video_url=?"
        if excluding_pks:
            sql += " AND id NOT IN (" + ",".join("?" for _ in excluding_pks) + ")"
            params.extend(excluding_pks)
        if conn.execute(sql + " LIMIT 1", params).fetchone():
            continue
        if storage.backend_name == "s3" and storage.exists(key):
            storage.delete(key)
        local.delete(key)
        location["local_path"].unlink(missing_ok=True)
    return True


def render_filename(video_id, entry, plan_index, *, video_pk=None):
    video = resolve_video(video_id, video_pk=video_pk)
    name = entry.get("output_filename") or entry.get("clip_filename")
    conn = get_db_readonly()
    try:
        if name:
            row = conn.execute(
                """SELECT clip_filename, output_filename, storage_file_key
                   FROM shorts_generated_videos WHERE source_video_id = ?
                   AND user_id = ? AND brand_id IS NOT DISTINCT FROM ?
                   AND (clip_filename = ? OR output_filename = ?)""",
                [video.youtube_id, video.owner_user_id, video.brand_id, name, name],
            ).fetchone()
            if row:
                stored = str(row[2] or "")
                name = row[1] or row[0]
                assert_asset_owner(conn, stored or asset_key(name), video.owner_user_id, video.brand_id)
                authorize_clip_delete(conn, name, video_pk=video.video_pk,
                                      owner_user_id=video.owner_user_id, brand_id=video.brand_id)
                return safe_filename(name)
            # A frozen owner's existing legacy media must survive unrelated edits,
            # even before the manual lifecycle/asset reconciliation has run.
            if (re.fullmatch(r"\d+_[A-Za-z0-9_-]+\.mp4", name)
                    and not re.search(r"_v\d+\.mp4$", name) and clip_exists(name, conn=conn)):
                authorize_clip_delete(conn, name, video_pk=video.video_pk,
                                      owner_user_id=video.owner_user_id, brand_id=video.brand_id)
                return safe_filename(name)
        name = clip_filename(video, plan_index)
        assert_asset_owner(conn, asset_key(name), video.owner_user_id, video.brand_id)
        return name
    finally:
        conn.close()


def authorize_clip_delete(conn, filename, *, video_pk=None, owner_user_id=None, brand_id=None):
    name = safe_filename(filename)
    match = re.search(r"_v(\d+)\.mp4$", name)
    if match:
        filename_pk = int(match.group(1))
        if video_pk is not None and int(video_pk) != filename_pk:
            raise TenantMediaConflict("Clip belongs to a different video row")
        video_pk = filename_pk
    rows = conn.execute(
        """SELECT DISTINCT v.id, v.video_id, v.owner_user_id, v.brand_id
           FROM youtube_videos v JOIN shorts_generated_videos s
             ON s.source_video_id = v.video_id AND s.user_id = v.owner_user_id
             AND s.brand_id IS NOT DISTINCT FROM v.brand_id
           WHERE (s.clip_filename = ? OR s.output_filename = ?)
             AND s.user_id = ? AND s.brand_id IS NOT DISTINCT FROM ?""",
        [name, name, owner_user_id, brand_id],
    ).fetchall()
    if video_pk is not None:
        rows = [r for r in rows if int(r[0]) == int(video_pk)]
    if not rows:
        existing = conn.execute("SELECT 1 FROM shorts_generated_videos WHERE clip_filename=? OR output_filename=?",
                                [name, name]).fetchone()
        legacy = re.fullmatch(r"\d+_([A-Za-z0-9_-]+)\.mp4", name)
        if not existing and legacy and not match:
            assert_clip_write_owner(conn, name, owner_user_id, brand_id, video_pk=video_pk)
            rows = conn.execute("""SELECT v.id,v.video_id,v.owner_user_id,v.brand_id
                FROM youtube_videos v JOIN shorts_video_legacy_owners l ON l.video_pk=v.id
                WHERE l.youtube_id=?""", [legacy[1]]).fetchall()
    if len(rows) != 1:
        raise TenantMediaConflict("Clip is not bound to this tenant's video")
    location = clip_location(name, conn=conn)
    assert_asset_owner(conn, location["file_key"] if location else asset_key(name), owner_user_id, brand_id)
    foreign = conn.execute(
        """SELECT 1 FROM shorts_generated_videos
           WHERE (clip_filename = ? OR output_filename = ?) AND
             (user_id IS DISTINCT FROM ? OR brand_id IS DISTINCT FROM ?)
           LIMIT 1""", [name, name, owner_user_id, brand_id],
    ).fetchone()
    if foreign:
        raise TenantMediaConflict("Legacy clip is referenced by another tenant; manual review required")
    row = rows[0]
    assert_clip_write_owner(conn, name, owner_user_id, brand_id,
                            video_pk=row[0], source_id=row[1])
    return VideoIdentity(int(row[0]), str(row[1]), str(row[2]), str(row[3]) if row[3] else None)


def load_state(video_id, kind="plan", *, video_pk=None, migrate=True):
    video = resolve_video(video_id, video_pk=video_pk)
    path = state_path(video, kind, read=True, migrate=migrate)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        return {}
    if kind.startswith("plan"):
        # Old shared plans may contain another tenant's completed clips. Never
        # expose those entries through the legacy-owner fallback.
        cache_key = (video.youtube_id, video.owner_user_id, video.brand_id)
        request_cache = g.setdefault("_tenant_foreign_plan_names", {}) if has_request_context() else {}
        foreign_names = request_cache.get(cache_key)
        if foreign_names is None:
            conn = get_db_readonly()
            try:
                foreign = conn.execute(
                    """SELECT clip_filename, output_filename FROM shorts_generated_videos
                       WHERE source_video_id = ? AND
                       (user_id IS DISTINCT FROM ? OR brand_id IS DISTINCT FROM ?)""",
                    [video.youtube_id, video.owner_user_id, video.brand_id],
                ).fetchall()
                foreign_names = {str(name) for row in foreign for name in row if name}
            finally:
                conn.close()
            request_cache[cache_key] = foreign_names
        for key in ("plan", "clips"):
            if isinstance(data.get(key), list):
                data[key] = [e for e in data[key] if isinstance(e, dict) and not any(e.get(k) in foreign_names for k in ("clip_filename", "output_filename"))]
    return data


def write_state(video_id, data, kind="plan", *, video_pk=None):
    video = resolve_video(video_id, video_pk=video_pk)
    if kind.startswith("plan"):
        data = dict(data)
        for field in ("plan", "clips"):
            if not isinstance(data.get(field), list):
                continue
            prepared = []
            for raw in data[field]:
                if not isinstance(raw, dict):
                    continue
                entry = dict(raw)
                index = entry.get("plan_index")
                if index is not None:
                    previous = entry.get("output_filename") or entry.get("clip_filename")
                    name = render_filename(video, entry, index)
                    entry["clip_filename"] = name
                    if previous and previous != name:
                        for key in ("output_filename", "poster_filename", "storage_file_key", "render_job_id"):
                            entry.pop(key, None)
                        if entry.get("status") == "created":
                            entry["status"] = "pending"
                prepared.append(entry)
            data[field] = prepared
    path = state_path(video, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replace atomically so readers never see a partially serialized plan.
    import os
    import tempfile
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def iter_plans(*, owner_user_id=None, brand_id=None, migrate=True):
    """Enumerate rows, never infer tenant identities by splitting filenames."""
    if owner_user_id is None and has_request_context():
        owner_user_id, brand_id = current_scope()
        if not owner_user_id:
            raise TenantMediaConflict("Plan enumeration requires an owner")
    conn = get_db_readonly()
    try:
        sql = "SELECT id, video_id, owner_user_id, brand_id FROM youtube_videos"
        params = []
        if owner_user_id:
            sql += " WHERE owner_user_id = ? AND brand_id IS NOT DISTINCT FROM ?"
            params = [owner_user_id, brand_id]
        elif brand_id:
            sql += " WHERE brand_id = ?"
            params = [brand_id]
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    for row in rows:
        video = VideoIdentity(int(row[0]), str(row[1]), str(row[2] or ""), str(row[3]) if row[3] else None)
        path = PRIVATE_STATE_DIR / f"{video.video_pk}_plan.json"
        if not path.exists():
            candidates = [root / name for root in (PRIVATE_STATE_DIR / "legacy", SHORTS_DIR)
                          for name in (f"{video.video_pk}_plan.json", f"{video.youtube_id}_plan.json")]
            if not any(p.is_file() for p in candidates):
                continue
            path = state_path(video, read=True, migrate=migrate)
        if path.exists():
            yield video, path
