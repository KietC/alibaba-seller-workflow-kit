from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from capture_derivation import (
    capture_trust_status,
    derive_current_field_rows,
    derived_capture_bindings,
)


SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
NORMALIZED_SCHEMA = SCHEMA_DIR / "official-schema-xml-normalized.schema.json"
VALUES_SCHEMA = SCHEMA_DIR / "official-schema-current-values.schema.json"
RECEIPT_SCHEMA = SCHEMA_DIR / "official-schema-values-verification-receipt.schema.json"
CAPTURE_SCHEMA = SCHEMA_DIR / "browser-observation-capture.schema.json"
SUPPORTED_RULES = {
    "requiredRule", "disableRule", "readOnlyRule", "maxLengthRule", "minLengthRule",
    "valueTypeRule", "maxValueRule", "minValueRule", "maxInputNumRule", "minInputNumRule",
    "regexRule", "regxRule",
}
TRUE = {"true", "1"}
FALSE = {"false", "0"}
FUTURE_CLOCK_SKEW = timedelta(seconds=5)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_bound(path: Path, label: str) -> tuple[dict[str, Any], str, list[str]]:
    try:
        payload = path.resolve(strict=True).read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, "", [f"cannot read {label}: {exc}"]
    if not isinstance(value, dict):
        return {}, sha256_bytes(payload), [f"{label} must be a JSON object"]
    return value, sha256_bytes(payload), []


def schema_errors(value: dict[str, Any], schema_path: Path, label: str) -> list[str]:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"cannot read {label} schema: {exc}"]
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    return [
        f"{label} schema {'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in errors
    ]


def parse_time(value: Any, label: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{label} is not an RFC3339 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} is not an RFC3339 timestamp")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{label} must include timezone")
        return None
    return parsed.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def flatten(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for field in fields:
        result.append(field)
        result.extend(flatten([row for row in field.get("children", []) if isinstance(row, dict)]))
    return result


def verify_normalized_rule_hashes(normalized: dict[str, Any], errors: list[str]) -> None:
    """Recompute every hash emitted by parse_official_schema_xml.py.

    A file hash only binds bytes; it does not prove that the internal rule
    hashes still describe those bytes.  Recompute the complete recursive rule
    graph before any rule is evaluated.
    """

    def walk(field: dict[str, Any]) -> None:
        for rule in field.get("rules", []):
            if not isinstance(rule, dict):
                errors.append(f"{field.get('path')}: rule is not an object")
                continue
            material = copy.deepcopy(rule)
            declared = str(material.pop("rule_sha256", "") or "").lower()
            computed = object_sha256(material)
            if declared != computed:
                errors.append(f"normalized rule hash mismatch: {field.get('path')}:{rule.get('name')}")
        children = [row for row in field.get("children", []) if isinstance(row, dict)]
        for child in children:
            walk(child)
        field_material = {
            "path": field.get("path"),
            "type": field.get("type"),
            "rules": field.get("rules", []),
            "options": field.get("options", []),
            "children_rule_sha256": [child.get("rule_sha256") for child in children],
        }
        if str(field.get("rule_sha256") or "").lower() != object_sha256(field_material):
            errors.append(f"normalized field rule hash mismatch: {field.get('path')}")

    fields = [row for row in normalized.get("fields", []) if isinstance(row, dict)]
    for field in fields:
        walk(field)
    graph = [
        {"path": field.get("path"), "rule_sha256": field.get("rule_sha256")}
        for field in flatten(fields)
    ]
    if str(normalized.get("rule_graph_sha256") or "").lower() != object_sha256(graph):
        errors.append("normalized schema rule_graph_sha256 mismatch")


def verify_normalized_matches_raw(
    normalized: dict[str, Any], raw_path: Path, errors: list[str]
) -> None:
    """Reparse the bound XML and compare the complete rule-bearing tree."""

    from parse_official_schema_xml import ParseError, SCRIPT_PATH as PARSER_PATH, parse_schema_xml

    captured = parse_time(normalized.get("captured_at"), "schema captured_at", errors)
    expires = parse_time(normalized.get("expires_at"), "schema expires_at", errors)
    if captured is None or expires is None:
        return
    seconds = int((expires - captured).total_seconds())
    if seconds <= 0 or seconds > 15 * 60 or seconds % 60:
        errors.append("official schema validity interval is not an exact 1-15 minute window")
        return
    parser_binding = normalized.get("parser") or {}
    if str(parser_binding.get("tool_sha256") or "").lower() != sha256_file(PARSER_PATH):
        errors.append("normalized schema parser tool hash does not match the current parser")
    try:
        with tempfile.TemporaryDirectory(prefix="sellerkit-schema-reparse-") as temporary:
            reparsed = parse_schema_xml(
                xml_path=raw_path,
                output=Path(temporary) / "normalized.json",
                capture_id=str(normalized.get("capture_id") or ""),
                run_id=str(normalized.get("run_id") or ""),
                target_model=str(normalized.get("target_model") or ""),
                template_product_id=str(normalized.get("template_product_id") or ""),
                category_id=str(normalized.get("category_id") or ""),
                product_type=str(normalized.get("product_type") or ""),
                captured_at=str(normalized.get("captured_at") or ""),
                valid_minutes=seconds // 60,
            )
    except (OSError, ParseError, ValueError) as exc:
        errors.append(f"bound raw schema XML could not be reparsed: {exc}")
        return
    for name in (
        "fields", "field_paths_sha256", "rule_graph_sha256", "manual_review_rules",
        "unsupported_constructs", "status",
    ):
        if normalized.get(name) != reparsed.get(name):
            errors.append(f"normalized schema does not match bound raw XML: {name}")


def present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(item, str) and bool(item.strip()) for item in value)
    return False


def scalar_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def strict_bool(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in TRUE:
        return True
    if text in FALSE:
        return False
    return None


def decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def dependency_value(field_id: str, by_id: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], unsupported: list[str]) -> tuple[Any, dict[str, Any] | None]:
    field = by_id.get(field_id)
    if field is None:
        unsupported.append(f"dependency references missing or ambiguous field id: {field_id}")
        return None, None
    ledger = current.get(field["path"])
    if ledger is None:
        unsupported.append(f"dependency field has no current value: {field_id}")
        return None, field
    return ledger.get("value"), field


def expression_true(expression: dict[str, Any], by_id: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], unsupported: list[str]) -> bool:
    value, field = dependency_value(str(expression.get("field_id") or ""), by_id, current, unsupported)
    if field is None:
        return False
    symbol = expression.get("symbol")
    expected = expression.get("value")
    values = scalar_values(value)
    if symbol == "is null":
        return not present(value)
    if symbol == "==":
        return str(expected) in values
    if symbol == "!=":
        return str(expected) not in values
    if symbol == "contains":
        return any(str(expected) in item for item in values)
    if symbol == "not contains":
        return all(str(expected) not in item for item in values)
    if symbol in {">", "<", ">=", "<="}:
        if len(values) != 1 or decimal(values[0]) is None or decimal(expected) is None:
            unsupported.append(f"non-numeric dependency comparison for field {field['id']}")
            return False
        left, right = decimal(values[0]), decimal(expected)
        assert left is not None and right is not None
        return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[symbol]
    options = {str(row.get("value")) for row in field.get("options", []) if row.get("value") is not None}
    if symbol == "this field's value in fieldOptions":
        return bool(values) and all(item in options for item in values)
    if symbol == "this field's value not in fieldOptions":
        return any(item not in options for item in values)
    unsupported.append(f"unsupported dependency symbol: {symbol}")
    return False


def rule_active(rule: dict[str, Any], by_id: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], unsupported: list[str]) -> bool:
    groups = rule.get("depend_groups") or []
    if not groups:
        return True
    group_results: list[bool] = []
    for group in groups:
        results = [expression_true(row, by_id, current, unsupported) for row in group.get("expressions", [])]
        if not results:
            unsupported.append(f"empty dependency group on rule {rule.get('name')}")
            group_results.append(False)
        elif group.get("operator") == "and":
            group_results.append(all(results))
        elif group.get("operator") == "or":
            group_results.append(any(results))
        else:
            unsupported.append(f"unsupported dependency group operator: {group.get('operator')}")
            group_results.append(False)
    return any(group_results)


def check_type(kind: str, text: str) -> bool | None:
    name = kind.strip().lower()
    if name in {"text", "string"}:
        return True
    if name in {"integer", "int", "long"}:
        return bool(re.fullmatch(r"[+-]?\d+", text.strip()))
    if name in {"decimal", "number", "double", "float"}:
        return decimal(text) is not None
    if name in {"boolean", "bool"}:
        return text.strip().lower() in TRUE | FALSE
    if name == "date":
        try:
            datetime.strptime(text.strip(), "%Y-%m-%d")
            return True
        except ValueError:
            return False
    if name == "time":
        return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", text.strip()))
    if name in {"datetime", "date-time"}:
        try:
            datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    if name == "url":
        return bool(re.fullmatch(r"https?://[^\s]+", text.strip()))
    return None


def evaluate_field(field: dict[str, Any], ledger: dict[str, Any], by_id: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], unsupported: list[str]) -> dict[str, Any]:
    value = ledger.get("value")
    values = scalar_values(value)
    is_present = present(value)
    violations: list[str] = []
    reviews: list[str] = []
    applied: list[str] = []
    active_rules: list[dict[str, Any]] = []
    for rule in field.get("rules", []):
        if not rule.get("machine_enforceable"):
            continue
        if rule.get("name") not in SUPPORTED_RULES:
            unsupported.append(f"{field['path']}: unsupported machine rule {rule.get('name')}")
            continue
        if rule_active(rule, by_id, current, unsupported):
            active_rules.append(rule)
            applied.append(str(rule.get("rule_sha256")))

    disabled = False
    required = False
    read_only = False
    for rule in active_rules:
        name = rule["name"]
        if name in {"disableRule", "requiredRule", "readOnlyRule"}:
            flag = strict_bool(rule.get("value"))
            if flag is None:
                unsupported.append(f"{field['path']}: {name} has non-boolean value")
            elif name == "disableRule" and flag:
                disabled = True
            elif name == "requiredRule" and flag:
                required = True
            elif name == "readOnlyRule" and flag:
                read_only = True

    if ledger.get("observed_disabled") is not disabled:
        violations.append("observed disabled state does not match active disableRule")
    if (disabled or read_only) and ledger.get("write_targeted") is True:
        violations.append("disabled/read-only field is write-targeted")
    if not disabled:
        if required and not is_present:
            violations.append("required value is empty")
        # Count constraints apply to the collection itself.  In particular an
        # empty list must not bypass minInputNumRule merely because it is not
        # considered a present value.
        for rule in active_rules:
            name = rule["name"]
            if name not in {"maxInputNumRule", "minInputNumRule"}:
                continue
            try:
                limit = int(str(rule.get("value")))
            except ValueError:
                unsupported.append(f"{field['path']}: {name} has non-integer limit")
                continue
            count = len(values)
            if name == "maxInputNumRule" and count > limit:
                violations.append(f"input count exceeds {limit}")
            if name == "minInputNumRule" and count < limit:
                violations.append(f"input count is below {limit}")
        if is_present:
            option_values = {str(row.get("value")) for row in field.get("options", []) if row.get("value") is not None}
            if option_values:
                for item in values:
                    if item not in option_values:
                        violations.append(f"value is not an allowed option: {item}")
            for rule in active_rules:
                name = rule["name"]
                raw_limit = rule.get("value")
                if name in {"maxLengthRule", "minLengthRule"}:
                    try:
                        limit = int(str(raw_limit))
                    except ValueError:
                        unsupported.append(f"{field['path']}: {name} has non-integer limit")
                        continue
                    unit = str((rule.get("attributes") or {}).get("unit") or "").strip().lower()
                    if unit not in {"byte", "bytes", "character", "characters", "char", "chars"}:
                        reviews.append(f"{name} has missing/unknown length unit")
                        continue
                    for index, item in enumerate(values):
                        measured = len(item.encode("utf-8")) if unit in {"byte", "bytes"} else len(item)
                        if name == "maxLengthRule" and measured > limit:
                            violations.append(f"value[{index}] exceeds {limit} {unit}")
                        if name == "minLengthRule" and measured < limit:
                            violations.append(f"value[{index}] is shorter than {limit} {unit}")
                elif name == "valueTypeRule":
                    for index, item in enumerate(values):
                        valid = check_type(str(raw_limit or ""), item)
                        if valid is None:
                            unsupported.append(f"{field['path']}: unsupported value type {raw_limit!r}")
                        elif not valid:
                            violations.append(f"value[{index}] does not match type {raw_limit}")
                elif name in {"maxValueRule", "minValueRule"}:
                    limit = decimal(raw_limit)
                    if limit is None:
                        unsupported.append(f"{field['path']}: {name} has non-numeric limit")
                        continue
                    for index, item in enumerate(values):
                        observed = decimal(item)
                        if observed is None:
                            violations.append(f"value[{index}] is not numeric for {name}")
                        elif name == "maxValueRule" and observed > limit:
                            violations.append(f"value[{index}] exceeds numeric maximum {limit}")
                        elif name == "minValueRule" and observed < limit:
                            violations.append(f"value[{index}] is below numeric minimum {limit}")
                elif name in {"maxInputNumRule", "minInputNumRule"}:
                    # Evaluated above even when the value collection is empty.
                    continue
                elif name in {"regexRule", "regxRule"}:
                    mode = str((rule.get("attributes") or {}).get("exProperty") or "").strip().lower()
                    if mode not in {"include", "not include"}:
                        reviews.append(f"{name} has missing/unknown exProperty")
                        continue
                    try:
                        pattern = re.compile(str(raw_limit or ""))
                    except re.error:
                        unsupported.append(f"{field['path']}: invalid regular expression")
                        continue
                    for index, item in enumerate(values):
                        matched = pattern.search(item) is not None
                        if mode == "include" and not matched:
                            violations.append(f"value[{index}] does not include required regex")
                        if mode == "not include" and matched:
                            violations.append(f"value[{index}] includes forbidden regex")

    chars = sum(len(item) for item in values)
    byte_length = sum(len(item.encode("utf-8")) for item in values)
    if violations:
        status = "FAIL"
    elif reviews:
        status = "REVIEW_REQUIRED"
    elif disabled:
        status = "DISABLED_NOT_TARGETED"
    else:
        status = "PASS"
    return {
        "path": field["path"], "id": field["id"], "disabled": disabled, "required": required,
        "value_present": is_present, "input_count": len(values), "character_length": chars,
        "utf8_byte_length": byte_length, "applied_rule_sha256": sorted(set(applied)),
        "violations": sorted(set(violations)), "review_reasons": sorted(set(reviews)), "status": status,
    }


def verify_values(
    *, normalized_schema_path: Path, current_values_path: Path, capture_path: Path,
    expected_run_id: str,
    expected_target_model: str, expected_template_product_id: str,
    expected_browser_session_sha256: str, now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    normalized, normalized_hash, errors = read_bound(normalized_schema_path, "normalized schema")
    ledger, ledger_hash, ledger_errors = read_bound(current_values_path, "current values ledger")
    errors.extend(ledger_errors)
    capture, capture_hash, capture_errors = read_bound(capture_path, "browser observation capture")
    errors.extend(capture_errors)
    if normalized:
        errors.extend(schema_errors(normalized, NORMALIZED_SCHEMA, "normalized schema"))
        verify_normalized_rule_hashes(normalized, errors)
    if ledger:
        errors.extend(schema_errors(ledger, VALUES_SCHEMA, "current values ledger"))
    if capture:
        errors.extend(schema_errors(capture, CAPTURE_SCHEMA, "browser observation capture"))
    derived_rows, derivation_errors = derive_current_field_rows(capture)
    errors.extend(derivation_errors)
    trust_status = capture_trust_status(capture)
    unsupported: list[str] = list(normalized.get("unsupported_constructs") or [])
    manual = [
        f"{row.get('field_path')}:{row.get('rule_name')}:{row.get('rule_sha256')}"
        for row in normalized.get("manual_review_rules", []) if isinstance(row, dict)
    ]
    if normalized.get("status") == "BLOCKED_UNSUPPORTED_SCHEMA":
        unsupported.append("normalized schema status is BLOCKED_UNSUPPORTED_SCHEMA")
    if normalized.get("status") == "PARSED_REVIEW_REQUIRED" and not manual:
        manual.append("normalized schema requires review without a recorded manual rule")

    expected_target_model = expected_target_model.strip().upper()
    for label, document in (
        ("normalized schema", normalized),
        ("current values ledger", ledger),
        ("browser observation capture", capture),
    ):
        if document.get("run_id") != expected_run_id:
            errors.append(f"{label} run_id mismatch")
        if str(document.get("target_model") or "").upper() != expected_target_model:
            errors.append(f"{label} target_model mismatch")
        if str(document.get("template_product_id") or "") != expected_template_product_id:
            errors.append(f"{label} template_product_id mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_browser_session_sha256):
        errors.append("expected browser session SHA-256 is invalid")
    if ledger.get("browser_session_sha256") != expected_browser_session_sha256:
        errors.append("current values ledger browser session mismatch")
    if capture.get("browser_session_sha256") != expected_browser_session_sha256:
        errors.append("browser observation capture session mismatch")
    ledger_source = ledger.get("source") or {}
    capture_source = capture.get("source") or {}
    if ledger_source.get("capture_source_kind") != capture_source.get("kind"):
        errors.append("current values capture source kind is not capture-derived")
    if ledger_source.get("login_active") is not capture_source.get("login_active"):
        errors.append("current values login state is not capture-derived")
    if ledger_source.get("captcha_present") is not capture_source.get("captcha_present"):
        errors.append("current values CAPTCHA state is not capture-derived")
    if ledger_source.get("credential_values_recorded") is not capture_source.get("credential_values_recorded"):
        errors.append("current values credential-recording state is not capture-derived")
    if ledger_source.get("trust_status") != trust_status:
        errors.append("current values trust status is not capture-derived")

    raw = normalized.get("raw_xml") or {}
    raw_hash = str(raw.get("sha256") or "").lower()
    raw_path: Path | None = None
    try:
        raw_path = Path(str(raw.get("path") or "")).resolve(strict=True)
        if raw_path.stat().st_size != raw.get("bytes") or sha256_file(raw_path) != raw_hash:
            errors.append("raw schema XML file bytes/hash drifted")
    except OSError as exc:
        errors.append(f"raw schema XML file is unavailable: {exc}")
    if raw_path is not None:
        verify_normalized_matches_raw(normalized, raw_path, errors)
    bindings = ledger.get("bindings") or {}
    expected_bindings = {
        "normalized_schema_sha256": normalized_hash, "raw_schema_sha256": raw_hash,
        "schema_capture_id": normalized.get("capture_id"), "schema_field_paths_sha256": normalized.get("field_paths_sha256"),
        "schema_rule_graph_sha256": normalized.get("rule_graph_sha256"),
        **derived_capture_bindings(capture, capture_hash),
    }
    for key, value in expected_bindings.items():
        if bindings.get(key) != value:
            errors.append(f"current values ledger binding mismatch: {key}")
    ledger_body = copy.deepcopy(ledger)
    declared_ledger_body = str(ledger_body.pop("ledger_body_sha256", "") or "").lower()
    computed_ledger_body = object_sha256(ledger_body)
    if declared_ledger_body != computed_ledger_body:
        errors.append("current values ledger body hash mismatch")

    schema_captured = parse_time(normalized.get("captured_at"), "schema captured_at", errors)
    schema_expires = parse_time(normalized.get("expires_at"), "schema expires_at", errors)
    values_captured = parse_time(ledger.get("captured_at"), "values captured_at", errors)
    observation_captured = parse_time(capture.get("captured_at"), "browser observation captured_at", errors)
    if schema_captured and schema_captured > now + FUTURE_CLOCK_SKEW:
        errors.append("official schema capture time is in the future")
    if values_captured and values_captured > now + FUTURE_CLOCK_SKEW:
        errors.append("current values capture time is in the future")
    if observation_captured and observation_captured > now + FUTURE_CLOCK_SKEW:
        errors.append("browser observation capture time is in the future")
    if schema_captured and values_captured and values_captured < schema_captured:
        errors.append("current values predate the official schema capture")
    if schema_captured and observation_captured and observation_captured < schema_captured:
        errors.append("browser observations predate the official schema capture")
    if ledger.get("captured_at") != capture.get("captured_at"):
        errors.append("current values captured_at is not capture-derived")
    if schema_expires and (now > schema_expires or (values_captured and values_captured > schema_expires)):
        errors.append("official schema capture expired")

    fields = flatten([row for row in normalized.get("fields", []) if isinstance(row, dict)])
    paths = [row.get("path") for row in fields]
    if normalized.get("field_paths_sha256") != object_sha256(paths):
        errors.append("normalized schema field_paths_sha256 mismatch")
    current_rows = [row for row in ledger.get("fields", []) if isinstance(row, dict)]
    if current_rows != derived_rows:
        derived_by_path = {str(row.get("path") or ""): row for row in derived_rows}
        ledger_by_path = {str(row.get("path") or ""): row for row in current_rows}
        for path in sorted(set(derived_by_path) | set(ledger_by_path)):
            expected_row = derived_by_path.get(path)
            actual_row = ledger_by_path.get(path)
            if expected_row is None:
                errors.append(f"ledger field has no browser observation: {path}")
                continue
            if actual_row is None:
                errors.append(f"capture-derived ledger field is missing: {path}")
                continue
            for key in sorted(set(expected_row) | set(actual_row)):
                if actual_row.get(key) != expected_row.get(key):
                    errors.append(f"current field is not capture-derived: {path}.{key}")
    current: dict[str, dict[str, Any]] = {}
    for row in current_rows:
        path = str(row.get("path") or "")
        if path in current:
            errors.append(f"duplicate current field path: {path}")
        current[path] = row
        if row.get("value_sha256") != object_sha256(row.get("value")):
            errors.append(f"current field value hash mismatch: {path}")
        if row.get("value_present") is not present(row.get("value")):
            errors.append(f"current field value_present mismatch: {path}")
    if set(current) != set(paths):
        for path in sorted(set(paths) - set(current)):
            errors.append(f"missing current field: {path}")
        for path in sorted(set(current) - set(paths)):
            errors.append(f"unexpected current field: {path}")
    if ledger.get("field_paths_sha256") != object_sha256([row.get("path") for row in current_rows]):
        errors.append("current values field_paths_sha256 mismatch")

    id_groups: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        id_groups.setdefault(str(field.get("id") or ""), []).append(field)
    by_id = {key: rows[0] for key, rows in id_groups.items() if len(rows) == 1}
    for key, rows in id_groups.items():
        if len(rows) > 1 and any(rule.get("depend_groups") for field in fields for rule in field.get("rules", []) if any(expr.get("field_id") == key for group in rule.get("depend_groups", []) for expr in group.get("expressions", []))):
            unsupported.append(f"ambiguous dependency field id: {key}")

    field_results: list[dict[str, Any]] = []
    for field in fields:
        ledger_row = current.get(str(field.get("path") or ""))
        if ledger_row is None:
            continue
        if ledger_row.get("id") != field.get("id"):
            errors.append(f"current field id mismatch: {field.get('path')}")
        field_results.append(evaluate_field(field, ledger_row, by_id, current, unsupported))

    violations = [f"{row['path']}: {message}" for row in field_results for message in row["violations"]]
    field_reviews = [f"{row['path']}: {message}" for row in field_results for message in row["review_reasons"]]
    manual.extend(field_reviews)
    errors.extend(violations)
    unsupported = sorted(set(unsupported))
    manual = sorted(set(manual))
    errors = sorted(set(errors))
    if unsupported:
        status = "BLOCKED_UNSUPPORTED_SCHEMA"
    elif errors:
        status = "BLOCKED_VALUE_VALIDATION"
    elif manual:
        status = "REVIEW_REQUIRED"
    else:
        status = "PASS_MACHINE_RULES"
    receipt = {
        "schema_version": "sellerkit.alibaba-official-schema-values-verification.v2",
        "generated_at": utc_text(now), "status": status,
        "trust_status": trust_status, "live_attestation_verified": False,
        "result_semantics": "READ_ONLY_VALUE_VALIDATION_NOT_WRITE_AUTHORITY",
        "run_id": expected_run_id or None, "target_model": expected_target_model or None,
        "template_product_id": expected_template_product_id or None,
        "browser_session_sha256": expected_browser_session_sha256 or None,
        "bindings": {
            "normalized_schema_sha256": normalized_hash, "raw_schema_sha256": raw_hash,
            "current_values_ledger_sha256": ledger_hash, "current_values_ledger_body_sha256": computed_ledger_body,
            "observation_capture_id": str(capture.get("capture_id") or ""),
            "observation_capture_sha256": capture_hash,
            "observation_capture_body_sha256": str(capture.get("capture_body_sha256") or ""),
            "observation_ids_sha256": str(capture.get("observation_ids_sha256") or ""),
            "external_attestation_verifier_receipt_sha256": None,
            "field_paths_sha256": str(normalized.get("field_paths_sha256") or ""),
            "rule_graph_sha256": str(normalized.get("rule_graph_sha256") or ""),
        },
        "field_count": len(fields), "evaluated_field_count": len(field_results), "field_results": field_results,
        "manual_review_rules": manual, "unsupported_rules": unsupported, "errors": errors,
        "write_authority": "NONE_READ_ONLY_EVIDENCE", "final_write_enabled": False,
    }
    receipt_schema_errors = schema_errors(receipt, RECEIPT_SCHEMA, "verification receipt")
    if receipt_schema_errors:
        receipt["errors"] = sorted(set(receipt["errors"] + receipt_schema_errors))
        if receipt["status"] == "PASS_MACHINE_RULES":
            receipt["status"] = "BLOCKED_VALUE_VALIDATION"
    return receipt


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite receipt: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate current values against normalized official Alibaba schema XML")
    parser.add_argument("--normalized-schema", type=Path, required=True)
    parser.add_argument("--current-values", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-target-model", required=True)
    parser.add_argument("--expected-template-product-id", required=True)
    parser.add_argument("--expected-browser-session-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.receipt.resolve().exists():
        print(json.dumps({
            "status": "BLOCKED_RECEIPT_EXISTS",
            "error": f"refusing to overwrite receipt: {args.receipt.resolve()}",
            "receipt_written": False,
            "final_write_enabled": False,
        }, ensure_ascii=False, sort_keys=True))
        return 3
    receipt = verify_values(
        normalized_schema_path=args.normalized_schema, current_values_path=args.current_values,
        capture_path=args.capture,
        expected_run_id=args.expected_run_id, expected_target_model=args.expected_target_model,
        expected_template_product_id=args.expected_template_product_id,
        expected_browser_session_sha256=args.expected_browser_session_sha256,
    )
    try:
        atomic_write(args.receipt, receipt)
    except (OSError, FileExistsError) as exc:
        print(json.dumps({
            "status": "BLOCKED_RECEIPT_WRITE", "error": str(exc),
            "receipt_written": False, "final_write_enabled": False,
        }, ensure_ascii=False, sort_keys=True))
        return 3
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    if receipt["status"] != "PASS_MACHINE_RULES":
        return 3
    # A locally hash-bound capture (including a Codex tool-result self-report)
    # proves consistency only.  Exit 0 is reserved for a future independently
    # verified LIVE_ATTESTED path; this implementation cannot produce it.
    return 0 if receipt["trust_status"] == "LIVE_ATTESTED" else 4


if __name__ == "__main__":
    raise SystemExit(main())
