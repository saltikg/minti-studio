import hashlib
from pathlib import Path
from typing import List, Optional

from app.video_shorts.config import MINTI_BACKGROUNDS_DIR, STATIC_IMG_DIR


SYSTEM_BACKGROUND_KEY_PREFIX = "systembg:"
_ALLOWED_SYSTEM_BACKGROUND_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_FALLBACK_DIR_CANDIDATES = (
    STATIC_IMG_DIR / "bg",
    STATIC_IMG_DIR,
    MINTI_BACKGROUNDS_DIR,
)
_STATIC_ROOT = STATIC_IMG_DIR.parent


def _existing_background_dirs() -> List[Path]:
    seen = set()
    directories: List[Path] = []
    for candidate in _FALLBACK_DIR_CANDIDATES:
        resolved = candidate.resolve()
        if resolved in seen or not candidate.exists() or not candidate.is_dir():
            continue
        seen.add(resolved)
        directories.append(candidate)
    return directories


def list_system_background_paths() -> List[Path]:
    backgrounds: List[Path] = []
    seen_names = set()
    for directory in _existing_background_dirs():
        for entry in directory.iterdir():
            if not entry.is_file() or entry.suffix.lower() not in _ALLOWED_SYSTEM_BACKGROUND_EXTS:
                continue
            if entry.name.lower().startswith("favicon"):
                continue
            if entry.name in seen_names:
                continue
            seen_names.add(entry.name)
            backgrounds.append(entry)
    return sorted(backgrounds, key=lambda entry: entry.name.lower())


def system_background_static_filename(path_or_name: Path | str) -> str:
    candidate_name = Path(path_or_name).name
    for candidate in list_system_background_paths():
        if candidate.name != candidate_name:
            continue
        try:
            return str(candidate.relative_to(_STATIC_ROOT))
        except ValueError:
            pass
    # Fallback to the canonical folder name for older keys.
    return str(Path("mintibackgrounds") / candidate_name)


def make_system_background_key(filename: str) -> str:
    return f"{SYSTEM_BACKGROUND_KEY_PREFIX}{Path(filename).name}"


def is_system_background_key(value: Optional[str]) -> bool:
    return str(value or "").startswith(SYSTEM_BACKGROUND_KEY_PREFIX)


def system_background_filename_from_key(value: Optional[str]) -> str:
    if not is_system_background_key(value):
        return ""
    return Path(str(value).split(":", 1)[1]).name


def resolve_system_background_path(value: Optional[str]) -> Optional[Path]:
    target_name = system_background_filename_from_key(value) or Path(str(value or "")).name
    if not target_name:
        return None
    for candidate in list_system_background_paths():
        if candidate.name == target_name:
            return candidate
    return None


def choose_deterministic_system_background(video_id: Optional[str]) -> Optional[Path]:
    candidates = [candidate for candidate in list_system_background_paths() if candidate.exists()]
    if not candidates:
        return None
    if not video_id:
        return candidates[0]
    digest = hashlib.sha256(str(video_id).encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(candidates)
    return candidates[index]
