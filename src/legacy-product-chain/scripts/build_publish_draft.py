#!/usr/bin/env python3
"""Build a source-grounded, non-executable Alibaba publish draft."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_SOURCE_DISPOSITIONS = frozenset({"APPROVED_INTERNAL_SOURCE"})
REJECTED_SOURCE_DISPOSITIONS = frozenset(
    {
        "UNUSABLE_WASTE_RECORD",
        "BLOCKED_UNCLASSIFIED",
        "HISTORICAL_REFERENCE_ONLY",
    }
)
KNOWN_SOURCE_DISPOSITIONS = ALLOWED_SOURCE_DISPOSITIONS | REJECTED_SOURCE_DISPOSITIONS
MANIFEST_PARSE_ERRORS_KEY = "__manifest_parse_errors__"
MANIFEST_LINE_KEY = "__manifest_line__"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_disposition_jsonl(text: str) -> Any:
    """Parse JSONL rows while preserving line-numbered errors for fail-closed output."""
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(
                {
                    "code": "invalid_disposition_jsonl_line",
                    "source_id": None,
                    "field": None,
                    "detail": f"line {line_number}: {error.msg} at column {error.colno}",
                }
            )
            continue
        if not isinstance(value, dict):
            errors.append(
                {
                    "code": "invalid_disposition_jsonl_row",
                    "source_id": None,
                    "field": None,
                    "detail": f"line {line_number}: expected object, got {type(value).__name__}",
                }
            )
            continue
        rows.append({**value, MANIFEST_LINE_KEY: line_number})
    if errors:
        return {"records": rows, MANIFEST_PARSE_ERRORS_KEY: errors}
    return rows


def load_disposition_manifest(path: Path) -> Any:
    """Load JSON manifests or JSONL detected by extension/content."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".jsonl":
        return parse_disposition_jsonl(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return parse_disposition_jsonl(text)


def status_for_fact(fact: dict[str, Any]) -> str:
    mapping = {
        "verified": "ready",
        "needs_review": "needs_review",
        "blocked": "blocked",
        "not_applicable": "omitted",
    }
    return mapping[fact["status"]]


def field_from_fact(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "logical_name": fact["field"],
        "value": fact.get("value"),
        "status": status_for_fact(fact),
        "source_ids": fact.get("source_ids", []),
        "reason": fact.get("note"),
    }


def disposition_rows(manifest: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize the supported disposition-manifest shapes without guessing values."""
    if manifest is None:
        return [], []
    errors: list[dict[str, Any]] = []
    if isinstance(manifest, list):
        rows = manifest
    elif isinstance(manifest, dict):
        parse_errors = manifest.get(MANIFEST_PARSE_ERRORS_KEY)
        if isinstance(parse_errors, list):
            errors.extend(error for error in parse_errors if isinstance(error, dict))
        if isinstance(manifest.get("sources"), list):
            rows = manifest["sources"]
        elif isinstance(manifest.get("records"), list):
            rows = manifest["records"]
        elif isinstance(manifest.get("dispositions"), dict):
            rows = [
                {
                    "source_id": source_id,
                    "record_disposition": (
                        value.get("record_disposition", value.get("disposition"))
                    )
                    if isinstance(value, dict)
                    else value,
                }
                for source_id, value in manifest["dispositions"].items()
            ]
        elif any(manifest.get(key) is not None for key in ("source_id", "product_id", "record_id")):
            rows = [manifest]
        else:
            return [], errors + [
                {
                    "code": "invalid_disposition_manifest_shape",
                    "source_id": None,
                    "field": None,
                    "detail": "expected sources[], records[], dispositions{}, or a top-level array",
                }
            ]
    else:
        return [], errors + [
            {
                "code": "invalid_disposition_manifest_shape",
                "source_id": None,
                "field": None,
                "detail": f"expected object or array, got {type(manifest).__name__}",
            }
        ]

    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(
                {
                    "code": "invalid_disposition_manifest_row",
                    "source_id": None,
                    "field": None,
                    "detail": f"row {index + 1} is not an object",
                }
            )
            continue
        location = (
            f"line {row[MANIFEST_LINE_KEY]}"
            if isinstance(row.get(MANIFEST_LINE_KEY), int)
            else f"row {index + 1}"
        )
        source_id = row.get("source_id") or row.get("product_id") or row.get("record_id")
        disposition = row.get("record_disposition", row.get("disposition"))
        if source_id is None:
            errors.append(
                {
                    "code": "manifest_row_missing_source_id",
                    "source_id": None,
                    "field": None,
                    "detail": location,
                }
            )
            continue
        normalized.append(
            {
                "source_id": str(source_id),
                "record_disposition": disposition if isinstance(disposition, str) else None,
            }
        )
    return normalized, errors


def evaluate_source_gate(master: dict[str, Any], disposition_manifest: Any = None) -> dict[str, Any]:
    manifest_rows, errors = disposition_rows(disposition_manifest)
    manifest_map: dict[str, str | None] = {}
    for row in manifest_rows:
        source_id = row["source_id"]
        disposition = row["record_disposition"]
        if source_id in manifest_map and manifest_map[source_id] != disposition:
            errors.append(
                {
                    "code": "conflicting_manifest_disposition",
                    "source_id": source_id,
                    "field": None,
                    "detail": f"{manifest_map[source_id]!r} != {disposition!r}",
                }
            )
            continue
        manifest_map[source_id] = disposition

    source_rows = master.get("sources", [])
    if not isinstance(source_rows, list):
        source_rows = []
        errors.append(
            {
                "code": "invalid_source_registry",
                "source_id": None,
                "field": None,
                "detail": "master.sources must be an array",
            }
        )

    source_registry: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(source_rows):
        if not isinstance(source, dict) or not source.get("source_id"):
            errors.append(
                {
                    "code": "invalid_source_registry_row",
                    "source_id": None,
                    "field": None,
                    "detail": f"row {index + 1}",
                }
            )
            continue
        source_id = str(source["source_id"])
        if source_id in source_registry:
            errors.append(
                {
                    "code": "duplicate_source_id",
                    "source_id": source_id,
                    "field": None,
                    "detail": None,
                }
            )
            continue
        source_registry[source_id] = source

    if not source_registry:
        errors.append(
            {
                "code": "no_registered_sources",
                "source_id": None,
                "field": None,
                "detail": None,
            }
        )

    fact_rows = master.get("facts", []) + master.get("claims", []) + list(master.get("commercial", {}).values())
    referenced_source_ids: set[str] = set()
    for fact in fact_rows:
        if not isinstance(fact, dict):
            errors.append(
                {
                    "code": "invalid_fact_row",
                    "source_id": None,
                    "field": None,
                    "detail": None,
                }
            )
            continue
        source_ids = fact.get("source_ids", [])
        field_name = str(fact.get("field") or "") or None
        if not isinstance(source_ids, list):
            errors.append(
                {
                    "code": "invalid_fact_source_ids",
                    "source_id": None,
                    "field": field_name,
                    "detail": None,
                }
            )
            continue
        normalized_ids = [str(source_id) for source_id in source_ids if source_id is not None]
        referenced_source_ids.update(normalized_ids)
        if fact.get("status") in {"verified", "needs_review"} and not normalized_ids:
            errors.append(
                {
                    "code": "fact_missing_source_id",
                    "source_id": None,
                    "field": field_name,
                    "detail": None,
                }
            )

    for source_id in sorted(referenced_source_ids):
        if source_id not in source_registry:
            errors.append(
                {
                    "code": "unregistered_fact_source",
                    "source_id": source_id,
                    "field": None,
                    "detail": None,
                }
            )

    checked_sources: list[dict[str, Any]] = []
    for source_id, source in sorted(source_registry.items()):
        lookup_ids = [source_id]
        for alias_name in ("product_id", "record_id"):
            alias = source.get(alias_name)
            if alias is not None and str(alias) not in lookup_ids:
                lookup_ids.append(str(alias))
        matches = {
            manifest_map[lookup_id]
            for lookup_id in lookup_ids
            if lookup_id in manifest_map
        }
        if len(matches) > 1:
            disposition = None
            reason = "conflicting_manifest_disposition"
            errors.append(
                {
                    "code": reason,
                    "source_id": source_id,
                    "field": None,
                    "detail": ",".join(sorted(str(value) for value in matches)),
                }
            )
        else:
            disposition = next(iter(matches), None)
            if disposition is None:
                reason = "missing_disposition"
            elif disposition not in KNOWN_SOURCE_DISPOSITIONS:
                reason = "unknown_disposition"
            elif disposition in REJECTED_SOURCE_DISPOSITIONS:
                reason = "disposition_rejected"
            else:
                reason = None
        decision = "PASS" if reason is None else "REJECTED"
        checked_sources.append(
            {
                "source_id": source_id,
                "record_disposition": disposition,
                "decision": decision,
                "reason": reason,
            }
        )
        if reason is not None:
            errors.append(
                {
                    "code": reason,
                    "source_id": source_id,
                    "field": None,
                    "detail": disposition,
                }
            )

    errors = sorted(
        errors,
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("source_id") or ""),
            str(item.get("field") or ""),
            str(item.get("detail") or ""),
        ),
    )
    return {
        "status": "REJECTED" if errors else "PASS",
        "manifest_provided": disposition_manifest is not None,
        "allowed_dispositions": sorted(ALLOWED_SOURCE_DISPOSITIONS),
        "referenced_source_ids": sorted(referenced_source_ids),
        "checked_sources": checked_sources,
        "errors": errors,
        "ready_field_count": 0,
    }


def build_draft(
    master: dict[str, Any],
    assets: dict[str, Any],
    disposition_manifest: Any = None,
) -> dict[str, Any]:
    if not master.get("model"):
        raise ValueError("product master is missing model")
    facts = master.get("facts", [])
    claims = master.get("claims", [])
    commercial = list(master.get("commercial", {}).values())
    fields = [field_from_fact(item) for item in facts + claims + commercial]
    source_gate = evaluate_source_gate(master, disposition_manifest)

    ready_images = [
        item
        for item in assets.get("candidates", [])
        if item.get("compliance", {}).get("upload_ready_main")
    ][:6]
    ready_videos = [
        item
        for item in assets.get("candidates", [])
        if item.get("compliance", {}).get("upload_ready_main_video")
    ]

    blockers = []
    blocked_fields = [item["logical_name"] for item in fields if item["status"] == "blocked"]
    if blocked_fields:
        blockers.append("blocked_fields:" + ",".join(sorted(blocked_fields)))
    if len(ready_images) < 4:
        blockers.append(f"main_images:{len(ready_images)}/4")
    if not ready_videos:
        blockers.append("main_video:no_candidate_within_45_seconds_and_100mb")
    category = master.get("category", {})
    if category.get("verification_status") != "verified" or not category.get("category_id"):
        blockers.append("category_id:not_verified")
    if source_gate["status"] == "REJECTED":
        blockers.append("source_gate:REJECTED")
        for field in fields:
            if field["status"] == "ready":
                field["status"] = "blocked"
                field["reason"] = "source_gate:REJECTED"
        ready_images = []
        ready_videos = []

    source_gate["ready_field_count"] = sum(item["status"] == "ready" for item in fields)

    return {
        "schema_version": "1.0",
        "model": master["model"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "external_write_enabled": False,
        "fields": fields,
        "assets": {
            "main_image_candidates": ready_images,
            "main_video_candidates": ready_videos,
        },
        "blockers": sorted(set(blockers)),
        "approval": {"required": True, "approved": False},
        "source_registry": master.get("sources", []),
        "source_gate": source_gate,
    }


def markdown_summary(draft: dict[str, Any]) -> str:
    lines = [
        f"# {draft['model']} 发布草稿",
        "",
        f"生成时间：{draft['generated_at']}",
        "",
        "外部写入：禁用",
        "",
        "## 字段",
        "",
        "| 字段 | 状态 | 值 | 来源 |",
        "|---|---|---|---|",
    ]
    for field in draft["fields"]:
        value = json.dumps(field["value"], ensure_ascii=False)
        sources = ", ".join(field["source_ids"]) or "-"
        lines.append(f"| {field['logical_name']} | {field['status']} | {value} | {sources} |")
    lines.extend(["", "## 阻断项", ""])
    if draft["blockers"]:
        lines.extend(f"- {item}" for item in draft["blockers"])
    else:
        lines.append("- 无（仍需人工批准）")
    lines.extend(
        [
            "",
            f"合规主图候选：{len(draft['assets']['main_image_candidates'])}",
            f"合规主视频候选：{len(draft['assets']['main_video_candidates'])}",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-master", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument(
        "--disposition-manifest",
        type=Path,
        help="Source/product disposition JSON or JSONL manifest; omission is fail-closed and cannot produce a PASS source gate.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    disposition_manifest = (
        load_disposition_manifest(args.disposition_manifest)
        if args.disposition_manifest
        else None
    )
    draft = build_draft(
        load_json(args.product_master),
        load_json(args.assets),
        disposition_manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown_summary(draft), encoding="utf-8")
    print(
        json.dumps(
            {
                "blockers": draft["blockers"],
                "source_gate": draft["source_gate"]["status"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 2 if draft["source_gate"]["status"] == "REJECTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
