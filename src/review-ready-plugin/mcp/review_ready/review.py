from __future__ import annotations

from typing import Any


REVIEW_CONTRACT_VERSION = "1.0.0"
FIELD_REVIEW_DECISIONS = {
    "APPROVE_FIELD",
    "REJECT_FIELD",
    "NEEDS_CHANGES",
}


def validate_review_identity(
    reviewer_id: Any,
    reviewer_role: Any,
    rationale: Any,
) -> tuple[str, str, str]:
    values = [str(value or "").strip() for value in (reviewer_id, reviewer_role, rationale)]
    if any(not value for value in values):
        raise ValueError("reviewer_id, reviewer_role, and rationale are required")
    if any(len(value) > 500 for value in values):
        raise ValueError("review identity fields are unexpectedly long")
    return values[0], values[1], values[2]


def review_state(
    review_subject_hash: str,
    review_events: list[dict[str, Any]],
    current_subject_review_events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "sellerkit.review_state.v1",
        "review_contract_version": REVIEW_CONTRACT_VERSION,
        "review_subject_hash": review_subject_hash,
        "review_events": review_events,
        "current_subject_review_events": current_subject_review_events,
        "event_count": len(review_events),
        "current_subject_event_count": len(current_subject_review_events),
        "stale_or_other_scope_event_count": len(review_events) - len(current_subject_review_events),
        "identity_assurance": "SELF_ATTESTED_UNVERIFIED",
        "approved_internal_promotion_supported_in_v1": False,
        "record_effect": "LOCAL_AUDIT_ONLY_NO_APPROVED_INTERNAL_PROMOTION",
    }
