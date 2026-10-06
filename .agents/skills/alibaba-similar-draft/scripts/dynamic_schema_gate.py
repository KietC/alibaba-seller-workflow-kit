from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from draft_core import GuardError, canonical_json, read_json, sha256_bytes, sha256_file
from parse_official_schema_xml import parse_schema_xml


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
SCHEMA_PATH = SKILL_ROOT / "references" / "schemas" / "dynamic-publish-schema.schema.json"
OFFICIAL_SCHEMA_PATH = SKILL_ROOT / "references" / "schemas" / "official-schema-xml-normalized.schema.json"
FIELD_REGISTRY_PATH = SKILL_ROOT / "references" / "publish-field-registry.v1.json"
NORMALIZER_PATH = SCRIPT_DIR / "normalize_dynamic_publish_schema.py"

OFFICIAL_SCHEMA_GET_ENDPOINT = "https://eco.taobao.com/router/rest"
OFFICIAL_SCHEMA_GET_OPERATION = "alibaba.icbu.product.schema.get"
MAX_CAPTURE_LIFETIME = timedelta(minutes=15)
MAX_CLOCK_SKEW = timedelta(minutes=5)


def _parse_utc(value: Any, label: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise GuardError(f"dynamic schema {label} is not a valid date-time") from exc
    if parsed.tzinfo is None:
        raise GuardError(f"dynamic schema {label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_json_schema(document: Any, schema_path: Path, label: str) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - deployment diagnostic
        raise GuardError("jsonschema runtime is required for dynamic schema validation") from exc
    schema = read_json(schema_path)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}" for error in errors[:12]
        )
        raise GuardError(f"{label} schema validation failed: {details}")


def _validated_file_binding(binding: Any, label: str) -> Path:
    if not isinstance(binding, dict):
        raise GuardError(f"{label} file binding is missing")
    path = Path(str(binding.get("path") or ""))
    if not path.is_absolute() or not path.is_file():
        raise GuardError(f"{label} is not an existing absolute file")
    if path.stat().st_size != int(binding.get("bytes") or -1):
        raise GuardError(f"{label} byte-size mismatch")
    if sha256_file(path) != str(binding.get("sha256") or "").lower():
        raise GuardError(f"{label} hash mismatch")
    return path.resolve()


def _file_binding(path: Path) -> dict[str, Any]:
    path = path.resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _flatten_official_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for field in fields:
        result.append(field)
        result.extend(_flatten_official_fields(field.get("children") or []))
    return result


_KNOWN_LOGICAL_ALIASES = {
    "producttitle": "product_title",
    "subject": "product_title",
    "minorderquantity": "minimum_order_quantity",
    "minimumorderquantity": "minimum_order_quantity",
}


def _logical_name(official_path: str) -> str:
    segments: list[str] = []
    for raw_segment in official_path.split("."):
        segment = re.sub(r"\[(\d+)\]", r"_\1", raw_segment)
        segment = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", segment)
        segment = re.sub(r"[^A-Za-z0-9_-]+", "_", segment).strip("_").lower()
        if not segment:
            raise GuardError(f"official schema field path cannot form a logical name: {official_path!r}")
        if not segment[0].isalpha():
            segment = f"field_{segment}"
        segments.append(segment)
    joined = ".".join(segments)
    return _KNOWN_LOGICAL_ALIASES.get(joined.replace("_", ""), joined)


def _registry_names_and_hash() -> tuple[set[str], str]:
    registry = read_json(FIELD_REGISTRY_PATH)
    names = {
        str(field.get("logical_name") or "")
        for field in (registry.get("fields") or [])
        if isinstance(field, dict) and str(field.get("logical_name") or "")
    }
    if not names:
        raise GuardError("trusted publish field registry exposes no logical fields")
    return names, sha256_file(FIELD_REGISTRY_PATH)


def _derived_max_length(field: dict[str, Any]) -> tuple[int | None, str, str | None]:
    rules = [rule for rule in (field.get("rules") or []) if rule.get("name") == "maxLengthRule"]
    if not rules:
        return None, "NOT_APPLICABLE", None
    if len(rules) != 1:
        raise GuardError(f"official schema field {field.get('path')} has multiple maxLengthRule entries")
    rule = rules[0]
    try:
        value = int(str(rule.get("value") or ""))
    except ValueError as exc:
        raise GuardError(f"official schema field {field.get('path')} has invalid maxLengthRule") from exc
    if value < 1:
        raise GuardError(f"official schema field {field.get('path')} has non-positive maxLengthRule")
    unit = str((rule.get("attributes") or {}).get("unit") or "").strip().casefold()
    if unit in {"byte", "bytes", "utf8_byte", "utf-8 byte", "utf8 bytes", "utf-8 bytes"}:
        normalized_unit = "UTF8_BYTES"
    elif unit in {"char", "chars", "character", "characters"}:
        normalized_unit = "CHARACTERS"
    elif not unit:
        normalized_unit = "PLATFORM_COUNTER"
    else:
        raise GuardError(
            f"official schema field {field.get('path')} uses unsupported maxLengthRule unit {unit!r}"
        )
    return value, normalized_unit, str(rule.get("rule_sha256") or "") or None


def derive_dynamic_fields(official_document: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive every live field row from the reparsed official field/rule tree.

    No caller-supplied observation is consulted.  The static registry only
    classifies a parser-derived logical name as a known top-level field;
    everything else is a category property named by the official schema.
    """

    registry_names, _ = _registry_names_and_hash()
    result: list[dict[str, Any]] = []
    for field in _flatten_official_fields(official_document.get("fields") or []):
        path = str(field.get("path") or "")
        field_id = str(field.get("id") or "")
        logical_name = _logical_name(path)
        label = str(field.get("name") or field_id).strip()
        if not label:
            raise GuardError(f"official schema field {path!r} has no label or id")
        maximum, unit, maximum_rule_sha256 = _derived_max_length(field)
        write_target: dict[str, Any]
        if logical_name in registry_names:
            write_target = {"kind": "FIELD_REGISTRY"}
        else:
            write_target = {"kind": "CATEGORY_PROPERTY", "approved_property_name": label}
        result.append({
            "logical_name": logical_name,
            "live_field_key": path,
            "observed_label": label,
            "required": field.get("required") is True,
            "write_target": write_target,
            "max_length": maximum,
            "max_length_unit": unit,
            "official_field_path": path,
            "official_field_id": field_id,
            "official_field_type": str(field.get("type") or ""),
            "rule_sha256": str(field.get("rule_sha256") or ""),
            "max_length_rule_sha256": maximum_rule_sha256,
        })
    logical_names = [row["logical_name"] for row in result]
    live_keys = [row["live_field_key"] for row in result]
    if len(logical_names) != len(set(logical_names)):
        raise GuardError("official schema derivation produced duplicate logical field names")
    if len(live_keys) != len(set(live_keys)):
        raise GuardError("official schema derivation produced duplicate live field keys")
    return sorted(result, key=lambda item: item["logical_name"])


def normalized_field_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in document.get("fields") or []:
        rows.append({
            "logical_name": str((raw or {}).get("logical_name") or ""),
            "live_field_key": str((raw or {}).get("live_field_key") or ""),
            "observed_label": str((raw or {}).get("observed_label") or ""),
            "required": (raw or {}).get("required") is True,
            "write_target": (raw or {}).get("write_target"),
            "max_length": (raw or {}).get("max_length"),
            "max_length_unit": (raw or {}).get("max_length_unit"),
            "official_field_path": str((raw or {}).get("official_field_path") or ""),
            "official_field_id": str((raw or {}).get("official_field_id") or ""),
            "official_field_type": str((raw or {}).get("official_field_type") or ""),
            "rule_sha256": str((raw or {}).get("rule_sha256") or ""),
            "max_length_rule_sha256": (raw or {}).get("max_length_rule_sha256"),
        })
    return sorted(rows, key=lambda item: item["logical_name"])


def field_set_sha256(document: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(normalized_field_rows(document)).encode("utf-8"))


def required_field_ids(document: dict[str, Any]) -> list[str]:
    return sorted(row["logical_name"] for row in normalized_field_rows(document) if row["required"])


def required_field_set_sha256(document: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(required_field_ids(document)).encode("utf-8"))


def _receipt_material(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": document.get("run_id"),
        "target_model": document.get("target_model"),
        "template_product_id": document.get("template_product_id"),
        "captured_at": document.get("captured_at"),
        "expires_at": document.get("expires_at"),
        "category": document.get("category"),
        "source": document.get("source"),
        "raw_payload": document.get("raw_payload"),
        "provenance": document.get("provenance"),
        "fields": normalized_field_rows(document),
        "field_set_sha256": document.get("field_set_sha256"),
        "required_field_set_sha256": document.get("required_field_set_sha256"),
    }


def receipt_sha256(document: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(_receipt_material(document)).encode("utf-8"))


def load_and_reparse_official_schema(
    normalized_path: Path,
    *,
    raw_xml_path: Path,
    expected_run_id: str,
    expected_target_model: str,
    expected_template_product_id: str,
    expected_category_id: str,
    expected_product_type: str,
) -> dict[str, Any]:
    normalized_path = normalized_path.resolve()
    raw_xml_path = raw_xml_path.resolve()
    if not normalized_path.is_file():
        raise GuardError(f"normalized official schema is missing: {normalized_path}")
    document = read_json(normalized_path)
    if not isinstance(document, dict):
        raise GuardError("normalized official schema must be a JSON object")
    _validate_json_schema(document, OFFICIAL_SCHEMA_PATH, "normalized official schema")
    exact_bindings = {
        "run_id": expected_run_id,
        "target_model": str(expected_target_model).strip().upper(),
        "template_product_id": str(expected_template_product_id),
        "category_id": str(expected_category_id),
        "product_type": str(expected_product_type),
    }
    for key, expected in exact_bindings.items():
        observed = str(document.get(key) or "")
        if observed != expected:
            raise GuardError(f"normalized official schema {key} binding mismatch")
    if document.get("status") != "PARSED_MACHINE_RULES_COMPLETE":
        raise GuardError("official schema is not machine-complete; manual/unsupported rules block live use")
    if document.get("manual_review_rules") or document.get("unsupported_constructs"):
        raise GuardError("official schema contains unresolved manual or unsupported rules")
    raw_binding_path = _validated_file_binding(document.get("raw_xml"), "normalized official schema raw XML")
    if raw_binding_path != raw_xml_path:
        raise GuardError("normalized official schema and dynamic capture do not bind the same raw XML path")
    captured_at = _parse_utc(document.get("captured_at"), "official captured_at")
    expires_at = _parse_utc(document.get("expires_at"), "official expires_at")
    lifetime_seconds = int((expires_at - captured_at).total_seconds())
    if lifetime_seconds < 60 or lifetime_seconds > int(MAX_CAPTURE_LIFETIME.total_seconds()):
        raise GuardError("official schema lifetime must be between 1 and 15 minutes")
    if lifetime_seconds % 60:
        raise GuardError("official schema lifetime must be an exact whole number of minutes")
    with tempfile.TemporaryDirectory(prefix="alibaba-schema-reparse-") as temporary:
        output = Path(temporary) / "normalized.json"
        reparsed = parse_schema_xml(
            xml_path=raw_xml_path,
            output=output,
            capture_id=str(document["capture_id"]),
            run_id=expected_run_id,
            target_model=expected_target_model,
            template_product_id=expected_template_product_id,
            category_id=expected_category_id,
            product_type=expected_product_type,
            captured_at=document["captured_at"],
            valid_minutes=lifetime_seconds // 60,
        )
    if canonical_json(reparsed) != canonical_json(document):
        raise GuardError("normalized official schema does not equal a same-source reparse of raw XML")
    return reparsed


def validate_dynamic_schema_capture(
    path: Path,
    *,
    expected_target_model: str,
    expected_template_product_id: str,
    expected_run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise GuardError(f"dynamic schema capture is missing: {path}")
    document = read_json(path)
    if not isinstance(document, dict):
        raise GuardError("dynamic schema capture must be a JSON object")
    _validate_json_schema(document, SCHEMA_PATH, "dynamic schema capture")

    if document["source"]["endpoint"] != OFFICIAL_SCHEMA_GET_ENDPOINT:
        raise GuardError("dynamic schema source endpoint is not the exact official TOP HTTPS endpoint")
    if document["source"]["operation"] != OFFICIAL_SCHEMA_GET_OPERATION:
        raise GuardError("dynamic schema source operation mismatch")
    target_model = str(document.get("target_model") or "").strip().upper()
    if target_model != str(expected_target_model or "").strip().upper():
        raise GuardError("dynamic schema target_model binding mismatch")
    if str(document.get("template_product_id") or "") != str(expected_template_product_id or ""):
        raise GuardError("dynamic schema template_product_id binding mismatch")
    if str(document.get("run_id") or "") != str(expected_run_id or ""):
        raise GuardError("dynamic schema run_id binding mismatch")

    captured_at = _parse_utc(document.get("captured_at"), "captured_at")
    expires_at = _parse_utc(document.get("expires_at"), "expires_at")
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if captured_at > observed_now + MAX_CLOCK_SKEW:
        raise GuardError("dynamic schema capture time is in the future")
    if expires_at <= observed_now:
        raise GuardError("dynamic schema capture is expired")
    if expires_at <= captured_at or expires_at - captured_at > MAX_CAPTURE_LIFETIME:
        raise GuardError("dynamic schema capture lifetime must be positive and no longer than 15 minutes")

    raw_path = _validated_file_binding(document.get("raw_payload"), "dynamic schema raw XML payload")
    provenance = document.get("provenance") or {}
    provenance_raw_path = _validated_file_binding(provenance.get("raw_xml"), "dynamic schema provenance raw XML")
    if provenance_raw_path != raw_path or provenance.get("raw_xml") != document.get("raw_payload"):
        raise GuardError("dynamic schema raw payload and provenance raw XML bindings differ")
    normalized_path = _validated_file_binding(
        provenance.get("normalized_official_schema"), "normalized official schema provenance"
    )
    official = load_and_reparse_official_schema(
        normalized_path,
        raw_xml_path=raw_path,
        expected_run_id=str(document["run_id"]),
        expected_target_model=str(document["target_model"]),
        expected_template_product_id=str(document["template_product_id"]),
        expected_category_id=str(document["category"]["id"]),
        expected_product_type=str(document["category"]["product_type"]),
    )
    if official["captured_at"] != document["captured_at"] or official["expires_at"] != document["expires_at"]:
        raise GuardError("dynamic schema and official schema capture windows differ")
    if provenance["official_field_paths_sha256"] != official["field_paths_sha256"]:
        raise GuardError("official field-path provenance hash mismatch")
    if provenance["official_rule_graph_sha256"] != official["rule_graph_sha256"]:
        raise GuardError("official rule-graph provenance hash mismatch")
    _, registry_hash = _registry_names_and_hash()
    if provenance["field_derivation"]["registry_sha256"] != registry_hash:
        raise GuardError("dynamic field derivation registry hash mismatch")

    expected_fields = derive_dynamic_fields(official)
    fields = normalized_field_rows(document)
    if canonical_json(fields) != canonical_json(expected_fields):
        raise GuardError("dynamic schema fields are not derived from the same-source official XML parse")
    required = required_field_ids(document)
    if not required:
        raise GuardError("dynamic schema exposes no required fields; fail closed")
    observed_field_hash = field_set_sha256(document)
    if str(document.get("field_set_sha256") or "").lower() != observed_field_hash:
        raise GuardError("dynamic schema field set hash mismatch")
    observed_required_hash = required_field_set_sha256(document)
    if str(document.get("required_field_set_sha256") or "").lower() != observed_required_hash:
        raise GuardError("dynamic schema required field set hash mismatch")
    expected_receipt = receipt_sha256(document)
    if document["normalizer"]["tool_sha256"] != sha256_file(NORMALIZER_PATH):
        raise GuardError("dynamic schema normalizer tool hash mismatch")
    if document["normalizer"]["receipt_sha256"] != expected_receipt:
        raise GuardError("dynamic schema normalizer receipt hash mismatch")
    if document["capture_id"] != f"dynamic-schema-{expected_receipt[:20]}":
        raise GuardError("dynamic schema capture_id is not receipt-derived")

    return {
        "status": "CURRENT_SCHEMA_BOUND",
        "capture_id": document["capture_id"],
        "category_id": str(document["category"]["id"]),
        "product_type": str(document["category"]["product_type"]),
        "captured_at": document["captured_at"],
        "expires_at": document["expires_at"],
        "capture": _file_binding(path),
        "raw_payload": _file_binding(raw_path),
        "normalized_official_schema": _file_binding(normalized_path),
        "official_field_paths_sha256": official["field_paths_sha256"],
        "official_rule_graph_sha256": official["rule_graph_sha256"],
        "field_ids": [row["logical_name"] for row in fields],
        "required_field_ids": required,
        "field_set_sha256": observed_field_hash,
        "required_field_set_sha256": observed_required_hash,
        "source_kind": "OFFICIAL_SCHEMA_GET",
        "source_endpoint": OFFICIAL_SCHEMA_GET_ENDPOINT,
        "source_operation": OFFICIAL_SCHEMA_GET_OPERATION,
        "provenance_status": "OFFICIAL_XML_SAME_SOURCE_REPARSED",
        "final_write_enabled": False,
    }


def load_decision_schema_binding(
    binding: dict[str, Any],
    *,
    decision_dir: Path,
    expected_target_model: str,
    expected_template_product_id: str,
    expected_run_id: str,
) -> dict[str, Any]:
    path = Path(str((binding or {}).get("path") or ""))
    if not path.is_absolute():
        path = decision_dir / path
    path = path.resolve()
    if not path.is_file():
        raise GuardError(f"dynamic schema decision path is missing: {path}")
    if path.stat().st_size != int((binding or {}).get("bytes") or -1):
        raise GuardError("dynamic schema decision byte-size binding mismatch")
    if sha256_file(path) != str((binding or {}).get("sha256") or "").lower():
        raise GuardError("dynamic schema decision hash binding mismatch")
    result = validate_dynamic_schema_capture(
        path,
        expected_target_model=expected_target_model,
        expected_template_product_id=expected_template_product_id,
        expected_run_id=expected_run_id,
    )
    result["decision_id"] = str((binding or {}).get("decision_id") or "")
    if not result["decision_id"]:
        raise GuardError("dynamic schema decision_id is required")
    return result
