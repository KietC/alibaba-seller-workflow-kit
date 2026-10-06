from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator, FormatChecker


SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
SCHEMAS = {
    "action_plan": SCHEMA_DIR / "browser-action-plan.schema.json",
    "run_spec": SCHEMA_DIR / "run-spec.schema.json",
    "probe_verification": SCHEMA_DIR / "probe-verification-receipt.schema.json",
    "pre_save_verification": SCHEMA_DIR / "pre-save-verification-receipt.schema.json",
    "activation_receipt": SCHEMA_DIR / "draft-save-activation-receipt.schema.json",
    "before_inventory": SCHEMA_DIR / "product-inventory-snapshot.schema.json",
    "after_inventory": SCHEMA_DIR / "product-inventory-snapshot.schema.json",
    "save_result": SCHEMA_DIR / "draft-save-result-evidence.schema.json",
    "post_refresh": SCHEMA_DIR / "post-refresh-verification.schema.json",
    "adapter": None,
}
RECEIPT_SCHEMA = SCHEMA_DIR / "inventory-delta-verification-receipt.schema.json"
CAPTURE_MATERIAL_SCHEMA = SCHEMA_DIR / "browser-capture-material.schema.json"
LIVE_CAPTURE_SOURCE = "CODEX_IN_APP_BROWSER_TOOL_RESULT"
SYNTHETIC_CAPTURE_SOURCE = "SYNTHETIC_TEST"
# No offline JSON field can authenticate a Codex tool call.  Keep production
# live PASS unreachable until a separately reviewed provenance verifier exists.
LIVE_TOOL_PROVENANCE_VERIFIER_IMPLEMENTED = False


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


def recompute_body_hash(document: dict[str, Any], field: str, label: str, errors: list[str]) -> str:
    body = copy.deepcopy(document)
    declared = str(body.pop(field, "") or "").lower()
    computed = object_sha256(body)
    if declared != computed:
        errors.append(f"{label} {field} does not hash its canonical body")
    return computed


def normalized_id_set(snapshot: dict[str, Any], name: str, label: str, errors: list[str]) -> set[str]:
    value = snapshot.get(name)
    if not isinstance(value, dict):
        errors.append(f"{label} {name} inventory is missing")
        return set()
    ids = value.get("ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) and item.isdigit() for item in ids):
        errors.append(f"{label} {name} ids must be a numeric-string list")
        return set()
    canonical_order = sorted(set(ids), key=lambda item: (len(item), item))
    if ids != canonical_order:
        errors.append(f"{label} {name} ids are not unique canonical numeric order")
    if value.get("count") != len(ids):
        errors.append(f"{label} {name} count does not match ids")
    if str(value.get("id_set_sha256") or "").lower() != object_sha256(ids):
        errors.append(f"{label} {name} id_set_sha256 mismatch")
    return set(ids)


def check_bindings(document: dict[str, Any], expected: dict[str, Any], label: str, errors: list[str]) -> None:
    bindings = document.get("bindings")
    if not isinstance(bindings, dict):
        errors.append(f"{label} bindings are missing")
        return
    for key, value in expected.items():
        observed = bindings.get(key)
        if isinstance(value, str):
            observed = str(observed or "").lower()
        if observed != value:
            errors.append(f"{label} binding mismatch: {key}")


def validate_capture_chain(
    document: dict[str, Any],
    *,
    label: str,
    expected_roles: set[str],
    expected_session_sha256: str,
    expected_page_fingerprints: dict[str, str],
    expected_payloads: dict[str, dict[str, Any]],
    expected_captured_at: dict[str, str],
    errors: list[str],
) -> list[str]:
    """Validate independent capture files, not merely summary-JSON assertions."""

    descriptors = document.get("capture_evidence")
    if not isinstance(descriptors, list):
        errors.append(f"{label} trusted capture evidence is missing")
        return []
    observed_roles = {
        str(row.get("role") or "") for row in descriptors if isinstance(row, dict)
    }
    if observed_roles != expected_roles or len(descriptors) != len(expected_roles):
        errors.append(f"{label} trusted capture roles are incomplete or duplicated")
    material_hashes: list[str] = []
    for index, descriptor in enumerate(descriptors):
        if not isinstance(descriptor, dict):
            errors.append(f"{label} capture descriptor {index} is not an object")
            continue
        role = str(descriptor.get("role") or "")
        raw_path = str(descriptor.get("path") or "")
        path = Path(raw_path)
        if not path.is_absolute():
            errors.append(f"{label} capture {role} path is not absolute")
            continue
        material, digest, read_errors = read_bound_object(path, f"{label} capture {role}")
        errors.extend(read_errors)
        if read_errors:
            continue
        material_hashes.append(digest)
        if descriptor.get("sha256") != digest:
            errors.append(f"{label} capture {role} sha256 mismatch")
        try:
            observed_bytes = path.resolve(strict=True).stat().st_size
        except OSError as exc:
            errors.append(f"{label} capture {role} cannot be stat'ed: {exc}")
            observed_bytes = -1
        if descriptor.get("bytes") != observed_bytes:
            errors.append(f"{label} capture {role} byte count mismatch")
        errors.extend(schema_errors(material, CAPTURE_MATERIAL_SCHEMA, f"{label} capture {role}"))
        if material.get("role") != role or material.get("capture_id") != descriptor.get("capture_id"):
            errors.append(f"{label} capture {role} descriptor identity mismatch")
        if material.get("source") not in {LIVE_CAPTURE_SOURCE, SYNTHETIC_CAPTURE_SOURCE}:
            errors.append(f"{label} capture {role} has an unsupported source")
        if material.get("browser_session_sha256") != expected_session_sha256:
            errors.append(f"{label} capture {role} browser session mismatch")
        if material.get("page_fingerprint_sha256") != expected_page_fingerprints.get(role):
            errors.append(f"{label} capture {role} page fingerprint mismatch")
        if material.get("captured_at") != expected_captured_at.get(role):
            errors.append(f"{label} capture {role} timestamp mismatch")
        payload = material.get("payload")
        if not isinstance(payload, dict) or payload != expected_payloads.get(role):
            errors.append(f"{label} capture {role} payload does not match the verified summary")
        if material.get("payload_sha256") != object_sha256(payload):
            errors.append(f"{label} capture {role} payload_sha256 mismatch")
        if material.get("credential_values_recorded") is not False or material.get("raw_secret_values_recorded") is not False:
            errors.append(f"{label} capture {role} retained secret material")
    if len(material_hashes) != len(set(material_hashes)):
        errors.append(f"{label} reuses one capture artifact for multiple evidence roles")
    return material_hashes


def verify_inventory_delta(
    *, action_plan_path: Path, run_spec_path: Path, probe_verification_path: Path,
    pre_save_verification_path: Path, activation_receipt_path: Path,
    before_inventory_path: Path, after_inventory_path: Path, save_result_path: Path,
    post_refresh_path: Path, adapter_path: Path, now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    paths = {
        "action_plan": action_plan_path, "run_spec": run_spec_path, "probe_verification": probe_verification_path,
        "pre_save_verification": pre_save_verification_path, "activation_receipt": activation_receipt_path,
        "before_inventory": before_inventory_path, "after_inventory": after_inventory_path,
        "save_result": save_result_path, "post_refresh": post_refresh_path,
        "adapter": adapter_path,
    }
    documents: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    errors: list[str] = []
    for key, path in paths.items():
        document, digest, read_errors = read_bound_object(path, key.replace("_", " "))
        documents[key], hashes[key] = document, digest
        errors.extend(read_errors)
        if not read_errors and SCHEMAS[key] is not None:
            errors.extend(schema_errors(document, SCHEMAS[key], key.replace("_", " ")))

    plan = documents["action_plan"]
    run_spec = documents["run_spec"]
    probe = documents["probe_verification"]
    pre_save = documents["pre_save_verification"]
    activation = documents["activation_receipt"]
    before = documents["before_inventory"]
    after = documents["after_inventory"]
    save = documents["save_result"]
    refresh = documents["post_refresh"]
    adapter = documents["adapter"]
    plan_body_hash = recompute_body_hash(plan, "plan_body_sha256", "action plan", errors)
    recompute_body_hash(before, "snapshot_body_sha256", "before inventory", errors)
    recompute_body_hash(after, "snapshot_body_sha256", "after inventory", errors)
    recompute_body_hash(save, "result_body_sha256", "save result", errors)

    target = plan.get("target") or {}
    expected_model = str(target.get("model") or "")
    expected_template = str(target.get("template_product_id") or "")
    expected_run = str(target.get("run_id") or "")
    for label, document in (
        ("run spec", run_spec), ("probe verification", probe), ("pre-save verification", pre_save),
        ("activation receipt", activation.get("scope") or {}), ("before inventory", before),
        ("after inventory", after), ("save result", save), ("post-refresh", refresh),
    ):
        if document.get("target_model") != expected_model:
            errors.append(f"{label} target_model mismatch")
        if str(document.get("template_product_id") or "") != expected_template:
            errors.append(f"{label} template_product_id mismatch")
    for label, document in (
        ("pre-save verification", pre_save), ("activation receipt", activation.get("scope") or {}),
        ("before inventory", before), ("after inventory", after), ("save result", save), ("post-refresh", refresh),
    ):
        if document.get("run_id") != expected_run:
            errors.append(f"{label} run_id mismatch")

    if str((((plan.get("bindings") or {}).get("three_party") or {}).get("run_spec_sha256")) or "").lower() != hashes["run_spec"]:
        errors.append("action plan does not bind the supplied run spec")
    if str((((plan.get("bindings") or {}).get("adapter") or {}).get("sha256")) or "").lower() != hashes["adapter"]:
        errors.append("action plan does not bind the supplied adapter")
    if probe.get("status") != "PASS_ZERO_WRITE" or probe.get("errors") != []:
        errors.append("probe verification is not a clean PASS_ZERO_WRITE")
    if str((probe.get("bindings") or {}).get("action_plan_sha256") or "").lower() != hashes["action_plan"]:
        errors.append("probe verification does not bind the action plan")
    if str((probe.get("bindings") or {}).get("run_spec_sha256") or "").lower() != hashes["run_spec"]:
        errors.append("probe verification does not bind the run spec")
    if pre_save.get("status") != "PASS_PRE_SAVE_NO_WRITE" or pre_save.get("errors") != []:
        errors.append("pre-save verification did not pass")
    check_bindings(pre_save, {
        "action_plan_sha256": hashes["action_plan"], "run_spec_sha256": hashes["run_spec"],
        "probe_verification_sha256": hashes["probe_verification"],
    }, "pre-save verification", errors)
    if activation.get("status") != "PASS_ONE_SHOT_DRAFT_SAVE_ACTIVATED" or activation.get("result_semantics") != "ATTEMPT_CONSUMED_NOT_DRAFT_SUCCESS":
        errors.append("draft-save activation is not a consumed one-shot attempt")
    check_bindings(activation, {
        "action_plan_sha256": hashes["action_plan"], "action_plan_body_sha256": plan_body_hash,
        "verified_probe_receipt_sha256": hashes["probe_verification"],
    }, "activation receipt", errors)
    attempt = activation.get("attempt_transition") or {}
    if attempt.get("before") != 0 or attempt.get("after") != 1 or attempt.get("maximum") != 1 or attempt.get("retry_allowed") is not False:
        errors.append("activation receipt does not prove the atomic one-shot transition 0 -> 1")

    common_snapshot_bindings = {
        "action_plan_sha256": hashes["action_plan"], "action_plan_body_sha256": plan_body_hash,
        "run_spec_sha256": hashes["run_spec"], "probe_verification_sha256": hashes["probe_verification"],
        "pre_save_verification_sha256": hashes["pre_save_verification"],
    }
    check_bindings(before, {**common_snapshot_bindings, "draft_save_activation_receipt_sha256": None}, "before inventory", errors)
    check_bindings(after, {**common_snapshot_bindings, "draft_save_activation_receipt_sha256": hashes["activation_receipt"]}, "after inventory", errors)
    if before.get("phase") != "BEFORE_DRAFT_SAVE" or after.get("phase") != "AFTER_DRAFT_SAVE":
        errors.append("inventory phases are not an independent before/after pair")
    for label, snapshot in (("before inventory", before), ("after inventory", after)):
        collection = snapshot.get("collection") or {}
        if collection.get("complete") is not True or collection.get("pagination_exhausted") is not True or collection.get("all_pages_read") is not True:
            errors.append(f"{label} is not a complete all-pages inventory")
        if snapshot.get("final_write_count") != 0 or snapshot.get("final_submit_triggered") is not False:
            errors.append(f"{label} contains a final-write signal")

    capture_hashes: list[str] = []
    for label, snapshot, phase_prefix in (
        ("before inventory", before, "INVENTORY_BEFORE"),
        ("after inventory", after, "INVENTORY_AFTER"),
    ):
        page_role = f"{phase_prefix}_VISIBLE_PAGE"
        network_role = f"{phase_prefix}_NETWORK_RESPONSE"
        page_fingerprint = str(snapshot.get("page_fingerprint_sha256") or "")
        snapshot_time = str(snapshot.get("captured_at") or "")
        capture_hashes.extend(validate_capture_chain(
            snapshot,
            label=label,
            expected_roles={page_role, network_role},
            expected_session_sha256=str(snapshot.get("browser_session_sha256") or ""),
            expected_page_fingerprints={page_role: page_fingerprint, network_role: page_fingerprint},
            expected_captured_at={page_role: snapshot_time, network_role: snapshot_time},
            expected_payloads={
                page_role: {
                    "phase": snapshot.get("phase"),
                    "collection_source": (snapshot.get("collection") or {}).get("source"),
                    "page_fingerprint_sha256": page_fingerprint,
                },
                network_role: {
                    "phase": snapshot.get("phase"),
                    "online_ids": (snapshot.get("online") or {}).get("ids"),
                    "pending_ids": (snapshot.get("pending") or {}).get("ids"),
                    "draft_ids": (snapshot.get("draft") or {}).get("ids"),
                    "complete": (snapshot.get("collection") or {}).get("complete"),
                    "pagination_exhausted": (snapshot.get("collection") or {}).get("pagination_exhausted"),
                    "all_pages_read": (snapshot.get("collection") or {}).get("all_pages_read"),
                },
            },
            errors=errors,
        ))

    before_sets = {name: normalized_id_set(before, name, "before inventory", errors) for name in ("online", "pending", "draft")}
    after_sets = {name: normalized_id_set(after, name, "after inventory", errors) for name in ("online", "pending", "draft")}
    delta = {
        "online_added": sorted(after_sets["online"] - before_sets["online"], key=lambda x: (len(x), x)),
        "online_removed": sorted(before_sets["online"] - after_sets["online"], key=lambda x: (len(x), x)),
        "pending_added": sorted(after_sets["pending"] - before_sets["pending"], key=lambda x: (len(x), x)),
        "pending_removed": sorted(before_sets["pending"] - after_sets["pending"], key=lambda x: (len(x), x)),
        "draft_added": sorted(after_sets["draft"] - before_sets["draft"], key=lambda x: (len(x), x)),
        "draft_removed": sorted(before_sets["draft"] - after_sets["draft"], key=lambda x: (len(x), x)),
    }

    saved_id = str(save.get("saved_draft_id") or "")
    if delta["online_added"] or delta["online_removed"]:
        errors.append("online product ID set changed")
    if delta["pending_added"] or delta["pending_removed"]:
        errors.append("pending product ID set changed")
    if delta["draft_added"] != [saved_id] or delta["draft_removed"]:
        errors.append("draft delta is not exactly the single saved_draft_id")
    if saved_id in before_sets["draft"]:
        errors.append("saved_draft_id already existed in the before snapshot")

    save_bindings = {
        "action_plan_sha256": hashes["action_plan"], "action_plan_body_sha256": plan_body_hash,
        "run_spec_sha256": hashes["run_spec"], "probe_verification_sha256": hashes["probe_verification"],
        "pre_save_verification_sha256": hashes["pre_save_verification"],
        "draft_save_activation_receipt_sha256": hashes["activation_receipt"],
        "before_inventory_sha256": hashes["before_inventory"], "after_inventory_sha256": hashes["after_inventory"],
        "post_refresh_receipt_sha256": hashes["post_refresh"],
    }
    check_bindings(save, save_bindings, "save result", errors)
    response = save.get("save_response") or {}
    reopen = save.get("reopen") or {}
    activation_fingerprint = str(((documents["activation_receipt"].get("scope") or {}).get("page_fingerprint_sha256") or ""))
    if not activation_fingerprint:
        activation_fingerprint = str((documents["probe_verification"].get("page_fingerprint") or ""))
    save_roles = {"DRAFT_SAVE_NETWORK_RESPONSE", "DRAFT_REOPEN_VISIBLE_PAGE"}
    capture_hashes.extend(validate_capture_chain(
        save,
        label="save result",
        expected_roles=save_roles,
        expected_session_sha256=str(save.get("browser_session_sha256") or ""),
        expected_page_fingerprints={
            "DRAFT_SAVE_NETWORK_RESPONSE": activation_fingerprint,
            "DRAFT_REOPEN_VISIBLE_PAGE": str(reopen.get("page_fingerprint_sha256") or ""),
        },
        expected_captured_at={
            "DRAFT_SAVE_NETWORK_RESPONSE": str(response.get("captured_at") or ""),
            "DRAFT_REOPEN_VISIBLE_PAGE": str(reopen.get("captured_at") or ""),
        },
        expected_payloads={
            "DRAFT_SAVE_NETWORK_RESPONSE": {
                key: response.get(key)
                for key in (
                    "origin", "path", "method", "http_status", "business_success",
                    "business_status", "response_draft_id", "error_code",
                )
            },
            "DRAFT_REOPEN_VISIBLE_PAGE": {
                key: reopen.get(key)
                for key in (
                    "url", "query", "draft_id_readback", "same_draft_reopened",
                    "login_active", "captcha_present", "page_fingerprint_sha256",
                )
            },
        },
        errors=errors,
    ))
    if len(capture_hashes) != len(set(capture_hashes)):
        errors.append("trusted capture chain reuses one raw artifact across evidence roles")

    capture_sources: set[str] = set()
    for owner in (before, after, save):
        for descriptor in owner.get("capture_evidence") or []:
            if not isinstance(descriptor, dict):
                continue
            material, _, material_errors = read_bound_object(Path(str(descriptor.get("path") or "")), "capture provenance")
            if not material_errors:
                capture_sources.add(str(material.get("source") or ""))
    attestation = ((adapter.get("capabilities") or {}).get("capture_attestation") or {})
    live_attested = (
        LIVE_TOOL_PROVENANCE_VERIFIER_IMPLEMENTED
        and
        capture_sources == {LIVE_CAPTURE_SOURCE}
        and attestation.get("reviewed") is True
        and attestation.get("live_tool_call_provenance_verified") is True
        and attestation.get("raw_material_hashes_required") is True
        and attestation.get("session_and_page_binding_required") is True
        and attestation.get("provenance_verifier_id") == "codex-in-app-browser-tool-call-provenance-v1"
    )
    if not live_attested:
        errors.append(
            "live capture attestation is unavailable: current adapter lacks reviewed tool-call provenance verification"
        )
    if response.get("business_success") is not True or response.get("business_status") != "SUCCESS" or response.get("error_code") is not None:
        errors.append("save response is not an unambiguous business success")
    if response.get("response_draft_id") != saved_id:
        errors.append("save response draft ID does not match saved_draft_id")
    if reopen.get("draft_id_readback") != saved_id or (reopen.get("query") or {}).get("itemId") != saved_id:
        errors.append("reopen evidence draft ID does not match saved_draft_id")
    try:
        parsed = urlsplit(str(reopen.get("url") or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.scheme != "https" or parsed.netloc != "post.alibaba.com" or parsed.path != "/product/publish.htm":
            errors.append("reopen URL has the wrong origin or path")
        if query.get("itemId") != [saved_id] or query.get("pubAction") != ["draft"]:
            errors.append("reopen URL does not bind exactly one saved itemId and pubAction=draft")
        if any(value in {"submit", "publish", "formal"} for value in query.get("pubAction", [])):
            errors.append("reopen URL contains a final-write action")
    except (TypeError, ValueError):
        errors.append("reopen URL is invalid")

    if refresh.get("draft_id") != saved_id or refresh.get("same_draft_reopened") is not True or refresh.get("persistence_verified") is not True:
        errors.append("post-refresh receipt does not prove persistence of saved_draft_id")
    refresh_bindings = refresh.get("bindings") or {}
    if str(refresh_bindings.get("action_plan_sha256") or "").lower() != hashes["action_plan"]:
        errors.append("post-refresh receipt does not bind the action plan")
    if str(refresh_bindings.get("run_spec_sha256") or "").lower() != hashes["run_spec"]:
        errors.append("post-refresh receipt does not bind the run spec")
    if str(refresh_bindings.get("probe_receipt_sha256") or "").lower() != hashes["probe_verification"]:
        errors.append("post-refresh receipt does not bind the verified probe")

    sessions = {
        str((activation.get("scope") or {}).get("browser_session_sha256") or ""),
        str(before.get("browser_session_sha256") or ""), str(after.get("browser_session_sha256") or ""),
        str(save.get("browser_session_sha256") or ""),
    }
    if len(sessions) != 1 or "" in sessions:
        errors.append("activation, inventories, and save/reopen evidence are not from one browser session")
    before_time = parse_timestamp(before.get("captured_at"), "before inventory captured_at", errors)
    event_time = parse_timestamp(((activation.get("visible_ui_event") or {}).get("event_at")), "save event_at", errors)
    response_time = parse_timestamp(response.get("captured_at"), "save response captured_at", errors)
    after_time = parse_timestamp(after.get("captured_at"), "after inventory captured_at", errors)
    reopen_time = parse_timestamp(reopen.get("captured_at"), "reopen captured_at", errors)
    if before_time and event_time and before_time >= event_time:
        errors.append("before inventory must be captured strictly before the visible save click")
    if event_time and response_time and response_time < event_time:
        errors.append("save response predates the visible save click")
    if response_time and after_time and after_time < response_time:
        errors.append("after inventory predates the save response")
    if response_time and reopen_time and reopen_time < response_time:
        errors.append("reopen evidence predates the save response")
    if after_time and after_time > now:
        errors.append("after inventory is from the future")
    if save.get("draft_save_count") != 1 or refresh.get("draft_save_count") != 1:
        errors.append("draft save count is not exactly one")
    for label, document in (("save result", save), ("post-refresh", refresh)):
        if document.get("final_write_count") != 0 or document.get("final_submit_triggered") is not False:
            errors.append(f"{label} contains a final-write signal")

    unique_errors = sorted(set(errors))
    bindings = {
        "action_plan_sha256": hashes["action_plan"], "run_spec_sha256": hashes["run_spec"],
        "adapter_sha256": hashes["adapter"],
        "probe_verification_sha256": hashes["probe_verification"], "pre_save_verification_sha256": hashes["pre_save_verification"],
        "draft_save_activation_receipt_sha256": hashes["activation_receipt"],
        "before_inventory_sha256": hashes["before_inventory"], "after_inventory_sha256": hashes["after_inventory"],
        "save_result_evidence_sha256": hashes["save_result"], "post_refresh_receipt_sha256": hashes["post_refresh"],
    }
    consistency_errors = [
        row for row in unique_errors
        if not row.startswith("live capture attestation is unavailable:")
    ]
    if consistency_errors:
        status = "BLOCKED_INVENTORY_DELTA"
    elif live_attested:
        status = "PASS_EXACTLY_ONE_DRAFT_SAVED"
    else:
        status = "BLOCKED_LIVE_ATTESTATION"
    receipt = {
        "schema_version": "sellerkit.alibaba.inventory-delta-verification.v1", "generated_at": utc_text(now),
        "status": status,
        "result_semantics": "DRAFT_ONLY_NOT_PUBLISHED" if live_attested else "CONSISTENCY_ONLY_NOT_LIVE_ATTESTED",
        "trust_status": "LIVE_ATTESTED" if live_attested else "CONSISTENCY_ONLY", "target_model": expected_model or None,
        "template_product_id": expected_template or None, "run_id": expected_run or None, "saved_draft_id": saved_id or None,
        "bindings": bindings,
        "before_counts": {name: len(before_sets[name]) for name in ("online", "pending", "draft")},
        "after_counts": {name: len(after_sets[name]) for name in ("online", "pending", "draft")},
        "delta": delta, "draft_save_count": save.get("draft_save_count"), "final_write_count": save.get("final_write_count"),
        "errors": unique_errors,
    }
    receipt_errors = schema_errors(receipt, RECEIPT_SCHEMA, "inventory delta verification receipt")
    if receipt_errors:
        receipt["errors"] = sorted(set(receipt["errors"] + receipt_errors))
        receipt["status"] = "BLOCKED_INVENTORY_DELTA"
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
    parser = argparse.ArgumentParser(description="Fail-closed exact-one-draft inventory delta validator")
    parser.add_argument("--action-plan", type=Path, required=True)
    parser.add_argument("--run-spec", type=Path, required=True)
    parser.add_argument("--probe-verification", type=Path, required=True)
    parser.add_argument("--pre-save-verification", type=Path, required=True)
    parser.add_argument("--activation-receipt", type=Path, required=True)
    parser.add_argument("--before-inventory", type=Path, required=True)
    parser.add_argument("--after-inventory", type=Path, required=True)
    parser.add_argument("--save-result", type=Path, required=True)
    parser.add_argument("--post-refresh", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.receipt.resolve().exists():
        print(json.dumps({
            "status": "BLOCKED_RECEIPT_EXISTS",
            "error": f"refusing to overwrite existing receipt: {args.receipt.resolve()}",
            "receipt_written": False, "final_write_enabled": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    receipt = verify_inventory_delta(
        action_plan_path=args.action_plan, run_spec_path=args.run_spec, probe_verification_path=args.probe_verification,
        pre_save_verification_path=args.pre_save_verification, activation_receipt_path=args.activation_receipt,
        before_inventory_path=args.before_inventory, after_inventory_path=args.after_inventory,
        save_result_path=args.save_result, post_refresh_path=args.post_refresh,
        adapter_path=args.adapter,
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
    return 0 if receipt["status"] == "PASS_EXACTLY_ONE_DRAFT_SAVED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
