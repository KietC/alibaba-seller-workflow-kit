#!/usr/bin/env python3
"""Validate the high-confidence checklist for one Alibaba similar-draft run.

This utility has no browser or network capability.  It verifies JSON Schema,
artifact hashes, cross-bindings, stage-specific invariants, and emits a
deterministic receipt.  A PASS is only a local gate; it never means published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
SCHEMA_PATH = SKILL_ROOT / "references" / "schemas" / "next-product-checklist.schema.json"
STAGE_STEPS = {
    "pre_write": tuple(f"P{index:02d}" for index in range(0, 19)),
    "pre_save": tuple(f"P{index:02d}" for index in range(0, 24)),
    "post_save": tuple(f"P{index:02d}" for index in range(0, 28)),
}
PASS_STATE = {
    "pre_write": "READY_FOR_CONTROLLED_UI_FILL",
    "pre_save": "READY_FOR_VISIBLE_UI_SAVE_DRAFT",
    "post_save": "FINAL_REVIEW_READY",
}
FORBIDDEN_DISPOSITIONS = {"UNUSABLE_WASTE_RECORD", "UNRESOLVED", "BLOCKED"}
ZERO_SHA256 = "0" * 64
# Red-team status 2026-08-08: several artifact roles are not yet parsed into
# canonical semantics.  Keep every live stage fail-closed until those role
# validators and their negative tests are integrated.  Do not expose a CLI
# switch or environment override for this gate.
IMPLEMENTATION_READY = False


class ChecklistError(RuntimeError):
    """Raised when the checklist cannot even be parsed."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ChecklistError(f"file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ChecklistError(f"invalid JSON {path}: {exc}") from exc
    except (OSError, UnicodeError) as exc:
        raise ChecklistError(f"cannot read JSON {path}: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_utc(value: str, label: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        errors.append(f"{label}: invalid date-time: {exc}")
        return None


def _schema_errors(document: Any) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        return ["jsonschema runtime is unavailable; use the bundled offline wheelhouse"]

    schema = _read_json(SCHEMA_PATH)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    result: list[str] = []
    for error in sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        result.append(f"schema:{location}: {error.message}")
    return result


def _walk_artifacts(value: Any, location: str = "$") -> Iterable[tuple[str, dict[str, str]]]:
    if isinstance(value, dict):
        if set(value) == {"path", "bytes", "sha256"} and isinstance(value.get("path"), str):
            yield location, value
            return
        for key, child in value.items():
            yield from _walk_artifacts(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_artifacts(child, f"{location}[{index}]")


def _verify_artifacts(document: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for location, artifact in _walk_artifacts(document):
        raw_path = artifact["path"]
        expected = artifact["sha256"].lower()
        key = (raw_path, expected)
        if key in seen:
            continue
        seen.add(key)

        path = Path(raw_path)
        if raw_path == "REPLACE" or expected == ZERO_SHA256:
            errors.append(f"{location}: unresolved placeholder artifact")
            continue
        if not path.is_absolute():
            errors.append(f"{location}: artifact path must be absolute: {raw_path}")
            continue
        if not path.is_file():
            errors.append(f"{location}: artifact is not a file: {raw_path}")
            continue
        actual = _sha256_file(path)
        if actual.lower() != expected:
            errors.append(f"{location}: SHA-256 mismatch for {raw_path}; expected {expected}, got {actual}")
            continue
        if path.stat().st_size != artifact["bytes"]:
            errors.append(
                f"{location}: byte-size mismatch for {raw_path}; expected {artifact['bytes']}, got {path.stat().st_size}"
            )
            continue
        verified.append({"path": str(path), "bytes": path.stat().st_size, "sha256": actual})
    return verified


def _expect(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _validate_step_set(document: dict[str, Any], stage: str, errors: list[str]) -> None:
    steps = document.get("steps", [])
    ids = [entry.get("id") for entry in steps if isinstance(entry, dict)]
    expected_all = [f"P{index:02d}" for index in range(28)]
    _expect(ids == expected_all, "steps: must contain P00..P27 exactly once and in order", errors)
    by_id = {entry.get("id"): entry for entry in steps if isinstance(entry, dict)}
    for step_id in STAGE_STEPS[stage]:
        _expect(by_id.get(step_id, {}).get("status") == "PASS", f"steps.{step_id}: must be PASS for {stage}", errors)
        _expect(bool(by_id.get(step_id, {}).get("evidence")), f"steps.{step_id}: PASS requires evidence", errors)


def _validate_action_plan_file(document: dict[str, Any], errors: list[str]) -> None:
    artifact = document["action_plan"]["artifact"]
    path = Path(artifact["path"])
    if not path.is_file() or artifact["sha256"] == ZERO_SHA256:
        return
    try:
        plan = _read_json(path)
    except ChecklistError as exc:
        errors.append(f"action_plan.artifact: {exc}")
        return
    target = plan.get("target", {}) if isinstance(plan, dict) else {}
    _expect(target.get("run_id") == document["run_id"], "action plan file: target.run_id mismatch", errors)
    _expect(target.get("model") == document["target_model"], "action plan file: target.model mismatch", errors)
    _expect(
        str(target.get("template_product_id")) == document["template_product_id"],
        "action plan file: target.template_product_id mismatch",
        errors,
    )
    _expect(target.get("mode") == "SAVE_DRAFT_ONLY", "action plan file: mode must be SAVE_DRAFT_ONLY", errors)
    _expect(target.get("maximum_draft_save_count") == 1, "action plan file: maximum_draft_save_count must be 1", errors)
    quality = plan.get("quality", {}) if isinstance(plan, dict) else {}
    _expect(quality.get("publish_ready") is False, "action plan file: publish_ready must be false", errors)
    _expect(quality.get("final_write_count_limit") == 0, "action plan file: final_write_count_limit must be 0", errors)


def _validate_common(document: dict[str, Any], now: datetime, errors: list[str]) -> None:
    _expect(document["run_kind"] == "CREATE_SIMILAR_DRAFT", "run_kind must be CREATE_SIMILAR_DRAFT", errors)
    _expect(document["mode"] == "save_draft", "mode must be save_draft", errors)
    _expect(not document["unresolved_p0"], "unresolved_p0 must be empty", errors)

    policy = document["policy"]
    _expect(policy["target_disposition"] not in FORBIDDEN_DISPOSITIONS, "target disposition is not eligible", errors)
    _expect(policy["template_disposition"] not in FORBIDDEN_DISPOSITIONS, "template disposition is not eligible", errors)
    _expect(policy["target_publish_draft"] is True, "policy must allow target draft publication", errors)
    _expect(policy["target_external_write_enabled"] is True, "policy must allow the scoped external draft write", errors)
    _expect(policy["target_allow_rag"] is True, "target facts must be eligible for approved retrieval", errors)
    _expect(policy["template_allowed_as_ui_skeleton"] is True, "template is not approved as a UI skeleton", errors)

    source = document["source_freeze"]
    _expect(source["all_inputs_hashed"] is True, "source_freeze.all_inputs_hashed must be true", errors)
    _expect(source["fact_conflicts_resolved"] is True, "source_freeze.fact_conflicts_resolved must be true", errors)

    schema = document["dynamic_schema"]
    _expect(schema["current_for_account_and_category"] is True, "dynamic schema is not bound to current account/category", errors)
    captured = _parse_utc(schema["captured_at_utc"], "dynamic_schema.captured_at_utc", errors)
    if captured:
        age_minutes = (now - captured).total_seconds() / 60
        _expect(age_minutes >= -5, "dynamic schema capture time is implausibly in the future", errors)
        _expect(age_minutes <= schema["max_age_minutes"], "dynamic schema is stale", errors)

    plan = document["action_plan"]
    _expect(plan["run_id"] == document["run_id"], "action_plan.run_id mismatch", errors)
    _expect(plan["target_model"] == document["target_model"], "action_plan.target_model mismatch", errors)
    _expect(plan["template_product_id"] == document["template_product_id"], "action_plan.template_product_id mismatch", errors)
    _expect(plan["save_draft_operations"] == 1, "action plan must contain exactly one draft-save operation", errors)
    _expect(plan["final_write_operations"] == 0, "action plan contains final-write operations", errors)

    plan_sha = plan["artifact"]["sha256"].lower()
    write = document["approvals"]["write"]
    expires = _parse_utc(write["expires_at_utc"], "approvals.write.expires_at_utc", errors)
    if expires:
        _expect(expires > now, "draft write approval is expired", errors)

    probe = document["probe"]
    _expect(probe["template_product_id"] == document["template_product_id"], "probe template_product_id mismatch", errors)
    _expect(probe["request_count"] >= 1, "probe must contain at least one captured request", errors)
    _expect(probe["write_request_count"] == 0, "probe observed a write request", errors)
    _expect(probe["final_write_request_count"] == 0, "probe observed a final-write request", errors)

    guard = document["endpoint_guard"]
    _expect(guard["unknown_writes_default"] == "BLOCK", "unknown writes must default to BLOCK", errors)
    _expect(guard["draft_write_mode"] == "VISIBLE_UI_SINGLE_SAVE_ONLY", "draft write mode is too broad", errors)
    _expect(guard["final_write_enabled"] is False, "final write must be permanently disabled", errors)
    _expect(guard["final_write_double_blocked"] is True, "final write must have two independent blocks", errors)
    _expect(guard["bound_action_plan_sha256"].lower() == plan_sha, "endpoint guard action-plan binding mismatch", errors)
    _expect(
        guard["bound_probe_sha256"].lower() == probe["artifact"]["sha256"].lower(),
        "endpoint guard probe binding mismatch",
        errors,
    )
    _expect(document["baseline"]["target_duplicate_count"] == 0, "an existing target draft/product was detected", errors)
    _expect(document["save"]["final_write_request_count"] == 0, "final-write request count is non-zero", errors)

    _validate_action_plan_file(document, errors)


def _validate_pre_write(document: dict[str, Any], errors: list[str]) -> None:
    save = document["save"]
    _expect(save["attempt_count"] == 0, "pre_write requires zero save attempts", errors)
    _expect(save["draft_id"] is None, "pre_write must not already have a draft_id", errors)
    _expect(save["ui_receipt"] is None and save["network_receipt"] is None, "pre_write has save receipts", errors)


def _validate_filled_state(document: dict[str, Any], errors: list[str]) -> None:
    execution = document["execution"]
    _expect(execution["fill_receipt"] is not None, "filled state requires a fill receipt", errors)
    plan_fields = set(document["action_plan"]["approved_field_ids"])
    approved_fields = set(execution["approved_field_ids"])
    readback_fields = set(execution["readback_field_ids"])
    _expect(approved_fields == plan_fields, "execution approved-field set differs from action plan", errors)
    _expect(readback_fields == approved_fields, "readback-field set must exactly equal the approved-field set", errors)
    required = set(document["dynamic_schema"]["required_field_ids"])
    _expect(required <= readback_fields, "one or more current-schema required fields lack readback evidence", errors)
    _expect(execution["template_residue_count"] == 0, "template residue remains", errors)
    _expect(execution["non_target_diff_count"] == 0, "non-target fields changed", errors)
    _expect(execution["all_required_fields_valid"] is True, "current required fields are not all valid", errors)
    _expect(execution["asset_hashes_match"] is True, "uploaded asset hashes do not match approvals", errors)


def _validate_pre_save(document: dict[str, Any], errors: list[str]) -> None:
    _validate_filled_state(document, errors)
    _validate_pre_write(document, errors)


def _validate_post_save(document: dict[str, Any], errors: list[str]) -> None:
    _validate_filled_state(document, errors)
    save = document["save"]
    _expect(save["attempt_count"] == 1, "post_save requires exactly one save attempt", errors)
    _expect(save["ui_receipt"] is not None, "post_save requires the visible-UI receipt", errors)
    _expect(save["network_receipt"] is not None, "post_save requires the network receipt", errors)
    _expect(save["business_success"] is True, "draft save did not report business success", errors)
    _expect(save["business_error_count"] == 0, "draft save has business errors", errors)
    _expect(isinstance(save["draft_id"], str) and bool(save["draft_id"]), "post_save requires a draft_id", errors)

    post = document["post_save"]
    _expect(post["reopen_receipt"] is not None, "post_save requires reopen/readback evidence", errors)
    _expect(post["after_artifact"] is not None, "post_save requires an after-state artifact", errors)
    _expect(post["after_counts"] is not None, "post_save requires after counts", errors)
    _expect(post["persistence_match"] is True, "reopened draft does not match approved values", errors)
    _expect(post["new_target_draft_count"] == 1, "exactly one new target draft was not proven", errors)
    _expect(post["online_id_set_unchanged"] is True, "online product ID set changed", errors)
    _expect(post["pending_id_set_unchanged"] is True, "pending product ID set changed", errors)
    _expect(post["stopped_before_final_submit"] is True, "browser did not stop before final submission", errors)
    if post["after_counts"] is not None:
        before = document["baseline"]["counts"]
        after = post["after_counts"]
        _expect(after["online"] == before["online"], "online count changed", errors)
        _expect(after["pending"] == before["pending"], "pending count changed", errors)
        _expect(after["draft"] == before["draft"] + 1, "draft count did not increase by exactly one", errors)

    # The pre-save facts remain mandatory after the save, except the attempt/receipt assertions.


def validate(document: dict[str, Any], stage: str, now: datetime | None = None) -> dict[str, Any]:
    """Return a deterministic validation result without writing anything."""

    errors = _schema_errors(document)
    warnings: list[str] = []
    verified: list[dict[str, Any]] = []
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not errors:
        verified = _verify_artifacts(document, errors)
        _validate_step_set(document, stage, errors)
        _validate_common(document, now, errors)
        if stage == "pre_write":
            _validate_pre_write(document, errors)
        elif stage == "pre_save":
            _validate_pre_save(document, errors)
        elif stage == "post_save":
            _validate_post_save(document, errors)
        else:  # argparse prevents this; keep library use fail-closed.
            errors.append(f"unsupported stage: {stage}")

    if not IMPLEMENTATION_READY:
        errors.append(
            "implementation readiness is not certified: artifact-role semantic validators remain incomplete"
        )

    status = "PASS" if not errors else "BLOCKED"
    return {
        "schema_version": "sellerkit.alibaba.next-product-checklist-verification.v1",
        "stage": stage,
        "status": status,
        "next_state": PASS_STATE[stage] if status == "PASS" else f"BLOCKED_{stage.upper()}",
        "checklist_semantic_sha256": _canonical_sha256(document),
        "verified_artifact_count": len(verified),
        "verified_artifacts": sorted(verified, key=lambda item: item["path"].casefold()),
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "browser_or_network_executed": False,
        "final_write_enabled": False,
        "implementation_ready": IMPLEMENTATION_READY,
        "validator_sha256": _sha256_file(Path(__file__).resolve()),
        "checklist_schema_sha256": _sha256_file(SCHEMA_PATH),
        "validated_at_utc": now.isoformat().replace("+00:00", "Z"),
    }


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path = path.resolve()
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing receipt: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checklist", required=True, type=Path)
    parser.add_argument("--stage", required=True, choices=tuple(STAGE_STEPS))
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)

    if args.receipt and args.receipt.resolve().exists():
        result = {
            "schema_version": "sellerkit.alibaba.next-product-checklist-verification.v1",
            "stage": args.stage,
            "status": "BLOCKED",
            "next_state": f"BLOCKED_{args.stage.upper()}",
            "errors": [f"refusing to overwrite existing receipt: {args.receipt.resolve()}"],
            "warnings": [],
            "browser_or_network_executed": False,
            "final_write_enabled": False,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 2

    try:
        document = _read_json(args.checklist.resolve())
        if not isinstance(document, dict):
            raise ChecklistError("checklist root must be an object")
        result = validate(document, args.stage)
    except ChecklistError as exc:
        result = {
            "schema_version": "sellerkit.alibaba.next-product-checklist-verification.v1",
            "stage": args.stage,
            "status": "BLOCKED",
            "next_state": f"BLOCKED_{args.stage.upper()}",
            "errors": [str(exc)],
            "warnings": [],
            "browser_or_network_executed": False,
            "final_write_enabled": False,
        }

    output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    print(output)
    if args.receipt:
        try:
            _atomic_write_json(args.receipt.resolve(), result)
        except (OSError, FileExistsError) as exc:
            blocked = {
                "schema_version": "sellerkit.alibaba.next-product-checklist-verification.v1",
                "stage": args.stage,
                "status": "BLOCKED",
                "next_state": f"BLOCKED_{args.stage.upper()}",
                "errors": [f"receipt write failed: {exc}"],
                "warnings": [],
                "browser_or_network_executed": False,
                "final_write_enabled": False,
            }
            print(json.dumps(blocked, ensure_ascii=False, sort_keys=True, indent=2))
            return 2
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
