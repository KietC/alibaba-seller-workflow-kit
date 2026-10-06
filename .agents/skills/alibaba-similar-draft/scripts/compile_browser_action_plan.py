from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from draft_core import (
    FIELD_REGISTRY_ALIASES,
    canonical_json,
    canonical_commercial_field,
    duplicate_registry_targets,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now,
    validate_approved_manifest,
)
from localized_field_contract import LocalizedFieldError, validate_localized_target


SCHEMA_VERSION = "alibaba.browser-action-plan.v1"
COMPILER_VERSION = "1.2.0"
DEFAULT_ADAPTER = Path(__file__).resolve().parent.parent / "references" / "adapters" / "iab-v1.json"
PLAN_SCHEMA = Path(__file__).resolve().parent.parent / "references" / "schemas" / "browser-action-plan.schema.json"
WRITE_APPROVAL_SCHEMA = (
    Path(__file__).resolve().parent.parent
    / "references"
    / "schemas"
    / "draft-write-approval.schema.json"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STALE_RUNTIME_KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9])p-[0-9]+")
CATEGORY_PROPERTY_PREFIX = "category_properties."
BLOCK_EQUIVALENTS = {
    name: {alias, target}
    for alias, target in FIELD_REGISTRY_ALIASES.items()
    for name in (alias, target)
}
ASSET_REGISTRY_FIELDS = {
    "MAIN_IMAGE": "main_images",
    "MAIN_VIDEO": "main_video",
    "DETAIL_IMAGE": "detail_images",
    "DETAIL_VIDEO": "detail_video",
    "CERTIFICATE": "certificates",
}


class PlanCompileError(RuntimeError):
    """A fail-closed action-plan compilation error."""


def _validate_output_schema(plan: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - deployment diagnostic
        raise RuntimeError(
            "jsonschema is required; use the bundled offline wheelhouse"
        ) from exc
    schema = _load_object(PLAN_SCHEMA, "browser action plan schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(plan), key=lambda item: list(item.absolute_path))
    if errors:
        detail = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
            for error in errors
        )
        raise PlanCompileError(f"browser action plan schema validation failed: {detail}")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanCompileError(f"{label} is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanCompileError(f"{label} must be a JSON object")
    return value


def _require_sha256(value: Any, label: str) -> str:
    normalized = str(value or "").lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise PlanCompileError(f"{label} is not a lowercase SHA-256")
    return normalized


def _binding_hash(values: dict[str, str]) -> str:
    return sha256_bytes(canonical_json(values).encode("utf-8"))


def _validate_document_schema(document: dict[str, Any], schema_path: Path, label: str) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - deployment diagnostic
        raise RuntimeError(
            "jsonschema is required; use the bundled offline wheelhouse"
        ) from exc
    schema = _load_object(schema_path, f"{label} schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    if errors:
        detail = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
            for error in errors
        )
        raise PlanCompileError(f"{label} schema validation failed: {detail}")


def _parse_timestamp(value: Any, label: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise PlanCompileError(f"{label} is not a valid date-time") from exc
    if parsed.tzinfo is None:
        raise PlanCompileError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _expanded_blocked_fields(blocked_fields: Iterable[str]) -> set[str]:
    expanded: set[str] = set()
    for field in blocked_fields:
        normalized = str(field).strip()
        if not normalized:
            continue
        expanded.update(BLOCK_EQUIVALENTS.get(normalized, {normalized}))
    return expanded


def _validate_run_and_bindings(
    source_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    run_spec: dict[str, Any],
    field_registry: dict[str, Any],
    endpoint_policy: dict[str, Any],
    *,
    registry_sha256: str,
    policy_sha256: str,
) -> None:
    errors: list[str] = []
    if run_spec.get("schema_version") != "sellerkit.similar_draft_run_spec.v1":
        errors.append("unsupported run spec schema")
    if run_spec.get("run_kind") != "CREATE_SIMILAR_DRAFT":
        errors.append("run spec must explicitly declare CREATE_SIMILAR_DRAFT")
    if run_spec.get("mode") != "save_draft":
        errors.append("run spec mode must be save_draft")
    for key in ("external_write_enabled", "direct_endpoint_write_enabled", "final_write_enabled"):
        if run_spec.get(key) is not False:
            errors.append(f"run spec {key} must be false")
    if run_spec.get("inherited_content_must_be_cleared") is not True:
        errors.append("run spec must require inherited content clearing")
    if run_spec.get("max_draft_save_count") != 1:
        errors.append("run spec max_draft_save_count must be exactly one")

    target_model = str(source_manifest.get("target_model") or "").strip().upper()
    template_product_id = str((source_manifest.get("bindings") or {}).get("template_product_id") or "")
    if str(run_spec.get("target_model") or "").strip().upper() != target_model:
        errors.append("run spec target model does not match approved source manifest")
    if str(run_spec.get("template_product_id") or "") != template_product_id:
        errors.append("run spec template product id does not match approved source manifest")
    if str(asset_manifest.get("target_model") or "").strip().upper() != target_model:
        errors.append("approved asset manifest target model mismatch")
    if source_manifest.get("run_id") != asset_manifest.get("run_id"):
        errors.append("approved source and asset manifest run ids differ")

    source_bindings = source_manifest.get("bindings") or {}
    asset_bindings = asset_manifest.get("bindings") or {}
    if source_bindings.get("field_registry_sha256") != registry_sha256:
        errors.append("approved source manifest is not bound to the supplied field registry")
    if asset_bindings.get("field_registry_sha256") != registry_sha256:
        errors.append("approved asset manifest is not bound to the supplied field registry")

    if field_registry.get("schema_version") != "alibaba.field-registry.v1":
        errors.append("unsupported field registry schema")
    registry_policy = field_registry.get("policy") or {}
    if registry_policy.get("final_write_enabled") is not False or registry_policy.get("final_write_count_limit") != 0:
        errors.append("field registry does not permanently disable final write")
    names = [str(row.get("logical_name") or "") for row in field_registry.get("fields") or []]
    if not names or not all(names) or len(names) != len(set(names)):
        errors.append("field registry logical field names are empty or duplicated")

    if endpoint_policy.get("schema_version") != "alibaba.endpoint-policy.v1":
        errors.append("unsupported endpoint policy schema")
    if endpoint_policy.get("default_decision") != "DENY":
        errors.append("endpoint policy default decision must be DENY")
    if endpoint_policy.get("final_write_enabled") is not False:
        errors.append("endpoint policy final_write_enabled must be false")
    final_rules = [
        row for row in endpoint_policy.get("rules") or [] if isinstance(row, dict) and row.get("classification") == "FINAL_WRITE"
    ]
    if not final_rules:
        errors.append("endpoint policy has no FINAL_WRITE hard-deny rule")
    for rule in final_rules:
        unsafe = (
            rule.get("decision") in {"ALLOW", "ALLOW_UI_ONLY"}
            or rule.get("direct_call_allowed") is True
            or rule.get("ui_only") is True
        )
        if unsafe:
            errors.append(f"endpoint policy FINAL_WRITE rule is unsafe: {rule.get('id') or '<unnamed>'}")

    run_policy = run_spec.get("endpoint_policy") or {}
    if str(run_policy.get("sha256") or "").lower() != policy_sha256:
        errors.append("run spec endpoint policy hash does not match supplied endpoint policy")

    if errors:
        raise PlanCompileError("; ".join(errors))


def _validate_write_approval(
    write_approval: dict[str, Any],
    source_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    run_spec: dict[str, Any],
    *,
    input_hashes: dict[str, str],
    now: datetime | None = None,
) -> None:
    """Validate the independent, one-run authority to save one visible-UI draft."""
    _validate_document_schema(write_approval, WRITE_APPROVAL_SCHEMA, "draft write approval")
    errors: list[str] = []
    observed_now = now or datetime.now(timezone.utc)
    approved_at = _parse_timestamp(write_approval.get("approved_at"), "write approval approved_at")
    expires_at = _parse_timestamp(write_approval.get("expires_at"), "write approval expires_at")
    if approved_at > observed_now:
        errors.append("write approval approved_at is in the future")
    if expires_at <= observed_now:
        errors.append("write approval is expired")
    if expires_at <= approved_at:
        errors.append("write approval expires_at must be later than approved_at")

    target_model = str(source_manifest.get("target_model") or "").strip().upper()
    run_id = str(source_manifest.get("run_id") or "")
    template_product_id = str(run_spec.get("template_product_id") or "")
    if str(write_approval.get("target_model") or "").strip().upper() != target_model:
        errors.append("write approval target model mismatch")
    if str(write_approval.get("run_id") or "") != run_id:
        errors.append("write approval run id mismatch")
    if str(write_approval.get("template_product_id") or "") != template_product_id:
        errors.append("write approval template product id mismatch")
    if str(asset_manifest.get("run_id") or "") != run_id:
        errors.append("approved asset manifest run id mismatch")

    approval_bindings = write_approval.get("bindings") or {}
    expected_bindings = {
        "approved_source_manifest_sha256": input_hashes["approved_source_manifest"],
        "approved_asset_manifest_sha256": input_hashes["approved_asset_manifest"],
        "run_spec_sha256": input_hashes["run_spec"],
        "field_registry_sha256": input_hashes["field_registry"],
        "endpoint_policy_sha256": input_hashes["endpoint_policy"],
        "adapter_sha256": input_hashes["adapter"],
    }
    for key, expected in expected_bindings.items():
        if str(approval_bindings.get(key) or "").lower() != expected:
            errors.append(f"write approval binding mismatch: {key}")

    if errors:
        raise PlanCompileError("; ".join(errors))


def _validate_adapter(adapter: dict[str, Any], *, require_localized_fields: bool = False) -> None:
    errors: list[str] = []
    if adapter.get("schema_version") != "alibaba.iab-logical-adapter.v1":
        errors.append("unsupported in-app browser adapter schema")
    runtime = adapter.get("runtime_resolution") or {}
    if (
        runtime.get("current_probe_required") is not True
        or runtime.get("re_resolve_after_each_page_change") is not True
        or runtime.get("discard_prior_resolutions") is not True
        or runtime.get("coordinate_fallback_allowed") is not False
        or runtime.get("stale_dom_binding_allowed") is not False
    ):
        errors.append("adapter runtime resolution policy is not fail-closed")
    locator_ids = {
        str(row.get("locator_id") or "")
        for row in adapter.get("logical_locators") or []
        if isinstance(row, dict)
    }
    required = {
        "page.current_probe",
        "field.registry_runtime",
        "field.category_property_runtime",
        "asset.upload_runtime",
        "action.save_draft_runtime",
        "guard.final_submit",
    }
    if not required <= locator_ids:
        errors.append(f"adapter is missing logical locators: {sorted(required - locator_ids)}")
    if require_localized_fields:
        localized = ((adapter.get("capabilities") or {}).get("localized_fields") or {})
        required_capability = {
            "enabled": True,
            "locator_id": "field.localized_runtime",
            "resolution_source": "CURRENT_VISIBLE_LANGUAGE_LABEL_AND_LOCALE",
            "direction_validation_required": True,
            "max_length_rule_validation_required": True,
            "page_fingerprint_binding_required": True,
            "current_probe_required": True,
            "cached_language_index_allowed": False,
            "fixed_language_order_allowed": False,
            "coordinate_fallback_allowed": False,
        }
        if localized != required_capability:
            errors.append(
                "adapter lacks explicit LOCALIZED_FIELD support or its capability is not fail-closed"
            )
        if "field.localized_runtime" not in locator_ids:
            errors.append("adapter is missing localized field locator: field.localized_runtime")

    forbidden_keys = {"selector", "css", "xpath", "coordinates", "coordinate", "x", "y", "dom_id", "element_id"}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in forbidden_keys:
                    errors.append(f"adapter contains forbidden stale/coordinate locator key: {key}")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(adapter)
    if STALE_RUNTIME_KEY_PATTERN.search(canonical_json(adapter)):
        errors.append("adapter contains a concrete stale category runtime key")
    if errors:
        raise PlanCompileError("; ".join(errors))


def _approved_value_operations(
    source_manifest: dict[str, Any],
    registry_fields: dict[str, dict[str, Any]],
    blocked_expanded: set[str],
) -> list[dict[str, Any]]:
    sources = {
        str(row.get("source_id") or ""): row
        for row in source_manifest.get("sources") or []
        if isinstance(row, dict)
    }
    decisions = source_manifest.get("approved_field_values") or {}
    if not isinstance(decisions, dict):
        raise PlanCompileError("approved_field_values must be an object")
    duplicate_targets = duplicate_registry_targets(decisions)
    if duplicate_targets:
        raise PlanCompileError(f"duplicate approved registry targets after alias normalization: {duplicate_targets}")

    operations: list[dict[str, Any]] = []
    errors: list[str] = []
    for order, field in enumerate(sorted(decisions), 1):
        decision = decisions[field]
        if not isinstance(decision, dict):
            errors.append(f"approved value decision is not an object: {field}")
            continue
        if field in blocked_expanded:
            errors.append(f"blocked field has an approved write value: {field}")
            continue
        source = sources.get(str(decision.get("source_id") or ""))
        if source is None or field not in {str(item) for item in source.get("approved_fields") or []}:
            errors.append(f"field value is outside approved source scope: {field}")
            continue
        if decision.get("decision_id") != source.get("decision_id"):
            errors.append(f"field value decision binding mismatch: {field}")
            continue
        value = decision.get("value")
        value_hash = sha256_bytes(canonical_json(value).encode("utf-8"))
        if str(decision.get("value_sha256") or "").lower() != value_hash:
            errors.append(f"field value hash mismatch: {field}")
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"field value is empty: {field}")
            continue

        write_target = decision.get("write_target")
        if isinstance(write_target, dict) and write_target.get("kind") == "LOCALIZED_FIELD":
            try:
                validate_localized_target(write_target, value)
            except LocalizedFieldError as exc:
                errors.append(f"localized field target is invalid for {field}: {exc}")
                continue
            expected_field_key = (
                f"localized.{write_target['logical_name']}.{str(write_target['locale']).lower()}"
            )
            if field != expected_field_key:
                errors.append(
                    f"localized manifest field key does not match target identity: {field} != {expected_field_key}"
                )
                continue
            target = dict(write_target)
            locator_ref = "field.localized_runtime"
            rule = write_target["max_length_rule"]
            readback = {
                "required": True,
                "compare": "LOCALIZED_CANONICAL_IDENTITY_AND_VALUE_SHA256",
                "expected_sha256": value_hash,
                "identity_sha256": write_target["identity_sha256"],
                "max_length_rule_hash": write_target["max_length_rule_hash"],
                "expected_direction": write_target["direction"],
                "expected_live_language_label": write_target["live_language_label"],
                "require_current_counter_within_rule": True,
            }
            length_validation = {
                "rule_hash": write_target["max_length_rule_hash"],
                "unit": rule["unit"],
                "maximum": rule["maximum"],
                "expected_count": rule["observed_count"],
                "page_fingerprint_sha256": rule["page_fingerprint_sha256"],
                "require_current_counter_readback": True,
                "failure_action": "STOP_PRESERVE_EVIDENCE",
            }
        else:
            registry_name = FIELD_REGISTRY_ALIASES.get(field, field)
            registry_row = registry_fields.get(registry_name)
            if registry_row is not None and registry_row.get("source_policy") == "APPROVED_COMMERCIAL_DECISION":
                if canonical_commercial_field(field) is None:
                    errors.append(f"commercial registry field has no qualified decision schema: {field}")
                    continue
                if source.get("source_role") != "COMMERCIAL_DECISION":
                    errors.append(f"commercial registry field requires COMMERCIAL_DECISION: {field}")
                    continue
            readback = {
                "required": True,
                "compare": "CANONICAL_VALUE_SHA256",
                "expected_sha256": value_hash,
            }
            length_validation = None
        if not (isinstance(write_target, dict) and write_target.get("kind") == "LOCALIZED_FIELD") and registry_row is not None:
            if write_target is not None and (
                not isinstance(write_target, dict)
                or write_target.get("kind") != "FIELD_REGISTRY"
            ):
                errors.append(
                    f"registered field cannot be redirected to a category property: {field}"
                )
                continue
            target = {
                "kind": "FIELD_REGISTRY",
                "logical_name": registry_name,
                "section": registry_row.get("section"),
                "runtime_key": None,
            }
            locator_ref = "field.registry_runtime"
        elif not (isinstance(write_target, dict) and write_target.get("kind") == "LOCALIZED_FIELD"):
            if (
                not isinstance(write_target, dict)
                or write_target.get("kind") != "CATEGORY_PROPERTY"
                or not str(write_target.get("approved_property_name") or "").strip()
            ):
                errors.append(
                    f"unknown field is not registered and is not explicitly approved as a category property: {field}"
                )
                continue
            target = {
                "kind": "CATEGORY_PROPERTY_RUNTIME_KEY",
                "logical_name": "category_properties",
                "section": "CATEGORY_PROPERTIES",
                "approved_property_name": str(write_target["approved_property_name"]).strip(),
                "runtime_key": None,
            }
            locator_ref = "field.category_property_runtime"

        operation = {
                "order": order,
                "operation": "WRITE_APPROVED_FIELD_VALUE",
                "target": target,
                "locator": {
                    "adapter_ref": locator_ref,
                    "resolve_from_current_probe": True,
                    "reuse_prior_resolution": False,
                },
                "value": value,
                "value_sha256": value_hash,
                "approval": {
                    "source_id": decision.get("source_id"),
                    "decision_id": decision.get("decision_id"),
                },
                "readback": readback,
            }
        if length_validation is not None:
            operation["length_validation"] = length_validation
        operations.append(operation)
    if errors:
        raise PlanCompileError("; ".join(errors))
    return operations


def _clear_operations(
    field_registry: dict[str, Any],
    registry_fields: dict[str, dict[str, Any]],
    blocked_fields: list[str],
    localized_targets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    targets: list[tuple[str, dict[str, Any], str]] = []
    seen: set[tuple[str, str]] = set()

    for row in field_registry.get("fields") or []:
        if not isinstance(row, dict) or row.get("clear_before_fill") is not True:
            continue
        name = str(row.get("logical_name") or "")
        key = ("FIELD_REGISTRY", name)
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            (
                "field.registry_runtime",
                {
                    "kind": "FIELD_REGISTRY",
                    "logical_name": name,
                    "section": row.get("section"),
                    "runtime_key": None,
                },
                "REGISTRY_CLEAR_BEFORE_FILL",
            )
        )

    errors: list[str] = []
    for target in localized_targets or []:
        identity = str(target.get("identity_sha256") or "")
        key = ("LOCALIZED_FIELD", identity)
        if key in seen:
            continue
        seen.add(key)
        targets.append(("field.localized_runtime", target, "LOCALIZED_CLEAR_BEFORE_FILL"))

    for blocked in sorted(blocked_fields):
        registry_name = FIELD_REGISTRY_ALIASES.get(blocked, blocked)
        if registry_name in registry_fields:
            key = ("FIELD_REGISTRY", registry_name)
            if key in seen:
                continue
            seen.add(key)
            row = registry_fields[registry_name]
            targets.append(
                (
                    "field.registry_runtime",
                    {
                        "kind": "FIELD_REGISTRY",
                        "logical_name": registry_name,
                        "section": row.get("section"),
                        "runtime_key": None,
                    },
                    "BLOCKED_FIELD_MUST_REMAIN_EMPTY",
                )
            )
        elif blocked.startswith(CATEGORY_PROPERTY_PREFIX) and blocked[len(CATEGORY_PROPERTY_PREFIX) :].strip():
            property_name = blocked[len(CATEGORY_PROPERTY_PREFIX) :].strip()
            key = ("CATEGORY_PROPERTY_RUNTIME_KEY", property_name)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                (
                    "field.category_property_runtime",
                    {
                        "kind": "CATEGORY_PROPERTY_RUNTIME_KEY",
                        "logical_name": "category_properties",
                        "section": "CATEGORY_PROPERTIES",
                        "approved_property_name": property_name,
                        "runtime_key": None,
                    },
                    "BLOCKED_FIELD_MUST_REMAIN_EMPTY",
                )
            )
        else:
            errors.append(
                f"unknown blocked field is not registered and is not explicitly namespaced as a category property: {blocked}"
            )

    if errors:
        raise PlanCompileError("; ".join(errors))

    return [
        {
            "order": index,
            "operation": "CLEAR_INHERITED_FIELD",
            "target": target,
            "locator": {
                "adapter_ref": adapter_ref,
                "resolve_from_current_probe": True,
                "reuse_prior_resolution": False,
            },
            "reason": reason,
            "readback": {"required": True, "expected_state": "EMPTY"},
        }
        for index, (adapter_ref, target, reason) in enumerate(targets, 1)
    ]


def _asset_operations(asset_manifest: dict[str, Any], registry_fields: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    assets = sorted(
        asset_manifest.get("assets") or [],
        key=lambda row: (int(row.get("upload_order") or 0), str(row.get("asset_id") or "")),
    )
    observed_orders = [int(row.get("upload_order") or 0) for row in assets]
    if observed_orders != list(range(1, len(assets) + 1)):
        raise PlanCompileError("asset upload_order must be contiguous and start at one")
    for index, asset in enumerate(assets, 1):
        role = str(asset.get("role") or "")
        registry_name = ASSET_REGISTRY_FIELDS.get(role)
        if registry_name is None or registry_name not in registry_fields:
            raise PlanCompileError(f"asset role has no approved registry upload target: {role or '<empty>'}")
        path = Path(str(asset.get("path") or "")).resolve(strict=True)
        observed_hash = sha256_file(path)
        observed_bytes = path.stat().st_size
        expected_hash = _require_sha256(asset.get("sha256"), f"asset {asset.get('asset_id')} hash")
        if observed_hash != expected_hash or observed_bytes != int(asset.get("bytes") or -1):
            raise PlanCompileError(f"asset hash/size drift before plan compilation: {asset.get('asset_id')}")
        operations.append(
            {
                "order": index,
                "operation": "UPLOAD_APPROVED_ASSET",
                "target": {
                    "kind": "ASSET_SLOT",
                    "logical_name": registry_name,
                    "section": registry_fields[registry_name].get("section"),
                    "role": role,
                    "upload_order": int(asset.get("upload_order")),
                },
                "locator": {
                    "adapter_ref": "asset.upload_runtime",
                    "resolve_from_current_probe": True,
                    "reuse_prior_resolution": False,
                },
                "asset": {
                    "asset_id": asset.get("asset_id"),
                    "path": str(path),
                    "bytes": observed_bytes,
                    "sha256": observed_hash,
                    "media_type": asset.get("media_type"),
                },
                "pre_upload_check": "PATH_BYTES_SHA256_EXACT_MATCH",
                "readback": {
                    "required": True,
                    "compare": "UPLOAD_ORDER_AND_CONTENT_SHA256",
                    "expected_sha256": observed_hash,
                },
            }
        )
    return operations


def compile_action_plan(
    *,
    approved_source_manifest_path: Path,
    approved_asset_manifest_path: Path,
    run_spec_path: Path,
    write_approval_path: Path,
    field_registry_path: Path,
    endpoint_policy_path: Path,
    adapter_path: Path = DEFAULT_ADAPTER,
) -> dict[str, Any]:
    paths = {
        "approved_source_manifest": approved_source_manifest_path.resolve(strict=True),
        "approved_asset_manifest": approved_asset_manifest_path.resolve(strict=True),
        "run_spec": run_spec_path.resolve(strict=True),
        "write_approval": write_approval_path.resolve(strict=True),
        "field_registry": field_registry_path.resolve(strict=True),
        "endpoint_policy": endpoint_policy_path.resolve(strict=True),
        "adapter": adapter_path.resolve(strict=True),
    }
    source_manifest = _load_object(paths["approved_source_manifest"], "approved source manifest")
    asset_manifest = _load_object(paths["approved_asset_manifest"], "approved asset manifest")
    run_spec = _load_object(paths["run_spec"], "run spec")
    write_approval = _load_object(paths["write_approval"], "draft write approval")
    field_registry = _load_object(paths["field_registry"], "field registry")
    endpoint_policy = _load_object(paths["endpoint_policy"], "endpoint policy")
    adapter = _load_object(paths["adapter"], "in-app browser adapter")

    preflight = validate_approved_manifest(
        source_manifest,
        asset_manifest,
        manifest_path=paths["approved_source_manifest"],
        asset_manifest_path=paths["approved_asset_manifest"],
    )
    if preflight.get("status") != "PASS":
        raise PlanCompileError("approved manifest preflight blocked: " + "; ".join(preflight.get("errors") or []))

    hashes = {name: sha256_file(path) for name, path in paths.items()}
    _validate_run_and_bindings(
        source_manifest,
        asset_manifest,
        run_spec,
        field_registry,
        endpoint_policy,
        registry_sha256=hashes["field_registry"],
        policy_sha256=hashes["endpoint_policy"],
    )
    _validate_write_approval(
        write_approval,
        source_manifest,
        asset_manifest,
        run_spec,
        input_hashes=hashes,
    )
    decisions = source_manifest.get("approved_field_values") or {}
    has_localized_fields = any(
        isinstance(row, dict)
        and isinstance(row.get("write_target"), dict)
        and row["write_target"].get("kind") == "LOCALIZED_FIELD"
        for row in decisions.values()
    ) if isinstance(decisions, dict) else False
    _validate_adapter(adapter, require_localized_fields=has_localized_fields)

    registry_fields = {
        str(row.get("logical_name")): row
        for row in field_registry.get("fields") or []
        if isinstance(row, dict) and row.get("logical_name")
    }
    field_policy = source_manifest.get("draft_field_policy") or {}
    blocked_fields = sorted({str(value) for value in field_policy.get("blocked_fields") or [] if str(value)})
    blocked_expanded = _expanded_blocked_fields(blocked_fields)
    write_operations = _approved_value_operations(source_manifest, registry_fields, blocked_expanded)
    localized_targets = [
        operation["target"]
        for operation in write_operations
        if operation["target"].get("kind") == "LOCALIZED_FIELD"
    ]
    clear_operations = _clear_operations(
        field_registry,
        registry_fields,
        blocked_fields,
        localized_targets=localized_targets,
    )
    asset_operations = _asset_operations(asset_manifest, registry_fields)

    blocked_category_properties = {
        field[len(CATEGORY_PROPERTY_PREFIX) :]
        for field in blocked_fields
        if field.startswith(CATEGORY_PROPERTY_PREFIX)
    }
    if any(
        operation["target"].get("logical_name") in blocked_expanded
        or (
            operation["target"].get("kind") == "CATEGORY_PROPERTY_RUNTIME_KEY"
            and operation["target"].get("approved_property_name") in blocked_category_properties
        )
        for operation in write_operations
    ):
        raise PlanCompileError("internal invariant failed: blocked field entered write operations")

    source_profile = str(preflight.get("draft_profile") or "FULL_DRAFT")
    partial = source_profile == "PARTIAL_DRAFT_FILL_APPROVED_ONLY"
    three_party = {
        "approved_source_manifest_sha256": hashes["approved_source_manifest"],
        "approved_asset_manifest_sha256": hashes["approved_asset_manifest"],
        "run_spec_sha256": hashes["run_spec"],
    }
    three_party["binding_sha256"] = _binding_hash(three_party)
    write_approval_binding = {
        "sha256": hashes["write_approval"],
        "approval_id": write_approval.get("approval_id"),
        "decision_id": write_approval.get("decision_id"),
        "approved_at": write_approval.get("approved_at"),
        "expires_at": write_approval.get("expires_at"),
        "target_model": write_approval.get("target_model"),
        "run_id": write_approval.get("run_id"),
        "template_product_id": write_approval.get("template_product_id"),
    }
    execution_binding = {
        "three_party_binding_sha256": three_party["binding_sha256"],
        "write_approval_sha256": hashes["write_approval"],
    }
    write_approval_binding["execution_binding_sha256"] = _binding_hash(execution_binding)

    validation_operations: list[dict[str, Any]] = []
    for index, operation in enumerate(write_operations, 1):
        validation_operation = {
                "order": index,
                "operation": "VERIFY_FIELD_READBACK",
                "target": operation["target"],
                "expected_sha256": operation["value_sha256"],
                "failure_action": "STOP_PRESERVE_EVIDENCE",
            }
        if operation["target"].get("kind") == "LOCALIZED_FIELD":
            validation_operation["localized_identity_sha256"] = operation["target"]["identity_sha256"]
            validation_operation["max_length_rule_hash"] = operation["target"]["max_length_rule_hash"]
        validation_operations.append(validation_operation)
    offset = len(validation_operations)
    for index, operation in enumerate(asset_operations, 1):
        validation_operations.append(
            {
                "order": offset + index,
                "operation": "VERIFY_ASSET_READBACK",
                "target": operation["target"],
                "expected_sha256": operation["asset"]["sha256"],
                "failure_action": "STOP_PRESERVE_EVIDENCE",
            }
        )

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": f"{str(source_manifest.get('target_model') or '').lower()}-{write_approval_binding['execution_binding_sha256'][:16]}",
        "generated_at": utc_now(),
        "compiler": {
            "name": "compile_browser_action_plan.py",
            "version": COMPILER_VERSION,
            "sha256": sha256_file(Path(__file__).resolve()),
            "browser_or_network_executed": False,
            "output_semantics": "PLAN_ONLY_NO_SIDE_EFFECTS",
        },
        "target": {
            "model": source_manifest.get("target_model"),
            "run_id": source_manifest.get("run_id"),
            "template_product_id": str(run_spec.get("template_product_id")),
            "mode": "SAVE_DRAFT_ONLY",
            "maximum_draft_save_count": 1,
        },
        "source_profile": source_profile,
        "quality": {
            "terminal_quality": "REVIEW_REQUIRED" if partial else "FINAL_REVIEW_REQUIRED",
            "publish_ready": False,
            "incomplete_draft_allowed": bool(partial),
            "final_write_count_limit": 0,
        },
        "bindings": {
            "three_party": three_party,
            "write_approval": write_approval_binding,
            "field_registry": {
                "sha256": hashes["field_registry"],
                "registry_id": field_registry.get("registry_id"),
                "registry_version": field_registry.get("registry_version"),
            },
            "endpoint_policy": {
                "sha256": hashes["endpoint_policy"],
                "schema_version": endpoint_policy.get("schema_version"),
                "default_decision": "DENY",
            },
            "adapter": {
                "sha256": hashes["adapter"],
                "adapter_id": adapter.get("adapter_id"),
                "adapter_version": adapter.get("adapter_version"),
            },
        },
        "runtime_requirements": {
            "current_probe_required": True,
            "current_probe_must_be_newer_than_plan": True,
            "reparse_after_every_page_change": True,
            "discard_prior_locators": True,
            "resolve_dynamic_category_keys_from_current_page_only": True,
            "fail_on_non_unique_locator": True,
            "fail_on_captcha_or_login": True,
            "fail_on_unknown_write_request": True,
            "write_approval_must_be_unexpired_at_execution": True,
            "localized_fields_require_live_language_label": True,
            "localized_fields_require_direction_match": True,
            "localized_max_length_rule_must_match_current_page": True,
            "localized_locator_requires_adapter_capability": True,
        },
        "invariants": {
            "clear_all_inherited_fields_before_any_write": True,
            "write_values_only_from_approved_field_values": True,
            "blocked_fields_never_written": True,
            "asset_path_bytes_sha256_bound": True,
            "asset_upload_order_bound": True,
            "save_draft_visible_ui_only": True,
            "save_draft_requires_valid_write_approval": True,
            "direct_endpoint_write_allowed": False,
            "submit_or_final_write_decision": "HARD_DENY",
            "final_write_permanently_disabled": True,
            "localized_field_identity_hash_bound": True,
            "localized_field_values_are_per_locale": True,
        },
        "blocked_fields": sorted(blocked_expanded),
        "phases": [
            {
                "order": 1,
                "name": "CLEAR_INHERITED_FIELDS",
                "must_complete_before_next_phase": True,
                "operations": clear_operations,
            },
            {
                "order": 2,
                "name": "WRITE_APPROVED_FIELD_VALUES",
                "must_complete_before_next_phase": True,
                "operations": write_operations,
            },
            {
                "order": 3,
                "name": "UPLOAD_APPROVED_ASSETS",
                "must_complete_before_next_phase": True,
                "operations": asset_operations,
            },
            {
                "order": 4,
                "name": "VERIFY_READBACK",
                "must_complete_before_next_phase": True,
                "operations": validation_operations,
            },
            {
                "order": 5,
                "name": "SAVE_DRAFT_VISIBLE_UI",
                "must_complete_before_next_phase": True,
                "operations": [
                    {
                        "order": 1,
                        "operation": "SAVE_DRAFT",
                        "locator": {
                            "adapter_ref": "action.save_draft_runtime",
                            "resolve_from_current_probe": True,
                            "reuse_prior_resolution": False,
                        },
                        "visible_ui_required": True,
                        "direct_endpoint_call_allowed": False,
                        "maximum_execution_count": 1,
                        "requires_all_prior_phase_readbacks": True,
                        "approval": {
                            "approval_id": write_approval.get("approval_id"),
                            "decision_id": write_approval.get("decision_id"),
                            "write_approval_sha256": hashes["write_approval"],
                            "expires_at": write_approval.get("expires_at"),
                        },
                    }
                ],
            },
            {
                "order": 6,
                "name": "STOP_BEFORE_FINAL_WRITE",
                "must_complete_before_next_phase": True,
                "operations": [
                    {
                        "order": 1,
                        "operation": "STOP_AND_PRESERVE_REVIEW_STATE",
                        "terminal_quality": "REVIEW_REQUIRED" if partial else "FINAL_REVIEW_REQUIRED",
                        "publish_ready": False,
                    }
                ],
            },
        ],
        "forbidden_actions": [
            {
                "logical_name": "submit_product",
                "classification": "FINAL_WRITE",
                "decision": "HARD_DENY",
                "permanent": True,
                "visible_ui_click_allowed": False,
                "direct_endpoint_call_allowed": False,
                "adapter_ref": "guard.final_submit",
            },
            {
                "logical_name": "all_final_write_endpoints",
                "classification": "FINAL_WRITE",
                "decision": "HARD_DENY",
                "permanent": True,
                "visible_ui_click_allowed": False,
                "direct_endpoint_call_allowed": False,
                "endpoint_policy_sha256": hashes["endpoint_policy"],
            },
        ],
    }
    plan["plan_body_sha256"] = sha256_bytes(canonical_json(plan).encode("utf-8"))
    _validate_output_schema(plan)
    return plan


def write_json_atomic_new(path: Path, value: dict[str, Any]) -> str:
    destination = path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PlanCompileError(f"refusing to overwrite existing output: {destination}")
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
    except FileExistsError as exc:
        raise PlanCompileError(f"refusing to overwrite existing output: {destination}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return sha256_file(destination)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile a hash-bound in-app-browser action plan without executing browser or network actions."
    )
    parser.add_argument("--approved-source-manifest", required=True, type=Path)
    parser.add_argument("--approved-asset-manifest", required=True, type=Path)
    parser.add_argument("--run-spec", required=True, type=Path)
    parser.add_argument("--write-approval", required=True, type=Path)
    parser.add_argument("--field-registry", required=True, type=Path)
    parser.add_argument("--endpoint-policy", required=True, type=Path)
    parser.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = compile_action_plan(
            approved_source_manifest_path=args.approved_source_manifest,
            approved_asset_manifest_path=args.approved_asset_manifest,
            run_spec_path=args.run_spec,
            write_approval_path=args.write_approval,
            field_registry_path=args.field_registry,
            endpoint_policy_path=args.endpoint_policy,
            adapter_path=args.adapter,
        )
        output_sha256 = write_json_atomic_new(args.output, plan)
    except (PlanCompileError, FileNotFoundError, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "COMPILED_PLAN_ONLY",
                "output": str(args.output.resolve(strict=True)),
                "output_sha256": output_sha256,
                "plan_body_sha256": plan["plan_body_sha256"],
                "browser_or_network_executed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
