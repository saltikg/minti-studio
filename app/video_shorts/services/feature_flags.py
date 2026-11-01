from __future__ import annotations

import os


_TRUE_VALUES = {"1", "true", "yes", "on"}


def comments_enabled() -> bool:
    """Return whether the temporarily hidden Comments feature is enabled."""
    return str(os.getenv("COMMENTS_ENABLED") or "false").strip().lower() in _TRUE_VALUES


def tiktok_enabled() -> bool:
    """Return whether TikTok UI is enabled."""
    return str(os.getenv("TIKTOK_ENABLED") or "false").strip().lower() in _TRUE_VALUES
