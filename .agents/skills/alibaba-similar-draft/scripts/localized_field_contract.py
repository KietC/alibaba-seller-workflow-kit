from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from draft_core import canonical_json, read_json, sha256_bytes


SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "references"
    / "schemas"
    / "localized-field-target.schema.json"
)
RTL_LANGUAGES = {
    "ar",
    "arc",
    "ckb",
    "dv",
    "fa",
    "he",
    "ks",
    "ku",
    "nqo",
    "ps",
    "sd",
    "syr",
    "ug",
    "ur",
    "yi",
}
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_RULE_LIFETIME = timedelta(hours=24)


class LocalizedFieldError(RuntimeError):
    """A per-locale target is stale, ambiguous, or not hash-bound."""


def _sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _parse_timestamp(value: Any, label: str) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalizedFieldError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise LocalizedFieldError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_identity(target: dict[str, Any]) -> dict[str, Any]:
    """Return the minimum canonical identity required to address one locale."""

    return {
        "logical_name": target.get("logical_name"),
        "locale": target.get("locale"),
        "live_language_label": target.get("live_language_label"),
        "direction": target.get("direction"),
        "expected_sha256": target.get("expected_sha256"),
        "max_length_rule_hash": target.get("max_length_rule_hash"),
    }


def identity_sha256(target: dict[str, Any]) -> str:
    return _sha256_json(canonical_identity(target))


def expected_direction(locale: str) -> str:
    return "RTL" if locale.split("-", 1)[0].lower() in RTL_LANGUAGES else "LTR"


def validate_localized_target(
    target: dict[str, Any],
    value: Any,
    *,
    expected_page_fingerprint_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate schema, hashes, direction, freshness, and the live length rule.

    This function is pure local validation. It does not inspect a browser and does
    not authorize a write. PLATFORM_COUNTER rules remain bound to their visible-UI
    evidence and require a fresh post-write counter readback at execution time.
    """

    if not isinstance(target, dict):
        raise LocalizedFieldError("localized field target must be an object")
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - deployment diagnostic
        raise RuntimeError("jsonschema is required; use the bundled offline wheelhouse") from exc

    schema = read_json(SCHEMA_PATH)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    schema_errors = sorted(validator.iter_errors(target), key=lambda item: list(item.absolute_path))
    if schema_errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
            for error in schema_errors
        )
        raise LocalizedFieldError(f"localized field target schema validation failed: {details}")

    errors: list[str] = []
    if not isinstance(value, str):
        errors.append("localized value must be a string")
    value_hash = _sha256_json(value)
    if target["expected_sha256"] != value_hash:
        errors.append("expected_sha256 does not match the canonical localized value")

    locale = str(target["locale"])
    required_direction = expected_direction(locale)
    if target["direction"] != required_direction:
        errors.append(f"direction must be {required_direction} for locale {locale}")

    rule = target["max_length_rule"]
    rule_hash = _sha256_json(rule)
    if target["max_length_rule_hash"] != rule_hash:
        errors.append("max_length_rule_hash does not match max_length_rule")
    if target["identity_sha256"] != identity_sha256(target):
        errors.append("identity_sha256 does not match the canonical localized identity")
    if rule["candidate_value_sha256"] != value_hash:
        errors.append("max length rule is bound to a different candidate value")
    if rule["locale"] != locale:
        errors.append("max length rule locale does not match the target locale")
    label_hash = sha256_bytes(str(target["live_language_label"]).encode("utf-8"))
    if rule["live_language_label_sha256"] != label_hash:
        errors.append("max length rule is bound to a different live language label")
    if (
        expected_page_fingerprint_sha256 is not None
        and rule["page_fingerprint_sha256"] != expected_page_fingerprint_sha256.lower()
    ):
        errors.append("max length rule page fingerprint does not match the current page")

    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    captured_at = _parse_timestamp(rule["captured_at"], "max length rule captured_at")
    expires_at = _parse_timestamp(rule["expires_at"], "max length rule expires_at")
    if captured_at > observed_now + MAX_CLOCK_SKEW:
        errors.append("max length rule capture time is in the future")
    if expires_at <= observed_now:
        errors.append("max length rule is expired")
    if expires_at <= captured_at:
        errors.append("max length rule expires_at must be later than captured_at")
    if expires_at - captured_at > MAX_RULE_LIFETIME:
        errors.append("max length rule lifetime exceeds 24 hours")

    observed_count = int(rule["observed_count"])
    maximum = int(rule["maximum"])
    if observed_count > maximum:
        errors.append("localized value exceeds the live maximum length")
    actual_code_points = len(value) if isinstance(value, str) else -1
    if rule["unit"] == "UNICODE_CODE_POINTS" and observed_count != actual_code_points:
        errors.append("UNICODE_CODE_POINTS observed_count does not match the value")
    if rule["unit"] == "UTF8_BYTES" and observed_count != len(str(value).encode("utf-8")):
        errors.append("UTF8_BYTES observed_count does not match the value")
    if rule["unit"] == "PLATFORM_COUNTER":
        if observed_count != actual_code_points:
            errors.append("PLATFORM_COUNTER observed_count does not match the candidate value")
        # A bare digest is not evidence of a live UI control/counter.  Keep this
        # mode closed until a current-session counter receipt (path, bytes,
        # digest, session, page, locale, control, count and maximum) is part of
        # the target contract and independently verified.
        errors.append("PLATFORM_COUNTER requires a verified current-session live-counter evidence artifact")

    if errors:
        raise LocalizedFieldError("; ".join(errors))
    return target


def build_localized_target(
    *,
    logical_name: str,
    locale: str,
    live_language_label: str,
    value: Any,
    page_fingerprint_sha256: str,
    live_control_evidence_sha256: str,
    maximum: int,
    unit: str,
    observed_count: int,
    captured_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """Build, hash, and validate a canonical LOCALIZED_FIELD target."""

    value_hash = _sha256_json(value)
    rule = {
        "source": "CURRENT_VISIBLE_UI",
        "captured_at": captured_at,
        "expires_at": expires_at,
        "locale": locale,
        "live_language_label_sha256": sha256_bytes(live_language_label.encode("utf-8")),
        "page_fingerprint_sha256": page_fingerprint_sha256.lower(),
        "live_control_evidence_sha256": live_control_evidence_sha256.lower(),
        "maximum": maximum,
        "unit": unit,
        "observed_count": observed_count,
        "candidate_value_sha256": value_hash,
        "current_session_only": True,
        "require_post_write_counter_readback": True,
        "final_write_enabled": False,
    }
    target: dict[str, Any] = {
        "kind": "LOCALIZED_FIELD",
        "logical_name": logical_name,
        "locale": locale,
        "live_language_label": live_language_label,
        "direction": expected_direction(locale),
        "expected_sha256": value_hash,
        "max_length_rule_hash": _sha256_json(rule),
        "max_length_rule": rule,
        "identity_sha256": "",
    }
    target["identity_sha256"] = identity_sha256(target)
    return validate_localized_target(target, value)
