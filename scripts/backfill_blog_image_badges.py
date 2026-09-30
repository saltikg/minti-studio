#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_shorts.services.blog_images import STATIC_BLOG_ROOT, _add_logo_badge  # noqa: E402
from app.video_shorts.services.db import get_db_readonly  # noqa: E402


SIDECAR_PATH = STATIC_BLOG_ROOT / ".badge_backfill.json"
BACKUP_ROOT = Path("/home/ubuntu/apps/minti_studio_backups/blog_images")
BADGE_VERSION = "minti_badge_v2"
IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<url>/video_shorts/static/img/blog/[^)\s\"]+)(?:\s+\"(?P<title>[^\"]*)\")?\)")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class BadgeCandidate:
    key: str
    path: Path
    kind: str
    source: Path
    public_backup: Path
    backup_target: Path


def _load_sidecar() -> dict[str, str]:
    if not SIDECAR_PATH.is_file():
        return {}
    try:
        return json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_sidecar(data: dict[str, str]) -> None:
    SIDECAR_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _local_blog_image_path(url: str) -> Path | None:
    parsed = urlparse(url)
    path = unquote(parsed.path or url)
    prefix = "/video_shorts/static/img/blog/"
    if not path.startswith(prefix):
        return None
    relative = path[len(prefix) :]
    if not relative or ".." in Path(relative).parts:
        return None
    candidate = STATIC_BLOG_ROOT / relative
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    if "library" in candidate.relative_to(STATIC_BLOG_ROOT).parts:
        return None
    return candidate


def _published_image_paths() -> list[tuple[Path, str]]:
    conn = get_db_readonly()
    try:
        rows = conn.execute(
            """
            SELECT slug, cover_image_url, content
            FROM blog_articles
            WHERE status = 'published'
            ORDER BY published_at DESC NULLS LAST, created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()
    paths: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for _slug, cover_url, content in rows:
        urls = [(str(cover_url or "").strip(), "cover")]
        for match in IMAGE_RE.finditer(str(content or "")):
            title = str(match.group("title") or "")
            kind = "screenshot" if "[screenshot]" in title.lower() else "inline"
            urls.append((match.group("url"), kind))
        for url, kind in urls:
            if not url:
                continue
            path = _local_blog_image_path(url)
            if path and path.is_file() and path not in seen:
                seen.add(path)
                paths.append((path, kind))
    return paths


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.orig{path.suffix}")


def _external_backup_path(path: Path) -> Path:
    return BACKUP_ROOT / _backup_path(path).relative_to(STATIC_BLOG_ROOT)


def _badge_ratio(kind: str) -> float:
    return 0.14 if kind == "screenshot" else 0.18


def _move_public_backups(paths: list[Path], *, apply: bool) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    for path in paths:
        public_backup = _backup_path(path)
        if not public_backup.is_file():
            continue
        target = _external_backup_path(path)
        moves.append((public_backup, target))
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(public_backup), str(target))
    return moves


def _candidates(sidecar: dict[str, str]) -> list[BadgeCandidate]:
    items: list[BadgeCandidate] = []
    for path, kind in _published_image_paths():
        key = str(path.relative_to(STATIC_BLOG_ROOT))
        if sidecar.get(key) == BADGE_VERSION:
            continue
        public_backup = _backup_path(path)
        external_backup = _external_backup_path(path)
        source = public_backup if public_backup.is_file() else external_backup if external_backup.is_file() else path
        items.append(
            BadgeCandidate(
                key=key,
                path=path,
                kind=kind,
                source=source,
                public_backup=public_backup,
                backup_target=external_backup,
            )
        )
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill MintiStudio logo badges onto published blog images.")
    parser.add_argument("--apply", action="store_true", help="Apply badges. Without this, prints a dry-run list only.")
    args = parser.parse_args()

    sidecar = _load_sidecar()
    candidates = _candidates(sidecar)

    mode = "APPLY" if args.apply else "DRY_RUN"
    print(f"{mode} badge candidates: {len(candidates)}")
    for candidate in candidates:
        source_label = "public-orig" if candidate.source == candidate.public_backup else "backup-orig" if candidate.source == candidate.backup_target else "current"
        print(f"{candidate.key}\t{candidate.kind}\tsource={source_label}")
    public_backup_moves = _move_public_backups([candidate.path for candidate in candidates], apply=False)
    print(f"{mode} public .orig backups to move: {len(public_backup_moves)}")
    for source, target in public_backup_moves:
        print(f"{source.relative_to(STATIC_BLOG_ROOT)} -> {target}")

    if not args.apply:
        return 0

    for candidate in candidates:
        candidate.backup_target.parent.mkdir(parents=True, exist_ok=True)
        if candidate.source == candidate.path and not candidate.backup_target.exists():
            shutil.copy2(candidate.path, candidate.backup_target)
        if candidate.source != candidate.path:
            shutil.copy2(candidate.source, candidate.path)
        _add_logo_badge(candidate.path, badge_ratio=_badge_ratio(candidate.kind), margin_ratio=0.02)
        sidecar[candidate.key] = BADGE_VERSION
    _move_public_backups([candidate.path for candidate in candidates], apply=True)
    _save_sidecar(sidecar)
    print(f"BADGED {len(candidates)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
