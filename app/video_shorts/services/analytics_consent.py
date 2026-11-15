from __future__ import annotations

from flask import has_request_context, request


ANALYTICS_CONSENT_COOKIE = "minti_analytics_consent"
ANALYTICS_CONSENT_ACCEPTED = "accepted"
ANALYTICS_CONSENT_DECLINED = "declined"
ANALYTICS_CONSENT_MAX_AGE_SECONDS = 60 * 60 * 24 * 180

EEA_UK_CH_COUNTRY_CODES = frozenset(
    {
        "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
        "FR", "GB", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU",
        "LV", "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
    }
)


def visitor_country_code() -> str:
    if not has_request_context():
        return ""
    for header_name in ("CF-IPCountry", "CloudFront-Viewer-Country"):
        value = str(request.headers.get(header_name) or "").strip().upper()
        if len(value) == 2 and value.isalpha():
            return value
    return ""


def analytics_consent_choice() -> str:
    if not has_request_context():
        return ""
    value = str(request.cookies.get(ANALYTICS_CONSENT_COOKIE) or "").strip().lower()
    if value in {ANALYTICS_CONSENT_ACCEPTED, ANALYTICS_CONSENT_DECLINED}:
        return value
    return ""


def should_show_analytics_consent_banner() -> bool:
    if not has_request_context() or analytics_consent_choice():
        return False
    country_code = visitor_country_code()
    return not country_code or country_code in EEA_UK_CH_COUNTRY_CODES


def analytics_measurement_allowed() -> bool:
    """Apply the browser consent policy to server-side GA4 conversion events."""
    if not has_request_context():
        return False
    choice = analytics_consent_choice()
    if choice == ANALYTICS_CONSENT_ACCEPTED:
        return True
    if choice == ANALYTICS_CONSENT_DECLINED:
        return False
    country_code = visitor_country_code()
    return bool(country_code and country_code not in EEA_UK_CH_COUNTRY_CODES)
