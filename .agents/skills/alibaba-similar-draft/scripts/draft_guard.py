# Final publish is forbidden; incomplete evidence must fail closed.
# 最终发布禁止；证据不完整必须阻断。
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

from draft_core import (
    DEFAULT_CUTOFF,
    HISTORICAL_DISPOSITION,
    READ_METHODS,
    REJECTED_DISPOSITIONS,
    WRITE_METHODS,
    GuardError,
    build_asset_candidates,
    canonical_json,
    classify_endpoint,
    extract_candidate,
    finalize_probe_verification_gate,
    inspect_source_inventory,
    load_official_model_configurations,
    load_official_product,
    load_pair_rank,
    load_visual_scores,
    official_field_candidates,
    parse_iso_date,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    utc_now,
    validate_approved_manifest,
    verify_fill_receipt,
    verify_overlay,
    verify_probe,
)
from state_store import (
    abort_checkpoint_no_write,
    atomic_write_json,
    checkpoint_hash,
    initialize_checkpoint,
    transition_checkpoint,
)
from runtime_config import resolve_workspace
from dynamic_schema_gate import validate_dynamic_schema_capture


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_POLICY = SKILL_DIR / "references" / "endpoint-policy.v1.json"
DEFAULT_SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
DEFAULT_WORKSPACE_PROFILE = SKILL_DIR / "profiles" / "example-profile.v1.json"

PREPARE_FILENAMES = (
    "candidate_review.json",
    "approved_source_manifest.template.json",
    "asset_manifest.candidates.json",
    "run_spec.json",
    "evidence_manifest.json",
)

COMMERCIAL_FIELDS = (
    "price",
    "minimum_order_quantity",
    "inventory",
    "lead_time",
    "trade_terms",
    "package_weight",
    "package_dimensions",
    "sample_policy",
    "after_sales_policy",
    "certificates",
    "warranty",
    "shipping_terms",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INPUT_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuardError(f"{label} must be a JSON object")
    return value


def _load_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise GuardError(f"missing {label}: {path}")
    return _require_object(read_json(path), label)


def _json_schema_errors(document: dict[str, Any], schema_name: str) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["jsonschema runtime is unavailable; preflight fails closed"]
    schema_path = DEFAULT_SCHEMA_DIR / schema_name
    try:
        schema = read_json(schema_path)
        validator = Draft202012Validator(schema)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"cannot load canonical schema {schema_name}: {exc}"]
    errors: list[str] = []
    for error in sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"schema {schema_name} at {location}: {error.message}")
    return errors


def _load_policy(path: Path) -> dict[str, Any]:
    policy = _load_object(path, "endpoint policy")
    if policy.get("schema_version") != "alibaba.endpoint-policy.v1":
        raise GuardError("unsupported endpoint policy schema")
    if policy.get("default_decision") != "DENY":
        raise GuardError("endpoint policy default_decision must be DENY")
    if policy.get("policy_mode") != "FAIL_CLOSED_AUDIT_ONLY":
        raise GuardError("endpoint policy must remain FAIL_CLOSED_AUDIT_ONLY")
    if policy.get("final_write_enabled") is not False:
        raise GuardError("endpoint policy final_write_enabled must be false")
    for rule in policy.get("rules") or []:
        if not isinstance(rule, dict):
            raise GuardError("endpoint policy contains a non-object rule")
        if rule.get("decision") == "ALLOW" or rule.get("evidence_state") == "LIVE_VERIFIED_ACTIVE":
            raise GuardError("global endpoint policy must contain zero active ALLOW rules")
    return policy


def _write_new_json(path: Path, value: Any) -> None:
    if path.exists():
        raise GuardError(f"refusing to overwrite evidence: {path}")
    atomic_write_json(path, value)


def _print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def _file_evidence(path: Path, role: str, *, required: bool = True) -> dict[str, Any]:
    path = path.resolve()
    exists = path.is_file()
    if required and not exists:
        raise GuardError(f"missing required {role}: {path}")
    return {
        "role": role,
        "path": str(path),
        "exists": exists,
        "bytes": path.stat().st_size if exists else None,
        "sha256": sha256_file(path) if exists else None,
    }


def _load_workspace_profile(path: Path) -> dict[str, Any]:
    profile = _load_object(path, "workspace profile")
    if profile.get("schema_version") != "sellerkit.alibaba-workspace-profile.v1":
        raise GuardError("unsupported workspace profile schema")
    policy = profile.get("policy") or {}
    if (
        policy.get("template_is_form_structure_only") is not True
        or policy.get("historical_records_are_review_only") is not True
        or policy.get("direct_endpoint_write_enabled") is not False
        or policy.get("final_write_enabled") is not False
        or policy.get("external_publish_status") != "NOT_ATTEMPTED"
    ):
        raise GuardError("workspace profile write policy is unsafe")
    cutoff = parse_iso_date(profile.get("cutoff"))
    if cutoff is None:
        raise GuardError("workspace profile cutoff is invalid")
    if int(profile.get("expected_catalog_count") or 0) < 1:
        raise GuardError("workspace profile expected_catalog_count is invalid")
    data_files = profile.get("data_files") or {}
    for key in ("official_current", "historical_sanitized", "visual_scores", "full_match_pairs_glob"):
        value = str(data_files.get(key) or "")
        candidate = Path(value)
        if not value or candidate.is_absolute() or ".." in candidate.parts:
            raise GuardError(f"workspace profile path is unsafe: {key}")
    bindings = profile.get("content_bindings") or {}
    for key in ("full_match_pairs_sha256", "overlay_derived_manifest_sha256", "catalog_index_sha256"):
        if not SHA256_RE.fullmatch(str(bindings.get(key) or "").lower()):
            raise GuardError(f"workspace profile hash is invalid: {key}")
    return profile


def _select_hash_bound_file(paths: Iterable[Path], expected_sha256: str, label: str) -> Path:
    candidates = [path.resolve() for path in paths if path.is_file()]
    matches = [path for path in candidates if sha256_file(path) == expected_sha256.lower()]
    if len(matches) != 1:
        raise GuardError(
            f"expected exactly one hash-bound {label}; found {len(matches)} matches among {len(candidates)} candidates"
        )
    return matches[0]


def _verify_overlay_with_utf8(workspace: Path) -> dict[str, Any]:
    """Verify sealed overlay content and rebind it to the current workspace."""

    return verify_overlay(workspace)


def _gate_view(row: dict[str, Any]) -> dict[str, Any]:
    disposition = row.get("record_disposition") or row.get("disposition")
    date_evidence = row.get("date_evidence") or {}
    return {
        "product_id": str(row.get("product_id") or ""),
        "subject": row.get("subject") or row.get("title") or "",
        "gmt_modified": row.get("gmt_modified") or date_evidence.get("value"),
        "record_disposition": disposition,
        "allow_rag": row.get("allow_rag"),
    }


def partition_historical_rows(
    rows: Iterable[dict[str, Any]],
    *,
    cutoff: Any = DEFAULT_CUTOFF,
    forbidden_product_ids: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply every historical-source gate before candidate extraction."""

    forbidden = {str(value) for value in forbidden_product_ids}
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_row in rows:
        if not isinstance(source_row, dict):
            raise GuardError("historical source row must be an object")
        row = _gate_view(source_row)
        product_id = row["product_id"]
        if not product_id:
            raise GuardError("historical source row has an empty product_id")
        if product_id in seen:
            raise GuardError(f"duplicate historical product_id: {product_id}")
        seen.add(product_id)

        reasons: list[str] = []
        disposition = row.get("record_disposition")
        if disposition in REJECTED_DISPOSITIONS:
            reasons.append("DISPOSITION_REJECTED")
        if disposition != HISTORICAL_DISPOSITION:
            reasons.append("DISPOSITION_NOT_HISTORICAL_REFERENCE_ONLY")
        if row.get("allow_rag") is not True:
            reasons.append("ALLOW_RAG_NOT_TRUE")
        modified = parse_iso_date(row.get("gmt_modified"))
        if modified is None:
            reasons.append("GMT_MODIFIED_MISSING_OR_INVALID")
        elif modified >= cutoff:
            reasons.append("GMT_MODIFIED_ON_OR_AFTER_CUTOFF")
        if product_id in forbidden:
            reasons.append("FORBIDDEN_PRODUCT_ID")

        if reasons:
            excluded.append(
                {
                    **row,
                    "cutoff": cutoff.isoformat(),
                    "reason_codes": sorted(set(reasons)),
                    "candidate_allowed": False,
                }
            )
        else:
            eligible.append(source_row)
    return eligible, excluded


def blocked_commercial_fields() -> list[dict[str, Any]]:
    return [
        {
            "field": field,
            "candidate_value": None,
            "evidence_value": None,
            "source_path": None,
            "source_sha256": None,
            "source_bytes": None,
            "source_disposition": "BLOCKED_PENDING_APPROVED_INTERNAL_SOURCE",
            "decision": "BLOCKED",
            "write_allowed": False,
            "note": "Commercial value requires a separately approved current internal source.",
        }
        for field in COMMERCIAL_FIELDS
    ]


def _normalize_assets(raw_assets: Iterable[dict[str, Any]], target_model: str) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for position, source in enumerate(raw_assets, 1):
        exists = source.get("exists") is True
        hash_match = source.get("hash_match") is True
        default = str(source.get("review_status") or source.get("default_decision") or "PENDING")
        if default == "REJECT" or not exists or not hash_match:
            decision = "REJECT"
        else:
            decision = "PENDING"
        source_type = str(source.get("source_type") or "UNKNOWN")
        if source_type == "OFFICIAL_CURRENT_IMAGE":
            disposition = "OFFICIAL_CURRENT_REVIEW_REQUIRED"
        elif source_type == "HASH_BOUND_PRODUCT_LOCK" and exists and hash_match:
            disposition = (
                "HASH_BOUND_DERIVATION_REVIEW_REQUIRED"
                if source.get("source_chain_status") == "COMPLETE"
                else "HASH_BOUND_DERIVATION_SOURCE_MISSING_REVIEW_REQUIRED"
            )
        elif source_type == "LOCAL_PRODUCT_RENDER" and exists and hash_match:
            disposition = "LOCAL_RENDER_REVIEW_REQUIRED"
        elif source_type == "LOCAL_ENGINEERING_REFERENCE":
            disposition = "ENGINEERING_REFERENCE_REJECTED"
        else:
            disposition = "SOURCE_CHAIN_INCOMPLETE"
        assets.append(
            {
                **source,
                "candidate_order": position,
                "target_model": target_model,
                "sha256": source.get("observed_sha256") if hash_match else None,
                "source_disposition": disposition,
                "decision": decision,
                "decision_id": None,
                "approved_at": None,
                "upload_order": None,
                "upload_allowed": False,
            }
        )
    return assets


def _prepare_documents(
    *,
    workspace: Path,
    target_model: str,
    template_product_id: str,
    workers: int,
    candidate_limit: int,
    workspace_profile_path: Path,
    dynamic_schema_path: Path | None = None,
    run_id: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not workspace.is_dir():
        raise GuardError(f"workspace does not exist: {workspace}")
    target_model = target_model.strip().upper()
    template_product_id = template_product_id.strip()
    if not target_model or not template_product_id.isdigit():
        raise GuardError("target_model and numeric template_product_id are required")
    if workers < 1 or candidate_limit < 1:
        raise GuardError("workers and candidate_limit must be positive")

    dynamic_schema_binding: dict[str, Any] | None = None
    resolved_run_id = str(run_id or "").strip()
    if dynamic_schema_path is not None:
        dynamic_schema_path = dynamic_schema_path.resolve()
        dynamic_document = read_json(dynamic_schema_path)
        if not isinstance(dynamic_document, dict):
            raise GuardError("dynamic schema capture must be a JSON object")
        if not resolved_run_id:
            resolved_run_id = str(dynamic_document.get("run_id") or "").strip()
        if not resolved_run_id:
            raise GuardError("run_id is required when binding a dynamic schema")
        dynamic_schema_binding = validate_dynamic_schema_capture(
            dynamic_schema_path,
            expected_target_model=target_model,
            expected_template_product_id=template_product_id,
            expected_run_id=resolved_run_id,
        )
    elif not resolved_run_id:
        resolved_run_id = f"candidate-{uuid.uuid4()}"

    profile = _load_workspace_profile(workspace_profile_path)
    data_files = profile["data_files"]
    content_bindings = profile["content_bindings"]
    cutoff = parse_iso_date(profile["cutoff"])
    if cutoff is None:
        raise GuardError("workspace profile cutoff is invalid")
    official_path = (workspace / data_files["official_current"]).resolve()
    historical_path = (workspace / data_files["historical_sanitized"]).resolve()
    visual_path = (workspace / data_files["visual_scores"]).resolve()
    pair_path = _select_hash_bound_file(
        workspace.glob(data_files["full_match_pairs_glob"]),
        content_bindings["full_match_pairs_sha256"],
        "full-match pair file",
    )

    overlay_receipt = _verify_overlay_with_utf8(workspace)
    if overlay_receipt.get("derived_manifest_sha256") != content_bindings["overlay_derived_manifest_sha256"]:
        raise GuardError("verified overlay does not match workspace profile content binding")
    if overlay_receipt.get("catalog_index_sha256") != content_bindings["catalog_index_sha256"]:
        raise GuardError("verified catalog does not match workspace profile content binding")
    if int(overlay_receipt.get("record_count") or -1) != int(profile["expected_catalog_count"]):
        raise GuardError("verified overlay count does not match workspace profile")
    if overlay_receipt.get("cutoff") != cutoff.isoformat():
        raise GuardError("verified overlay cutoff does not match workspace profile")
    overlay_root = Path(str(overlay_receipt.get("overlay_root") or ""))
    overlay_path = overlay_root / "02_overlay" / "record_disposition.jsonl"
    if not overlay_path.is_file():
        raise GuardError(f"verified overlay receipt points to a missing file: {overlay_path}")

    historical_rows = list(read_jsonl(historical_path))
    overlay_rows = list(read_jsonl(overlay_path))
    if len(overlay_rows) != int(overlay_receipt.get("record_count") or -1):
        raise GuardError("verified overlay count changed before candidate preparation")

    overlay_by_id: dict[str, dict[str, Any]] = {}
    for row in overlay_rows:
        product_id = str(row.get("product_id") or "")
        if not product_id or product_id in overlay_by_id:
            raise GuardError(f"invalid or duplicate overlay product_id: {product_id!r}")
        overlay_by_id[product_id] = row

    sanitized_ids: set[str] = set()
    for row in historical_rows:
        product_id = str(row.get("product_id") or "")
        if not product_id or product_id in sanitized_ids:
            raise GuardError(f"invalid or duplicate sanitized product_id: {product_id!r}")
        sanitized_ids.add(product_id)
        overlay = overlay_by_id.get(product_id)
        if overlay is None:
            raise GuardError(f"sanitized product missing from verified overlay: {product_id}")
        overlay_gate = _gate_view(overlay)
        if overlay_gate["record_disposition"] != HISTORICAL_DISPOSITION or overlay_gate["allow_rag"] is not True:
            raise GuardError(f"sanitized product conflicts with verified overlay: {product_id}")

    expected_eligible = int(overlay_receipt.get("eligible_count") or -1)
    if len(historical_rows) != expected_eligible:
        raise GuardError(
            f"sanitized historical count {len(historical_rows)} does not match verified eligible count {expected_eligible}"
        )

    profile_blocked_ids = {
        str(value)
        for value in (profile.get("blocked_product_ids_by_model") or {}).get(target_model, [])
    }
    forbidden_ids = {template_product_id, *profile_blocked_ids}
    eligible_rows, sanitized_exclusions = partition_historical_rows(
        historical_rows,
        cutoff=cutoff,
        forbidden_product_ids=forbidden_ids,
    )
    _, overlay_exclusions = partition_historical_rows(
        overlay_rows,
        cutoff=cutoff,
        forbidden_product_ids=forbidden_ids,
    )

    candidate_ids = {str(row.get("product_id") or "") for row in eligible_rows}
    if candidate_ids & forbidden_ids:
        raise GuardError("forbidden template/target record survived historical filtering")
    for blocked_id in profile_blocked_ids:
        if blocked_id not in {row["product_id"] for row in overlay_exclusions}:
            raise GuardError(f"known blocked {target_model} record is not proven excluded: {blocked_id}")

    pair_rank = load_pair_rank(pair_path, target_model)
    if not pair_rank:
        raise GuardError(f"full-match pair interface returned no ranks for {target_model}")
    visual_scores = load_visual_scores(visual_path, target_model)
    official_model_configurations = load_official_model_configurations(official_path)
    extracted: list[dict[str, Any]] = []
    for row in eligible_rows:
        product_id = str(row.get("product_id") or "")
        rank, retrieval = pair_rank.get(product_id, (None, 0.0))
        candidate_model = str(row.get("canonical_model") or row.get("raw_model") or "").strip().upper()
        extracted.append(
            extract_candidate(
                row,
                rank,
                retrieval,
                visual_scores.get(product_id, 0.0),
                official_model_configurations.get(candidate_model),
            )
        )
    extracted.sort(
        key=lambda row: (
            -float(row.get("final_score") or 0.0),
            int(row["rank_seed"]) if row.get("rank_seed") is not None else 10**9,
            str(row.get("product_id") or ""),
        )
    )
    selected = extracted[:candidate_limit]
    for index, row in enumerate(selected, 1):
        row["review_order"] = index

    official = load_official_product(official_path, target_model)
    fields = official_field_candidates(official, official_path)
    fields.extend(blocked_commercial_fields())
    if dynamic_schema_binding is not None:
        existing_fields = {str(row.get("field") or "") for row in fields}
        attributes_by_key: dict[str, list[str]] = {}
        for row in official.get("attributes") or []:
            if not isinstance(row, dict):
                continue
            key = re.sub(r"[^a-z0-9]+", "_", str(row.get("name") or "").strip().lower()).strip("_")
            value = str(row.get("raw_value") or "").strip()
            if key and value:
                attributes_by_key.setdefault(key, []).append(value)
        for logical_name in dynamic_schema_binding["required_field_ids"]:
            if logical_name in existing_fields:
                continue
            values = attributes_by_key.get(logical_name) or []
            if logical_name == "model" and not values:
                values = [target_model]
            candidate_value: Any = values[0] if len(values) == 1 else values or None
            fields.append(
                {
                    "field": logical_name,
                    "candidate_value": candidate_value,
                    "evidence_value": values,
                    "source_path": str((official.get("source") or {}).get("source_path") or official_path)
                    if candidate_value is not None
                    else None,
                    "source_sha256": (official.get("source") or {}).get("source_sha256")
                    if candidate_value is not None
                    else None,
                    "source_disposition": (official.get("source") or {}).get("source_disposition")
                    if candidate_value is not None
                    else "BLOCKED_PENDING_EXPLICIT_SOURCE",
                    "decision": "PENDING" if candidate_value is not None else "BLOCKED",
                    "write_allowed": False,
                    "dynamic_schema_required": True,
                    "note": (
                        "Current dynamic schema required field mapped from an exact official attribute."
                        if candidate_value is not None
                        else "Current dynamic schema required field has no target fact; explicit sourced decision is required."
                    ),
                }
            )
            existing_fields.add(logical_name)

    inventory_paths = list(workspace.glob(f"{target_model}*/evidence/source_inventory.json"))
    if len(inventory_paths) > 1:
        raise GuardError(f"multiple {target_model} source inventories found")
    inventory_path = inventory_paths[0].resolve() if inventory_paths else None
    inventory = inspect_source_inventory(inventory_path) if inventory_path else {"status": "MISSING_INVENTORY", "sources": []}
    inventory["inventory_path"] = str(inventory_path) if inventory_path else None
    inventory["inventory_sha256"] = sha256_file(inventory_path) if inventory_path else None

    raw_assets = build_asset_candidates(workspace, target_model, workers)
    assets = _normalize_assets(raw_assets, target_model)
    asset_document = {
        "schema_version": "sellerkit.asset_manifest_candidates.v1",
        "target_schema_version": "alibaba.approved-asset-manifest.v1",
        "generated_at": utc_now(),
        "target_model": target_model,
        "review_status": "PENDING",
        "upload_enabled": False,
        "required_approved_asset_count": 4,
        "assets": assets,
        "summary": {
            "candidate_count": len(assets),
            "existing_hash_matched_count": sum(
                row.get("exists") is True and row.get("hash_match") is True for row in assets
            ),
            "pending_count": sum(row.get("decision") == "PENDING" for row in assets),
            "rejected_count": sum(row.get("decision") == "REJECT" for row in assets),
            "upload_allowed_count": 0,
            "official_current_unique_count": sum(
                row.get("source_type") == "OFFICIAL_CURRENT_IMAGE"
                and row.get("exists") is True
                and row.get("hash_match") is True
                for row in assets
            ),
            "hash_bound_derivation_count": sum(
                row.get("source_type") == "HASH_BOUND_PRODUCT_LOCK"
                and row.get("exists") is True
                and row.get("hash_match") is True
                for row in assets
            ),
            "source_chain_incomplete_count": sum(
                row.get("source_chain_status") == "ORIGINAL_SOURCE_MISSING_HASH_BOUND_DERIVATION"
                for row in assets
            ),
        },
    }

    manifest_template = {
        "schema_version": "sellerkit.approved_source_manifest_template.v1",
        "target_schema_version": "alibaba.approved-source-manifest.v1",
        "generated_at": utc_now(),
        "target_model": target_model,
        "template_product_id": template_product_id,
        "review_status": "PENDING",
        "mode": "save_draft",
        "final_write_enabled": False,
        "external_write_enabled": False,
        "asset_manifest_sha256": sha256_bytes(canonical_json(asset_document).encode("utf-8")),
        "fields": fields,
        "dynamic_schema_binding": dynamic_schema_binding or {
            "status": "MISSING_CURRENT_SCHEMA",
            "reason": "Rerun prepare with --dynamic-schema before approval compilation.",
        },
        "approval_instructions": {
            "replace_template_filename": "approved_source_manifest.json",
            "required_output_schema_version": "alibaba.approved-source-manifest.v1",
            "required_asset_output_schema_version": "alibaba.approved-asset-manifest.v1",
            "commercial_decision_schema_version": "sellerkit.approved-commercial-decision.v1",
            "required_source_disposition": "APPROVED_INTERNAL_SOURCE",
            "required_audit_fields": ["decision_id", "approved_at", "source_path", "source_sha256", "source_bytes"],
            "commercial_fields_default": "BLOCKED",
        },
    }

    exclusions_by_id: dict[str, dict[str, Any]] = {}
    for row in [*overlay_exclusions, *sanitized_exclusions]:
        product_id = row["product_id"]
        existing = exclusions_by_id.get(product_id)
        if existing is None:
            exclusions_by_id[product_id] = row
        else:
            existing["reason_codes"] = sorted(set(existing["reason_codes"]) | set(row["reason_codes"]))
    exclusion_rows = sorted(exclusions_by_id.values(), key=lambda row: (row["product_id"]))
    exclusion_counts = Counter(reason for row in exclusion_rows for reason in row["reason_codes"])

    quality_warnings: list[str] = []
    if dynamic_schema_binding is None:
        quality_warnings.append(
            "No current dynamic publish schema is bound; this package is research-only and approval compilation must fail closed."
        )
    if not visual_scores:
        quality_warnings.append(
            "draft_core.load_visual_scores returned no target rows; visual_score was fixed at 0.0 and no visual claim is made."
        )
    if inventory.get("status") != "PASS":
        quality_warnings.append(
            f"The {target_model} source inventory is incomplete; official-current candidates remain review-only."
        )
    if sum(row.get("decision") == "PENDING" for row in assets) < 4:
        quality_warnings.append("Fewer than four upload assets are reviewable; asset preflight remains blocked.")
    official_asset_count = sum(
        row.get("source_type") == "OFFICIAL_CURRENT_IMAGE"
        and row.get("exists") is True
        and row.get("hash_match") is True
        for row in assets
    )
    if official_asset_count < 4:
        quality_warnings.append(
            f"Only {official_asset_count} unique official-current images are hash-verified; "
            "any hash-bound derivation requires an explicit human decision before it can satisfy the four-image gate."
        )

    candidate_review = {
        "schema_version": "sellerkit.candidate_review.v1",
        "generated_at": utc_now(),
        "status": "REVIEW_REQUIRED",
        "run_id": resolved_run_id,
        "target_model": target_model,
        "template_product_id": template_product_id,
        "template_scope": "FORM_STRUCTURE_ONLY",
        "template_facts_allowed": False,
        "template_assets_allowed": False,
        "cutoff": cutoff.isoformat(),
        "cutoff_semantics": "gmt_modified must be strictly before cutoff",
        "candidate_count_total": len(extracted),
        "candidate_count_presented": len(selected),
        "candidates": selected,
        "hard_exclusions": exclusion_rows,
        "hard_exclusion_reason_counts": dict(sorted(exclusion_counts.items())),
        "source_inventory": inventory,
        "official_source": official.get("source") or {},
        "dynamic_schema": dynamic_schema_binding or {"status": "MISSING_CURRENT_SCHEMA"},
        "commercial_fields_status": "BLOCKED",
        "quality_warnings": quality_warnings,
        "invariants": {
            "all_candidates_allow_rag_true": all(row.get("allow_rag") is True for row in selected),
            "all_candidates_historical_only": all(
                row.get("record_disposition") == HISTORICAL_DISPOSITION for row in selected
            ),
            "all_candidates_strictly_before_cutoff": all(
                (parse_iso_date(row.get("gmt_modified_proxy")) or cutoff) < cutoff
                for row in selected
            ),
            "all_candidates_copy_allowed_false": all(row.get("copy_allowed") is False for row in selected),
            "forbidden_ids_absent": not ({row.get("product_id") for row in selected} & forbidden_ids),
        },
        "input_counts": {
            "verified_overlay_records": len(overlay_rows),
            "verified_overlay_eligible": expected_eligible,
            "sanitized_historical_records": len(historical_rows),
            "ranked_target_records": len(pair_rank),
            "visual_target_records": len(visual_scores),
            "official_configuration_models": len(official_model_configurations),
        },
    }
    if not all(candidate_review["invariants"].values()):
        raise GuardError("candidate review invariants failed")

    run_spec = {
        "schema_version": "sellerkit.similar_draft_run_spec.v1",
        "run_id": resolved_run_id,
        "generated_at": utc_now(),
        "status": "BLOCKED_REVIEW_REQUIRED",
        "run_kind": "CREATE_SIMILAR_DRAFT",
        "target_model": target_model,
        "template_product_id": template_product_id,
        "mode": "save_draft",
        "approval_observed": False,
        "external_write_enabled": False,
        "final_write_enabled": False,
        "direct_endpoint_write_enabled": False,
        "max_draft_save_count": 1,
        "template_scope": "FORM_STRUCTURE_ONLY",
        "inherited_content_must_be_cleared": True,
        "required_preflight_status": "PASS",
        "required_probe_status": "PASS_ZERO_WRITE",
        "stop_state_before_final_submit": "DRAFT_SAVED_FINAL_REVIEW_READY",
        "endpoint_policy": {
            "path": str(DEFAULT_POLICY.resolve()),
            "sha256": sha256_file(DEFAULT_POLICY),
            "policy_mode": _load_policy(DEFAULT_POLICY).get("policy_mode"),
        },
        "workspace_profile": {
            "profile_id": profile["profile_id"],
            "path": str(workspace_profile_path.resolve()),
            "sha256": sha256_file(workspace_profile_path),
        },
    }

    input_evidence = [
        _file_evidence(official_path, "official_current"),
        _file_evidence(historical_path, "historical_sanitized"),
        _file_evidence(pair_path, "full_match_pairs"),
        _file_evidence(visual_path, "visual_scores", required=False),
        _file_evidence(overlay_path, "record_disposition_overlay"),
        _file_evidence(DEFAULT_POLICY, "endpoint_policy"),
        _file_evidence(workspace_profile_path, "workspace_profile"),
    ]
    if inventory_path:
        input_evidence.append(_file_evidence(inventory_path, "target_source_inventory"))
    if dynamic_schema_binding is not None and dynamic_schema_path is not None:
        input_evidence.append(_file_evidence(dynamic_schema_path, "current_dynamic_publish_schema"))
        input_evidence.append(
            _file_evidence(Path(dynamic_schema_binding["raw_payload"]["path"]), "current_dynamic_schema_raw_payload")
        )

    documents = {
        "candidate_review.json": candidate_review,
        "approved_source_manifest.template.json": manifest_template,
        "asset_manifest.candidates.json": asset_document,
        "run_spec.json": run_spec,
    }
    return documents, input_evidence


def prepare_package(
    *,
    workspace: Path,
    target_model: str,
    template_product_id: str,
    output: Path,
    workers: int = 4,
    candidate_limit: int = 25,
    workspace_profile_path: Path = DEFAULT_WORKSPACE_PROFILE,
    dynamic_schema_path: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    output = output.resolve()
    if output.exists():
        raise GuardError(f"prepare output already exists; refusing overwrite: {output}")
    documents, input_evidence = _prepare_documents(
        workspace=workspace,
        target_model=target_model,
        template_product_id=template_product_id,
        workers=workers,
        candidate_limit=candidate_limit,
        workspace_profile_path=workspace_profile_path.resolve(),
        dynamic_schema_path=dynamic_schema_path,
        run_id=run_id,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.preparing.{os.getpid()}.{uuid.uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    generated: list[dict[str, Any]] = []
    for name in PREPARE_FILENAMES[:-1]:
        atomic_write_json(staging / name, documents[name])
        generated.append(
            {
                "role": name,
                "path": str(output / name),
                "bytes": (staging / name).stat().st_size,
                "sha256": sha256_file(staging / name),
            }
        )
    evidence_manifest = {
        "schema_version": "sellerkit.similar_draft_evidence_manifest.v1",
        "generated_at": utc_now(),
        "status": "COMPLETE_REVIEW_PACKAGE",
        "run_id": documents["run_spec.json"]["run_id"],
        "target_model": target_model.strip().upper(),
        "template_product_id": template_product_id.strip(),
        "inputs": input_evidence,
        "generated_files": generated,
        "self_hash_policy": "evidence_manifest.json intentionally excludes its own hash",
        "write_boundary": {
            "external_write_enabled": False,
            "final_write_enabled": False,
            "draft_save_enabled_before_approval": False,
        },
    }
    atomic_write_json(staging / "evidence_manifest.json", evidence_manifest)
    os.replace(staging, output)

    for row in generated:
        final_path = Path(row["path"])
        if not final_path.is_file() or sha256_file(final_path) != row["sha256"]:
            raise GuardError(f"prepared output verification failed: {final_path}")
    if not (output / "evidence_manifest.json").is_file():
        raise GuardError("prepared evidence manifest is missing")
    return {
        "schema_version": "sellerkit.prepare_receipt.v1",
        "generated_at": utc_now(),
        "status": "REVIEW_REQUIRED",
        "run_id": documents["run_spec.json"]["run_id"],
        "target_model": target_model.strip().upper(),
        "template_product_id": template_product_id.strip(),
        "output": str(output),
        "files": [str(output / name) for name in PREPARE_FILENAMES],
        "external_write_enabled": False,
        "final_write_enabled": False,
    }


def strict_verify_probe(
    probe_data: dict[str, Any],
    policy: dict[str, Any],
    *,
    binding_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = verify_probe(probe_data, policy, binding_context=binding_context)
    errors = list(result.get("errors") or [])
    errors.extend(_json_schema_errors(probe_data, "probe-receipt.schema.json"))
    context = binding_context if isinstance(binding_context, dict) else {}
    read_activation = context.get("probe_read_activation")
    if read_activation is not None:
        if isinstance(read_activation, dict):
            errors.extend(
                _json_schema_errors(
                    read_activation,
                    "probe-read-activation-request.schema.json",
                )
            )
        else:
            errors.append("probe READ activation request must be a JSON object")
    result = finalize_probe_verification_gate(result, errors=errors)
    receipt_schema_errors = _json_schema_errors(
        result, "probe-verification-receipt.schema.json"
    )
    if receipt_schema_errors:
        result = finalize_probe_verification_gate(
            result,
            errors=list(result.get("errors") or []) + receipt_schema_errors,
        )
    return result


def _build_probe_read_activation_receipt(
    request: dict[str, Any],
    verification: dict[str, Any],
    *,
    endpoint_policy_path: Path,
    action_plan_path: Path,
    probe_source_path: Path,
    probe_verification_path: Path,
) -> dict[str, Any]:
    receipt = {
        "schema_version": "sellerkit.alibaba.probe-read-activation.v1",
        "generated_at": verification["generated_at"],
        "expires_at": request["expires_at"],
        "status": "PASS_CURRENT_SESSION_EXACT_READ_ACTIVATION",
        "scope": dict(request["scope"]),
        "bindings": {
            "global_endpoint_policy_sha256": sha256_file(endpoint_policy_path),
            "action_plan_sha256": sha256_file(action_plan_path),
            "probe_source_sha256": sha256_file(probe_source_path),
            "probe_verification_sha256": sha256_file(probe_verification_path),
        },
        "producer": {
            "validator_id": "draft_core.verify_probe.v3",
            "validator_sha256": sha256_file(SCRIPT_DIR / "draft_core.py"),
        },
        "request_activations": [dict(row) for row in request["request_activations"]],
        "global_policy_unchanged": True,
        "draft_write_enabled": False,
        "final_write_enabled": False,
        "errors": [],
    }
    receipt["body_sha256"] = sha256_bytes(
        canonical_json(receipt).encode("utf-8")
    )
    schema_errors = _json_schema_errors(
        receipt, "probe-read-activation-receipt.schema.json"
    )
    if schema_errors:
        raise GuardError(
            "generated probe READ activation receipt failed schema: "
            + "; ".join(schema_errors)
        )
    return receipt


def strict_verify_fill(
    fill_execution: dict[str, Any],
    post_refresh: dict[str, Any] | None = None,
    action_plan: dict[str, Any] | None = None,
    run_spec: dict[str, Any] | None = None,
    probe_receipt: dict[str, Any] | None = None,
    *,
    action_plan_sha256: str | None = None,
    run_spec_sha256: str | None = None,
    probe_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Fail closed unless both execution ledgers exactly satisfy the plan."""

    result = verify_fill_receipt(
        fill_execution,
        post_refresh,
        action_plan,
        run_spec,
        probe_receipt,
        action_plan_sha256=action_plan_sha256,
        run_spec_sha256=run_spec_sha256,
        probe_receipt_sha256=probe_receipt_sha256,
    )
    errors = list(result.get("errors") or [])
    if isinstance(fill_execution, dict):
        errors.extend(_json_schema_errors(fill_execution, "fill-execution.schema.json"))
    else:
        errors.append("fill execution input must be an object")
    if isinstance(post_refresh, dict):
        errors.extend(_json_schema_errors(post_refresh, "post-refresh-verification.schema.json"))
    else:
        errors.append("post-refresh input must be an object")
    if isinstance(action_plan, dict):
        errors.extend(_json_schema_errors(action_plan, "browser-action-plan.schema.json"))
    if isinstance(run_spec, dict):
        errors.extend(_json_schema_errors(run_spec, "run-spec.schema.json"))
    if isinstance(probe_receipt, dict):
        errors.extend(_json_schema_errors(probe_receipt, "probe-verification-receipt.schema.json"))
    result["errors"] = sorted(set(errors))
    result["status"] = "PASS" if not result["errors"] else "FAIL"
    return result


def _parse_key_value(values: Sequence[str], label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item:
            raise GuardError(f"invalid {label} entry (expected NAME=VALUE): {value}")
        if key in parsed:
            raise GuardError(f"duplicate {label} key: {key}")
        parsed[key] = item
    return parsed


def _input_hashes(run_dir: Path, values: Sequence[str]) -> dict[str, str]:
    if values:
        parsed = _parse_key_value(values, "input")
        hashes: dict[str, str] = {}
        for label, raw_path in parsed.items():
            if not INPUT_LABEL_RE.fullmatch(label):
                raise GuardError(f"invalid input label: {label}")
            path = Path(raw_path).resolve()
            if not path.is_file():
                raise GuardError(f"input is not a file: {path}")
            hashes[label] = sha256_file(path)
        return hashes
    hashes = {}
    for name in PREPARE_FILENAMES:
        path = run_dir / name
        if path.is_file():
            hashes[name] = sha256_file(path)
    if len(hashes) != len(PREPARE_FILENAMES):
        raise GuardError("init-run requires explicit --input values or a complete prepared package")
    return hashes


def _validate_init_run_identity(
    run_dir: Path,
    values: Sequence[str],
    *,
    run_id: str,
    target_model: str,
    template_product_id: str,
) -> None:
    """Bind a new checkpoint to any prepared run-spec evidence already supplied.

    ``init-run`` used to create a fresh identifier even when ``run_spec.json``
    and every downstream artifact already named another run.  The CLI now
    requires the existing identifier and rejects any contradictory run spec.
    """

    candidates: list[Path] = []
    prepared = run_dir / "run_spec.json"
    if prepared.is_file():
        candidates.append(prepared.resolve())
    if values:
        for label, raw_path in _parse_key_value(values, "input").items():
            path = Path(raw_path).resolve()
            if path.suffix.lower() == ".json" and (
                "run_spec" in label.lower() or path.name.lower() == "run_spec.json"
            ):
                candidates.append(path)

    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        document = _load_object(path, "run spec used by init-run")
        if document.get("schema_version") != "sellerkit.similar_draft_run_spec.v1":
            raise GuardError(f"init-run run spec has unsupported schema: {path}")
        if str(document.get("run_id") or "") != run_id:
            raise GuardError("init-run run_id does not match prepared run_spec.json")
        if str(document.get("target_model") or "").strip().upper() != target_model:
            raise GuardError("init-run target_model does not match prepared run_spec.json")
        if str(document.get("template_product_id") or "") != template_product_id:
            raise GuardError("init-run template_product_id does not match prepared run_spec.json")


def _valid_sha256(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise GuardError(f"{label} must be a lowercase or uppercase 64-character SHA-256")
    return normalized


def _parse_headers(values: Sequence[str]) -> dict[str, str]:
    return _parse_key_value(values, "header")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed Alibaba similar-draft guard")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Build a local review-only candidate package")
    prepare.add_argument("--target-model", required=True)
    prepare.add_argument("--template-product-id", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument(
        "--workspace",
        type=Path,
        help=(
            "business-data workspace; otherwise resolve SELLERKIT_ALIBABA_WORKSPACE, "
            "SELLERKIT_ALIBABA_CONFIG, or cwd ancestor markers"
        ),
    )
    prepare.add_argument("--workers", type=int, default=4)
    prepare.add_argument("--candidate-limit", type=int, default=25)
    prepare.add_argument(
        "--dynamic-schema",
        type=Path,
        help="current normalized publish-schema capture; omitting it produces a research-only package that cannot compile",
    )
    prepare.add_argument("--run-id", help="run binding; must match the dynamic schema capture when provided")
    prepare.add_argument(
        "--workspace-profile",
        type=Path,
        default=DEFAULT_WORKSPACE_PROFILE,
        help="portable logical-path and content-hash bindings for the private data snapshot",
    )

    preflight = subparsers.add_parser("preflight", help="Validate approved source and asset manifests")
    preflight.add_argument("--manifest", type=Path, required=True)
    preflight.add_argument("--asset-manifest", type=Path, required=True)
    preflight.add_argument("--output", type=Path)

    probe = subparsers.add_parser("verify-probe", help="Verify a zero-write browser probe")
    probe.add_argument("--probe", type=Path, required=True)
    probe.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    probe.add_argument("--run-spec", type=Path, required=True)
    probe.add_argument("--action-plan", type=Path, required=True)
    probe.add_argument("--adapter", type=Path, required=True)
    probe.add_argument(
        "--probe-read-activation",
        type=Path,
        help="fresh run/session-scoped exact READ-only overlay request; never changes the global endpoint policy",
    )
    probe.add_argument(
        "--probe-read-activation-output",
        type=Path,
        help="new bound READ activation receipt; required when --probe-read-activation is supplied",
    )
    probe.add_argument("--output", type=Path, required=True)

    endpoint = subparsers.add_parser("check-endpoint", help="Classify one captured request")
    endpoint.add_argument("--url", required=True)
    endpoint.add_argument("--method", required=True)
    endpoint.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    endpoint.add_argument("--output", type=Path)
    endpoint.add_argument("--body-present", action="store_true")
    endpoint.add_argument("--redirected", action="store_true")
    endpoint.add_argument("--credential-present", action="store_true")
    endpoint.add_argument("--storage-accessed", action="store_true")
    endpoint.add_argument("--bundle-sha256")
    endpoint.add_argument("--action")
    endpoint.add_argument("--dom-intent")
    endpoint.add_argument("--blocker", action="append", default=[])
    endpoint.add_argument("--evidence-id")
    endpoint.add_argument("--content-type")
    endpoint.add_argument("--header", action="append", default=[])

    init = subparsers.add_parser("init-run", help="Initialize an atomic model-scoped checkpoint")
    init.add_argument("--run-dir", type=Path, required=True)
    init.add_argument("--target-model", required=True)
    init.add_argument("--template-product-id", required=True)
    init.add_argument(
        "--run-id",
        required=True,
        help="existing run_id from the prepared run package; checkpoint initialization never invents a replacement",
    )
    init.add_argument(
        "--lock-root",
        type=Path,
        required=True,
        help="shared cross-run lock directory; must be outside the run-specific directory",
    )
    init.add_argument("--input", action="append", default=[], metavar="NAME=PATH")

    transition = subparsers.add_parser("transition", help="CAS transition an existing checkpoint")
    transition.add_argument("--checkpoint", type=Path, required=True)
    transition.add_argument("--expected-revision", type=int, required=True)
    transition.add_argument("--expected-sha256", required=True)
    transition.add_argument(
        "--draft-id",
        help="Required only when recording DRAFT_SAVED; never accepts or triggers a publish ID",
    )
    transition.add_argument(
        "--new-state",
        required=True,
        choices=(
            "CANDIDATE_REVIEW_APPROVED",
            "TEMPLATE_PROBED",
            "INHERITED_DATA_CLEARED",
            "FORM_FILLED",
            "VALIDATED",
            "DRAFT_SAVED",
            "FINAL_REVIEW_READY",
        ),
    )
    transition.add_argument(
        "--evidence",
        type=Path,
        required=True,
        help="Typed checkpoint-transition-evidence JSON; a bare digest is never accepted",
    )

    abort = subparsers.add_parser(
        "abort-no-write",
        help="CAS-abort an active run only when no draft/final write was attempted",
    )
    abort.add_argument("--checkpoint", type=Path, required=True)
    abort.add_argument("--expected-revision", type=int, required=True)
    abort.add_argument("--expected-sha256", required=True)
    abort.add_argument("--reason", required=True)

    fill = subparsers.add_parser(
        "verify-fill",
        help="Verify exact fill execution and post-refresh persistence against bound inputs",
    )
    fill.add_argument("--action-plan", type=Path, required=True)
    fill.add_argument("--run-spec", type=Path, required=True)
    fill.add_argument("--probe-receipt", type=Path, required=True)
    fill.add_argument("--fill-execution", type=Path, required=True)
    fill.add_argument("--post-refresh", type=Path, required=True)
    fill.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            resolution = resolve_workspace(args.workspace)
            if resolution.path is None:
                raise GuardError(
                    "WORKSPACE_NOT_CONFIGURED: pass --workspace or set SELLERKIT_ALIBABA_WORKSPACE"
                )
            receipt = prepare_package(
                workspace=resolution.path,
                target_model=args.target_model,
                template_product_id=args.template_product_id,
                output=args.output,
                workers=args.workers,
                candidate_limit=args.candidate_limit,
                workspace_profile_path=args.workspace_profile,
                dynamic_schema_path=args.dynamic_schema,
                run_id=args.run_id,
            )
            _print_json(receipt)
            return 0

        if args.command == "preflight":
            manifest = _load_object(args.manifest, "approved source manifest")
            asset_manifest = _load_object(args.asset_manifest, "approved asset manifest")
            receipt = validate_approved_manifest(
                manifest,
                asset_manifest,
                manifest_path=args.manifest.resolve(),
                asset_manifest_path=args.asset_manifest.resolve(),
            )
            schema_errors = _json_schema_errors(manifest, "approved-source-manifest.schema.json")
            schema_errors.extend(_json_schema_errors(asset_manifest, "approved-asset-manifest.schema.json"))
            receipt["errors"] = sorted(set(list(receipt.get("errors") or []) + schema_errors))
            receipt["status"] = "PASS" if not receipt["errors"] else "BLOCKED_PREFLIGHT"
            receipt["json_schema_validation"] = "PASS" if not schema_errors else "FAIL_CLOSED"
            if args.output:
                _write_new_json(args.output.resolve(), receipt)
            _print_json(receipt)
            return 0 if receipt.get("status") == "PASS" else 3

        if args.command == "verify-probe":
            if bool(args.probe_read_activation) != bool(args.probe_read_activation_output):
                raise GuardError(
                    "--probe-read-activation and --probe-read-activation-output must be supplied together"
                )
            probe_data = _load_object(args.probe, "probe")
            policy = _load_policy(args.policy)
            run_spec = _load_object(args.run_spec, "run spec")
            action_plan = _load_object(args.action_plan, "browser action plan")
            adapter = _load_object(args.adapter, "browser adapter")
            read_activation_request = (
                _load_object(args.probe_read_activation, "probe READ activation request")
                if args.probe_read_activation
                else None
            )
            binding_context = {
                "run_spec": run_spec,
                "action_plan": action_plan,
                "adapter": adapter,
                "probe_read_activation": read_activation_request,
                "loaded_policy": policy,
                "loaded_policy_sha256": sha256_file(args.policy.resolve()),
                "hashes": {
                    "run_spec_sha256": sha256_file(args.run_spec.resolve()),
                    "action_plan_sha256": sha256_file(args.action_plan.resolve()),
                    "endpoint_policy_sha256": sha256_file(args.policy.resolve()),
                    "adapter_sha256": sha256_file(args.adapter.resolve()),
                    "probe_source_sha256": sha256_file(args.probe.resolve()),
                    "probe_read_activation_request_sha256": (
                        sha256_file(args.probe_read_activation.resolve())
                        if args.probe_read_activation
                        else None
                    ),
                },
            }
            receipt = strict_verify_probe(probe_data, policy, binding_context=binding_context)
            _write_new_json(args.output.resolve(), receipt)
            if read_activation_request is not None and receipt.get("execution_authorized") is True:
                activation_receipt = _build_probe_read_activation_receipt(
                    read_activation_request,
                    receipt,
                    endpoint_policy_path=args.policy.resolve(),
                    action_plan_path=args.action_plan.resolve(),
                    probe_source_path=args.probe.resolve(),
                    probe_verification_path=args.output.resolve(),
                )
                _write_new_json(
                    args.probe_read_activation_output.resolve(), activation_receipt
                )
            _print_json(receipt)
            if receipt.get("production_execution_authorized") is True:
                return 0
            return 4 if receipt.get("zero_write_confirmed") is True else 3

        if args.command == "check-endpoint":
            policy = _load_policy(args.policy)
            receipt = classify_endpoint(
                policy,
                args.url,
                args.method,
                body_present=args.body_present,
                headers=_parse_headers(args.header),
                redirected=args.redirected,
                credential_present=args.credential_present,
                storage_accessed=args.storage_accessed,
                bundle_sha256=args.bundle_sha256,
                action=args.action,
                dom_intent=args.dom_intent,
                blockers=args.blocker,
                evidence_id=args.evidence_id,
                content_type=args.content_type,
            )
            receipt["policy_sha256"] = sha256_file(args.policy)
            if args.output:
                _write_new_json(args.output.resolve(), receipt)
            _print_json(receipt)
            # Exit 0 is execution-authorizing and therefore belongs only to an
            # exact ALLOW.  ALLOW_UI_ONLY may permit a visible UI gesture but
            # must never be mistaken for request-release authority by shell
            # callers that check only the process exit code.
            return 0 if receipt.get("decision") == "ALLOW" else 3

        if args.command == "init-run":
            run_dir = args.run_dir.resolve()
            if not args.template_product_id.isdigit():
                raise GuardError("template_product_id must be numeric")
            target_model = args.target_model.strip().upper()
            run_id = str(args.run_id or "").strip()
            if not run_id:
                raise GuardError("run_id is required")
            _validate_init_run_identity(
                run_dir,
                args.input,
                run_id=run_id,
                target_model=target_model,
                template_product_id=args.template_product_id,
            )
            hashes = _input_hashes(run_dir, args.input)
            checkpoint = initialize_checkpoint(
                run_dir,
                target_model,
                args.template_product_id,
                hashes,
                run_id=run_id,
                lock_root=args.lock_root,
            )
            _print_json(
                {
                    "status": "PRECHECK",
                    "checkpoint_path": str(run_dir / "checkpoint.json"),
                    "checkpoint_sha256": checkpoint_hash(checkpoint),
                    "checkpoint": checkpoint,
                }
            )
            return 0

        if args.command == "transition":
            checkpoint_path = args.checkpoint.resolve()
            if not checkpoint_path.is_file():
                raise GuardError(f"missing checkpoint: {checkpoint_path}")
            expected_hash = _valid_sha256(args.expected_sha256, "expected_sha256")
            evidence_path = args.evidence.resolve()
            if not evidence_path.is_file():
                raise GuardError(f"missing transition evidence: {evidence_path}")
            checkpoint = transition_checkpoint(
                checkpoint_path,
                args.expected_revision,
                expected_hash,
                args.new_state,
                evidence_path,
                draft_id=args.draft_id,
            )
            _print_json(
                {
                    "status": checkpoint.get("state"),
                    "checkpoint_path": str(checkpoint_path),
                    "checkpoint_sha256": checkpoint_hash(checkpoint),
                    "checkpoint": checkpoint,
                }
            )
            return 0

        if args.command == "abort-no-write":
            checkpoint_path = args.checkpoint.resolve()
            if not checkpoint_path.is_file():
                raise GuardError(f"missing checkpoint: {checkpoint_path}")
            expected_hash = _valid_sha256(args.expected_sha256, "expected_sha256")
            checkpoint = abort_checkpoint_no_write(
                checkpoint_path,
                args.expected_revision,
                expected_hash,
                args.reason,
            )
            _print_json(
                {
                    "status": checkpoint.get("state"),
                    "checkpoint_path": str(checkpoint_path),
                    "checkpoint_sha256": checkpoint_hash(checkpoint),
                    "checkpoint": checkpoint,
                }
            )
            return 0

        if args.command == "verify-fill":
            action_plan = _load_object(args.action_plan, "browser action plan")
            run_spec = _load_object(args.run_spec, "run spec")
            probe_receipt = _load_object(args.probe_receipt, "probe receipt")
            fill_execution = _load_object(args.fill_execution, "fill execution")
            post_refresh = _load_object(args.post_refresh, "post-refresh verification")
            receipt = strict_verify_fill(
                fill_execution,
                post_refresh,
                action_plan,
                run_spec,
                probe_receipt,
                action_plan_sha256=sha256_file(args.action_plan.resolve()),
                run_spec_sha256=sha256_file(args.run_spec.resolve()),
                probe_receipt_sha256=sha256_file(args.probe_receipt.resolve()),
            )
            _write_new_json(args.output.resolve(), receipt)
            _print_json(receipt)
            return 0 if receipt.get("status") == "PASS" else 3

        raise GuardError(f"unsupported command: {args.command}")
    except (GuardError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _print_json(
            {
                "schema_version": "sellerkit.guard_error.v1",
                "generated_at": utc_now(),
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
