"""Normalize queue owners without falling back to an unscoped social account."""


def queue_identity(user_id, brand_id=None):
    owner, separator, legacy_brand = str(user_id or "").strip().partition("::")
    brand = str(brand_id or "").strip() or None
    if not owner:
        raise ValueError("Social queue owner is missing.")
    if separator:
        if not legacy_brand or "::" in legacy_brand or (brand and brand != legacy_brand):
            raise ValueError("Social queue brand identity is invalid or conflicting.")
        brand = legacy_brand
    return owner, brand


def queue_token_identity(job):
    owner, brand = queue_identity(job.get("user_id"), job.get("brand_id"))
    if not brand:
        raise ValueError("Social queue brand is missing; refusing an unscoped account lookup.")
    return f"{owner}::{brand}"


def queue_identity_filter(user_id, brand_id):
    """Match canonical and legacy rows only within the same owner and brand."""
    owner, brand = queue_identity(user_id, brand_id)
    if brand is None:
        return "(user_id = ? AND brand_id IS NULL)", [owner]
    return (
        "((user_id = ? AND brand_id = ?) OR "
        "(user_id = ? AND (brand_id IS NULL OR brand_id = ?)))",
        [owner, brand, f"{owner}::{brand}", brand],
    )
