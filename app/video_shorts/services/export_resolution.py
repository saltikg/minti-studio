from dataclasses import dataclass
import logging
from typing import Optional

from app.video_shorts import config
from app.video_shorts.services.billing import plan_is_paid
from app.video_shorts.services.db import get_db_readonly


EXPORT_RESOLUTION_OVERRIDE_KEY = "export_resolution_override"
logger = logging.getLogger(__name__)


def export_quality_label(
    value: Optional[str],
    *,
    audience: str = "user",
    resolved_value: Optional[str] = None,
    language: str = "en",
) -> str:
    normalized = str(value or "auto").strip().lower()
    normalized_audience = "admin" if str(audience or "").strip().lower() == "admin" else "user"
    if normalized == "auto":
        resolved = str(resolved_value or "").strip().lower()
        if resolved:
            normalized = resolved
        elif normalized_audience == "admin":
            return "Auto (by plan)"
        else:
            normalized = "720"

    if normalized_audience == "admin":
        labels = {
            "720": "Standard · 720p",
            "1080": "Full HD · 1080p",
            "1080_hq": "Full HD Premium · 1080p (1440p source)",
        }
    else:
        labels = {
            "720": "Standart (720p)" if str(language or "").strip().lower() == "tr" else "Standard (720p)",
            "1080": "Full HD (1080p)",
            "1080_hq": "Full HD Premium (1080p)",
        }
    return labels.get(normalized, labels["720"])


@dataclass(frozen=True)
class ExportResolutionDecision:
    resolution: str
    override: Optional[str]
    eligible: bool
    flag_enabled: bool
    is_guest: bool

    @property
    def is_hq(self) -> bool:
        return self.override == "1080_hq"


def resolve_export_resolution(user_id: Optional[str], *, conn=None) -> ExportResolutionDecision:
    """Resolve the account-level portrait export resolution, defaulting safely to 720p."""
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return ExportResolutionDecision("720", None, False, bool(config.EXPORT_1080_FOR_PAID), True)

    own_conn = conn is None
    db = conn
    try:
        if db is None:
            db = get_db_readonly()
        account_row = db.execute(
            """
            SELECT
                COALESCE(plan_id, ''),
                lower(COALESCE(service_mode, '')),
                COALESCE(is_guest, FALSE)
            FROM shorts_users
            WHERE CAST(id AS VARCHAR) = CAST(? AS VARCHAR)
            LIMIT 1
            """,
            [normalized_user_id],
        ).fetchone()
        if not account_row:
            return ExportResolutionDecision("720", None, False, bool(config.EXPORT_1080_FOR_PAID), True)

        plan_id = str(account_row[0] or "").strip()
        service_mode = str(account_row[1] or "").strip().lower()
        is_guest = bool(account_row[2])
        eligible = bool(plan_is_paid(plan_id) or service_mode == "autopilot")
        flag_enabled = bool(config.EXPORT_1080_FOR_PAID)

        if is_guest:
            return ExportResolutionDecision("720", None, False, flag_enabled, True)

        override_row = db.execute(
            """
            SELECT preference_value
            FROM shorts_user_preferences
            WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
              AND preference_key = ?
            LIMIT 1
            """,
            [normalized_user_id, EXPORT_RESOLUTION_OVERRIDE_KEY],
        ).fetchone()
        override = str((override_row[0] if override_row else "") or "").strip()
        if override not in {"720", "1080", "1080_hq"}:
            override = None

        unactivated_lead = False
        if not service_mode:
            unactivated_lead = bool(
                db.execute(
                    """
                    SELECT 1
                    FROM autopilot_leads
                    WHERE CAST(user_id AS VARCHAR) = CAST(? AS VARCHAR)
                      AND converted_at IS NULL
                    LIMIT 1
                    """,
                    [normalized_user_id],
                ).fetchone()
            )
        eligible = bool(eligible and not unactivated_lead)

        if override:
            resolution = "1080" if override == "1080_hq" else override
        elif unactivated_lead:
            resolution = "720"
        elif eligible and flag_enabled:
            resolution = "1080"
        else:
            resolution = "720"
        return ExportResolutionDecision(resolution, override, eligible, flag_enabled, False)
    except Exception as exc:
        logger.warning("Export resolution lookup failed for user %s: %s", normalized_user_id, exc)
        return ExportResolutionDecision("720", None, False, bool(config.EXPORT_1080_FOR_PAID), False)
    finally:
        if own_conn and db is not None:
            db.close()
