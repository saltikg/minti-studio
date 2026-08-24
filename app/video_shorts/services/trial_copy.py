from __future__ import annotations

from typing import Any

DEFAULT_SHARE_TRIAL_DAYS = 60
LEGACY_SHARE_TRIAL_DAYS = 90


def normalize_trial_days(value: Any, *, default: int = DEFAULT_SHARE_TRIAL_DAYS) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return int(default)
    return days if days > 0 else int(default)


def trial_duration_text(days: Any, language: str = "EN") -> str:
    normalized_days = normalize_trial_days(days)
    normalized_language = str(language or "").strip().upper()
    if normalized_days % 30 == 0:
        months = round(normalized_days / 30)
        if normalized_language == "TR":
            return f"{months} ay"
        return f"{months} month" if months == 1 else f"{months} months"
    if normalized_language == "TR":
        return f"{normalized_days} gün"
    return f"{normalized_days} days"


def trial_access_phrase(days: Any, language: str = "EN") -> str:
    duration_text = trial_duration_text(days, language)
    normalized_language = str(language or "").strip().upper()
    if normalized_language == "TR":
        return f"{duration_text} ücretsiz erişim"
    return f"{duration_text} of complimentary access"

