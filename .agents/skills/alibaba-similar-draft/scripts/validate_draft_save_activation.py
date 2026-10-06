from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator, FormatChecker

import draft_core as draft_core_module
from draft_core import GuardError, classify_endpoint
from state_store import (
    checkpoint_hash,
    checkpoint_mutation_guard,
    transition_checkpoint_under_guard,
)
from validate_next_product_checklist import (
    IMPLEMENTATION_READY as CHECKLIST_IMPLEMENTATION_READY,
    validate as validate_next_product_checklist,
)
from validate_pre_save_readback import verify_pre_save


SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
ACTIVATION_SCHEMA = SCHEMA_DIR / "draft-save-activation-request.schema.json"
ACTIVATION_RECEIPT_SCHEMA = SCHEMA_DIR / "draft-save-activation-receipt.schema.json"
ATTEMPT_CLAIM_RECEIPT_SCHEMA = SCHEMA_DIR / "draft-save-attempt-claim-receipt.schema.json"
BODY_SCHEMA = SCHEMA_DIR / "draft-save-body-semantics.schema.json"
EVENT_SCHEMA = SCHEMA_DIR / "visible-ui-draft-save-event.schema.json"
ACTION_PLAN_SCHEMA = SCHEMA_DIR / "browser-action-plan.schema.json"
PROBE_SOURCE_SCHEMA = SCHEMA_DIR / "probe-receipt.schema.json"
PROBE_VERIFICATION_SCHEMA = SCHEMA_DIR / "probe-verification-receipt.schema.json"
PROBE_READ_ACTIVATION_SCHEMA = SCHEMA_DIR / "probe-read-activation-receipt.schema.json"
RUN_SPEC_SCHEMA = SCHEMA_DIR / "run-spec.schema.json"
PRE_SAVE_READBACK_SCHEMA = SCHEMA_DIR / "pre-save-readback.schema.json"
PRE_SAVE_VERIFICATION_SCHEMA = SCHEMA_DIR / "pre-save-verification-receipt.schema.json"
CHECKPOINT_SCHEMA = SCHEMA_DIR / "checkpoint.schema.json"
CHECKLIST_SCHEMA = SCHEMA_DIR / "next-product-checklist.schema.json"
CHECKLIST_RECEIPT_SCHEMA = SCHEMA_DIR / "next-product-checklist-verification-receipt.schema.json"

MAX_PROBE_AGE = timedelta(minutes=15)
MAX_ACTIVATION_LIFETIME = timedelta(minutes=2)
MAX_EVENT_AGE = timedelta(seconds=30)
MAX_PAGE_EVIDENCE_AGE = timedelta(seconds=30)
FUTURE_CLOCK_SKEW = timedelta(seconds=5)


class ActivationError(RuntimeError):
    """A fail-closed draft-save activation validation error."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def object_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ActivationError(f"{label} must be a JSON object: {path}")
    return value


def read_bound_object(path: Path, label: str) -> tuple[dict[str, Any], str]:
    """Read and hash the exact same bytes to avoid a hash/read TOCTOU gap."""

    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ActivationError(f"{label} must be a JSON object: {path}")
    return value, sha256_bytes(payload)


def validate_schema(document: dict[str, Any], schema_path: Path, label: str) -> None:
    schema = read_object(schema_path, f"{label} schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda item: tuple(str(part) for part in item.path))
    if not errors:
        return
    details: list[str] = []
    for error in errors[:8]:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        details.append(f"{location}: {error.message}")
    if len(errors) > 8:
        details.append(f"... {len(errors) - 8} more")
    raise ActivationError(f"{label} schema validation failed: {'; '.join(details)}")


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ActivationError(f"{label} must be an RFC3339 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ActivationError(f"{label} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ActivationError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ActivationError(message)


def require_hash(actual: Any, expected: str, label: str) -> None:
    require(isinstance(actual, str) and actual.lower() == expected.lower(), f"{label} hash mismatch")


def recompute_action_plan_body_hash(action_plan: dict[str, Any]) -> str:
    body = copy.deepcopy(action_plan)
    declared = str(body.pop("plan_body_sha256", "") or "").lower()
    computed = object_sha256(body)
    require(declared == computed, "action plan plan_body_sha256 does not hash its canonical body")
    return computed


def recompute_body_semantics(body_semantics: dict[str, Any]) -> str:
    business = body_semantics["business_field_digests"]
    business_keys = set(business)
    ephemeral = body_semantics["ephemeral_credential_field_names"]
    volatile = body_semantics["volatile_nonsemantic_field_names"]
    ephemeral_keys = set(ephemeral)
    volatile_keys = set(volatile)
    require(len(ephemeral_keys) == len(ephemeral), "ephemeral credential field names must be unique")
    require(len(volatile_keys) == len(volatile), "volatile nonsemantic field names must be unique")
    require(not (business_keys & ephemeral_keys), "business and credential body field sets overlap")
    require(not (business_keys & volatile_keys), "business and volatile body field sets overlap")
    require(not (ephemeral_keys & volatile_keys), "credential and volatile body field sets overlap")

    expected_names = sorted(business_keys | ephemeral_keys | volatile_keys)
    require(body_semantics["body_key_names"] == expected_names, "body_key_names must be the sorted exact union of all redacted field classes")
    require_hash(
        body_semantics["body_key_set_sha256"],
        object_sha256(expected_names),
        "body key set",
    )

    projection = copy.deepcopy(body_semantics)
    declared = str(projection.pop("semantic_projection_sha256", "") or "").lower()
    computed = object_sha256(projection)
    require(declared == computed, "redacted body semantic_projection_sha256 does not hash its canonical projection")
    return computed


def _policy_rule(policy: dict[str, Any], rule_id: str) -> dict[str, Any]:
    matches = [rule for rule in policy.get("rules", []) if isinstance(rule, dict) and rule.get("id") == rule_id]
    require(len(matches) == 1, f"endpoint policy must contain exactly one {rule_id} rule")
    return matches[0]


def validate_endpoint_policy(policy: dict[str, Any]) -> None:
    require(policy.get("schema_version") == "alibaba.endpoint-policy.v1", "wrong endpoint policy schema_version")
    require(policy.get("default_decision") == "DENY", "endpoint policy default decision must remain DENY")
    require(policy.get("final_write_enabled") is False, "endpoint policy final_write_enabled must remain false")

    draft_rule = _policy_rule(policy, "ui-save-draft-unverified-seed")
    require(draft_rule.get("classification") == "REVERSIBLE_DRAFT_WRITE", "draft-save seed has wrong classification")
    require(draft_rule.get("evidence_state") == "UNVERIFIED_DENY_ONLY", "draft-save route was globally activated")
    require(draft_rule.get("methods") == ["UNKNOWN"], "draft-save route must retain UNKNOWN global method evidence")
    require(draft_rule.get("ui_only") is True, "draft-save seed must remain UI-only")
    require(draft_rule.get("direct_call_allowed") is False, "draft-save seed must deny direct calls")

    rules = [rule for rule in policy.get("rules", []) if isinstance(rule, dict)]
    for rule in rules:
        require(rule.get("decision") != "ALLOW", f"global endpoint policy rule {rule.get('id')} cannot ALLOW")
        require(rule.get("evidence_state") not in {"LIVE_VERIFIED_ACTIVE", "ALLOW", "ALLOWED"}, f"global endpoint policy rule {rule.get('id')} is active")

    final_ids = {
        rule.get("id")
        for rule in rules
        if rule.get("classification") == "FINAL_WRITE" and rule.get("evidence_state") == "HARD_DENY"
    }
    require("final-ui-submit-hard-deny" in final_ids, "network policy is missing the final UI submit hard deny")
    require("final-publish-route-mutation-hard-deny" in final_ids, "network policy is missing the final publish mutation hard deny")


def validate_action_plan_guards(action_plan: dict[str, Any], now: datetime) -> None:
    target = action_plan["target"]
    require(target.get("mode") == "SAVE_DRAFT_ONLY", "action plan is not SAVE_DRAFT_ONLY")
    require(target.get("maximum_draft_save_count") == 1, "action plan does not cap draft saves at one")
    require(action_plan.get("quality", {}).get("final_write_count_limit") == 0, "action plan final write limit is not zero")

    invariants = action_plan["invariants"]
    require(invariants.get("save_draft_visible_ui_only") is True, "action plan does not require visible UI draft save")
    require(invariants.get("direct_endpoint_write_allowed") is False, "action plan permits direct endpoint write")
    require(invariants.get("submit_or_final_write_decision") == "HARD_DENY", "action plan does not hard-deny final submit")
    require(invariants.get("final_write_permanently_disabled") is True, "action plan does not permanently disable final writes")

    save_phases = [phase for phase in action_plan["phases"] if phase.get("name") == "SAVE_DRAFT_VISIBLE_UI"]
    require(len(save_phases) == 1, "action plan must contain exactly one visible-UI save phase")
    operations = save_phases[0].get("operations") or []
    require(len(operations) == 1, "action plan must contain exactly one draft-save operation")
    operation = operations[0]
    require(operation.get("operation") == "SAVE_DRAFT", "action plan save operation has wrong type")
    require(operation.get("visible_ui_required") is True, "draft-save operation is not visible-UI-only")
    require(operation.get("direct_endpoint_call_allowed") is False, "draft-save operation permits direct replay")
    require(operation.get("maximum_execution_count") == 1, "draft-save operation is not one-shot")

    write_approval = action_plan.get("bindings", {}).get("write_approval", {})
    require(parse_timestamp(write_approval.get("expires_at"), "action-plan write approval expires_at") >= now, "independent draft write approval expired")

    forbidden = {row.get("logical_name"): row for row in action_plan.get("forbidden_actions", []) if isinstance(row, dict)}
    for name in ("submit_product", "all_final_write_endpoints"):
        row = forbidden.get(name) or {}
        require(row.get("classification") == "FINAL_WRITE", f"missing {name} FINAL_WRITE guard")
        require(row.get("decision") == "HARD_DENY", f"{name} is not HARD_DENY")
        require(row.get("permanent") is True, f"{name} deny is not permanent")
        require(row.get("visible_ui_click_allowed") is False, f"{name} permits visible UI click")
        require(row.get("direct_endpoint_call_allowed") is False, f"{name} permits direct endpoint calls")


def validate_checkpoint_checklist_evidence(checkpoint: dict[str, Any]) -> None:
    """Re-run the typed VALIDATED evidence instead of trusting checkpoint text."""

    entries = [row for row in checkpoint.get("evidence") or [] if isinstance(row, dict) and row.get("state") == "VALIDATED"]
    require(len(entries) == 1, "checkpoint must contain exactly one typed VALIDATED evidence bundle")
    entry = entries[0]
    expected_roles = {"PRE_SAVE_CHECKLIST_SOURCE", "PRE_SAVE_CHECKLIST_RECEIPT"}
    require(set(entry.get("roles") or []) == expected_roles, "checkpoint VALIDATED evidence roles are incomplete")
    artifacts = {
        row.get("role"): row
        for row in entry.get("artifacts") or []
        if isinstance(row, dict) and row.get("role") in expected_roles
    }
    require(set(artifacts) == expected_roles, "checkpoint VALIDATED evidence artifacts are incomplete")
    documents: dict[str, dict[str, Any]] = {}
    for role in sorted(expected_roles):
        descriptor = artifacts[role]
        artifact_path = Path(str(descriptor.get("path") or ""))
        require(artifact_path.is_absolute(), f"checkpoint {role} path must be absolute")
        document, digest = read_bound_object(artifact_path, f"checkpoint {role}")
        require_hash(descriptor.get("sha256"), digest, f"checkpoint {role}")
        require(descriptor.get("bytes") == artifact_path.stat().st_size, f"checkpoint {role} byte-size mismatch")
        documents[role] = document
    source = documents["PRE_SAVE_CHECKLIST_SOURCE"]
    claimed = documents["PRE_SAVE_CHECKLIST_RECEIPT"]
    validate_schema(source, CHECKLIST_SCHEMA, "checkpoint pre-save checklist source")
    validate_schema(claimed, CHECKLIST_RECEIPT_SCHEMA, "checkpoint pre-save checklist receipt")
    for key in ("run_id", "target_model", "template_product_id"):
        require(source.get(key) == checkpoint.get(key), f"checkpoint checklist source {key} mismatch")
    checklist_validator_path = Path(__file__).resolve().with_name("validate_next_product_checklist.py")
    require_hash(claimed.get("validator_sha256"), sha256_file(checklist_validator_path), "checkpoint checklist validator")
    validated_at = parse_timestamp(claimed.get("validated_at_utc"), "checkpoint checklist validated_at_utc")
    recomputed = validate_next_product_checklist(source, "pre_save", now=validated_at)
    require(canonical_json(recomputed) == canonical_json(claimed), "checkpoint checklist receipt does not match validator recomputation")
    require(
        recomputed.get("status") == "PASS" and recomputed.get("implementation_ready") is True,
        "checkpoint checklist validator is not implementation-ready",
    )


def validate_activation(
    *,
    activation_path: Path,
    action_plan_path: Path,
    probe_verification_path: Path,
    probe_source_path: Path,
    endpoint_policy_path: Path,
    probe_read_activation_path: Path,
    body_semantics_path: Path,
    visible_ui_event_path: Path,
    run_spec_path: Path,
    pre_save_readback_path: Path,
    pre_save_verification_path: Path,
    checkpoint_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or utc_now()).astimezone(timezone.utc)
    paths = {
        "activation": activation_path.resolve(strict=True),
        "action_plan": action_plan_path.resolve(strict=True),
        "probe_verification": probe_verification_path.resolve(strict=True),
        "probe_source": probe_source_path.resolve(strict=True),
        "endpoint_policy": endpoint_policy_path.resolve(strict=True),
        "probe_read_activation": probe_read_activation_path.resolve(strict=True),
        "body_semantics": body_semantics_path.resolve(strict=True),
        "visible_ui_event": visible_ui_event_path.resolve(strict=True),
        "run_spec": run_spec_path.resolve(strict=True),
        "pre_save_readback": pre_save_readback_path.resolve(strict=True),
        "pre_save_verification": pre_save_verification_path.resolve(strict=True),
        "checkpoint": checkpoint_path.resolve(strict=True),
    }
    documents: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    labels = {
        "activation": "activation request",
        "action_plan": "browser action plan",
        "probe_verification": "verified probe receipt",
        "probe_source": "probe source receipt",
        "endpoint_policy": "endpoint policy",
        "probe_read_activation": "current-session probe READ activation",
        "body_semantics": "redacted body semantics",
        "visible_ui_event": "visible UI event",
        "run_spec": "run spec",
        "pre_save_readback": "pre-save readback",
        "pre_save_verification": "pre-save verification",
        "checkpoint": "validated checkpoint",
    }
    for name, path in paths.items():
        documents[name], hashes[name] = read_bound_object(path, labels[name])
    activation = documents["activation"]
    action_plan = documents["action_plan"]
    probe_verification = documents["probe_verification"]
    probe_source = documents["probe_source"]
    endpoint_policy = documents["endpoint_policy"]
    read_activation = documents["probe_read_activation"]
    body_semantics = documents["body_semantics"]
    event = documents["visible_ui_event"]
    run_spec = documents["run_spec"]
    pre_save_readback = documents["pre_save_readback"]
    pre_save_verification = documents["pre_save_verification"]
    checkpoint = documents["checkpoint"]

    validate_schema(activation, ACTIVATION_SCHEMA, "activation request")
    validate_schema(action_plan, ACTION_PLAN_SCHEMA, "browser action plan")
    validate_schema(probe_verification, PROBE_VERIFICATION_SCHEMA, "verified probe receipt")
    validate_schema(probe_source, PROBE_SOURCE_SCHEMA, "probe source receipt")
    validate_schema(read_activation, PROBE_READ_ACTIVATION_SCHEMA, "current-session probe READ activation")
    validate_schema(body_semantics, BODY_SCHEMA, "redacted body semantics")
    validate_schema(event, EVENT_SCHEMA, "visible UI event")
    validate_schema(run_spec, RUN_SPEC_SCHEMA, "run spec")
    validate_schema(pre_save_readback, PRE_SAVE_READBACK_SCHEMA, "pre-save readback")
    validate_schema(pre_save_verification, PRE_SAVE_VERIFICATION_SCHEMA, "pre-save verification")
    validate_schema(checkpoint, CHECKPOINT_SCHEMA, "validated checkpoint")

    issued_at = parse_timestamp(activation["issued_at"], "activation issued_at")
    expires_at = parse_timestamp(activation["expires_at"], "activation expires_at")
    require(issued_at <= now + FUTURE_CLOCK_SKEW, "activation issued_at is in the future")
    require(expires_at >= now, "activation request is expired")
    require(expires_at > issued_at, "activation expires_at must be after issued_at")
    require(expires_at - issued_at <= MAX_ACTIVATION_LIFETIME, "activation lifetime exceeds two minutes")

    plan_body_sha256 = recompute_action_plan_body_hash(action_plan)
    body_semantic_sha256 = recompute_body_semantics(body_semantics)
    validate_endpoint_policy(endpoint_policy)
    validate_action_plan_guards(action_plan, now)

    bindings = activation["bindings"]
    require_hash(bindings["action_plan_sha256"], hashes["action_plan"], "activation action plan")
    require_hash(bindings["action_plan_body_sha256"], plan_body_sha256, "activation action plan body")
    require_hash(bindings["verified_probe_receipt_sha256"], hashes["probe_verification"], "activation verified probe receipt")
    require_hash(bindings["probe_source_sha256"], hashes["probe_source"], "activation probe source")
    require_hash(bindings["endpoint_policy_sha256"], hashes["endpoint_policy"], "activation endpoint policy")
    require_hash(bindings["probe_read_activation_sha256"], hashes["probe_read_activation"], "activation probe READ activation")
    require_hash(bindings["redacted_body_semantics_file_sha256"], hashes["body_semantics"], "activation body semantics file")
    require_hash(bindings["redacted_body_semantic_sha256"], body_semantic_sha256, "activation redacted body semantic")
    require_hash(bindings["visible_ui_event_sha256"], hashes["visible_ui_event"], "activation visible UI event")
    require_hash(bindings["run_spec_sha256"], hashes["run_spec"], "activation run spec")
    require_hash(bindings["pre_save_readback_sha256"], hashes["pre_save_readback"], "activation pre-save readback")
    require_hash(bindings["pre_save_verification_sha256"], hashes["pre_save_verification"], "activation pre-save verification")
    require_hash(bindings["checkpoint_sha256"], hashes["checkpoint"], "activation checkpoint")
    require_hash(activation["endpoint_request"]["redacted_body_semantic_sha256"], body_semantic_sha256, "endpoint redacted body semantic")

    target = action_plan["target"]
    scope = activation["scope"]
    require(scope["run_id"] == target["run_id"], "activation run_id does not match the action plan")
    require(scope["target_model"] == target["model"], "activation target model does not match the action plan")
    require(scope["template_product_id"] == target["template_product_id"], "activation template does not match the action plan")

    # Re-run the pre-save validator from its source artifacts at the receipt's
    # own timestamp.  A hash-bound, hand-written PASS receipt is not authority.
    pre_save_generated_at = parse_timestamp(
        pre_save_verification.get("generated_at"), "pre-save verification generated_at"
    )
    recomputed_pre_save = verify_pre_save(
        action_plan_path=paths["action_plan"],
        run_spec_path=paths["run_spec"],
        probe_source_path=paths["probe_source"],
        probe_verification_path=paths["probe_verification"],
        pre_save_readback_path=paths["pre_save_readback"],
        now=pre_save_generated_at,
    )
    require(
        canonical_json(recomputed_pre_save) == canonical_json(pre_save_verification),
        "pre-save verification does not match validator recomputation",
    )
    require(
        pre_save_verification.get("status") == "PASS_PRE_SAVE_NO_WRITE"
        and pre_save_verification.get("errors") == []
        and pre_save_verification.get("draft_save_count") == 0
        and pre_save_verification.get("final_write_count") == 0,
        "pre-save verification is not a clean zero-write PASS",
    )

    require(probe_source["run_id"] == scope["run_id"], "probe source run_id mismatch")
    require(probe_source["target_model"] == scope["target_model"], "probe source target model mismatch")
    require(probe_source["template_product_id"] == scope["template_product_id"], "probe source template mismatch")
    require(probe_source["result"] == "READ_ONLY_VERIFIED", "probe source is not READ_ONLY_VERIFIED")
    require(probe_source["errors"] == [], "probe source contains errors")
    require(probe_source["network"]["write_requests_allowed"] == 0, "probe source allowed a write")
    require(probe_source["network"]["unknown_write_count"] == 0, "probe source observed an unknown write")
    require(probe_source["network"]["final_write_count"] == 0, "probe source observed a final write")
    require(probe_source["final_write_count"] == 0 and probe_source["submit_triggered"] is False, "probe source final-write guard failed")
    require_hash(probe_verification["input_probe_sha256"], hashes["probe_source"], "verified receipt input probe")
    require(probe_verification["status"] == "PASS_ZERO_WRITE", "verified probe receipt is not PASS_ZERO_WRITE")
    require(probe_verification["errors"] == [], "verified probe receipt contains errors")
    require(probe_verification["zero_write_confirmed"] is True, "verified probe receipt did not confirm zero write")
    require(
        probe_verification["execution_authorized"] is True,
        "verified probe receipt does not authorize controlled execution",
    )
    require(
        probe_verification["production_execution_authorized"] is True,
        "verified probe receipt does not authorize production execution",
    )
    require(
        probe_verification["adapter_production_execution_ready"] is True,
        "verified probe receipt adapter production readiness is not certified",
    )
    require(
        probe_verification["production_authorization_errors"] == [],
        "verified probe receipt contains production authorization errors",
    )
    require(
        probe_verification["authorization_errors"] == [],
        "verified probe receipt contains execution authorization errors",
    )
    classifications = probe_verification["request_classifications"]
    require(bool(classifications), "verified probe receipt has no request classifications")
    read_requests = probe_source["network"]["read_requests"]
    read_activation_body = copy.deepcopy(read_activation)
    declared_read_activation_hash = str(read_activation_body.pop("body_sha256", "") or "").lower()
    require(
        declared_read_activation_hash == object_sha256(read_activation_body),
        "probe READ activation body_sha256 mismatch",
    )
    require(read_activation["status"] == "PASS_CURRENT_SESSION_EXACT_READ_ACTIVATION", "probe READ activation did not pass")
    require(read_activation["errors"] == [], "probe READ activation contains errors")
    require(read_activation["global_policy_unchanged"] is True, "probe READ activation does not preserve global policy")
    require(read_activation["draft_write_enabled"] is False and read_activation["final_write_enabled"] is False, "probe READ activation enables a write")
    read_activation_bindings = read_activation["bindings"]
    require_hash(read_activation_bindings["global_endpoint_policy_sha256"], hashes["endpoint_policy"], "probe READ activation global policy")
    require_hash(read_activation_bindings["action_plan_sha256"], hashes["action_plan"], "probe READ activation action plan")
    require_hash(read_activation_bindings["probe_source_sha256"], hashes["probe_source"], "probe READ activation source")
    require_hash(read_activation_bindings["probe_verification_sha256"], hashes["probe_verification"], "probe READ activation verification")
    require(
        read_activation["producer"]["validator_sha256"] == sha256_file(Path(__file__).resolve().with_name("draft_core.py")),
        "probe READ activation producer validator hash mismatch",
    )
    read_scope = read_activation["scope"]
    require(read_scope["run_id"] == scope["run_id"], "probe READ activation run_id mismatch")
    require(read_scope["target_model"] == scope["target_model"], "probe READ activation target mismatch")
    require(read_scope["template_product_id"] == scope["template_product_id"], "probe READ activation template mismatch")
    require(read_scope["browser_session_sha256"] == scope["browser_session_sha256"], "probe READ activation browser session mismatch")
    require(read_scope["page_fingerprint_sha256"] == probe_source["page"]["fingerprint_sha256"], "probe READ activation page fingerprint mismatch")
    read_activation_generated = parse_timestamp(read_activation["generated_at"], "probe READ activation generated_at")
    read_activation_expires = parse_timestamp(read_activation["expires_at"], "probe READ activation expires_at")
    require(read_activation_generated <= issued_at + FUTURE_CLOCK_SKEW, "activation predates probe READ activation")
    require(read_activation_expires >= now, "probe READ activation is expired")
    require(read_activation_expires - read_activation_generated <= MAX_PROBE_AGE, "probe READ activation lifetime exceeds fifteen minutes")

    activation_rows = read_activation["request_activations"]
    require(len(activation_rows) == len(read_requests), "probe READ activation count does not match raw requests")
    effective_rules: list[dict[str, Any]] = []
    for index, request in enumerate(read_requests):
        row = activation_rows[index]
        require(row["method"] == request["method"] and row["url"] == request["url"], f"probe READ activation {index} does not bind exact method/url")
        require(row["source_request_sha256"] == object_sha256(request), f"probe READ activation {index} request hash mismatch")
        parsed = urlsplit(request["url"])
        target_path = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        effective_rules.append({
            "id": f"session-read-{index}",
            "classification": "READ",
            "methods": [request["method"]],
            "method_evidence": "CURRENT_SESSION_VERIFIED",
            "evidence_state": "LIVE_VERIFIED_ACTIVE",
            "url_regex": f"^{re.escape(target_path)}$",
            "query_allowlist": sorted(query),
            "query_values": {key: sorted(set(values)) for key, values in sorted(query.items())},
        })
    effective_policy = copy.deepcopy(endpoint_policy)
    effective_policy["rules"] = effective_rules + list(effective_policy.get("rules") or [])
    require(
        len(classifications) == len(read_requests),
        "verified probe request-classification count does not match raw probe requests",
    )
    for index, request in enumerate(read_requests):
        recomputed = classify_endpoint(
            effective_policy,
            str(request.get("url") or ""),
            str(request.get("method") or ""),
        )
        stored = classifications[index]
        require(
            isinstance(stored, dict) and canonical_json(stored) == canonical_json(recomputed),
            f"verified probe request classification {index} does not match policy recomputation",
        )
        require(
            recomputed.get("decision") == "ALLOW",
            f"recomputed probe request classification {index} is not exact ALLOW",
        )
    require(probe_verification["target_model"] == scope["target_model"], "verified probe target model mismatch")
    require(probe_verification["template_product_id"] == scope["template_product_id"], "verified probe template mismatch")

    probe_bindings = probe_verification["bindings"]
    source_bindings = probe_source["bindings"]
    for candidate, label in ((probe_bindings, "verified probe"), (source_bindings, "probe source")):
        require_hash(candidate["action_plan_sha256"], hashes["action_plan"], f"{label} action plan")
        require_hash(candidate["endpoint_policy_sha256"], hashes["endpoint_policy"], f"{label} endpoint policy")
    require(probe_bindings["action_plan_id"] == action_plan["plan_id"], "verified probe action_plan_id mismatch")
    require(source_bindings["action_plan_id"] == action_plan["plan_id"], "probe source action_plan_id mismatch")
    require(source_bindings["action_plan_generated_at"] == action_plan["generated_at"], "probe source action-plan timestamp mismatch")
    require_hash(action_plan["bindings"]["endpoint_policy"]["sha256"], hashes["endpoint_policy"], "action plan endpoint policy")

    plan_generated_at = parse_timestamp(action_plan["generated_at"], "action plan generated_at")
    probe_navigation_at = parse_timestamp(probe_source["network"]["navigation_started_at"], "probe navigation_started_at")
    probe_completed_at = parse_timestamp(probe_source["completed_at"], "probe source completed_at")
    verification_generated_at = parse_timestamp(probe_verification["generated_at"], "verified probe generated_at")
    require(plan_generated_at < probe_navigation_at, "fresh probe must navigate after action-plan generation")
    require(probe_completed_at <= issued_at + FUTURE_CLOCK_SKEW, "activation predates probe completion")
    require(verification_generated_at <= issued_at + FUTURE_CLOCK_SKEW, "activation predates probe verification")
    require(issued_at - probe_completed_at <= MAX_PROBE_AGE, "probe source is older than fifteen minutes")
    require(issued_at - verification_generated_at <= MAX_PROBE_AGE, "verified probe receipt is older than fifteen minutes")
    require(probe_verification["probe_captured_at"] == probe_source["captured_at"], "verified probe captured_at mismatch")

    fingerprint = activation["current_page"]["fingerprint_sha256"]
    require_hash(probe_source["page"]["fingerprint_sha256"], fingerprint, "probe source page fingerprint")
    require_hash(probe_verification["page_fingerprint"], fingerprint, "verified probe page fingerprint")
    require_hash(event["page"]["fingerprint_sha256"], fingerprint, "visible UI event page fingerprint")

    session_sha256 = scope["browser_session_sha256"]
    require_hash(probe_source["network"]["interception_session_sha256"], session_sha256, "probe browser session")
    require_hash(event["page"]["browser_session_sha256"], session_sha256, "visible UI event browser session")

    # The public consume gate only accepts the exact VALIDATED CAS checkpoint.
    # Runtime session/page hashes are carried in input_hashes so a stale or
    # cross-session checkpoint cannot authorize a paused request.
    require(checkpoint.get("state") == "VALIDATED", "checkpoint state must be VALIDATED")
    require(checkpoint.get("validation_status") == "VALIDATED", "checkpoint validation_status must be VALIDATED")
    require(checkpoint.get("run_status") == "ACTIVE", "checkpoint run_status must be ACTIVE")
    require(checkpoint.get("lock_state") == "HELD", "checkpoint model lock must be HELD")
    require(checkpoint.get("save_attempt_count") == 0, "checkpoint save_attempt_count must be zero")
    require(checkpoint.get("draft_save_count") == 0, "checkpoint draft_save_count must be zero")
    require(checkpoint.get("draft_id") is None, "checkpoint must not already have a draft_id")
    require(checkpoint.get("final_write_count") == 0, "checkpoint final_write_count must be zero")
    require(checkpoint.get("submit_triggered") is False and checkpoint.get("publish_triggered") is False, "checkpoint contains a final-write signal")
    require(checkpoint.get("external_publish_status") == "NOT_ATTEMPTED", "checkpoint external publish status changed")
    require(checkpoint.get("run_id") == scope["run_id"], "checkpoint run_id mismatch")
    require(checkpoint.get("target_model") == scope["target_model"], "checkpoint target_model mismatch")
    require(checkpoint.get("template_product_id") == scope["template_product_id"], "checkpoint template_product_id mismatch")
    checkpoint_inputs = checkpoint.get("input_hashes") or {}
    for name, expected in (
        ("run_spec_sha256", hashes["run_spec"]),
        ("action_plan_sha256", hashes["action_plan"]),
        ("probe_verification_sha256", hashes["probe_verification"]),
        ("pre_save_readback_sha256", hashes["pre_save_readback"]),
        ("pre_save_verification_sha256", hashes["pre_save_verification"]),
        ("browser_session_sha256", session_sha256),
        ("page_fingerprint_sha256", fingerprint),
    ):
        require_hash(checkpoint_inputs.get(name), expected, f"checkpoint input {name}")
    validate_checkpoint_checklist_evidence(checkpoint)
    # Until the checklist validator has complete role semantics, no production
    # checkpoint may be treated as legitimately VALIDATED.  This deliberately
    # blocks even a schema-valid hand-written checkpoint.
    require(CHECKLIST_IMPLEMENTATION_READY is True, "checklist implementation readiness is not certified")
    require(
        draft_core_module.PRODUCTION_BROWSER_ATTESTATION_VERIFIER_IMPLEMENTED is True,
        "production browser attestation verifier is not implemented",
    )

    template_id = scope["template_product_id"]
    require(activation["current_page"]["query"]["itemId"] == template_id, "activation page query template mismatch")
    require(event["page"]["query"]["itemId"] == template_id, "visible UI event page query template mismatch")
    require(probe_source["route"]["query"]["itemId"] == template_id, "probe source page query template mismatch")

    require(body_semantics["run_id"] == scope["run_id"], "body semantics run_id mismatch")
    require(body_semantics["target_model"] == scope["target_model"], "body semantics target model mismatch")
    require(body_semantics["template_product_id"] == template_id, "body semantics template mismatch")
    require_hash(body_semantics["action_plan_body_sha256"], plan_body_sha256, "body semantics action plan body")

    event_at = parse_timestamp(event["captured_at"], "visible UI event captured_at")
    page_observed_at = parse_timestamp(activation["current_page"]["observed_at"], "current page observed_at")
    require(event_at <= issued_at + FUTURE_CLOCK_SKEW, "activation predates the visible UI event")
    require(page_observed_at <= event_at + FUTURE_CLOCK_SKEW, "visible UI event predates current-page evidence")
    require(issued_at - event_at <= MAX_EVENT_AGE, "visible UI event evidence is older than thirty seconds")
    require(issued_at - page_observed_at <= MAX_PAGE_EVIDENCE_AGE, "current page evidence is older than thirty seconds")

    return {
        "activation": activation,
        "action_plan": action_plan,
        "probe_verification": probe_verification,
        "probe_source": probe_source,
        "endpoint_policy": endpoint_policy,
        "probe_read_activation": read_activation,
        "body_semantics": body_semantics,
        "visible_ui_event": event,
        "run_spec": run_spec,
        "pre_save_readback": pre_save_readback,
        "pre_save_verification": pre_save_verification,
        "checkpoint": checkpoint,
        "hashes": hashes,
        "paths": paths,
        "computed": {
            "action_plan_body_sha256": plan_body_sha256,
            "redacted_body_semantic_sha256": body_semantic_sha256,
        },
        "now": now,
    }


def validation_only_report(validated: dict[str, Any]) -> dict[str, Any]:
    activation = validated["activation"]
    return {
        "schema_version": "sellerkit.alibaba.draft-save-activation-check.v1",
        "generated_at": format_timestamp(validated["now"]),
        "status": "PASS_VALIDATION_ONLY_NOT_ACTIVATED",
        "activation_id": activation["activation_id"],
        "activation_request_sha256": validated["hashes"]["activation"],
        "attempt_before": 0,
        "attempt_after": 0,
        "request_authorized": False,
        "note": "check-only never grants or consumes a draft-save attempt",
    }


def _write_json_atomic_new(path: Path, value: dict[str, Any]) -> None:
    destination = path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ActivationError(f"refusing to overwrite existing output: {destination}")
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A link is an atomic create-new operation on NTFS and POSIX filesystems.
        # os.replace on Windows would silently overwrite a destination created
        # after the pre-check, violating the evidence new-only contract.
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ActivationError(f"refusing to overwrite existing output: {destination}") from exc
        except OSError as exc:
            raise ActivationError(f"cannot atomically create output {destination}: {exc}") from exc
        temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _claim_one_shot_ledger(ledger_dir: Path, validated: dict[str, Any]) -> tuple[Path, str]:
    activation = validated["activation"]
    activation_key = sha256_bytes(str(activation["activation_id"]).encode("utf-8"))
    run_attempt_key = object_sha256(
        {
            "run_id": activation["scope"]["run_id"],
            "target_model": activation["scope"]["target_model"],
            "template_product_id": activation["scope"]["template_product_id"],
            "attempt_kind": "SAVE_ONE_DRAFT_VISIBLE_UI_ONLY",
        }
    )
    ledger_dir = ledger_dir.resolve(strict=False)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / f"{run_attempt_key}.consumed.json"
    marker = {
        "schema_version": "sellerkit.alibaba.draft-save-attempt-ledger.v1",
        "run_attempt_key_sha256": run_attempt_key,
        "activation_id_sha256": activation_key,
        "activation_request_sha256": validated["hashes"]["activation"],
        "checkpoint_sha256": validated["hashes"]["checkpoint"],
        "checkpoint_revision": validated["checkpoint"]["revision"],
        "run_id_sha256": sha256_bytes(str(activation["scope"]["run_id"]).encode("utf-8")),
        "browser_session_sha256": activation["scope"]["browser_session_sha256"],
        "claimed_at": format_timestamp(validated["now"]),
        "attempt_before": 0,
        "attempt_after": 1,
        "maximum": 1,
        "state": "CONSUMED_FAIL_CLOSED",
    }
    payload = (json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(ledger_path, flags, 0o600)
    except FileExistsError as exc:
        raise ActivationError("draft-save activation was already consumed; retry is permanently denied") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        # A partially written exclusive claim still burns the attempt. Leaving
        # it in place is safer than permitting a second request.
        raise
    return ledger_path, sha256_file(ledger_path)


def consume_activation(*, ledger_dir: Path, receipt_path: Path, **validation_args: Any) -> dict[str, Any]:
    receipt_destination = receipt_path.resolve(strict=False)
    claim_destination = receipt_destination.with_name(
        f"{receipt_destination.stem}.attempt-claim.json"
    )
    evidence_destination = receipt_destination.with_name(
        f"{receipt_destination.stem}.checkpoint-transition-evidence.json"
    )
    for label, destination in (
        ("activation receipt", receipt_destination),
        ("attempt claim receipt", claim_destination),
        ("checkpoint transition evidence", evidence_destination),
    ):
        if destination.exists():
            raise ActivationError(f"refusing to overwrite existing {label}: {destination}")

    checkpoint_path = Path(validation_args["checkpoint_path"]).resolve(strict=True)
    checkpoint_run_dir = checkpoint_path.parent
    expected_ledger_dir = checkpoint_run_dir / "draft-save-attempt-ledger"
    if ledger_dir.resolve(strict=False) != expected_ledger_dir:
        raise ActivationError(
            "ledger-dir must be the fixed run-scoped checkpoint sibling "
            f"{expected_ledger_dir}"
        )
    if receipt_destination.parent != checkpoint_run_dir:
        raise ActivationError(
            "activation receipt and transition evidence must be written in the checkpoint run directory"
        )
    try:
        with checkpoint_mutation_guard(checkpoint_path) as mutation_token:
            validated = validate_activation(**validation_args)
            activation = validated["activation"]
            event = validated["visible_ui_event"]
            # The same OS-level checkpoint lock now spans validation, exclusive
            # ledger claim, receipt/evidence creation and SAVE_ATTEMPTED commit.
            # No concurrent abort/transition can validate the old revision.
            _, checkpoint_before = read_bound_object(
                validated["paths"]["checkpoint"], "validated checkpoint before consume"
            )
            require_hash(checkpoint_before, validated["hashes"]["checkpoint"], "checkpoint before consume")
            _, ledger_sha256 = _claim_one_shot_ledger(ledger_dir, validated)
            # Any failure after this point is irreversible.  Keep the mutation
            # lock as a fail-closed reconciliation marker instead of allowing
            # abort or a second save attempt to disguise the consumed ledger.
            mutation_token["retain_on_error"] = True
            _, checkpoint_after = read_bound_object(
                validated["paths"]["checkpoint"], "validated checkpoint after consume"
            )
            require_hash(checkpoint_after, validated["hashes"]["checkpoint"], "checkpoint after consume")
            attempt_claim = {
                "schema_version": "sellerkit.alibaba.draft-save-attempt-claim-receipt.v1",
                "claim_id": f"draft-save-claim-{validated['hashes']['activation'][:24]}",
                "generated_at": format_timestamp(validated["now"]),
                "status": "ATTEMPT_CLAIMED_CHECKPOINT_COMMIT_PENDING",
                "result_semantics": "ATTEMPT_CONSUMED_NOT_RELEASE_AUTHORIZATION",
                "run_id": activation["scope"]["run_id"],
                "target_model": activation["scope"]["target_model"],
                "template_product_id": activation["scope"]["template_product_id"],
                "browser_session_sha256": activation["scope"]["browser_session_sha256"],
                "page_fingerprint_sha256": activation["current_page"]["fingerprint_sha256"],
                "activation_request_sha256": validated["hashes"]["activation"],
                "visible_ui_event_sha256": validated["hashes"]["visible_ui_event"],
                "pre_save_verification_sha256": validated["hashes"]["pre_save_verification"],
                "checkpoint_sha256_before": checkpoint_hash(validated["checkpoint"]),
                "checkpoint_revision_before": validated["checkpoint"]["revision"],
                "ledger_entry_sha256": ledger_sha256,
                "attempt_transition": {
                    "before": 0,
                    "after": 1,
                    "maximum": 1,
                    "atomic_ledger_claimed": True,
                    "retry_allowed": False,
                },
                "release_authorized": False,
                "final_write_enabled": False,
            }
            validate_schema(attempt_claim, ATTEMPT_CLAIM_RECEIPT_SCHEMA, "attempt claim receipt")
            _write_json_atomic_new(claim_destination, attempt_claim)
            claim_payload = claim_destination.read_bytes()
            evidence = {
                "schema_version": "sellerkit.checkpoint-transition-evidence.v1",
                "evidence_id": f"save-attempt-{validated['hashes']['activation'][:24]}",
                "created_at": format_timestamp(validated["now"]),
                "run_id": activation["scope"]["run_id"],
                "target_model": activation["scope"]["target_model"],
                "template_product_id": activation["scope"]["template_product_id"],
                "new_state": "SAVE_ATTEMPTED",
                "artifacts": [{
                    "role": "DRAFT_SAVE_ATTEMPT_CLAIM_RECEIPT",
                    "path": str(claim_destination),
                    "bytes": len(claim_payload),
                    "sha256": sha256_bytes(claim_payload),
                }],
            }
            evidence["body_sha256"] = object_sha256(evidence)
            _write_json_atomic_new(evidence_destination, evidence)
            updated_checkpoint = transition_checkpoint_under_guard(
                checkpoint_path,
                validated["checkpoint"]["revision"],
                checkpoint_hash(validated["checkpoint"]),
                "SAVE_ATTEMPTED",
                evidence_destination,
                mutation_token=mutation_token,
            )
            require(
                updated_checkpoint.get("state") == "SAVE_ATTEMPTED"
                and updated_checkpoint.get("save_attempt_count") == 1
                and updated_checkpoint.get("run_status") == "SAVE_ATTEMPTED_UNCONFIRMED",
                "checkpoint did not atomically commit SAVE_ATTEMPTED",
            )
            committed_checkpoint = read_object(checkpoint_path, "committed checkpoint")
            require(
                canonical_json(committed_checkpoint) == canonical_json(updated_checkpoint),
                "committed checkpoint differs from the SAVE_ATTEMPTED transition result",
            )
            checkpoint_sha256_after = checkpoint_hash(committed_checkpoint)
            claim_sha256 = sha256_file(claim_destination)
            receipt = {
                "schema_version": "sellerkit.alibaba.draft-save-activation-receipt.v1",
                "receipt_id": f"draft-save-{validated['hashes']['activation'][:24]}",
                "generated_at": format_timestamp(validated["now"]),
                "expires_at": activation["expires_at"],
                "status": "PASS_ONE_SHOT_DRAFT_SAVE_ACTIVATED",
                "result_semantics": "ATTEMPT_CONSUMED_NOT_DRAFT_SUCCESS",
                "activation_request_sha256": validated["hashes"]["activation"],
                "scope": {
                    "run_id": activation["scope"]["run_id"],
                    "target_model": activation["scope"]["target_model"],
                    "template_product_id": activation["scope"]["template_product_id"],
                    "browser_session_sha256": activation["scope"]["browser_session_sha256"],
                },
                "bindings": {
                    "action_plan_sha256": activation["bindings"]["action_plan_sha256"],
                    "action_plan_body_sha256": activation["bindings"]["action_plan_body_sha256"],
                    "verified_probe_receipt_sha256": activation["bindings"]["verified_probe_receipt_sha256"],
                    "probe_source_sha256": activation["bindings"]["probe_source_sha256"],
                    "endpoint_policy_sha256": activation["bindings"]["endpoint_policy_sha256"],
                    "probe_read_activation_sha256": activation["bindings"]["probe_read_activation_sha256"],
                    "current_page_fingerprint_sha256": activation["current_page"]["fingerprint_sha256"],
                    "redacted_body_semantic_sha256": activation["bindings"]["redacted_body_semantic_sha256"],
                    "visible_ui_event_sha256": activation["bindings"]["visible_ui_event_sha256"],
                    "run_spec_sha256": activation["bindings"]["run_spec_sha256"],
                    "pre_save_readback_sha256": activation["bindings"]["pre_save_readback_sha256"],
                    "pre_save_verification_sha256": activation["bindings"]["pre_save_verification_sha256"],
                    "checkpoint_sha256": activation["bindings"]["checkpoint_sha256"],
                    "checkpoint_revision": validated["checkpoint"]["revision"],
                    "attempt_claim_receipt_sha256": claim_sha256,
                },
                "endpoint_request": {
                    "origin": activation["endpoint_request"]["origin"],
                    "path": activation["endpoint_request"]["path"],
                    "method": activation["endpoint_request"]["method"],
                    "visible_ui_only": True,
                    "direct_replay_allowed": False,
                },
                "visible_ui_event": {
                    "event_id": event["event_id"],
                    "event_at": event["captured_at"],
                    "event_evidence_sha256": validated["hashes"]["visible_ui_event"],
                },
                "attempt_transition": {
                    "before": 0,
                    "after": 1,
                    "maximum": 1,
                    "atomic_ledger_claimed": True,
                    "retry_allowed": False,
                },
                "guard_assertions": {
                    "global_policy_remains_default_deny": True,
                    "unknown_requests_denied": True,
                    "draft_route_not_globally_allowed": True,
                    "direct_replay_denied": True,
                    "final_write_ui_hard_denied": True,
                    "final_write_network_hard_denied": True,
                    "request_guard_must_release_exactly_this_request_once": True,
                },
                "ledger_entry_sha256": ledger_sha256,
                "checkpoint_transition": {
                    "state": "SAVE_ATTEMPTED",
                    "evidence_path": str(evidence_destination),
                    "checkpoint_revision_before": validated["checkpoint"]["revision"],
                    "checkpoint_revision_after": committed_checkpoint["revision"],
                    "checkpoint_sha256_after": checkpoint_sha256_after,
                },
                "release_authorized": True,
            }
            validate_schema(receipt, ACTIVATION_RECEIPT_SCHEMA, "activation receipt")
            # This PASS/release receipt is created only after SAVE_ATTEMPTED is
            # durably committed.  Any earlier failure leaves only the explicit
            # non-authorizing claim and a retained fail-closed mutation lock.
            _write_json_atomic_new(receipt_destination, receipt)
            return receipt
    except GuardError as exc:
        raise ActivationError(str(exc)) from exc


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--activation", type=Path, required=True)
    parser.add_argument("--action-plan", type=Path, required=True)
    parser.add_argument("--probe-verification", type=Path, required=True)
    parser.add_argument("--probe-source", type=Path, required=True)
    parser.add_argument("--endpoint-policy", type=Path, required=True)
    parser.add_argument("--probe-read-activation", type=Path, required=True)
    parser.add_argument("--body-semantics", type=Path, required=True)
    parser.add_argument("--visible-ui-event", type=Path, required=True)
    parser.add_argument("--run-spec", type=Path, required=True)
    parser.add_argument("--pre-save-readback", type=Path, required=True)
    parser.add_argument("--pre-save-verification", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and atomically consume one session-scoped visible-UI Alibaba draft-save activation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="Validate only; this never grants a request.")
    _common_arguments(check)
    check.add_argument("--report", type=Path)
    consume = subparsers.add_parser(
        "consume",
        help="Consume attempt 0->1 and commit checkpoint SAVE_ATTEMPTED under one fail-closed mutation lock.",
    )
    _common_arguments(consume)
    consume.add_argument("--ledger-dir", type=Path, required=True)
    consume.add_argument("--receipt", type=Path, required=True)
    return parser


def _validation_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "activation_path": args.activation,
        "action_plan_path": args.action_plan,
        "probe_verification_path": args.probe_verification,
        "probe_source_path": args.probe_source,
        "endpoint_policy_path": args.endpoint_policy,
        "probe_read_activation_path": args.probe_read_activation,
        "body_semantics_path": args.body_semantics,
        "visible_ui_event_path": args.visible_ui_event,
        "run_spec_path": args.run_spec,
        "pre_save_readback_path": args.pre_save_readback,
        "pre_save_verification_path": args.pre_save_verification,
        "checkpoint_path": args.checkpoint,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            report = validation_only_report(validate_activation(**_validation_args(args)))
            if args.report:
                _write_json_atomic_new(args.report, report)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        receipt = consume_activation(
            ledger_dir=args.ledger_dir,
            receipt_path=args.receipt,
            **_validation_args(args),
        )
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        ActivationError,
        GuardError,
        FileNotFoundError,
        KeyError,
        TypeError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "sellerkit.alibaba.draft-save-activation-error.v1",
                    "status": "BLOCKED_DRAFT_SAVE_ACTIVATION",
                    "error": str(exc),
                    "request_authorized": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
