from __future__ import annotations

import json
import os
import uuid
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from draft_core import GuardError, canonical_json, sha256_bytes, utc_now
from validate_next_product_checklist import validate as validate_next_product_checklist


TRANSITIONS = {
    "PRECHECK": frozenset({"CANDIDATE_REVIEW_APPROVED"}),
    "CANDIDATE_REVIEW_APPROVED": frozenset({"TEMPLATE_PROBED"}),
    "TEMPLATE_PROBED": frozenset({"INHERITED_DATA_CLEARED"}),
    "INHERITED_DATA_CLEARED": frozenset({"FORM_FILLED"}),
    # A save attempt may only start after the independently recomputed
    # pre-save checklist has advanced the run to VALIDATED.  An unexpected
    # external draft must be reconciled by a separate recovery workflow; it
    # must never be represented by skipping this gate.
    "FORM_FILLED": frozenset({"VALIDATED"}),
    "VALIDATED": frozenset({"SAVE_ATTEMPTED"}),
    "SAVE_ATTEMPTED": frozenset({"DRAFT_SAVED"}),
    "DRAFT_SAVED": frozenset({"FINAL_REVIEW_READY"}),
}

ABORTABLE_NO_WRITE_STATES = frozenset({
    "PRECHECK",
    "CANDIDATE_REVIEW_APPROVED",
    "TEMPLATE_PROBED",
    "INHERITED_DATA_CLEARED",
    "FORM_FILLED",
    "VALIDATED",
})

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
TRANSITION_EVIDENCE_SCHEMA = SCHEMA_DIR / "checkpoint-transition-evidence.schema.json"
ROLE_REQUIREMENTS: dict[str, dict[str, str]] = {
    "APPROVAL_COMPILATION_RECEIPT": {"schema_version": "sellerkit.approval-compile-receipt.v1", "status": "PASS"},
    "PROBE_VERIFICATION_RECEIPT": {"schema_version": "sellerkit.browser_probe_receipt.v3", "status": "PASS_ZERO_WRITE"},
    "INHERITED_CLEAR_PRE_SAVE_RECEIPT": {"schema_version": "sellerkit.alibaba.pre-save-verification.v1", "status": "PASS_PRE_SAVE_NO_WRITE"},
    "FORM_FILL_PRE_SAVE_RECEIPT": {"schema_version": "sellerkit.alibaba.pre-save-verification.v1", "status": "PASS_PRE_SAVE_NO_WRITE"},
    "PRE_SAVE_CHECKLIST_RECEIPT": {"schema_version": "sellerkit.alibaba.next-product-checklist-verification.v1", "status": "PASS"},
    "PRE_SAVE_CHECKLIST_SOURCE": {"schema_version": "1.0.0"},
    "INVENTORY_DELTA_VERIFICATION_RECEIPT": {"schema_version": "sellerkit.alibaba.inventory-delta-verification.v1", "status": "PASS_EXACTLY_ONE_DRAFT_SAVED"},
    "DRAFT_SAVE_ATTEMPT_CLAIM_RECEIPT": {"schema_version": "sellerkit.alibaba.draft-save-attempt-claim-receipt.v1", "status": "ATTEMPT_CLAIMED_CHECKPOINT_COMMIT_PENDING"},
    "DRAFT_SAVE_ACTIVATION_RECEIPT": {"schema_version": "sellerkit.alibaba.draft-save-activation-receipt.v1", "status": "PASS_ONE_SHOT_DRAFT_SAVE_ACTIVATED"},
    "FILL_VERIFICATION_RECEIPT": {"schema_version": "sellerkit.fill_receipt_verification.v2", "status": "PASS"},
    "POST_SAVE_CHECKLIST_RECEIPT": {"schema_version": "sellerkit.alibaba.next-product-checklist-verification.v1", "status": "PASS"},
    "POST_SAVE_CHECKLIST_SOURCE": {"schema_version": "1.0.0"},
}
ROLE_SCHEMA_FILES: dict[str, str] = {
    "APPROVAL_COMPILATION_RECEIPT": "approval-compile-receipt.schema.json",
    "PROBE_VERIFICATION_RECEIPT": "probe-verification-receipt.schema.json",
    "INHERITED_CLEAR_PRE_SAVE_RECEIPT": "pre-save-verification-receipt.schema.json",
    "FORM_FILL_PRE_SAVE_RECEIPT": "pre-save-verification-receipt.schema.json",
    "PRE_SAVE_CHECKLIST_SOURCE": "next-product-checklist.schema.json",
    "PRE_SAVE_CHECKLIST_RECEIPT": "next-product-checklist-verification-receipt.schema.json",
    "DRAFT_SAVE_ATTEMPT_CLAIM_RECEIPT": "draft-save-attempt-claim-receipt.schema.json",
    "DRAFT_SAVE_ACTIVATION_RECEIPT": "draft-save-activation-receipt.schema.json",
    "INVENTORY_DELTA_VERIFICATION_RECEIPT": "inventory-delta-verification-receipt.schema.json",
    "FILL_VERIFICATION_RECEIPT": "fill-verification-receipt.schema.json",
    "POST_SAVE_CHECKLIST_SOURCE": "next-product-checklist.schema.json",
    "POST_SAVE_CHECKLIST_RECEIPT": "next-product-checklist-verification-receipt.schema.json",
}
STATE_REQUIRED_ROLES: dict[str, frozenset[str]] = {
    "CANDIDATE_REVIEW_APPROVED": frozenset({"APPROVAL_COMPILATION_RECEIPT"}),
    "TEMPLATE_PROBED": frozenset({"PROBE_VERIFICATION_RECEIPT"}),
    "INHERITED_DATA_CLEARED": frozenset({"INHERITED_CLEAR_PRE_SAVE_RECEIPT"}),
    "FORM_FILLED": frozenset({"FORM_FILL_PRE_SAVE_RECEIPT"}),
    "VALIDATED": frozenset({"PRE_SAVE_CHECKLIST_SOURCE", "PRE_SAVE_CHECKLIST_RECEIPT"}),
    "SAVE_ATTEMPTED": frozenset({"DRAFT_SAVE_ATTEMPT_CLAIM_RECEIPT"}),
    "DRAFT_SAVED": frozenset({"INVENTORY_DELTA_VERIFICATION_RECEIPT"}),
    "FINAL_REVIEW_READY": frozenset({"FILL_VERIFICATION_RECEIPT", "POST_SAVE_CHECKLIST_SOURCE", "POST_SAVE_CHECKLIST_RECEIPT"}),
}


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def checkpoint_hash(checkpoint: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(checkpoint).encode("utf-8"))


def _file_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.resolve(strict=True).read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise GuardError(f"{label} must be a JSON object")
    return document, payload


def _validate_transition_evidence(
    evidence_path: Path,
    current: dict[str, Any],
    new_state: str,
    *,
    draft_id: str | None,
) -> dict[str, Any]:
    bundle, payload = _file_object(evidence_path, "checkpoint transition evidence")
    schema = json.loads(TRANSITION_EVIDENCE_SCHEMA.read_text(encoding="utf-8"))
    validation = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(bundle),
        key=lambda item: list(item.absolute_path),
    )
    if validation:
        details = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
            for error in validation[:8]
        )
        raise GuardError(f"checkpoint transition evidence schema failed: {details}")
    body = copy.deepcopy(bundle)
    declared_body_hash = str(body.pop("body_sha256") or "").lower()
    if declared_body_hash != sha256_bytes(canonical_json(body).encode("utf-8")):
        raise GuardError("checkpoint transition evidence body_sha256 mismatch")
    for key, expected in (
        ("run_id", current.get("run_id")),
        ("target_model", current.get("target_model")),
        ("template_product_id", current.get("template_product_id")),
        ("new_state", new_state),
    ):
        if bundle.get(key) != expected:
            raise GuardError(f"checkpoint transition evidence {key} mismatch")

    required_roles = STATE_REQUIRED_ROLES[new_state]
    artifacts = bundle["artifacts"]
    roles = [str(row.get("role") or "") for row in artifacts]
    if set(roles) != set(required_roles) or len(roles) != len(set(roles)):
        raise GuardError(f"checkpoint transition roles must be exactly {sorted(required_roles)}")

    prior_bundle_hashes = {str(row.get("bundle_sha256") or "").lower() for row in current.get("evidence") or []}
    prior_artifact_hashes = {
        str(artifact.get("sha256") or "").lower()
        for row in current.get("evidence") or []
        for artifact in row.get("artifacts") or []
        if isinstance(artifact, dict)
    }
    bundle_hash = sha256_bytes(payload)
    if bundle_hash in prior_bundle_hashes:
        raise GuardError("checkpoint transition evidence bundle hash was already used")
    evidence_ids = {str(row.get("evidence_id") or "") for row in current.get("evidence") or []}
    if bundle["evidence_id"] in evidence_ids:
        raise GuardError("checkpoint transition evidence_id was already used")

    normalized_artifacts: list[dict[str, Any]] = []
    receipts_by_role: dict[str, dict[str, Any]] = {}
    observed_hashes: set[str] = set()
    for descriptor in artifacts:
        role = descriptor["role"]
        artifact_path = Path(descriptor["path"])
        if not artifact_path.is_absolute():
            raise GuardError(f"checkpoint artifact path is not absolute: {role}")
        receipt, receipt_payload = _file_object(artifact_path, f"checkpoint artifact {role}")
        digest = sha256_bytes(receipt_payload)
        if descriptor["sha256"] != digest or descriptor["bytes"] != len(receipt_payload):
            raise GuardError(f"checkpoint artifact bytes/hash mismatch: {role}")
        if digest in prior_artifact_hashes or digest in observed_hashes:
            raise GuardError(f"checkpoint artifact hash reuse is forbidden: {role}")
        observed_hashes.add(digest)
        requirement = ROLE_REQUIREMENTS[role]
        if receipt.get("schema_version") != requirement["schema_version"]:
            raise GuardError(f"checkpoint artifact is not the required passing validator receipt: {role}")
        if "status" in requirement and receipt.get("status") != requirement["status"]:
            raise GuardError(f"checkpoint artifact is not the required passing validator receipt: {role}")
        role_schema = json.loads((SCHEMA_DIR / ROLE_SCHEMA_FILES[role]).read_text(encoding="utf-8"))
        role_validation = sorted(
            Draft202012Validator(role_schema, format_checker=FormatChecker()).iter_errors(receipt),
            key=lambda item: list(item.absolute_path),
        )
        if role_validation:
            details = "; ".join(
                f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
                for error in role_validation[:8]
            )
            raise GuardError(f"checkpoint artifact schema failed for {role}: {details}")
        if receipt.get("errors") not in (None, []):
            raise GuardError(f"checkpoint artifact contains validator errors: {role}")
        for key in ("run_id", "target_model", "template_product_id"):
            if key in receipt and receipt.get(key) != current.get(key):
                raise GuardError(f"checkpoint artifact {role} {key} mismatch")
        if role == "PROBE_VERIFICATION_RECEIPT":
            if receipt.get("zero_write_confirmed") is not True or receipt.get("execution_authorized") is not True:
                raise GuardError("probe checkpoint receipt lacks execution authorization")
            if (
                receipt.get("production_execution_authorized") is not True
                or receipt.get("adapter_production_execution_ready") is not True
                or receipt.get("production_authorization_errors") != []
            ):
                raise GuardError("probe checkpoint receipt lacks production execution authorization")
            if receipt.get("authorization_errors") != [] or not receipt.get("request_classifications"):
                raise GuardError("probe checkpoint receipt lacks exact request authorization evidence")
            if any(row.get("decision") != "ALLOW" for row in receipt["request_classifications"]):
                raise GuardError("probe checkpoint receipt contains a non-ALLOW decision")
        if role in {"INHERITED_CLEAR_PRE_SAVE_RECEIPT", "FORM_FILL_PRE_SAVE_RECEIPT"}:
            if receipt.get("draft_save_count") != 0 or receipt.get("final_write_count") != 0:
                raise GuardError(f"pre-save checkpoint receipt has write signals: {role}")
        if role == "PRE_SAVE_CHECKLIST_RECEIPT":
            if receipt.get("stage") != "pre_save" or receipt.get("implementation_ready") is not True:
                raise GuardError("pre-save checklist is not implementation-ready")
        if role == "INVENTORY_DELTA_VERIFICATION_RECEIPT":
            if receipt.get("trust_status") != "LIVE_ATTESTED":
                raise GuardError("inventory checkpoint receipt is not live-attested")
            if receipt.get("saved_draft_id") != draft_id:
                raise GuardError("inventory checkpoint receipt draft_id mismatch")
        if role == "DRAFT_SAVE_ATTEMPT_CLAIM_RECEIPT":
            attempt = receipt.get("attempt_transition") or {}
            if (
                attempt.get("before") != 0
                or attempt.get("after") != 1
                or attempt.get("maximum") != 1
                or attempt.get("retry_allowed") is not False
            ):
                raise GuardError("save-attempt checkpoint receipt does not prove burned attempt 0 -> 1")
            if receipt.get("release_authorized") is not False or receipt.get("final_write_enabled") is not False:
                raise GuardError("save-attempt claim must not authorize request release or final write")
        if role == "FILL_VERIFICATION_RECEIPT":
            if receipt.get("draft_save_count") != 1 or receipt.get("final_write_count") != 0:
                raise GuardError("fill checkpoint receipt does not prove one draft and zero final writes")
        if role == "POST_SAVE_CHECKLIST_RECEIPT":
            if receipt.get("stage") != "post_save" or receipt.get("implementation_ready") is not True:
                raise GuardError("post-save checklist is not implementation-ready")
        receipts_by_role[role] = receipt
        normalized_artifacts.append({
            "role": role,
            "path": str(artifact_path.resolve(strict=True)),
            "bytes": len(receipt_payload),
            "sha256": digest,
            "schema_version": receipt["schema_version"],
            "status": receipt.get("status", "SOURCE_INPUT"),
        })

    for stage, source_role, receipt_role in (
        ("pre_save", "PRE_SAVE_CHECKLIST_SOURCE", "PRE_SAVE_CHECKLIST_RECEIPT"),
        ("post_save", "POST_SAVE_CHECKLIST_SOURCE", "POST_SAVE_CHECKLIST_RECEIPT"),
    ):
        if source_role not in receipts_by_role and receipt_role not in receipts_by_role:
            continue
        source = receipts_by_role.get(source_role)
        claimed = receipts_by_role.get(receipt_role)
        if source is None or claimed is None:
            raise GuardError(f"{stage} checklist source/receipt pair is incomplete")
        try:
            validated_at = datetime.fromisoformat(str(claimed["validated_at_utc"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError) as exc:
            raise GuardError(f"{stage} checklist validated_at_utc is invalid") from exc
        recomputed = validate_next_product_checklist(source, stage, now=validated_at)
        if canonical_json(recomputed) != canonical_json(claimed):
            raise GuardError(f"{stage} checklist receipt does not match validator recomputation")
        if recomputed.get("status") != "PASS" or recomputed.get("implementation_ready") is not True:
            raise GuardError(f"{stage} checklist validator is not implementation-ready")

    if new_state == "FINAL_REVIEW_READY":
        prior_roles = {
            str(role)
            for row in current.get("evidence") or []
            for role in row.get("roles") or []
        }
        if "INVENTORY_DELTA_VERIFICATION_RECEIPT" not in prior_roles:
            raise GuardError("FINAL_REVIEW_READY requires a prior live inventory-delta validator receipt")

    return {
        "state": new_state,
        "evidence_id": bundle["evidence_id"],
        "bundle_path": str(evidence_path.resolve(strict=True)),
        "bundle_sha256": bundle_hash,
        "roles": sorted(required_roles),
        "artifacts": sorted(normalized_artifacts, key=lambda row: row["role"]),
    }


def acquire_model_lock(lock_path: Path, target_model: str, run_id: str) -> dict[str, Any]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "sellerkit.model_lock.v1",
        "target_model": target_model,
        "run_id": run_id,
        "pid": os.getpid(),
        "created_at": utc_now(),
    }
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise GuardError(f"RUN_LOCKED: {lock_path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return payload


def release_model_lock(lock_path: Path, run_id: str) -> None:
    if not lock_path.exists():
        return
    current = json.loads(lock_path.read_text(encoding="utf-8"))
    if current.get("run_id") != run_id:
        raise GuardError("lock ownership mismatch")
    lock_path.unlink()


@contextmanager
def checkpoint_mutation_guard(path: Path):
    """Hold an OS-level fail-closed lock across checkpoint read/validate/commit.

    The prior compare-and-swap checked a revision/hash and then used
    ``os.replace`` without excluding a second writer.  Two processes could
    both validate the same revision and the later replace would hide the first
    transition.  O_EXCL on one lock file is the cross-process serialization
    primitive here.  A hard process crash leaves the lock behind, which is a
    deliberate safe failure requiring reconciliation.

    The yielded token lets an irreversible caller (draft-attempt ledger claim)
    retain the lock if a later step fails.  Ordinary validation failures release
    it so corrected evidence can be retried.
    """

    checkpoint_path = path.resolve(strict=True)
    lock_path = checkpoint_path.with_name(f"{checkpoint_path.name}.mutation.lock")
    token: dict[str, Any] = {"retain_on_error": False, "lock_path": str(lock_path)}
    payload = {
        "schema_version": "sellerkit.checkpoint_mutation_lock.v1",
        "checkpoint_path": str(checkpoint_path),
        "pid": os.getpid(),
        "created_at": utc_now(),
        "state": "HELD",
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise GuardError(f"CHECKPOINT_MUTATION_LOCKED: {lock_path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            yield token
        except BaseException:
            if token.get("retain_on_error"):
                # Existence is the safety property.  Best-effort metadata helps
                # an operator understand why automated recovery is forbidden.
                try:
                    retained = dict(payload)
                    retained["state"] = "HELD_FAIL_CLOSED_IRREVERSIBLE_ATTEMPT"
                    retained["retained_at"] = utc_now()
                    lock_path.write_text(
                        json.dumps(retained, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            else:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        else:
            try:
                lock_path.unlink()
            except FileNotFoundError as exc:
                raise GuardError("checkpoint mutation lock disappeared before commit completed") from exc
    except BaseException:
        # If setup failed before the caller received the token, do not leave a
        # spurious lock.  Once retain_on_error is set, the inner handler owns it.
        if not token.get("retain_on_error") and lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass
        raise


def initialize_checkpoint(
    run_dir: Path,
    target_model: str,
    template_product_id: str,
    input_hashes: dict[str, str],
    *,
    run_id: str | None = None,
    lock_root: Path | None = None,
) -> dict[str, Any]:
    checkpoint_path = run_dir / "checkpoint.json"
    if checkpoint_path.exists():
        raise GuardError(f"duplicate run checkpoint: {checkpoint_path}")
    resolved_run_id = str(run_id or "").strip()
    if not resolved_run_id:
        resolved_run_id = f"{target_model.lower()}-{uuid.uuid4().hex[:12]}"
    if len(resolved_run_id) > 300 or "\r" in resolved_run_id or "\n" in resolved_run_id:
        raise GuardError("run_id is invalid")
    resolved_lock_root = (
        lock_root.resolve()
        if lock_root is not None
        else (run_dir.parent / "locks").resolve()
    )
    resolved_run_dir = run_dir.resolve()
    if lock_root is not None and (
        resolved_lock_root == resolved_run_dir
        or resolved_lock_root.is_relative_to(resolved_run_dir)
    ):
        raise GuardError("lock_root must be shared outside the run-specific directory")
    lock_path = resolved_lock_root / f"{target_model.upper()}.lock.json"
    acquire_model_lock(lock_path, target_model, resolved_run_id)
    checkpoint = {
        "schema_version": "sellerkit.similar_draft_checkpoint.v2",
        "run_id": resolved_run_id,
        "revision": 0,
        "previous_checkpoint_sha256": None,
        "state": "PRECHECK",
        "run_status": "ACTIVE",
        "target_model": target_model,
        "template_product_id": str(template_product_id),
        "input_hashes": input_hashes,
        "draft_id": None,
        "save_attempt_count": 0,
        "draft_save_count": 0,
        "final_write_count": 0,
        "submit_triggered": False,
        "publish_triggered": False,
        "external_publish_status": "NOT_ATTEMPTED",
        "validation_status": "NOT_VALIDATED",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "lock_path": str(lock_path),
        "lock_state": "HELD",
        "evidence": [],
    }
    try:
        atomic_write_json(checkpoint_path, checkpoint)
    except Exception:
        release_model_lock(lock_path, resolved_run_id)
        raise
    return checkpoint


def _transition_checkpoint_locked(
    path: Path,
    expected_revision: int,
    expected_sha256: str,
    new_state: str,
    evidence_path: Path,
    *,
    draft_id: str | None = None,
) -> dict[str, Any]:
    current = json.loads(path.read_text(encoding="utf-8"))
    if current.get("schema_version") != "sellerkit.similar_draft_checkpoint.v2":
        raise GuardError("unsupported checkpoint schema; start a new v2 run instead of mutating legacy evidence")
    observed_hash = checkpoint_hash(current)
    if current.get("revision") != expected_revision:
        raise GuardError("checkpoint revision mismatch")
    if observed_hash != expected_sha256:
        raise GuardError("checkpoint hash mismatch")
    allowed_states = TRANSITIONS.get(str(current.get("state")), frozenset())
    if new_state not in allowed_states:
        raise GuardError(f"invalid transition {current.get('state')} -> {new_state}")
    if current.get("state") == "DRAFT_SAVED" and current.get("validation_status") != "VALIDATED":
        raise GuardError("review-required draft cannot advance to FINAL_REVIEW_READY")
    normalized_draft_id = str(draft_id or "").strip() or None
    if new_state == "DRAFT_SAVED" and normalized_draft_id is None:
        raise GuardError("draft_id is required when recording DRAFT_SAVED")
    if new_state != "DRAFT_SAVED" and normalized_draft_id is not None:
        raise GuardError("draft_id may only be supplied for DRAFT_SAVED")
    normalized_evidence = _validate_transition_evidence(
        evidence_path,
        current,
        new_state,
        draft_id=normalized_draft_id,
    )
    updated = dict(current)
    updated["previous_checkpoint_sha256"] = observed_hash
    updated["revision"] = expected_revision + 1
    updated["state"] = new_state
    updated["updated_at"] = utc_now()
    updated.setdefault("evidence", []).append(normalized_evidence)
    terminal = False
    if new_state == "VALIDATED":
        updated["validation_status"] = "VALIDATED"
    if new_state == "SAVE_ATTEMPTED":
        updated["save_attempt_count"] = int(updated.get("save_attempt_count") or 0) + 1
        updated["run_status"] = "SAVE_ATTEMPTED_UNCONFIRMED"
        updated["lock_state"] = "HELD_SAVE_ATTEMPT_UNCONFIRMED"
    if new_state == "DRAFT_SAVED":
        if int(current.get("save_attempt_count") or 0) != 1:
            raise GuardError("DRAFT_SAVED requires exactly one prior burned save attempt")
        updated["draft_save_count"] = int(updated.get("draft_save_count") or 0) + 1
        updated["draft_id"] = normalized_draft_id
        if current.get("validation_status") != "VALIDATED":
            updated["validation_status"] = "REVIEW_REQUIRED"
            updated["run_status"] = "FAILED_DRAFT_PRESERVED"
            # A real draft now exists.  Keep the model lock so a retry cannot
            # silently create a second draft for the same target model.  A
            # future recovery/abandonment workflow must resolve this lock
            # explicitly; failure is not an authority to start over.
            updated["lock_state"] = "HELD_FAILED_DRAFT_PRESERVED"
        else:
            updated["run_status"] = "ACTIVE"
            updated["lock_state"] = "HELD"
    if new_state == "FINAL_REVIEW_READY":
        updated["run_status"] = "DRAFT_SAVED_FINAL_REVIEW_READY"
        updated["lock_state"] = "RELEASED"
        terminal = True
    if (
        updated["save_attempt_count"] > 1
        or updated["draft_save_count"] > 1
        or updated.get("final_write_count") != 0
        or updated.get("submit_triggered") is not False
        or updated.get("publish_triggered") is not False
        or updated.get("external_publish_status") != "NOT_ATTEMPTED"
    ):
        raise GuardError("draft/final-write invariant failed")
    atomic_write_json(path, updated)
    if terminal:
        release_model_lock(Path(str(updated["lock_path"])), str(updated["run_id"]))
    return updated


def transition_checkpoint(
    path: Path,
    expected_revision: int,
    expected_sha256: str,
    new_state: str,
    evidence_path: Path,
    *,
    draft_id: str | None = None,
) -> dict[str, Any]:
    with checkpoint_mutation_guard(path):
        return _transition_checkpoint_locked(
            path,
            expected_revision,
            expected_sha256,
            new_state,
            evidence_path,
            draft_id=draft_id,
        )


def transition_checkpoint_under_guard(
    path: Path,
    expected_revision: int,
    expected_sha256: str,
    new_state: str,
    evidence_path: Path,
    *,
    mutation_token: dict[str, Any],
    draft_id: str | None = None,
) -> dict[str, Any]:
    """Transition while a caller already owns ``checkpoint_mutation_guard``."""

    checkpoint_path = path.resolve(strict=True)
    expected_lock = checkpoint_path.with_name(f"{checkpoint_path.name}.mutation.lock")
    token_path = Path(str(mutation_token.get("lock_path") or ""))
    if token_path != expected_lock or not expected_lock.is_file():
        raise GuardError("checkpoint mutation token is not bound to the current checkpoint")
    try:
        lock_record = json.loads(expected_lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardError("checkpoint mutation lock record is unreadable") from exc
    if (
        lock_record.get("checkpoint_path") != str(checkpoint_path)
        or lock_record.get("pid") != os.getpid()
        or lock_record.get("state") != "HELD"
    ):
        raise GuardError("checkpoint mutation lock ownership mismatch")
    return _transition_checkpoint_locked(
        checkpoint_path,
        expected_revision,
        expected_sha256,
        new_state,
        evidence_path,
        draft_id=draft_id,
    )


def _abort_checkpoint_no_write_locked(
    path: Path,
    expected_revision: int,
    expected_sha256: str,
    reason: str,
) -> dict[str, Any]:
    """Terminally stop a run only while every external-write signal is zero.

    The checkpoint is committed before the model lock is removed.  A crash in
    that narrow interval therefore leaves a stale lock (safe/fail-closed), and
    repeating this command with the current CAS values finishes the cleanup.
    It is deliberately unavailable once SAVE_ATTEMPTED has been reached.
    """

    current = json.loads(path.read_text(encoding="utf-8"))
    if current.get("schema_version") != "sellerkit.similar_draft_checkpoint.v2":
        raise GuardError("unsupported checkpoint schema; cannot abort legacy evidence")
    observed_hash = checkpoint_hash(current)
    if current.get("revision") != expected_revision:
        raise GuardError("checkpoint revision mismatch")
    if observed_hash != expected_sha256:
        raise GuardError("checkpoint hash mismatch")

    normalized_reason = " ".join(str(reason or "").split())
    if not normalized_reason or len(normalized_reason) > 500:
        raise GuardError("abort reason must contain 1-500 normalized characters")

    invariant_errors: list[str] = []
    if current.get("draft_id") is not None:
        invariant_errors.append("draft_id is present")
    for field in ("save_attempt_count", "draft_save_count", "final_write_count"):
        if int(current.get(field) or 0) != 0:
            invariant_errors.append(f"{field} is not zero")
    for field in ("submit_triggered", "publish_triggered"):
        if current.get(field) is not False:
            invariant_errors.append(f"{field} is not false")
    if current.get("external_publish_status") != "NOT_ATTEMPTED":
        invariant_errors.append("external_publish_status is not NOT_ATTEMPTED")
    if invariant_errors:
        raise GuardError("abort-no-write invariant failed: " + "; ".join(invariant_errors))
    run_dir = path.resolve(strict=True).parent
    ledger_dir = run_dir / "draft-save-attempt-ledger"
    if ledger_dir.exists() and any(item.is_file() for item in ledger_dir.iterdir()):
        raise GuardError("abort-no-write is forbidden because a draft-save attempt ledger exists")
    if any(run_dir.glob("*.attempt-claim.json")):
        raise GuardError("abort-no-write is forbidden because a draft-save attempt claim exists")

    state = str(current.get("state") or "")
    lock_path = Path(str(current.get("lock_path") or ""))
    run_id = str(current.get("run_id") or "")
    if state == "ABORTED_NO_WRITE":
        if current.get("run_status") != "ABORTED_NO_WRITE" or current.get("lock_state") != "RELEASED":
            raise GuardError("aborted checkpoint has inconsistent terminal status")
        # Recovery for a process crash after the checkpoint commit and before
        # unlinking the lock.  Ownership is checked by release_model_lock.
        release_model_lock(lock_path, run_id)
        return current

    if state not in ABORTABLE_NO_WRITE_STATES:
        raise GuardError(f"abort-no-write is forbidden from state {state}")
    if current.get("run_status") != "ACTIVE" or current.get("lock_state") != "HELD":
        raise GuardError("abort-no-write requires an active run with its model lock held")

    updated = dict(current)
    updated["previous_checkpoint_sha256"] = observed_hash
    updated["revision"] = expected_revision + 1
    updated["state"] = "ABORTED_NO_WRITE"
    updated["run_status"] = "ABORTED_NO_WRITE"
    updated["updated_at"] = utc_now()
    updated["aborted_at"] = updated["updated_at"]
    updated["abort_reason"] = normalized_reason
    updated["lock_state"] = "RELEASED"
    atomic_write_json(path, updated)
    release_model_lock(lock_path, run_id)
    return updated


def abort_checkpoint_no_write(
    path: Path,
    expected_revision: int,
    expected_sha256: str,
    reason: str,
) -> dict[str, Any]:
    # Recovery is intentionally limited to the already committed terminal
    # ABORTED_NO_WRITE state.  A process can crash after the checkpoint commit
    # but before the model/mutation locks are removed.  Requiring the normal
    # mutation guard here would make that safe cleanup permanently unreachable.
    # No non-terminal or irreversible-attempt lock may use this path.
    current = json.loads(path.read_text(encoding="utf-8"))
    if current.get("state") == "ABORTED_NO_WRITE":
        if current.get("revision") != expected_revision or checkpoint_hash(current) != expected_sha256:
            raise GuardError("checkpoint CAS mismatch during terminal abort cleanup")
        mutation_lock = path.resolve(strict=True).with_name(f"{path.name}.mutation.lock")
        mutation_lock_present = mutation_lock.exists()
        if mutation_lock_present:
            try:
                record = json.loads(mutation_lock.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise GuardError("terminal abort mutation lock is unreadable") from exc
            if (
                record.get("schema_version") != "sellerkit.checkpoint_mutation_lock.v1"
                or record.get("checkpoint_path") != str(path.resolve(strict=True))
                or record.get("state") != "HELD"
            ):
                raise GuardError(
                    "terminal abort cleanup refuses a nonstandard or irreversible-attempt mutation lock"
                )
        # The inner validator checks every zero-write invariant and releases
        # the model lock.  Do not remove the stale mutation lock until that
        # validation and cleanup have both succeeded; a damaged terminal
        # checkpoint must retain both fail-closed barriers.
        recovered = _abort_checkpoint_no_write_locked(
            path,
            expected_revision,
            expected_sha256,
            reason,
        )
        if mutation_lock_present:
            try:
                mutation_lock.unlink()
            except FileNotFoundError:
                pass
        return recovered
    with checkpoint_mutation_guard(path):
        return _abort_checkpoint_no_write_locked(
            path,
            expected_revision,
            expected_sha256,
            reason,
        )
