from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import struct
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from draft_core import (
    approved_asset_sequence_sha256,
    canonical_json,
    canonical_commercial_field,
    canonical_registry_field,
    commercial_registry_fields,
    commercial_value_matches as _commercial_value_matches,
    duplicate_registry_targets,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now,
    validate_approved_manifest,
)
from dynamic_schema_gate import load_decision_schema_binding
from localized_field_contract import LocalizedFieldError, validate_localized_target
from local_schema_validation import local_schema_errors


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
FIELD_REGISTRY = SKILL_DIR / "references" / "publish-field-registry.v1.json"

INPUT_NAMES = (
    "candidate_review.json",
    "approved_source_manifest.template.json",
    "asset_manifest.candidates.json",
    "run_spec.json",
    "evidence_manifest.json",
)
GENERATED_EVIDENCE_NAMES = INPUT_NAMES[:-1]
COMMERCIAL_FIELDS = commercial_registry_fields() | {
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
}
SUPPORTED_COMMERCIAL_FIELDS = {
    field for field in commercial_registry_fields() if canonical_commercial_field(field) is not None
}
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")


class CompileBlocked(RuntimeError):
    """The explicit approval package is incomplete, inconsistent, or unsafe."""


def _write_new_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _validate_schema(document: dict[str, Any], schema_name: str) -> None:
    errors = local_schema_errors(document, SCHEMA_DIR / schema_name)
    if errors:
        details = "; ".join(errors)
        raise CompileBlocked(f"{schema_name} validation failed: {details}")


def _normalized_model(value: Any) -> str:
    return str(value or "").strip().upper()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CompileBlocked(message)


def _file_row(name: str, path: Path, target_model: str, template_product_id: str | None) -> dict[str, Any]:
    return {
        "name": name,
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "target_model": target_model,
        "template_product_id": template_product_id,
    }


def _media_type(path: Path) -> str:
    known = {
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".html": "text/html",
        ".htm": "text/html",
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
    }
    return known.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _image_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        head = handle.read(32)
        if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
            return struct.unpack(">II", head[16:24])
        if not head.startswith(b"\xff\xd8"):
            raise CompileBlocked(f"selected image has an unsupported header: {path}")
        handle.seek(2)
        while True:
            marker_start = handle.read(1)
            if not marker_start:
                break
            if marker_start != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if marker in {b"\xd8", b"\xd9"}:
                continue
            length_bytes = handle.read(2)
            if len(length_bytes) != 2:
                break
            segment_length = struct.unpack(">H", length_bytes)[0]
            if marker and marker[0] in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                payload = handle.read(5)
                if len(payload) != 5:
                    break
                height, width = struct.unpack(">HH", payload[1:5])
                return width, height
            handle.seek(max(0, segment_length - 2), 1)
    raise CompileBlocked(f"cannot read selected image dimensions: {path}")


def _usage_policy() -> dict[str, bool]:
    return {
        "allow_rag": True,
        "fact_extraction": True,
        "publish_draft": True,
        "auto_listing": False,
        "external_disclosure": False,
        "final_write": False,
    }


def _schema_bundle_sha256() -> str:
    rows = [
        {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"), key=lambda item: item.name.casefold())
    ]
    return sha256_bytes(canonical_json(rows).encode("utf-8"))


def _resolve_source_path(raw: Any, *, decision_dir: Path) -> Path:
    path = Path(str(raw or ""))
    if not path.is_absolute():
        path = decision_dir / path
    return path.resolve()


def _verify_source_file(path: Path, *, expected_hash: Any, expected_bytes: Any, label: str) -> None:
    _require(path.is_absolute() and path.is_file(), f"{label} is not a readable absolute file: {path}")
    observed_hash = sha256_file(path)
    _require(observed_hash == str(expected_hash or "").lower(), f"{label} hash mismatch: {path}")
    try:
        expected_size = int(expected_bytes)
    except (TypeError, ValueError):
        raise CompileBlocked(f"{label} has no valid byte-size binding: {path}") from None
    _require(path.stat().st_size == expected_size, f"{label} byte-size mismatch: {path}")


def _load_and_bind_inputs(candidate_dir: Path, decision: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    missing = [name for name in INPUT_NAMES if not (candidate_dir / name).is_file()]
    _require(not missing, f"candidate directory is missing required files: {missing}")
    docs = {name: read_json(candidate_dir / name) for name in INPUT_NAMES}
    _require(all(isinstance(value, dict) for value in docs.values()), "all candidate documents must be JSON objects")
    expected_versions = {
        "candidate_review.json": "sellerkit.candidate_review.v1",
        "approved_source_manifest.template.json": "sellerkit.approved_source_manifest_template.v1",
        "asset_manifest.candidates.json": "sellerkit.asset_manifest_candidates.v1",
        "run_spec.json": "sellerkit.similar_draft_run_spec.v1",
        "evidence_manifest.json": "sellerkit.similar_draft_evidence_manifest.v1",
    }
    for name, version in expected_versions.items():
        _require(docs[name].get("schema_version") == version, f"unsupported schema_version in {name}")

    run_spec = docs["run_spec.json"]
    _require(run_spec.get("mode") == "save_draft", "run_spec mode must be save_draft")
    _require(run_spec.get("final_write_enabled") is False, "run_spec final_write_enabled must be false")
    if "external_write_enabled" in run_spec:
        _require(run_spec.get("external_write_enabled") is False, "run_spec external_write_enabled must be false before approval")
    template_document = docs["approved_source_manifest.template.json"]
    if "final_write_enabled" in template_document:
        _require(template_document.get("final_write_enabled") is False, "candidate template final_write_enabled must be false")
    evidence_boundary = docs["evidence_manifest.json"].get("write_boundary") or {}
    if evidence_boundary:
        _require(evidence_boundary.get("final_write_enabled") is False, "evidence package final_write_enabled must be false")
        _require(evidence_boundary.get("external_write_enabled") is False, "evidence package external_write_enabled must be false")

    target_model = _normalized_model(decision["target_model"])
    template_product_id = str(decision["template_product_id"])
    target_docs = {
        "candidate_review.json": docs["candidate_review.json"],
        "approved_source_manifest.template.json": docs["approved_source_manifest.template.json"],
        "asset_manifest.candidates.json": docs["asset_manifest.candidates.json"],
        "run_spec.json": docs["run_spec.json"],
        "evidence_manifest.json": docs["evidence_manifest.json"],
    }
    for name, document in target_docs.items():
        _require(
            _normalized_model(document.get("target_model")) == target_model,
            f"target_model mismatch in {name}",
        )
    for name in (
        "candidate_review.json",
        "approved_source_manifest.template.json",
        "run_spec.json",
        "evidence_manifest.json",
    ):
        _require(
            str(docs[name].get("template_product_id") or "") == template_product_id,
            f"template_product_id mismatch in {name}",
        )
    if docs["asset_manifest.candidates.json"].get("template_product_id") is not None:
        _require(
            str(docs["asset_manifest.candidates.json"]["template_product_id"]) == template_product_id,
            "template_product_id mismatch in asset_manifest.candidates.json",
        )
    if docs["run_spec.json"].get("run_id") is not None:
        _require(str(docs["run_spec.json"]["run_id"]) == str(decision["run_id"]), "run_id mismatch in run_spec.json")

    evidence = docs["evidence_manifest.json"]
    generated_by_role: dict[str, dict[str, Any]] = {}
    for row in evidence.get("generated_files") or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        _require(role not in generated_by_role, f"duplicate generated-file role in evidence manifest: {role}")
        generated_by_role[role] = row
    file_rows: list[dict[str, Any]] = []
    for name in GENERATED_EVIDENCE_NAMES:
        path = candidate_dir / name
        row = generated_by_role.get(name)
        _require(row is not None, f"evidence manifest does not bind {name}")
        _require(str(row.get("sha256") or "").lower() == sha256_file(path), f"evidence hash mismatch for {name}")
        _require(int(row.get("bytes") or -1) == path.stat().st_size, f"evidence byte-size mismatch for {name}")
        file_rows.append(_file_row(name, path, target_model, template_product_id))
    evidence_path = candidate_dir / "evidence_manifest.json"
    file_rows.append(_file_row("evidence_manifest.json", evidence_path, target_model, template_product_id))

    asset_file_hash = sha256_file(candidate_dir / "asset_manifest.candidates.json")
    asset_document_hash = sha256_bytes(
        canonical_json(docs["asset_manifest.candidates.json"]).encode("utf-8")
    )
    embedded_asset_hash = docs["approved_source_manifest.template.json"].get("asset_manifest_sha256")
    if embedded_asset_hash is not None:
        _require(
            str(embedded_asset_hash).lower() in {asset_document_hash, asset_file_hash},
            "template asset_manifest_sha256 binding mismatch",
        )

    package_sha256 = sha256_bytes(
        canonical_json([{"name": row["name"], "bytes": row["bytes"], "sha256": row["sha256"]} for row in file_rows]).encode(
            "utf-8"
        )
    )
    return docs, {"files": file_rows, "sha256": package_sha256}


def _commercial_decision_errors(document: dict[str, Any], target_model: str) -> list[str]:
    errors: list[str] = []
    try:
        _validate_schema(document, "approved-commercial-decision.schema.json")
    except CompileBlocked as exc:
        return [str(exc)]
    if _normalized_model(document.get("target_model")) != target_model:
        errors.append("commercial decision target_model mismatch")
    price = document.get("price") or {}
    p25, p50, p75, approved = (price.get(key) for key in ("p25", "p50", "p75", "approved_value"))
    if not (p25 <= p50 <= p75):
        errors.append("commercial price percentiles must satisfy p25 <= p50 <= p75")
    if not (p25 <= approved <= p75):
        errors.append("commercial approved price must stay inside p25/p75")
    return errors


def _source_descriptor(
    *,
    field: str,
    field_decision: dict[str, Any],
    template_fields: dict[str, dict[str, Any]],
    decision_dir: Path,
    target_model: str,
) -> dict[str, Any]:
    selector = field_decision["source_selector"]
    kind = selector["kind"]
    if kind == "CANDIDATE_FIELD_SOURCE":
        selected_field = str(selector["candidate_field"])
        _require(selected_field in template_fields, f"source selector references unknown candidate field: {selected_field}")
        row = template_fields[selected_field]
        _require(str(row.get("decision") or "").upper() != "REJECT", f"candidate source field is REJECT: {selected_field}")
        path = Path(str(row.get("source_path") or ""))
        _require(path.is_absolute(), f"candidate source path is not absolute: {selected_field}")
        path = path.resolve()
        _require(path.is_file(), f"candidate source is missing: {selected_field}")
        observed_hash = sha256_file(path)
        _require(observed_hash == str(row.get("source_sha256") or "").lower(), f"candidate source hash mismatch: {selected_field}")
        if row.get("source_bytes") is not None:
            _require(path.stat().st_size == int(row["source_bytes"]), f"candidate source byte-size mismatch: {selected_field}")
        return {
            "path": path,
            "sha256": observed_hash,
            "bytes": path.stat().st_size,
            "source_role": "PRODUCT_FACT",
            "commercial": None,
        }

    path = _resolve_source_path(selector["path"], decision_dir=decision_dir)
    _verify_source_file(
        path,
        expected_hash=selector["sha256"],
        expected_bytes=selector["bytes"],
        label=f"{kind} source for {field}",
    )
    if kind == "COMMERCIAL_DECISION":
        commercial = read_json(path)
        _require(isinstance(commercial, dict), f"commercial decision must be a JSON object: {path}")
        errors = _commercial_decision_errors(commercial, target_model)
        _require(not errors, f"commercial decision is not qualified: {errors}")
        _require(
            str(commercial.get("decision_id")) == str(field_decision["decision_id"]),
            f"commercial decision_id mismatch for {field}",
        )
        _require(_commercial_value_matches(field, field_decision["value"], commercial), f"commercial value mismatch for {field}")
        return {
            "path": path,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "source_role": "COMMERCIAL_DECISION",
            "commercial": commercial,
        }
    return {
        "path": path,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "source_role": selector["source_role"],
        "commercial": None,
    }


def _normalize_asset_selections(values: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for value in values:
        _require(isinstance(value, dict), "selected asset must be an explicit approval object")
        row = dict(value)
        candidate_order = int(row["candidate_order"])
        upload_order = int(row["upload_order"])
        _require(candidate_order > 0, "selected asset candidate_order must be positive")
        _require(upload_order > 0, "selected asset upload_order must be positive")
        for decision_key in ("decision_id", "identity_decision_id", "upload_decision_id"):
            _require(
                isinstance(row.get(decision_key), str) and bool(str(row.get(decision_key)).strip()),
                f"selected asset requires explicit {decision_key}",
            )
        normalized.append(
            {
                "candidate_order": candidate_order,
                "upload_order": upload_order,
                "decision_id": str(row["decision_id"]),
                "identity_decision_id": str(row["identity_decision_id"]),
                "upload_decision_id": str(row["upload_decision_id"]),
            }
        )
    orders = [row["candidate_order"] for row in normalized]
    upload_orders = [row["upload_order"] for row in normalized]
    _require(len(orders) == len(set(orders)), "selected_asset_orders contains duplicate candidate orders")
    _require(len(upload_orders) == len(set(upload_orders)), "selected_asset_orders contains duplicate upload orders")
    return normalized


def _asset_role(candidate: dict[str, Any]) -> str:
    category = str(candidate.get("category") or "").casefold()
    media = _media_type(Path(str(candidate.get("local_path") or "")))
    if "video" in category or media.startswith("video/"):
        return "MAIN_VIDEO"
    return "MAIN_IMAGE"


def _build_documents(
    *,
    docs: dict[str, Any],
    package: dict[str, Any],
    decision: dict[str, Any],
    decision_path: Path,
    source_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    target_model = _normalized_model(decision["target_model"])
    template_product_id = str(decision["template_product_id"])
    run_id = str(decision["run_id"])
    profile = str(decision["profile"])
    approved_by = str(decision["approved_by"])
    approved_at = utc_now()
    decision_sha256 = sha256_file(decision_path)
    user_instruction_sha256 = sha256_bytes(str(decision["user_instruction"]).encode("utf-8"))
    decision_root = f"approval-{decision_sha256[:16]}"
    schema_bundle_sha256 = _schema_bundle_sha256()

    try:
        dynamic_schema = load_decision_schema_binding(
            decision["dynamic_schema_binding"],
            decision_dir=decision_path.parent,
            expected_target_model=target_model,
            expected_template_product_id=template_product_id,
            expected_run_id=run_id,
        )
    except Exception as exc:
        raise CompileBlocked(f"current dynamic schema binding failed: {exc}") from exc

    template_rows = docs["approved_source_manifest.template.json"].get("fields") or []
    _require(all(isinstance(row, dict) and FIELD_NAME_RE.fullmatch(str(row.get("field") or "")) for row in template_rows), "candidate template contains an invalid field row")
    template_fields = {str(row["field"]): row for row in template_rows}
    _require(len(template_fields) == len(template_rows), "candidate template contains duplicate field names")
    approved_field_decisions = decision["approved_fields"]
    localized_field_decisions = decision.get("approved_localized_fields") or []
    _require(
        isinstance(localized_field_decisions, list),
        "approved_localized_fields must be an array",
    )
    localized_by_key: dict[str, dict[str, Any]] = {}
    localized_identities: set[str] = set()
    for index, field_decision in enumerate(localized_field_decisions, 1):
        _require(isinstance(field_decision, dict), f"localized field decision is not an object: {index}")
        target = field_decision.get("write_target")
        value = field_decision.get("value")
        try:
            validate_localized_target(target, value)
        except LocalizedFieldError as exc:
            raise CompileBlocked(f"localized field decision {index} is invalid: {exc}") from exc
        identity = str(target["identity_sha256"])
        _require(identity not in localized_identities, f"duplicate localized field identity: {identity}")
        localized_identities.add(identity)
        field_key = f"localized.{target['logical_name']}.{str(target['locale']).lower()}"
        _require(FIELD_NAME_RE.fullmatch(field_key) is not None, f"invalid localized field key: {field_key}")
        _require(field_key not in localized_by_key, f"duplicate localized field locale: {field_key}")
        localized_by_key[field_key] = field_decision
    approved_names = set(approved_field_decisions)
    duplicate_targets = duplicate_registry_targets(approved_names)
    _require(not duplicate_targets, f"duplicate approved registry targets after alias normalization: {duplicate_targets}")
    blocked_names = {str(value) for value in decision["blocked_fields"]}
    all_names = set(template_fields)
    template_schema_binding = docs["approved_source_manifest.template.json"].get("dynamic_schema_binding") or {}
    _require(
        template_schema_binding.get("status") == "CURRENT_SCHEMA_BOUND",
        "candidate package is not bound to a current dynamic schema; rerun prepare with --dynamic-schema",
    )
    _require(
        str((template_schema_binding.get("capture") or {}).get("sha256") or "").lower()
        == dynamic_schema["capture"]["sha256"],
        "candidate package and approval decision use different dynamic schema captures",
    )
    _require(
        template_schema_binding.get("required_field_set_sha256")
        == dynamic_schema["required_field_set_sha256"],
        "candidate package dynamic required-field binding mismatch",
    )
    dynamic_required = set(dynamic_schema["required_field_ids"])
    _require(
        dynamic_required <= all_names,
        f"current dynamic schema has required fields absent from candidate review: {sorted(dynamic_required - all_names)}",
    )
    _require(
        not ({canonical_registry_field(field) for field in approved_names}
             & {canonical_registry_field(field) for field in blocked_names}),
        "a field cannot be both approved and blocked (including registry aliases)",
    )
    _require(not (approved_names - all_names), f"approved_fields contains unknown fields: {sorted(approved_names - all_names)}")
    _require(not (blocked_names - all_names), f"blocked_fields contains unknown fields: {sorted(blocked_names - all_names)}")
    _require(approved_names | blocked_names == all_names, f"silence is not approval; undecided fields: {sorted(all_names - approved_names - blocked_names)}")

    if profile != "PARTIAL_DRAFT_FILL_APPROVED_ONLY":
        _require(not blocked_names, "APPROVED_DRAFT cannot contain blocked fields")
    missing_dynamic_required = dynamic_required - approved_names - blocked_names
    _require(
        not missing_dynamic_required,
        f"current dynamic schema required fields have no explicit decision: {sorted(missing_dynamic_required)}",
    )

    commercial_names = {
        field
        for field, row in template_fields.items()
        if field in COMMERCIAL_FIELDS
        or str(row.get("source_disposition") or "").upper()
        == "BLOCKED_PENDING_APPROVED_INTERNAL_SOURCE"
    }
    for field in sorted(commercial_names & approved_names):
        selector_kind = str((approved_field_decisions[field].get("source_selector") or {}).get("kind") or "")
        _require(field in SUPPORTED_COMMERCIAL_FIELDS, f"commercial field has no supported qualified decision schema and must be blocked: {field}")
        _require(selector_kind == "COMMERCIAL_DECISION", f"commercial field lacks a qualified commercial decision and must be blocked: {field}")

    compiled_field_decisions = {**approved_field_decisions, **localized_by_key}
    source_groups: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    source_descriptors: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    approved_field_values: dict[str, Any] = {}
    for field in sorted(compiled_field_decisions):
        field_decision = compiled_field_decisions[field]
        value = field_decision.get("value")
        _require(value is not None and not (isinstance(value, str) and not value.strip()), f"approved field has an empty value: {field}")
        descriptor = _source_descriptor(
            field=field,
            field_decision=field_decision,
            template_fields=template_fields,
            decision_dir=decision_path.parent,
            target_model=target_model,
        )
        key = (
            str(descriptor["path"]).casefold(),
            descriptor["sha256"],
            descriptor["source_role"],
            str(field_decision["decision_id"]),
        )
        source_groups[key].append(field)
        source_descriptors[key] = descriptor

    sources: list[dict[str, Any]] = []
    source_id_by_field: dict[str, str] = {}
    for key in sorted(source_groups):
        descriptor = source_descriptors[key]
        fields = sorted(source_groups[key])
        source_id = f"source-{sha256_bytes(canonical_json({'key': key, 'fields': fields}).encode('utf-8'))[:20]}"
        source = {
            "source_id": source_id,
            "source_role": descriptor["source_role"],
            "record_disposition": "APPROVED_INTERNAL_SOURCE",
            "path": str(descriptor["path"]),
            "media_type": _media_type(descriptor["path"]),
            "bytes": descriptor["bytes"],
            "sha256": descriptor["sha256"],
            "decision_id": key[3],
            "approved_fields": fields,
            "usage_policy": _usage_policy(),
        }
        sources.append(source)
        for field in fields:
            source_id_by_field[field] = source_id
            value = compiled_field_decisions[field]["value"]
            approved_field_values[field] = {
                "value": value,
                "value_sha256": sha256_bytes(canonical_json(value).encode("utf-8")),
                "source_id": source_id,
                "decision_id": compiled_field_decisions[field]["decision_id"],
            }
            if compiled_field_decisions[field].get("write_target") is not None:
                approved_field_values[field]["write_target"] = compiled_field_decisions[field]["write_target"]

    candidate_assets = docs["asset_manifest.candidates.json"].get("assets") or []
    _require(all(isinstance(row, dict) for row in candidate_assets), "asset candidates must be objects")
    candidate_by_order: dict[int, dict[str, Any]] = {}
    for row in candidate_assets:
        order = int(row.get("candidate_order") or 0)
        _require(order > 0 and order not in candidate_by_order, f"duplicate or invalid asset candidate_order: {order}")
        candidate_by_order[order] = row
    selections = _normalize_asset_selections(decision["selected_asset_orders"])
    selected_assets: list[dict[str, Any]] = []
    observed_hashes: set[str] = set()
    observed_paths: set[str] = set()
    for selection in selections:
        order = selection["candidate_order"]
        _require(order in candidate_by_order, f"selected asset candidate_order is missing: {order}")
        candidate = candidate_by_order[order]
        reject_markers = {
            str(candidate.get("decision") or "").upper(),
            str(candidate.get("default_decision") or "").upper(),
            str(candidate.get("review_status") or "").upper(),
        }
        _require("REJECT" not in reject_markers and "REJECT" not in str(candidate.get("source_disposition") or "").upper(), f"selected asset is marked REJECT: {order}")
        _require(candidate.get("exists") is True and candidate.get("hash_match") is True, f"selected asset is missing or candidate hash mismatch: {order}")
        _require(candidate.get("exact_model") is True, f"selected asset is not exact-model evidence: {order}")
        _require(_normalized_model(candidate.get("target_model")) == target_model, f"selected asset target_model mismatch: {order}")
        path = Path(str(candidate.get("local_path") or ""))
        _require(path.is_absolute() and path.is_file(), f"selected asset file is missing: {order}")
        path = path.resolve()
        observed_hash = sha256_file(path)
        reported_hashes = {
            str(candidate.get(key)).lower()
            for key in ("sha256", "observed_sha256", "recorded_sha256")
            if candidate.get(key)
        }
        _require(reported_hashes and reported_hashes == {observed_hash}, f"selected asset hash mismatch: {order}")
        _require(int(candidate.get("bytes") or -1) == path.stat().st_size, f"selected asset byte-size mismatch: {order}")
        path_key = str(path).casefold()
        _require(observed_hash not in observed_hashes, f"selected assets contain duplicate content: {order}")
        _require(path_key not in observed_paths, f"selected assets contain duplicate paths: {order}")
        observed_hashes.add(observed_hash)
        observed_paths.add(path_key)

        role = _asset_role(candidate)
        media_type = _media_type(path)
        source_id = f"asset-source-{order}-{observed_hash[:12]}"
        source_field = f"{'main_video' if role == 'MAIN_VIDEO' else 'main_image'}.asset_{selection['upload_order']}"
        sources.append(
            {
                "source_id": source_id,
                "source_role": "APPROVED_ASSET_ORIGIN",
                "record_disposition": "APPROVED_INTERNAL_SOURCE",
                "path": str(path),
                "media_type": media_type,
                "bytes": path.stat().st_size,
                "sha256": observed_hash,
                "decision_id": selection["decision_id"],
                "approved_fields": [source_field],
                "usage_policy": _usage_policy(),
            }
        )
        asset = {
            "asset_id": f"asset-{order}-{observed_hash[:16]}",
            "role": role,
            "upload_order": selection["upload_order"],
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": observed_hash,
            "media_type": media_type,
            "record_disposition": "APPROVED_INTERNAL_SOURCE",
            "source_id": source_id,
            "source_decision_id": selection["decision_id"],
            "product_identity": {
                "target_model": target_model,
                "match_status": "MATCHED",
                "match_method": "HASH_BOUND_DERIVATION"
                if str(candidate.get("source_type") or "") == "HASH_BOUND_PRODUCT_LOCK"
                else "HUMAN_VISUAL_REVIEW",
                "decision_id": selection["identity_decision_id"],
            },
            "duration_seconds": None,
            "compliance": {
                "format_ok": True,
                "size_ok": True,
                "dimensions_ok": True,
                "duration_ok": None,
                "identity_ok": True,
                "policy_ok": True,
                "upload_ready": True,
            },
            "upload_approval": {
                "approved": True,
                "decision_id": selection["upload_decision_id"],
                "approved_by": approved_by,
                "approved_at": approved_at,
            },
            "derived_from_template": False,
            "derived_from_historical_record": False,
        }
        if role == "MAIN_IMAGE":
            width, height = _image_dimensions(path)
            asset["dimensions"] = {"width": width, "height": height}
        else:
            duration = candidate.get("duration_seconds")
            _require(isinstance(duration, (int, float)) and duration > 0, f"selected video lacks duration_seconds: {order}")
            asset["duration_seconds"] = duration
        selected_assets.append(asset)

    main_images = [row for row in selected_assets if row["role"] == "MAIN_IMAGE"]
    main_videos = [row for row in selected_assets if row["role"] == "MAIN_VIDEO"]
    _require(4 <= len(main_images) <= 6, "approval requires four to six unique main images")
    _require(len(main_videos) <= 1, "approval allows at most one main video")

    source_inventory_rows = [
        row for row in docs["evidence_manifest.json"].get("inputs") or [] if row.get("role") == "target_source_inventory"
    ]
    _require(len(source_inventory_rows) == 1, "evidence manifest must bind exactly one target_source_inventory")
    inventory = source_inventory_rows[0]
    inventory_path = Path(str(inventory.get("path") or ""))
    _verify_source_file(
        inventory_path,
        expected_hash=inventory.get("sha256"),
        expected_bytes=inventory.get("bytes"),
        label="target_source_inventory",
    )

    common_approval = {
        "status": "APPROVED",
        "explicit": True,
        "origin": "HUMAN_REVIEWED_CANDIDATE_PACKAGE",
        "approved_by": approved_by,
        "approved_at": approved_at,
        "approval_package_sha256": package["sha256"],
    }
    source_manifest: dict[str, Any] = {
        "schema_version": "alibaba.approved-source-manifest.v1",
        "manifest_id": f"{decision_root}-source",
        "run_id": run_id,
        "target_model": target_model,
        "generated_at": approved_at,
        "approval": {
            **common_approval,
            "scope": "SIMILAR_DRAFT_SOURCE_USE_ONLY",
            "decision_id": f"{decision_root}-source-package",
            "notes_sha256": user_instruction_sha256,
        },
        "bindings": {
            "candidate_package_sha256": package["sha256"],
            "source_inventory_sha256": sha256_file(inventory_path),
            "field_registry_sha256": sha256_file(FIELD_REGISTRY),
            "schema_bundle_sha256": schema_bundle_sha256,
            "target_model_normalized": target_model,
            "template_product_id": template_product_id,
        },
        "dynamic_schema": dynamic_schema,
        "source_count": len(sources),
        "rejected_source_count": 0,
        "sources": sources,
        "approved_field_values": approved_field_values,
        "write_policy": {
            "candidate_external_write_enabled": False,
            "draft_save_requires_separate_approval": True,
            "final_write_enabled": False,
            "raw_disposition_may_grant_approval": False,
        },
    }
    if profile == "PARTIAL_DRAFT_FILL_APPROVED_ONLY":
        source_manifest["draft_field_policy"] = {
            "mode": "PARTIAL_DRAFT_FILL_APPROVED_ONLY",
            "explicit_user_instruction_sha256": user_instruction_sha256,
            "blocked_fields": sorted(blocked_names),
            "save_incomplete_draft_allowed": True,
            "publish_ready_claim_allowed": False,
            "final_write_enabled": False,
        }

    _validate_schema(source_manifest, "approved-source-manifest.schema.json")
    _write_new_json(source_path, source_manifest)
    asset_manifest = {
        "schema_version": "alibaba.approved-asset-manifest.v1",
        "manifest_id": f"{decision_root}-asset",
        "run_id": run_id,
        "target_model": target_model,
        "generated_at": approved_at,
        "approval": {
            **common_approval,
            "scope": "SIMILAR_DRAFT_ASSET_USE_ONLY",
            "decision_id": f"{decision_root}-asset-package",
        },
        "bindings": {
            "approved_source_manifest_sha256": sha256_file(source_path),
            "candidate_package_sha256": package["sha256"],
            "field_registry_sha256": sha256_file(FIELD_REGISTRY),
            "schema_bundle_sha256": schema_bundle_sha256,
            "target_model_normalized": target_model,
        },
        "asset_count": len(selected_assets),
        "main_image_count": len(main_images),
        "main_video_count": len(main_videos),
        "asset_sequence_sha256": approved_asset_sequence_sha256(selected_assets),
        "assets": selected_assets,
        "rules": {
            "main_image_minimum_count": 4,
            "main_image_maximum_count": 6,
            "main_video_maximum_count": 1,
            "unique_asset_ids": True,
            "unique_upload_orders": True,
            "hash_recheck_before_upload": True,
            "template_media_may_be_inherited": False,
            "runtime_count_verification_required": True,
        },
        "write_policy": {
            "candidate_external_write_enabled": False,
            "upload_requires_current_probe_receipt": True,
            "draft_save_requires_separate_approval": True,
            "final_write_enabled": False,
        },
    }
    _validate_schema(asset_manifest, "approved-asset-manifest.schema.json")
    preflight = validate_approved_manifest(
        source_manifest,
        asset_manifest,
        manifest_path=source_path,
    )
    _require(preflight.get("status") == "PASS", f"compiled approval failed canonical preflight: {preflight.get('errors')}")

    receipt = {
        "schema_version": "sellerkit.approval-compile-receipt.v1",
        "status": "PASS",
        "generated_at": approved_at,
        "run_id": run_id,
        "target_model": target_model,
        "template_product_id": template_product_id,
        "profile": profile,
        "decision": {
            "path": str(decision_path),
            "bytes": decision_path.stat().st_size,
            "sha256": decision_sha256,
            "approved_by": approved_by,
            "user_instruction_sha256": user_instruction_sha256,
        },
        "candidate_package": package,
        "approved_field_count": len(compiled_field_decisions),
        "approved_localized_field_count": len(localized_by_key),
        "blocked_fields": sorted(blocked_names),
        "approved_asset_count": len(selected_assets),
        "draft_fill_ready": True,
        "publish_ready": False,
        "final_write_enabled": False,
        "canonical_preflight": preflight,
        "outputs": {
            "approved_source_manifest.json": {
                "bytes": source_path.stat().st_size,
                "sha256": sha256_file(source_path),
            },
            "approved_asset_manifest.json": {
                "bytes": len((canonical_json(asset_manifest) + "\n").encode("utf-8")),
                "sha256": sha256_bytes((canonical_json(asset_manifest) + "\n").encode("utf-8")),
            },
        },
    }
    return source_manifest, asset_manifest, receipt


def compile_approval(
    candidate_dir: Path,
    decision_path: Path,
    output_dir: Path,
    *,
    replace_func: Callable[[str | os.PathLike[str], str | os.PathLike[str]], Any] = os.replace,
) -> dict[str, Any]:
    candidate_dir = candidate_dir.resolve()
    decision_path = decision_path.resolve()
    output_dir = output_dir.resolve()
    _require(not output_dir.exists(), f"refusing to overwrite approval output: {output_dir}")
    _require(decision_path.is_file(), f"human decision JSON is missing: {decision_path}")
    decision = read_json(decision_path)
    _require(isinstance(decision, dict), "human decision JSON must be an object")
    _validate_schema(decision, "approval-decision.schema.json")
    docs, package = _load_and_bind_inputs(candidate_dir, decision)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        source_path = staging / "approved_source_manifest.json"
        source_manifest, asset_manifest, receipt = _build_documents(
            docs=docs,
            package=package,
            decision=decision,
            decision_path=decision_path,
            source_path=source_path,
        )
        _write_new_json(staging / "approved_asset_manifest.json", asset_manifest)
        receipt["outputs"]["approved_asset_manifest.json"] = {
            "bytes": (staging / "approved_asset_manifest.json").stat().st_size,
            "sha256": sha256_file(staging / "approved_asset_manifest.json"),
        }
        receipt["outputs"]["approved_source_manifest.json"] = {
            "bytes": source_path.stat().st_size,
            "sha256": sha256_file(source_path),
        }
        _write_new_json(staging / "compile_receipt.json", receipt)
        _require(not output_dir.exists(), f"refusing to overwrite approval output: {output_dir}")
        replace_func(staging, output_dir)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return receipt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile an explicit human decision into canonical Alibaba draft manifests")
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        args = parse_args()
        result = compile_approval(candidate_dir=args.candidate_dir, decision_path=args.decision, output_dir=args.output)
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(3)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
