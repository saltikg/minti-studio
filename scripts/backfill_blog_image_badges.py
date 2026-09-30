#!/home/ubuntu/apps/minti_studio/.venv/bin/python
from __future__ import annotations

import argparse
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
IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<url>/video_shorts/static/img/blog/[^)\s]+)")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


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


def _published_image_paths() -> list[Path]:
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
    paths: list[Path] = []
    seen: set[Path] = set()
    for _slug, cover_url, content in rows:
        urls = [str(cover_url or "").strip()]
        urls.extend(match.group("url") for match in IMAGE_RE.finditer(str(content or "")))
        for url in urls:
            if not url:
                continue
            path = _local_blog_image_path(url)
            if path and path.is_file() and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.orig{path.suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill MintiStudio logo badges onto published blog images.")
    parser.add_argument("--apply", action="store_true", help="Apply badges. Without this, prints a dry-run list only.")
    args = parser.parse_args()

    sidecar = _load_sidecar()
    candidates = []
    for path in _published_image_paths():
        key = str(path.relative_to(STATIC_BLOG_ROOT))
        if key in sidecar:
            continue
        candidates.append((key, path))

    mode = "APPLY" if args.apply else "DRY_RUN"
    print(f"{mode} badge candidates: {len(candidates)}")
    for key, _path in candidates:
        print(key)

    if not args.apply:
        return 0

    for key, path in candidates:
        backup = _backup_path(path)
        if not backup.exists():
            shutil.copy2(path, backup)
        _add_logo_badge(path, badge_ratio=0.04)
        sidecar[key] = "badged"
    _save_sidecar(sidecar)
    print(f"BADGED {len(candidates)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
