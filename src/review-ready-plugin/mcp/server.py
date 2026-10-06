from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from review_ready.config import PRIORITY_MODELS, default_paths
from review_ready.package import compile_review_package, export_evidence_receipt, record_review_decision
from review_ready.store import build_database, inspect_sources
from review_ready.utils import ensure_within


SERVER_NAME = "SellerKit Alibaba Review Ready"
SERVER_VERSION = "0.4.0"
PROHIBITED_TOOL_FRAGMENTS = (
    "publish", "submit", "draft", "upload", "browser", "click", "fill", "schema_add", "remote_write"
)


TOOLS = [
    {
        "name": "inspect_sources",
        "title": "Inspect Frozen SellerKit and Alibaba Sources",
        "description": "Verify the approved frozen website replica, Alibaba catalog, and disposition overlay without writing any remote system.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "reconcile_catalogs",
        "title": "Reconcile Website and Alibaba Catalogs",
        "description": "Build the local sanitized SQLite catalog and exact-model reconciliation. Writes only under the allowlisted local V1 project root.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "materialize_website_images",
        "title": "Materialize Official Website Images",
        "description": (
            "Validate and copy cached official images into a local SHA-256 store, then use public HTTPS GETs only "
            "to catalog.example.invalid for unresolved official image URLs. It never contacts or writes Alibaba."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "roles": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["main_image", "gallery"]},
                    "minItems": 1,
                    "maxItems": 2,
                    "uniqueItems": True,
                },
                "workers": {"type": "integer", "minimum": 1, "maximum": 8},
                "allow_network": {"type": "boolean"},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
                "max_bytes": {"type": "integer", "minimum": 512, "maximum": 268435456},
                "attempts": {"type": "integer", "minimum": 1, "maximum": 5},
                "strict_current_r3": {"type": "boolean"},
                "build_main_view": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "build_vision_features",
        "title": "Build Local Visual and OCR Features",
        "description": (
            "Build local visual/OCR evidence using isolated local model workers. Inputs and outputs stay under "
            "the allowlisted project; no Alibaba remote action is available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cpu_workers": {"type": "integer", "minimum": 1, "maximum": 61},
                "include_openclip": {"type": "boolean"},
                "include_ppocr": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "build_full_match",
        "title": "Build Exhaustive Website-to-Alibaba Local Match Index",
        "description": (
            "Score all current website products against every eligible local Alibaba historical record, produce "
            "forced top-1/top-5 review evidence, and exclude waste records from content and image scoring. "
            "This is local analysis only and cannot create drafts or publish."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workers": {
                    "anyOf": [
                        {"type": "string", "enum": ["auto"]},
                        {"type": "integer", "minimum": 1, "maximum": 61},
                    ]
                },
                "visual_scores_path": {"type": "string", "minLength": 1},
                "ocr_scores_path": {"type": "string", "minLength": 1},
                "update_database": {"type": "boolean"},
                "vision_required": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "compile_review_package",
        "title": "Compile Local Review Package",
        "description": "Compile a local-only ad-hoc evidence package for one SellerKit model. It cannot open Alibaba, alter the current priority index, save a remote draft, or publish.",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string", "minLength": 1}},
            "required": ["model"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "record_review_decision",
        "title": "Record Local Human Review Decision",
        "description": "Append a hash-chained self-attested field-review note. V1 does not promote notes into approved internal facts and exposes no remote action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "minLength": 1},
                "field": {"type": "string", "minLength": 1},
                "decision": {
                    "type": "string",
                    "enum": ["APPROVE_FIELD", "REJECT_FIELD", "NEEDS_CHANGES"],
                },
                "reviewer_id": {"type": "string", "minLength": 1},
                "reviewer_role": {"type": "string", "minLength": 1},
                "rationale": {"type": "string", "minLength": 1},
            },
            "required": ["model", "field", "decision", "reviewer_id", "reviewer_role", "rationale"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "export_evidence_receipt",
        "title": "Export Local Evidence Receipt",
        "description": "Hash current local V1 artifacts and write an evidence receipt under the local project evidence directory.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(request_id: Any, value: dict[str, Any], is_error: bool = False) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)}],
                "structuredContent": value,
                "isError": is_error,
            },
        }
    )


def error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _matches_schema(value: Any, rule: dict[str, Any]) -> bool:
    alternatives = rule.get("anyOf") or rule.get("oneOf")
    if alternatives is not None:
        return any(_matches_schema(value, item) for item in alternatives)
    expected = rule.get("type")
    type_ok = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(expected, True)
    if not type_ok:
        return False
    if "enum" in rule and value not in rule["enum"]:
        return False
    if isinstance(value, str) and len(value.strip()) < int(rule.get("minLength", 0)):
        return False
    if isinstance(value, list):
        if len(value) < int(rule.get("minItems", 0)):
            return False
        if "maxItems" in rule and len(value) > int(rule["maxItems"]):
            return False
        if rule.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            return False
        item_rule = rule.get("items")
        if item_rule and any(not _matches_schema(item, item_rule) for item in value):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            return False
        if "maximum" in rule and value > rule["maximum"]:
            return False
        if "exclusiveMinimum" in rule and value <= rule["exclusiveMinimum"]:
            return False
        if "exclusiveMaximum" in rule and value >= rule["exclusiveMaximum"]:
            return False
    return True


def _project_input_path(paths: Any, raw_path: str, label: str) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = paths.project_root / candidate
    resolved = ensure_within(candidate, paths.project_root)
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} does not exist under the project root: {resolved}")
    return resolved


def _build_vision_features(paths: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from review_ready.vision import (
        VisionRuntimePaths,
        build_visual_input_index,
        run_gate0,
        run_visual_feature_pipeline,
    )

    include_openclip = bool(arguments.get("include_openclip", True))
    include_ppocr = bool(arguments.get("include_ppocr", False))
    runtime = VisionRuntimePaths(project_root=paths.project_root)
    index_path = paths.vision_work_root / "visual_input_index.json"
    index = build_visual_input_index(output_path=index_path, runtime=runtime)
    gate0 = run_gate0(
        index,
        output_root=paths.vision_work_root / "gate0",
        runtime=runtime,
        include_openclip=include_openclip,
        include_ppocr=include_ppocr,
    )
    if gate0.get("status") != "PASS_GATE0_FEATURES":
        return {
            "status": "BLOCKED_VISION_GATE0_NOT_PASSED",
            "index": {"path": str(index_path), "sha256": index.get("artifact_sha256"), "counts": index.get("counts")},
            "gate0": gate0,
            "external_write_enabled": False,
            "browser_actions": 0,
            "remote_drafts": 0,
            "publish_requests": 0,
        }
    pipeline = run_visual_feature_pipeline(
        index_path=index_path,
        output_root=paths.vision_work_root / "features",
        runtime=runtime,
        cpu_workers=int(arguments.get("cpu_workers", 16)),
        include_openclip=include_openclip,
        include_ppocr=include_ppocr,
    )
    incomplete: list[str] = []
    if pipeline.get("status") != "PASS_FEATURES_ONLY_NO_MATCH_DECISION":
        incomplete.append(f"pipeline={pipeline.get('status')}")
    if include_openclip and (pipeline.get("openclip") or {}).get("status") != "PASS":
        incomplete.append(f"openclip={(pipeline.get('openclip') or {}).get('status')}")
    if include_ppocr and (pipeline.get("ppocr") or {}).get("status") != "PASS":
        incomplete.append(f"ppocr={(pipeline.get('ppocr') or {}).get('status')}")
    if incomplete:
        return {
            "status": "BLOCKED_VISION_FEATURE_PIPELINE_INCOMPLETE",
            "reason": ", ".join(incomplete),
            "index": {"path": str(index_path), "sha256": index.get("artifact_sha256"), "counts": index.get("counts")},
            "gate0": gate0,
            "pipeline": pipeline,
            "external_write_enabled": False,
            "browser_actions": 0,
            "remote_drafts": 0,
            "publish_requests": 0,
        }
    return {
        "status": pipeline.get("status"),
        "index": {"path": str(index_path), "sha256": index.get("artifact_sha256"), "counts": index.get("counts")},
        "gate0": gate0,
        "pipeline": pipeline,
        "external_write_enabled": False,
        "browser_actions": 0,
        "remote_drafts": 0,
        "publish_requests": 0,
    }


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if any(fragment in name.lower() for fragment in PROHIBITED_TOOL_FRAGMENTS):
        return {
            "status": "BLOCKED_REMOTE_WRITE_NOT_AVAILABLE",
            "requested_tool": name,
            "external_write_enabled": False,
        }
    tool = next((item for item in TOOLS if item["name"] == name), None)
    if tool is None:
        return {
            "status": "BLOCKED_REMOTE_WRITE_NOT_AVAILABLE",
            "requested_tool": name,
            "message": "Unknown or unavailable tool. V1 exposes no browser, draft, upload, submit, or publish capability.",
            "external_write_enabled": False,
        }
    schema = tool["inputSchema"]
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    extra = sorted(set(arguments) - set(properties))
    missing = sorted(key for key in required if key not in arguments)
    invalid: list[str] = []
    for key, value in arguments.items():
        rule = properties.get(key) or {}
        if not _matches_schema(value, rule):
            invalid.append(key)
    if extra or missing or invalid:
        return {
            "status": "BLOCKED_INVALID_TOOL_ARGUMENTS",
            "requested_tool": name,
            "extra": extra,
            "missing": missing,
            "invalid": sorted(set(invalid)),
            "external_write_enabled": False,
        }
    paths = default_paths()
    if name == "inspect_sources":
        return inspect_sources(paths)
    if name == "reconcile_catalogs":
        return build_database(paths)
    if name == "materialize_website_images":
        from review_ready.images import build_website_main_image_view, materialize_website_images

        roles = tuple(arguments.get("roles") or ("main_image", "gallery"))
        materialization = materialize_website_images(
            paths,
            roles=roles,
            workers=int(arguments.get("workers", 8)),
            allow_network=bool(arguments.get("allow_network", True)),
            timeout_seconds=float(arguments.get("timeout_seconds", 30.0)),
            max_bytes=int(arguments.get("max_bytes", 64 * 1024 * 1024)),
            attempts=int(arguments.get("attempts", 3)),
            strict_current_r3=bool(arguments.get("strict_current_r3", True)),
        )
        if "main_image" in roles and bool(arguments.get("build_main_view", True)):
            materialization = {
                **materialization,
                "main_image_view": build_website_main_image_view(paths),
            }
        return materialization
    if name == "build_vision_features":
        return _build_vision_features(paths, arguments)
    if name == "build_full_match":
        from review_ready.full_match import build_full_match

        visual_path = (
            _project_input_path(paths, str(arguments["visual_scores_path"]), "visual_scores_path")
            if arguments.get("visual_scores_path")
            else None
        )
        ocr_path = (
            _project_input_path(paths, str(arguments["ocr_scores_path"]), "ocr_scores_path")
            if arguments.get("ocr_scores_path")
            else None
        )
        return build_full_match(
            paths,
            workers=arguments.get("workers", "auto"),
            visual_scores_path=visual_path,
            ocr_scores_path=ocr_path,
            update_database=bool(arguments.get("update_database", True)),
            vision_required=bool(arguments.get("vision_required", False)),
        )
    if name == "compile_review_package":
        return compile_review_package(paths, str(arguments.get("model") or ""))
    if name == "record_review_decision":
        return record_review_decision(
            paths,
            str(arguments.get("model") or ""),
            str(arguments.get("field") or ""),
            str(arguments.get("decision") or ""),
            str(arguments.get("reviewer_id") or ""),
            str(arguments.get("reviewer_role") or ""),
            str(arguments.get("rationale") or ""),
        )
    if name == "export_evidence_receipt":
        return export_evidence_receipt(paths)
    raise RuntimeError(f"Unreachable tool dispatch branch: {name}")


def handle(message: dict[str, Any]) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2025-11-25"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "This server performs local review, official-image materialization, local visual feature extraction, and exhaustive "
                        "catalog matching. Its only open-world operation is validated public HTTPS image GET to catalog.example.invalid. "
                        "It has no Alibaba browser, upload, remote draft, submit, publish, or other remote-write capability."
                    ),
                },
            }
        )
        return
    if method == "ping":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            value = call_tool(name, arguments)
            result(
                request_id,
                value,
                is_error=str(value.get("status") or "").startswith("BLOCKED"),
            )
        except Exception as exc:
            result(
                request_id,
                {
                    "status": "BLOCKED_OR_FAILED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "external_write_enabled": False,
                    "browser_actions": 0,
                    "remote_drafts": 0,
                    "publish_requests": 0,
                },
                is_error=True,
            )
        return
    if request_id is not None:
        error(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("method"):
            handle(message)


if __name__ == "__main__":
    main()
