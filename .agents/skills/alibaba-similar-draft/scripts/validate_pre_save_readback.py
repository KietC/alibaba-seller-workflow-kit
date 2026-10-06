from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from localized_field_contract import LocalizedFieldError, validate_localized_target


SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
ACTION_PLAN_SCHEMA = SCHEMA_DIR / "browser-action-plan.schema.json"
RUN_SPEC_SCHEMA = SCHEMA_DIR / "run-spec.schema.json"
PROBE_SOURCE_SCHEMA = SCHEMA_DIR / "probe-receipt.schema.json"
PROBE_VERIFICATION_SCHEMA = SCHEMA_DIR / "probe-verification-receipt.schema.json"
PRE_SAVE_SCHEMA = SCHEMA_DIR / "pre-save-readback.schema.json"
RECEIPT_SCHEMA = SCHEMA_DIR / "pre-save-verification-receipt.schema.json"

MAX_PROBE_AGE = timedelta(minutes=15)
FUTURE_CLOCK_SKEW = timedelta(seconds=5)
EMPTY_READBACK_SHA256 = hashlib.sha256(b'""').hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_bound_object(path: Path, label: str) -> tuple[dict[str, Any], str, list[str]]:
    try:
        payload = path.resolve(strict=True).read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, "", [f"cannot read {label}: {path}: {exc}"]
    if not isinstance(value, dict):
        return {}, sha256_bytes(payload), [f"{label} must be a JSON object"]
    return value, sha256_bytes(payload), []


def schema_errors(document: dict[str, Any], schema_path: Path, label: str) -> list[str]:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"cannot read {label} schema: {exc}"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    found = sorted(validator.iter_errors(document), key=lambda item: tuple(str(part) for part in item.path))
    result: list[str] = []
    for error in found:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        result.append(f"{label} schema: {location}: {error.message}")
    return result


def parse_timestamp(value: Any, label: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be an RFC3339 timestamp")
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        errors.append(f"{label} is not a valid RFC3339 timestamp")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{label} must include a timezone")
        return None
    return parsed.astimezone(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def action_plan_body_sha256(plan: dict[str, Any], errors: list[str]) -> str:
    body = copy.deepcopy(plan)
    declared = str(body.pop("plan_body_sha256", "") or "").lower()
    computed = object_sha256(body)
    if declared != computed:
        errors.append("action plan plan_body_sha256 does not hash its canonical body")
    return computed


def target_key(value: Any, label: str, errors: list[str], *, asset: bool = False) -> str | None:
    if not isinstance(value, dict):
        errors.append(f"{label} target is not an object")
        return None
    allowed = {"ASSET_SLOT"} if asset else {"FIELD_REGISTRY", "CATEGORY_PROPERTY_RUNTIME_KEY", "LOCALIZED_FIELD"}
    if value.get("kind") not in allowed or not str(value.get("logical_name") or "").strip():
        errors.append(f"{label} target identity is invalid")
        return None
    if value.get("kind") == "CATEGORY_PROPERTY_RUNTIME_KEY" and not str(value.get("approved_property_name") or "").strip():
        errors.append(f"{label} category target is missing approved_property_name")
        return None
    if value.get("kind") == "LOCALIZED_FIELD":
        required = {
            "locale", "live_language_label", "direction", "expected_sha256",
            "max_length_rule_hash", "max_length_rule", "identity_sha256",
        }
        if any(key not in value for key in required):
            errors.append(f"{label} localized target identity is incomplete")
            return None
    return canonical_json(value)


def phase_operations(plan: dict[str, Any], name: str, errors: list[str]) -> list[dict[str, Any]]:
    matches = [row for row in plan.get("phases", []) if isinstance(row, dict) and row.get("name") == name]
    if len(matches) != 1:
        errors.append(f"action plan must contain exactly one {name} phase")
        return []
    operations = matches[0].get("operations")
    if not isinstance(operations, list):
        errors.append(f"action plan {name} operations must be a list")
        return []
    return [row for row in operations if isinstance(row, dict)]


def expected_contract(
    plan: dict[str, Any],
    errors: list[str],
    *,
    expected_page_fingerprint_sha256: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    for index, operation in enumerate(phase_operations(plan, "WRITE_APPROVED_FIELD_VALUES", errors)):
        if operation.get("operation") != "WRITE_APPROVED_FIELD_VALUE":
            errors.append(f"unexpected write operation at index {index}")
            continue
        key = target_key(operation.get("target"), f"write operation {index}", errors)
        if key is None:
            continue
        if (operation.get("target") or {}).get("kind") == "LOCALIZED_FIELD":
            try:
                validate_localized_target(
                    operation["target"],
                    operation.get("value"),
                    expected_page_fingerprint_sha256=expected_page_fingerprint_sha256,
                )
            except (LocalizedFieldError, KeyError) as exc:
                errors.append(f"write operation {index} localized target is invalid: {exc}")
                continue
        if key in fields:
            errors.append(f"duplicate action-plan write target: {key}")
            continue
        value_hash = str(operation.get("value_sha256") or "").lower()
        approval = operation.get("approval") or {}
        if str((operation.get("readback") or {}).get("expected_sha256") or "").lower() != value_hash:
            errors.append(f"action-plan field readback hash mismatch: {key}")
        fields[key] = {
            "target": copy.deepcopy(operation.get("target")),
            "expected_sha256": value_hash,
            "source_id": str(approval.get("source_id") or ""),
            "decision_id": str(approval.get("decision_id") or ""),
        }

    clears: dict[str, dict[str, Any]] = {}
    for index, operation in enumerate(phase_operations(plan, "CLEAR_INHERITED_FIELDS", errors)):
        if operation.get("operation") != "CLEAR_INHERITED_FIELD":
            errors.append(f"unexpected clear operation at index {index}")
            continue
        key = target_key(operation.get("target"), f"clear operation {index}", errors)
        if key is None:
            continue
        if key in clears:
            errors.append(f"duplicate action-plan clear target: {key}")
            continue
        clears[key] = {"target": copy.deepcopy(operation.get("target")), "reason": operation.get("reason")}

    for key, row in sorted(clears.items()):
        target = row["target"]
        if target.get("kind") == "LOCALIZED_FIELD" and key not in fields:
            errors.append(f"localized clear target has no identical approved write target: {key}")

    assets: dict[str, dict[str, Any]] = {}
    for index, operation in enumerate(phase_operations(plan, "UPLOAD_APPROVED_ASSETS", errors)):
        if operation.get("operation") != "UPLOAD_APPROVED_ASSET":
            errors.append(f"unexpected asset operation at index {index}")
            continue
        key = target_key(operation.get("target"), f"asset operation {index}", errors, asset=True)
        if key is None:
            continue
        if key in assets:
            errors.append(f"duplicate action-plan asset target: {key}")
            continue
        descriptor = operation.get("asset") or {}
        expected_hash = str((operation.get("readback") or {}).get("expected_sha256") or "").lower()
        if expected_hash != str(descriptor.get("sha256") or "").lower():
            errors.append(f"action-plan asset readback hash mismatch: {key}")
        assets[key] = {
            "target": copy.deepcopy(operation.get("target")), "asset_id": descriptor.get("asset_id"),
            "path": descriptor.get("path"), "bytes": descriptor.get("bytes"), "media_type": descriptor.get("media_type"),
            "expected_sha256": expected_hash, "upload_order": (operation.get("target") or {}).get("upload_order"),
        }

    field_rows = [fields[key] for key in sorted(fields)]
    clear_rows = [clears[key] for key in sorted(clears)]
    asset_rows = [assets[key] for key in sorted(assets)]
    mutation_targets = sorted(
        [{"operation": "CLEAR", "target": row["target"]} for row in clear_rows]
        + [{"operation": "WRITE", "target": row["target"]} for row in field_rows]
        + [{"operation": "UPLOAD", "target": row["target"]} for row in asset_rows],
        key=canonical_json,
    )
    non_target_scope = {
        "scope": "VISIBLE_ENABLED_FORM_CONTROLS_EXCLUDING_EXACT_ACTION_PLAN_MUTATION_TARGETS",
        "normalization": "CANONICAL_TARGET_AND_VALUE_STATE_V1",
        "excluded_mutation_targets": mutation_targets,
    }
    return {
        "fields": fields, "clears": clears, "assets": assets,
        "field_set_sha256": object_sha256(field_rows), "clear_set_sha256": object_sha256(clear_rows),
        "asset_set_sha256": object_sha256(asset_rows), "non_target_scope_sha256": object_sha256(non_target_scope),
    }


def verify_rows(readback: dict[str, Any], contract: dict[str, Any], errors: list[str]) -> None:
    def index_rows(name: str, *, asset: bool = False) -> dict[str, dict[str, Any]]:
        rows = readback.get(name)
        if not isinstance(rows, list):
            errors.append(f"pre-save {name} must be a list")
            return {}
        result: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"pre-save {name} row {index} is not an object")
                continue
            key = target_key(row.get("target"), f"pre-save {name} row {index}", errors, asset=asset)
            if key is None:
                continue
            if key in result:
                errors.append(f"pre-save duplicate {name} target: {key}")
            result[key] = row
        return result

    observed_fields = index_rows("field_readbacks")
    observed_clears = index_rows("cleared_targets")
    observed_assets = index_rows("asset_readbacks", asset=True)
    for label, expected, observed in (
        ("field", contract["fields"], observed_fields), ("clear", contract["clears"], observed_clears),
        ("asset", contract["assets"], observed_assets),
    ):
        for key in sorted(set(expected) - set(observed)):
            errors.append(f"pre-save missing expected {label} readback: {key}")
        for key in sorted(set(observed) - set(expected)):
            errors.append(f"pre-save unexpected {label} readback: {key}")

    for key in sorted(set(contract["fields"]) & set(observed_fields)):
        expected, row = contract["fields"][key], observed_fields[key]
        for name in ("expected_sha256", "source_id", "decision_id"):
            if row.get(name) != expected[name]:
                errors.append(f"pre-save field {name} mismatch: {key}")
        if row.get("observed_sha256") != expected["expected_sha256"]:
            errors.append(f"pre-save field observed_sha256 mismatch: {key}")
        target = expected["target"]
        if target.get("kind") == "LOCALIZED_FIELD":
            rule = target.get("max_length_rule") or {}
            localized_expected = {
                "observed_locale": target.get("locale"),
                "observed_live_language_label": target.get("live_language_label"),
                "observed_direction": target.get("direction"),
                "observed_count": rule.get("observed_count"),
                "observed_maximum": rule.get("maximum"),
                "observed_unit": rule.get("unit"),
                "observed_max_length_rule_hash": target.get("max_length_rule_hash"),
                "observed_page_fingerprint_sha256": rule.get("page_fingerprint_sha256"),
                "post_write_counter_readback": True,
            }
            for name, value in localized_expected.items():
                if row.get(name) != value:
                    errors.append(f"pre-save localized field {name} mismatch: {key}")
            observed_count = row.get("observed_count")
            observed_maximum = row.get("observed_maximum")
            if not isinstance(observed_count, int) or not isinstance(observed_maximum, int) or observed_count > observed_maximum:
                errors.append(f"pre-save localized field live counter exceeds maximum: {key}")

    for key in sorted(set(contract["clears"]) & set(observed_clears)):
        expected, row = contract["clears"][key], observed_clears[key]
        if row.get("reason") != expected["reason"]:
            errors.append(f"pre-save clear reason mismatch: {key}")
        if row.get("captured_phase") != "CLEAR_INHERITED_FIELDS":
            errors.append(f"pre-save clear phase mismatch: {key}")
        if row.get("expected_state") != "EMPTY" or row.get("observed_state") != "EMPTY":
            errors.append(f"pre-save clear was not proven empty: {key}")
        if row.get("expected_sha256") != EMPTY_READBACK_SHA256 or row.get("observed_sha256") != EMPTY_READBACK_SHA256:
            errors.append(f"pre-save clear empty hash mismatch: {key}")

    for key in sorted(set(contract["assets"]) & set(observed_assets)):
        expected, row = contract["assets"][key], observed_assets[key]
        for name in ("asset_id", "path", "bytes", "media_type", "expected_sha256", "upload_order"):
            if row.get(name) != expected[name]:
                errors.append(f"pre-save asset {name} mismatch: {key}")
        if row.get("observed_sha256") != expected["expected_sha256"]:
            errors.append(f"pre-save asset observed_sha256 mismatch: {key}")


def verify_pre_save(
    *, action_plan_path: Path, run_spec_path: Path, probe_source_path: Path,
    probe_verification_path: Path, pre_save_readback_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    inputs: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    errors: list[str] = []
    specs = {
        "action_plan": (action_plan_path, ACTION_PLAN_SCHEMA, "action plan"),
        "run_spec": (run_spec_path, RUN_SPEC_SCHEMA, "run spec"),
        "probe_source": (probe_source_path, PROBE_SOURCE_SCHEMA, "probe source"),
        "probe_verification": (probe_verification_path, PROBE_VERIFICATION_SCHEMA, "probe verification"),
        "readback": (pre_save_readback_path, PRE_SAVE_SCHEMA, "pre-save readback"),
    }
    for key, (path, schema, label) in specs.items():
        document, digest, read_errors = read_bound_object(path, label)
        inputs[key], hashes[key] = document, digest
        errors.extend(read_errors)
        if not read_errors:
            errors.extend(schema_errors(document, schema, label))

    plan, run_spec = inputs["action_plan"], inputs["run_spec"]
    source, probe, readback = inputs["probe_source"], inputs["probe_verification"], inputs["readback"]
    body_hash = action_plan_body_sha256(plan, errors)
    page_fingerprint = (source.get("page") or {}).get("fingerprint_sha256")
    contract = expected_contract(
        plan,
        errors,
        expected_page_fingerprint_sha256=(
            str(page_fingerprint).lower() if isinstance(page_fingerprint, str) else None
        ),
    )
    expected_bindings = {
        "action_plan_sha256": hashes["action_plan"], "action_plan_body_sha256": body_hash,
        "run_spec_sha256": hashes["run_spec"], "probe_source_sha256": hashes["probe_source"],
        "probe_verification_sha256": hashes["probe_verification"],
        "expected_field_set_sha256": contract["field_set_sha256"], "expected_clear_set_sha256": contract["clear_set_sha256"],
        "expected_asset_set_sha256": contract["asset_set_sha256"], "expected_non_target_scope_sha256": contract["non_target_scope_sha256"],
    }
    for name, expected in expected_bindings.items():
        if str((readback.get("bindings") or {}).get(name) or "").lower() != expected:
            errors.append(f"pre-save binding mismatch: {name}")

    target = plan.get("target") or {}
    expected_model = str(target.get("model") or "")
    expected_template = str(target.get("template_product_id") or "")
    expected_run = str(target.get("run_id") or "")
    for label, document in (("run spec", run_spec), ("probe source", source), ("probe verification", probe), ("pre-save readback", readback)):
        if document.get("target_model") != expected_model:
            errors.append(f"{label} target_model mismatch")
        if str(document.get("template_product_id") or "") != expected_template:
            errors.append(f"{label} template_product_id mismatch")
    if source.get("run_id") != expected_run or readback.get("run_id") != expected_run:
        errors.append("run_id does not match the action plan")
    if str(((plan.get("bindings") or {}).get("three_party") or {}).get("run_spec_sha256") or "").lower() != hashes["run_spec"]:
        errors.append("action plan does not bind the supplied run spec file")
    probe_bindings = probe.get("bindings") or {}
    if str(probe_bindings.get("action_plan_sha256") or "").lower() != hashes["action_plan"]:
        errors.append("probe verification does not bind the supplied action plan file")
    if str(probe_bindings.get("run_spec_sha256") or "").lower() != hashes["run_spec"]:
        errors.append("probe verification does not bind the supplied run spec file")
    if probe_bindings.get("action_plan_id") != plan.get("plan_id"):
        errors.append("probe verification action_plan_id mismatch")
    if str(probe.get("input_probe_sha256") or "").lower() != hashes["probe_source"]:
        errors.append("probe verification does not bind the supplied probe source file")
    if probe.get("status") != "PASS_ZERO_WRITE" or probe.get("errors") != []:
        errors.append("probe verification is not a clean PASS_ZERO_WRITE")
    if probe.get("schema_version") != "sellerkit.browser_probe_receipt.v3":
        errors.append("probe verification is not the execution-gated v3 receipt")
    if probe.get("zero_write_confirmed") is not True:
        errors.append("probe verification did not confirm zero write")
    if probe.get("execution_authorized") is not True:
        errors.append("probe verification does not authorize controlled execution")
    if probe.get("production_execution_authorized") is not True:
        errors.append("probe verification does not authorize production execution")
    if probe.get("adapter_production_execution_ready") is not True:
        errors.append("probe verification adapter production readiness is not certified")
    if probe.get("production_authorization_errors") != []:
        errors.append("probe verification contains production authorization errors")
    if probe.get("authorization_errors") != []:
        errors.append("probe verification contains execution authorization errors")
    classifications = probe.get("request_classifications")
    if not isinstance(classifications, list) or not classifications:
        errors.append("probe verification has no request classifications")
    else:
        for index, classification in enumerate(classifications):
            decision = classification.get("decision") if isinstance(classification, dict) else None
            if decision != "ALLOW":
                errors.append(f"probe request classification {index} is not exactly ALLOW")
    if source.get("result") != "READ_ONLY_VERIFIED" or source.get("errors") != []:
        errors.append("probe source is not a clean READ_ONLY_VERIFIED receipt")
    network = source.get("network") or {}
    if network.get("write_requests_allowed") != 0 or network.get("unknown_write_count") != 0 or network.get("final_write_count") != 0:
        errors.append("probe source did not prove zero allowed/unknown/final writes")
    if readback.get("browser_session_sha256") != network.get("interception_session_sha256"):
        errors.append("pre-save browser session does not match the probed guarded session")
    if readback.get("current_page_fingerprint_sha256") != page_fingerprint or probe.get("page_fingerprint") != page_fingerprint:
        errors.append("pre-save page fingerprint does not match the current verified probe")

    captured = parse_timestamp(readback.get("captured_at"), "pre-save captured_at", errors)
    probe_completed = parse_timestamp(source.get("completed_at"), "probe completed_at", errors)
    if captured is not None:
        if captured > now + FUTURE_CLOCK_SKEW:
            errors.append("pre-save readback is from the future")
        if probe_completed is not None and (captured < probe_completed or captured - probe_completed > MAX_PROBE_AGE):
            errors.append("pre-save readback is not after a current probe within 15 minutes")
        approval_expires = parse_timestamp((((plan.get("bindings") or {}).get("write_approval") or {}).get("expires_at")), "write approval expires_at", errors)
        if approval_expires is not None and approval_expires < captured:
            errors.append("write approval expired before pre-save readback")

    verify_rows(readback, contract, errors)
    if readback.get("non_target_scope_sha256") != contract["non_target_scope_sha256"]:
        errors.append("pre-save non-target scope hash mismatch")
    if readback.get("non_target_snapshot_before_sha256") != readback.get("non_target_snapshot_after_sha256"):
        errors.append("pre-save non-target snapshot changed")
    if readback.get("non_target_field_diffs") != []:
        errors.append("pre-save non-target field diffs are not empty")
    if readback.get("draft_save_count") != 0 or readback.get("visible_ui_save_used") is not False:
        errors.append("pre-save evidence was captured after a draft-save action")
    if readback.get("final_write_count") != 0 or readback.get("final_submit_triggered") is not False:
        errors.append("pre-save evidence contains a final-write signal")
    for flag in ("request_guard_active", "final_ui_guard_active", "final_network_guard_active"):
        if readback.get(flag) is not True:
            errors.append(f"pre-save guard is not active: {flag}")

    unique_errors = sorted(set(errors))
    receipt = {
        "schema_version": "sellerkit.alibaba.pre-save-verification.v1", "generated_at": utc_text(now),
        "status": "PASS_PRE_SAVE_NO_WRITE" if not unique_errors else "BLOCKED_PRE_SAVE",
        "result_semantics": "FORM_READBACK_ONLY_NOT_DRAFT_SUCCESS_NOT_WRITE_AUTHORITY",
        "target_model": expected_model or None, "template_product_id": expected_template or None, "run_id": expected_run or None,
        "bindings": {**expected_bindings, "pre_save_readback_sha256": hashes["readback"]},
        "expected_field_count": len(contract["fields"]), "expected_clear_count": len(contract["clears"]),
        "expected_asset_count": len(contract["assets"]), "observed_field_count": len(readback.get("field_readbacks") or []),
        "observed_clear_count": len(readback.get("cleared_targets") or []), "observed_asset_count": len(readback.get("asset_readbacks") or []),
        "draft_save_count": readback.get("draft_save_count"), "final_write_count": readback.get("final_write_count"), "errors": unique_errors,
    }
    receipt_errors = schema_errors(receipt, RECEIPT_SCHEMA, "pre-save verification receipt")
    if receipt_errors:
        receipt["errors"] = sorted(set(receipt["errors"] + receipt_errors))
        receipt["status"] = "BLOCKED_PRE_SAVE"
    return receipt


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing receipt: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed pre-save exact readback validator")
    parser.add_argument("--action-plan", type=Path, required=True)
    parser.add_argument("--run-spec", type=Path, required=True)
    parser.add_argument("--probe-source", type=Path, required=True)
    parser.add_argument("--probe-verification", type=Path, required=True)
    parser.add_argument("--pre-save-readback", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.receipt.resolve().exists():
        print(json.dumps({
            "status": "BLOCKED_RECEIPT_EXISTS",
            "error": f"refusing to overwrite existing receipt: {args.receipt.resolve()}",
            "receipt_written": False, "final_write_enabled": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    receipt = verify_pre_save(
        action_plan_path=args.action_plan, run_spec_path=args.run_spec,
        probe_source_path=args.probe_source, probe_verification_path=args.probe_verification,
        pre_save_readback_path=args.pre_save_readback,
    )
    try:
        atomic_write_json(args.receipt, receipt)
    except (OSError, FileExistsError) as exc:
        print(json.dumps({
            "status": "BLOCKED_RECEIPT_WRITE", "error": str(exc),
            "receipt_written": False, "final_write_enabled": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS_PRE_SAVE_NO_WRITE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
