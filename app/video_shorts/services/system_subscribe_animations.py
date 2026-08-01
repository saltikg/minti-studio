import hashlib
from pathlib import Path
from typing import List, Optional

from app.video_shorts.config import STATIC_IMG_DIR


SYSTEM_SUBSCRIBE_KEY_PREFIX = "systemsub:"
_ALLOWED_SYSTEM_SUBSCRIBE_EXTS = {".gif", ".mp4"}
_SYSTEM_SUBSCRIBE_DIR = STATIC_IMG_DIR / "system_subscribe"
_LEGACY_SUBSCRIBE_CANDIDATES = (
    STATIC_IMG_DIR / "subscribe.gif",
    STATIC_IMG_DIR / "subscribe3.gif",
)
_STATIC_ROOT = STATIC_IMG_DIR.parent


def list_system_subscribe_paths() -> List[Path]:
    candidates: List[Path] = []
    seen_names = set()

    for candidate in _LEGACY_SUBSCRIBE_CANDIDATES:
        if not candidate.exists() or not candidate.is_file():
            continue
        if candidate.suffix.lower() not in _ALLOWED_SYSTEM_SUBSCRIBE_EXTS:
            continue
        if candidate.name in seen_names:
            continue
        seen_names.add(candidate.name)
        candidates.append(candidate)

    if _SYSTEM_SUBSCRIBE_DIR.exists() and _SYSTEM_SUBSCRIBE_DIR.is_dir():
        for entry in sorted(_SYSTEM_SUBSCRIBE_DIR.iterdir(), key=lambda item: item.name.lower()):
            if not entry.is_file() or entry.suffix.lower() not in _ALLOWED_SYSTEM_SUBSCRIBE_EXTS:
                continue
            if entry.name in seen_names:
                continue
            seen_names.add(entry.name)
            candidates.append(entry)

    return candidates


def system_subscribe_static_filename(path_or_name: Path | str) -> str:
    candidate_name = Path(path_or_name).name
    for candidate in list_system_subscribe_paths():
        if candidate.name != candidate_name:
            continue
        try:
            return str(candidate.relative_to(_STATIC_ROOT))
        except ValueError:
            pass
    return candidate_name


def make_system_subscribe_key(filename: str) -> str:
    return f"{SYSTEM_SUBSCRIBE_KEY_PREFIX}{Path(filename).name}"


def is_system_subscribe_key(value: Optional[str]) -> bool:
    return str(value or "").startswith(SYSTEM_SUBSCRIBE_KEY_PREFIX)


def system_subscribe_filename_from_key(value: Optional[str]) -> str:
    if not is_system_subscribe_key(value):
        return ""
    return Path(str(value).split(":", 1)[1]).name


def resolve_system_subscribe_path(value: Optional[str]) -> Optional[Path]:
    target_name = system_subscribe_filename_from_key(value) or Path(str(value or "")).name
    if not target_name:
        return None
    for candidate in list_system_subscribe_paths():
        if candidate.name == target_name:
            return candidate
    return None


def choose_deterministic_system_subscribe(video_id: Optional[str]) -> Optional[Path]:
    candidates = [candidate for candidate in list_system_subscribe_paths() if candidate.exists()]
    if not candidates:
        return None
    if not video_id:
        return candidates[0]
    digest = hashlib.sha256(str(video_id).encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(candidates)
    return candidates[index]
