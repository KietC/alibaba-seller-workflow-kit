from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from draft_core import GuardError, read_json, sha256_file
from dynamic_schema_gate import (
    FIELD_REGISTRY_PATH,
    OFFICIAL_SCHEMA_GET_ENDPOINT,
    OFFICIAL_SCHEMA_GET_OPERATION,
    derive_dynamic_fields,
    field_set_sha256,
    load_and_reparse_official_schema,
    receipt_sha256,
    required_field_set_sha256,
    validate_dynamic_schema_capture,
)


SCRIPT_PATH = Path(__file__).resolve()


def _file_binding(path: Path) -> dict[str, Any]:
    path = path.resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _parse_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise GuardError("captured_at must include a timezone")
    return parsed


def normalize_capture(
    *,
    raw_payload: Path,
    output: Path,
    run_id: str,
    target_model: str,
    template_product_id: str,
    category_id: str,
    product_type: str,
    source_kind: str,
    source_endpoint: str,
    official_schema: Path | None = None,
    source_operation: str = OFFICIAL_SCHEMA_GET_OPERATION,
    field_observations: Path | None = None,
    captured_at: str | None = None,
    valid_minutes: int | None = None,
) -> dict[str, Any]:
    """Normalize a live-eligible dynamic schema from one official XML parse.

    Caller-authored field observations are intentionally rejected.  A visible
    UI observation has no unified browser-capture provenance in this adapter,
    so it is LOCAL_CONTRACT_ONLY/BLOCKED_LIVE and cannot produce this artifact.
    """

    raw_payload = raw_payload.resolve()
    output = output.resolve()
    if output.exists():
        raise GuardError(f"refusing to overwrite dynamic schema capture: {output}")
    if field_observations is not None:
        raise GuardError("arbitrary field observations are forbidden; fields must derive from official XML")
    if source_kind == "VISIBLE_UI_ZERO_WRITE":
        raise GuardError(
            "VISIBLE_UI_ZERO_WRITE has no unified browser-capture provenance: LOCAL_CONTRACT_ONLY/BLOCKED_LIVE"
        )
    if source_kind != "OFFICIAL_SCHEMA_GET":
        raise GuardError("unsupported read-only dynamic schema source kind")
    if source_endpoint != OFFICIAL_SCHEMA_GET_ENDPOINT:
        raise GuardError("source endpoint must be the exact official TOP HTTPS endpoint")
    if source_operation != OFFICIAL_SCHEMA_GET_OPERATION:
        raise GuardError("source operation must be alibaba.icbu.product.schema.get")
    if official_schema is None:
        raise GuardError("normalized official schema binding is required")
    official_schema = official_schema.resolve()
    if not raw_payload.is_file() or raw_payload.stat().st_size < 1:
        raise GuardError("raw official XML payload must be a readable non-empty file")
    if not official_schema.is_file():
        raise GuardError("normalized official schema must be a readable file")
    if not str(template_product_id).isdigit():
        raise GuardError("template_product_id must be numeric")

    official_document = load_and_reparse_official_schema(
        official_schema,
        raw_xml_path=raw_payload,
        expected_run_id=run_id,
        expected_target_model=target_model,
        expected_template_product_id=str(template_product_id),
        expected_category_id=str(category_id),
        expected_product_type=str(product_type),
    )
    official_captured_at = str(official_document["captured_at"])
    official_expires_at = str(official_document["expires_at"])
    lifetime_minutes = int(
        (_parse_time(official_expires_at) - _parse_time(official_captured_at)).total_seconds() // 60
    )
    if captured_at is not None and _parse_time(captured_at) != _parse_time(official_captured_at):
        raise GuardError("captured_at must equal the normalized official schema capture time")
    if valid_minutes is not None and valid_minutes != lifetime_minutes:
        raise GuardError("valid_minutes must equal the normalized official schema lifetime")

    fields = derive_dynamic_fields(official_document)
    document = {
        "schema_version": "sellerkit.alibaba-current-publish-schema.v2",
        "capture_id": "PENDING_RECEIPT_DERIVATION",
        "run_id": str(run_id),
        "target_model": target_model.strip().upper(),
        "template_product_id": str(template_product_id),
        "captured_at": official_captured_at,
        "expires_at": official_expires_at,
        "category": {"id": str(category_id), "leaf": True, "product_type": str(product_type)},
        "source": {
            "kind": "OFFICIAL_SCHEMA_GET",
            "endpoint": OFFICIAL_SCHEMA_GET_ENDPOINT,
            "operation": OFFICIAL_SCHEMA_GET_OPERATION,
            "read_only": True,
            "execution_scope": "OFFICIAL_XML_BOUND",
        },
        "raw_payload": _file_binding(raw_payload),
        "provenance": {
            "status": "OFFICIAL_XML_SAME_SOURCE_REPARSED",
            "normalized_official_schema": _file_binding(official_schema),
            "raw_xml": _file_binding(raw_payload),
            "official_field_paths_sha256": official_document["field_paths_sha256"],
            "official_rule_graph_sha256": official_document["rule_graph_sha256"],
            "field_derivation": {
                "algorithm": "OFFICIAL_XML_FIELD_TREE_V1",
                "registry_sha256": sha256_file(FIELD_REGISTRY_PATH),
            },
        },
        "normalizer": {"tool_sha256": sha256_file(SCRIPT_PATH), "receipt_sha256": "0" * 64},
        "fields": fields,
        "field_set_sha256": "0" * 64,
        "required_field_set_sha256": "0" * 64,
        "final_write_enabled": False,
    }
    document["field_set_sha256"] = field_set_sha256(document)
    document["required_field_set_sha256"] = required_field_set_sha256(document)
    document["normalizer"]["receipt_sha256"] = receipt_sha256(document)
    document["capture_id"] = f"dynamic-schema-{document['normalizer']['receipt_sha256'][:20]}"

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        binding = validate_dynamic_schema_capture(
            output,
            expected_target_model=target_model,
            expected_template_product_id=str(template_product_id),
            expected_run_id=run_id,
        )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {
        "schema_version": "sellerkit.dynamic-schema-normalize-receipt.v2",
        "status": "PASS_CURRENT_SCHEMA_BOUND",
        "capture": binding,
        "official_schema_sha256": sha256_file(official_schema),
        "raw_xml_sha256": sha256_file(raw_payload),
        "field_derivation": "OFFICIAL_XML_FIELD_TREE_V1",
        "final_write_enabled": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize one official schema.get XML parse into a live-eligible dynamic publish schema"
    )
    parser.add_argument("--raw-payload", type=Path, required=True, help="original itemSchema XML file")
    parser.add_argument("--official-schema", type=Path, required=True, help="parse_official_schema_xml.py output")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-model", required=True)
    parser.add_argument("--template-product-id", required=True)
    parser.add_argument("--category-id", required=True)
    parser.add_argument("--product-type", required=True)
    parser.add_argument("--source-kind", choices=("OFFICIAL_SCHEMA_GET", "VISIBLE_UI_ZERO_WRITE"), required=True)
    parser.add_argument("--source-endpoint", required=True)
    parser.add_argument("--source-operation", default=OFFICIAL_SCHEMA_GET_OPERATION)
    parser.add_argument("--captured-at")
    parser.add_argument("--valid-minutes", type=int)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        result = normalize_capture(**vars(parse_args()))
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(3)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
