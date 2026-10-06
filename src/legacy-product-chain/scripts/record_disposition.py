#!/usr/bin/env python3
# Excluded records remain negative policy evidence and cannot become product facts.
# 被排除记录仅作禁止规则依据，不能变成产品事实。
"""Build and verify a fail-closed, local-only Alibaba record disposition overlay.

The source ``gmt_modified`` field is treated strictly as a last-modified proxy.
It is not evidence of product creation time or first-online time.  This tool
never writes to Alibaba and never mutates the source archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0.0"
POLICY_VERSION = "record-disposition-1.0.0"
DEFAULT_CUTOFF = "2000-01-02"
DEFAULT_EXPECTED_COUNT = 1  # Synthetic default; real runs must bind their own exact count. / 合成默认值；真实运行须指定自己的精确数量。
DATE_FIELD = "gmt_modified"
DATE_SEMANTICS = "last_modified_proxy_not_creation_date"
UNUSABLE = "UNUSABLE_WASTE_RECORD"
HISTORICAL = "HISTORICAL_REFERENCE_ONLY"
PUBLISH_PROMOTION = "APPROVED_INTERNAL_SOURCE"

CONTROL_MATRIX: dict[str, dict[str, bool]] = {
    UNUSABLE: {
        "allow_rag": False,
        "fact_extraction": False,
        "asset_reuse": False,
        "publish_draft": False,
        "auto_listing": False,
    },
    HISTORICAL: {
        "allow_rag": True,
        "fact_extraction": True,
        "asset_reuse": True,
        "publish_draft": False,
        "auto_listing": False,
    },
}

MANIFEST_RELATIVE_PATH = Path("03_audit") / "derived_manifest.json"
MANIFEST_SIDECAR_RELATIVE_PATH = Path("03_audit") / "derived_manifest.json.sha256"
OVERLAY_RELATIVE_PATH = Path("02_overlay") / "record_disposition.jsonl"
INDEX_RELATIVE_PATH = Path("02_overlay") / "record_disposition_index.json"
BINDING_RELATIVE_PATH = Path("01_date_evidence") / "base_catalog_binding.json"
POLICY_RELATIVE_PATH = Path("00_policy") / "record_disposition_policy.json"
SCHEMA_COPY_RELATIVE_PATH = Path("00_policy") / "record_disposition.schema.json"
ELIGIBLE_IDS_RELATIVE_PATH = Path("01_date_evidence") / "eligible_ids.txt"
UNUSABLE_IDS_RELATIVE_PATH = Path("01_date_evidence") / "unusable_ids.txt"
DATE_EVIDENCE_RELATIVE_PATH = Path("01_date_evidence") / "date_evidence.jsonl"
BUILD_RECEIPT_RELATIVE_PATH = Path("03_audit") / "build_receipt.json"
VERIFICATION_REPORT_RELATIVE_PATH = Path("03_audit") / "verification_report.json"
SECRET_SCAN_REPORT_RELATIVE_PATH = Path("03_audit") / "secret_scan_report.json"
COMMANDS_RELATIVE_PATH = Path("03_audit") / "commands.jsonl"
REPORT_RELATIVE_PATH = Path("04_reports") / "记录处置报告.md"

REQUIRED_DIRECTORIES = ("00_policy", "01_date_evidence", "02_overlay", "03_audit", "04_reports")
PRODUCT_ID_RE = re.compile(r"^[0-9]+$")
GATE_USAGES = ("rag", "fact_extraction", "asset_reuse", "publish_draft", "auto_listing")
USAGE_TO_RECORD_FIELD = {
    "rag": "allow_rag",
    "fact_extraction": "fact_extraction",
    "asset_reuse": "asset_reuse",
    "publish_draft": "publish_draft",
    "auto_listing": "auto_listing",
}
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    (
        "authorization",
        re.compile(r"(?i)(?:\bauthorization\b\s*[:=]\s*|\"authorization\"\s*:\s*\")[^\r\n\"]{8,}"),
    ),
    ("cookie", re.compile(r"(?i)(?:\bcookie\b\s*[:=]\s*|\"cookie\"\s*:\s*\")[^\r\n\"]{8,}")),
    ("ctoken", re.compile(r"(?i)(?:[?&]|&amp;|\")ctoken(?:=|\"\s*:\s*\")[^\s\"&]{8,}")),
    ("_tb_token_", re.compile(r"(?i)(?:[?&]|&amp;|\")_tb_token_(?:=|\"\s*:\s*\")[^\s\"&]{8,}")),
    ("_csrf_token_", re.compile(r"(?i)(?:[?&]|&amp;|\")_csrf_token_(?:=|\"\s*:\s*\")[^\s\"&]{8,}")),
)


class DispositionError(ValueError):
    """A fail-closed validation or policy error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def version_stamp(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return (
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    ).encode("utf-8")


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_json(path: Path, payload: Any) -> None:
    write_bytes(path, json_bytes(payload))


def parse_iso_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DispositionError(f"{field_name}: expected YYYY-MM-DD, got {value!r}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise DispositionError(f"{field_name}: invalid date {value!r}") from error
    if parsed.isoformat() != value:
        raise DispositionError(f"{field_name}: non-canonical date {value!r}")
    return parsed


def schema_source_path() -> Path:
    return Path(__file__).resolve().parents[1] / "schemas" / "record_disposition.schema.json"


def load_catalog(catalog_index: Path, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    catalog_index = catalog_index.resolve(strict=True)
    if not catalog_index.is_file():
        raise DispositionError(f"catalog index is not a file: {catalog_index}")
    raw_bytes = catalog_index.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DispositionError("catalog_index.jsonl is not valid UTF-8") from error

    source_lines = text.splitlines()
    if not source_lines:
        raise DispositionError("catalog_index.jsonl is empty")
    rows: list[dict[str, Any]] = []
    product_ids: set[str] = set()
    ordinals: set[int] = set()
    run_ids: set[str] = set()
    for line_number, source_line in enumerate(source_lines, start=1):
        if not source_line.strip():
            raise DispositionError(f"catalog line {line_number}: blank lines are not allowed")
        try:
            row = json.loads(source_line)
        except json.JSONDecodeError as error:
            raise DispositionError(f"catalog line {line_number}: invalid JSON: {error.msg}") from error
        if not isinstance(row, dict):
            raise DispositionError(f"catalog line {line_number}: expected object")

        product_id = row.get("product_id")
        if not isinstance(product_id, str) or not PRODUCT_ID_RE.fullmatch(product_id):
            raise DispositionError(f"catalog line {line_number}: invalid product_id {product_id!r}")
        if product_id in product_ids:
            raise DispositionError(f"catalog line {line_number}: duplicate product_id {product_id}")
        product_ids.add(product_id)

        ordinal = row.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise DispositionError(f"catalog line {line_number}: invalid ordinal {ordinal!r}")
        if ordinal != line_number:
            raise DispositionError(
                f"catalog line {line_number}: ordinal {ordinal!r} does not match source line"
            )
        if ordinal in ordinals:
            raise DispositionError(f"catalog line {line_number}: duplicate ordinal {ordinal}")
        ordinals.add(ordinal)

        run_id = row.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise DispositionError(f"catalog line {line_number}: missing run_id")
        run_ids.add(run_id)
        parse_iso_date(row.get(DATE_FIELD), f"catalog line {line_number} {DATE_FIELD}")

        rows.append(
            {
                "line_number": line_number,
                "source_line": source_line,
                "row_sha256": sha256_bytes(source_line.encode("utf-8")),
                "row": row,
            }
        )

    if len(rows) != expected_count:
        raise DispositionError(f"catalog coverage mismatch: expected {expected_count}, got {len(rows)}")
    if len(run_ids) != 1:
        raise DispositionError(f"catalog contains multiple run_id values: {sorted(run_ids)}")

    product_sequence = "".join(f"{item['row']['product_id']}\n" for item in rows).encode("utf-8")
    metadata = {
        "catalog_index_path": str(catalog_index),
        "catalog_index_sha256": sha256_bytes(raw_bytes),
        "catalog_index_bytes": len(raw_bytes),
        "catalog_index_rows": len(rows),
        "catalog_run_id": next(iter(run_ids)),
        "product_id_sequence_sha256": sha256_bytes(product_sequence),
    }
    return rows, metadata


def disposition_for(gmt_modified: str, cutoff: str) -> str:
    value_date = parse_iso_date(gmt_modified, DATE_FIELD)
    cutoff_date = parse_iso_date(cutoff, "cutoff")
    return UNUSABLE if value_date >= cutoff_date else HISTORICAL


def controls_for(disposition: str) -> dict[str, bool]:
    controls = CONTROL_MATRIX.get(disposition)
    if controls is None:
        raise DispositionError(f"unknown disposition {disposition!r}; fail closed")
    return dict(controls)


def build_date_evidence(
    catalog_item: dict[str, Any], cutoff: str, overlay_version: str
) -> dict[str, Any]:
    row = catalog_item["row"]
    disposition = disposition_for(row[DATE_FIELD], cutoff)
    return {
        "schema_version": SCHEMA_VERSION,
        "overlay_version": overlay_version,
        "product_id": row["product_id"],
        "ordinal": row["ordinal"],
        "catalog_index_line": catalog_item["line_number"],
        "catalog_row_sha256": catalog_item["row_sha256"],
        "field": DATE_FIELD,
        "value": row[DATE_FIELD],
        "semantics": DATE_SEMANTICS,
        "cutoff": cutoff,
        "comparison": "on_or_after_cutoff" if disposition == UNUSABLE else "before_cutoff",
        "derived_disposition": disposition,
    }


def build_record(
    catalog_item: dict[str, Any],
    catalog_sha256: str,
    cutoff: str,
    overlay_version: str,
    generated_at: str,
) -> dict[str, Any]:
    row = catalog_item["row"]
    disposition = disposition_for(row[DATE_FIELD], cutoff)
    controls = controls_for(disposition)
    reason_codes = (
        ["gmt_modified_on_or_after_cutoff", "record_marked_unusable_waste"]
        if disposition == UNUSABLE
        else [
            "gmt_modified_before_cutoff",
            "historical_reference_only",
            "publish_requires_approved_internal_source",
        ]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "overlay_version": overlay_version,
        "generated_at": generated_at,
        "product_id": row["product_id"],
        "ordinal": row["ordinal"],
        "subject": row.get("subject", "") if isinstance(row.get("subject", ""), str) else "",
        "base_binding": {
            "catalog_index_sha256": catalog_sha256,
            "catalog_index_line": catalog_item["line_number"],
            "catalog_row_sha256": catalog_item["row_sha256"],
        },
        "date_evidence": {
            "field": DATE_FIELD,
            "value": row[DATE_FIELD],
            "semantics": DATE_SEMANTICS,
            "cutoff": cutoff,
            "comparison": "on_or_after_cutoff" if disposition == UNUSABLE else "before_cutoff",
        },
        "record_disposition": disposition,
        "disposition": disposition,
        "disposition_is_final": True,
        **controls,
        "promotion_required_for_publish": PUBLISH_PROMOTION,
        "external_write_enabled": False,
        "reason_codes": reason_codes,
    }


def build_policy(cutoff: str, generated_at: str, overlay_version: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "overlay_version": overlay_version,
        "generated_at": generated_at,
        "source_field": DATE_FIELD,
        "source_field_semantics": DATE_SEMANTICS,
        "cutoff": cutoff,
        "cutoff_interpretation": "source_date_start_00:00:00_without_timezone_conversion",
        "creation_or_first_online_claim_allowed": False,
        "controls_by_disposition": CONTROL_MATRIX,
        "eligible_disposition": HISTORICAL,
        "unusable_disposition": UNUSABLE,
        "unknown_or_missing_handling": "FAIL_CLOSED_ABORT",
        "publish_promotion_required": PUBLISH_PROMOTION,
        "automatic_promotion_enabled": False,
        "external_write_enabled": False,
    }


def resolve_output_root(catalog_index: Path, output_root: Path | None, generated_at: str) -> tuple[Path, Path]:
    base_run_root = catalog_index.resolve(strict=True).parent.parent.resolve()
    if output_root is None:
        name = f"{base_run_root.name}__record_disposition_overlay__{version_stamp(generated_at)}"
        resolved = base_run_root.parent / name
    else:
        resolved = output_root.resolve(strict=False)
    if resolved.parent.resolve() != base_run_root.parent.resolve():
        raise DispositionError(
            f"overlay must be a sibling of the base run: expected parent {base_run_root.parent}"
        )
    if resolved == base_run_root or base_run_root in resolved.parents:
        raise DispositionError("overlay must not be the base archive or a child of it")
    if resolved.exists():
        raise DispositionError(f"versioned overlay path already exists: {resolved}")
    return base_run_root, resolved


def build_manifest(overlay_root: Path, generated_at: str, binding: dict[str, Any]) -> dict[str, Any]:
    excluded = {MANIFEST_RELATIVE_PATH.as_posix(), MANIFEST_SIDECAR_RELATIVE_PATH.as_posix()}
    files: list[dict[str, Any]] = []
    for path in sorted((item for item in overlay_root.rglob("*") if item.is_file()), key=lambda x: x.as_posix()):
        relative = path.relative_to(overlay_root).as_posix()
        if relative in excluded:
            continue
        files.append({"relative_path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    aggregate_payload = "".join(
        f"{item['sha256']}  {item['relative_path']}\n" for item in files
    ).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "COMPLETE",
        "manifest_scope": "all_overlay_files_except_manifest_and_sidecar",
        "base_catalog_index_sha256": binding["catalog_index_sha256"],
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "aggregate_sha256": sha256_bytes(aggregate_payload),
        "files": files,
    }


def chinese_report(
    *,
    generated_at: str,
    cutoff: str,
    binding: dict[str, Any],
    eligible_ids: list[str],
    unusable_ids: list[str],
) -> str:
    return "\n".join(
        [
            "# Alibaba 商品记录处置报告",
            "",
            f"- 生成时间：{generated_at}",
            f"- 基础归档：`{binding['base_run_root']}`",
            f"- Catalog SHA-256：`{binding['catalog_index_sha256']}`",
            f"- Catalog 行数：{binding['catalog_index_rows']}",
            f"- 截止日期：{cutoff}（按源 `gmt_modified` 日期语义，不做时区换算）",
            "- 字段语义：`last_modified_proxy_not_creation_date`",
            "",
            "> `gmt_modified` 仅是最后修改日期代理，不是商品创建日期，也不是首次上架日期。",
            "",
            "## 处置统计",
            "",
            f"- `HISTORICAL_REFERENCE_ONLY`：{len(eligible_ids)} 条",
            f"- `UNUSABLE_WASTE_RECORD`：{len(unusable_ids)} 条",
            "- 覆盖缺口：0 条",
            "",
            "## 权限矩阵",
            "",
            "| 状态 | 本地 RAG | 事实提取 | 素材复用 | 发布草稿 | 自动上架 |",
            "|---|---:|---:|---:|---:|---:|",
            "| HISTORICAL_REFERENCE_ONLY | 是 | 是 | 是 | 否 | 否 |",
            "| UNUSABLE_WASTE_RECORD | 否 | 否 | 否 | 否 | 否 |",
            "",
            f"发布相关动作必须另行升格为 `{PUBLISH_PROMOTION}`；本工具不执行升格。",
            "",
            "## 本地边界",
            "",
            "- 原始归档未修改。",
            "- 输出仅位于原归档同级的版本化 overlay。",
            "- 未调用任何 Alibaba 写接口，也未启用外部写入。",
            "",
            "ID 明细见 `01_date_evidence/eligible_ids.txt` 和 `01_date_evidence/unusable_ids.txt`。",
            "",
        ]
    )


def core_verification_report(
    *,
    generated_at: str,
    overlay_version: str,
    binding: dict[str, Any],
    catalog_index: Path,
    records: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
    eligible_ids: list[str],
    unusable_ids: list[str],
    expected_count: int,
) -> dict[str, Any]:
    checks = {
        "catalog_hash_unchanged": sha256_file(catalog_index) == binding["catalog_index_sha256"],
        "record_coverage_complete": len(records) == expected_count,
        "date_evidence_coverage_complete": len(evidence_rows) == expected_count,
        "product_ids_unique": len({record["product_id"] for record in records}) == expected_count,
        "canonical_disposition_matches_compatibility_field": all(
            record.get("record_disposition") == record.get("disposition") for record in records
        ),
        "date_semantics_locked": all(
            record.get("date_evidence", {}).get("semantics") == DATE_SEMANTICS for record in records
        ),
        "controls_match_disposition": all(not validate_record(record) for record in records),
        "id_lists_partition_records": (
            len(eligible_ids) + len(unusable_ids) == expected_count
            and not (set(eligible_ids) & set(unusable_ids))
            and set(eligible_ids + unusable_ids) == {record["product_id"] for record in records}
        ),
        "publish_requires_explicit_promotion": all(
            record.get("promotion_required_for_publish") == PUBLISH_PROMOTION
            and record.get("publish_draft") is False
            and record.get("auto_listing") is False
            for record in records
        ),
        "external_write_disabled": all(record.get("external_write_enabled") is False for record in records),
    }
    failures = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema_version": SCHEMA_VERSION,
        "overlay_version": overlay_version,
        "generated_at": generated_at,
        "verification_scope": "core_policy_and_base_binding_before_manifest_seal",
        "manifest_verification": "performed_by_final_self_verify_after_seal",
        "base_catalog_index_sha256": binding["catalog_index_sha256"],
        "expected_count": expected_count,
        "record_count": len(records),
        "eligible_count": len(eligible_ids),
        "unusable_count": len(unusable_ids),
        "checks": checks,
        "failures": failures,
        "status": "COMPLETE" if not failures else "FAIL",
        "external_write_enabled": False,
    }


def command_event(
    *,
    generated_at: str,
    overlay_version: str,
    operation: str,
    status: str,
    exit_code: int,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "overlay_version": overlay_version,
        "operation": operation,
        "status": status,
        "exit_code": exit_code,
        "detail": detail,
        "external_write_enabled": False,
        "online_write_performed": False,
    }


def scan_overlay_secrets(overlay_root: Path, generated_at: str) -> dict[str, Any]:
    excluded = {
        SECRET_SCAN_REPORT_RELATIVE_PATH.as_posix(),
        MANIFEST_RELATIVE_PATH.as_posix(),
        MANIFEST_SIDECAR_RELATIVE_PATH.as_posix(),
    }
    findings: list[dict[str, Any]] = []
    files_scanned = 0
    bytes_scanned = 0
    for path in sorted((item for item in overlay_root.rglob("*") if item.is_file()), key=lambda x: x.as_posix()):
        relative = path.relative_to(overlay_root).as_posix()
        if relative in excluded:
            continue
        payload = path.read_bytes()
        files_scanned += 1
        bytes_scanned += len(payload)
        text = payload.decode("utf-8", errors="replace")
        for pattern_name, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                matched = match.group(0)
                findings.append(
                    {
                        "relative_path": relative,
                        "line": text.count("\n", 0, match.start()) + 1,
                        "pattern": pattern_name,
                        "matched_sha256": sha256_bytes(matched.encode("utf-8")),
                        "matched_length": len(matched),
                        "evidence": "REDACTED",
                    }
                )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "scanner": "record_disposition_local_secret_scan_v1",
        "patterns": [name for name, _ in SECRET_PATTERNS],
        "excluded_relative_paths": sorted(excluded),
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "finding_count": len(findings),
        "findings": findings,
        "status": "PASS" if not findings else "FAIL",
        "external_write_enabled": False,
    }


def build_overlay(
    catalog_index: Path,
    cutoff: str = DEFAULT_CUTOFF,
    output_root: Path | None = None,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    cutoff = parse_iso_date(cutoff, "cutoff").isoformat()
    generated_at = generated_at or utc_now()
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise DispositionError(f"invalid generated_at {generated_at!r}") from error

    catalog_index = catalog_index.resolve(strict=True)
    rows, catalog_meta = load_catalog(catalog_index, expected_count)
    schema_path = schema_source_path()
    if not schema_path.is_file():
        raise DispositionError(f"record schema missing: {schema_path}")
    schema_bytes = schema_path.read_bytes()
    base_run_root, resolved_output = resolve_output_root(catalog_index, output_root, generated_at)
    overlay_version = f"record-disposition-{version_stamp(generated_at)}"

    date_evidence = [build_date_evidence(item, cutoff, overlay_version) for item in rows]
    records = [
        build_record(item, catalog_meta["catalog_index_sha256"], cutoff, overlay_version, generated_at)
        for item in rows
    ]
    unusable_ids = [record["product_id"] for record in records if record["disposition"] == UNUSABLE]
    eligible_ids = [record["product_id"] for record in records if record["disposition"] == HISTORICAL]
    if len(records) != len(unusable_ids) + len(eligible_ids):
        raise DispositionError("unknown disposition encountered; fail closed")

    binding = {
        "schema_version": SCHEMA_VERSION,
        "overlay_version": overlay_version,
        "generated_at": generated_at,
        "base_run_root": str(base_run_root),
        **catalog_meta,
        "expected_count": expected_count,
        "source_field": DATE_FIELD,
        "source_field_semantics": DATE_SEMANTICS,
        "cutoff": cutoff,
        "schema_sha256": sha256_bytes(schema_bytes),
        "original_archive_mutated": False,
        "external_write_enabled": False,
    }
    policy = build_policy(cutoff, generated_at, overlay_version)

    resolved_output.mkdir(parents=False, exist_ok=False)
    for directory in REQUIRED_DIRECTORIES:
        (resolved_output / directory).mkdir()

    write_json(resolved_output / POLICY_RELATIVE_PATH, policy)
    write_bytes(resolved_output / SCHEMA_COPY_RELATIVE_PATH, schema_bytes)
    write_json(resolved_output / BINDING_RELATIVE_PATH, binding)
    write_bytes(resolved_output / DATE_EVIDENCE_RELATIVE_PATH, jsonl_bytes(date_evidence))
    write_bytes(
        resolved_output / ELIGIBLE_IDS_RELATIVE_PATH,
        "".join(f"{product_id}\n" for product_id in eligible_ids).encode("utf-8"),
    )
    write_bytes(
        resolved_output / UNUSABLE_IDS_RELATIVE_PATH,
        "".join(f"{product_id}\n" for product_id in unusable_ids).encode("utf-8"),
    )
    write_bytes(resolved_output / OVERLAY_RELATIVE_PATH, jsonl_bytes(records))

    disposition_index = {
        "schema_version": SCHEMA_VERSION,
        "overlay_version": overlay_version,
        "generated_at": generated_at,
        "base_catalog_index_sha256": catalog_meta["catalog_index_sha256"],
        "source_field": DATE_FIELD,
        "source_field_semantics": DATE_SEMANTICS,
        "cutoff": cutoff,
        "total_records": len(records),
        "eligible_records": len(eligible_ids),
        "unusable_records": len(unusable_ids),
        "eligible_ids_path": ELIGIBLE_IDS_RELATIVE_PATH.as_posix(),
        "unusable_ids_path": UNUSABLE_IDS_RELATIVE_PATH.as_posix(),
        "record_file": OVERLAY_RELATIVE_PATH.as_posix(),
        "record_file_sha256": sha256_file(resolved_output / OVERLAY_RELATIVE_PATH),
        "coverage_complete": len(records) == expected_count,
        "external_write_enabled": False,
    }
    write_json(resolved_output / INDEX_RELATIVE_PATH, disposition_index)
    write_bytes(
        resolved_output / REPORT_RELATIVE_PATH,
        chinese_report(
            generated_at=generated_at,
            cutoff=cutoff,
            binding=binding,
            eligible_ids=eligible_ids,
            unusable_ids=unusable_ids,
        ).encode("utf-8"),
    )

    if sha256_file(catalog_index) != catalog_meta["catalog_index_sha256"]:
        raise DispositionError("base catalog changed during build; fail closed")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "overlay_version": overlay_version,
        "generated_at": generated_at,
        "status": "COMPLETE",
        "overlay_root": str(resolved_output),
        "base_catalog_index_sha256": catalog_meta["catalog_index_sha256"],
        "record_count": len(records),
        "eligible_count": len(eligible_ids),
        "unusable_count": len(unusable_ids),
        "coverage_complete": len(records) == expected_count,
        "original_catalog_unchanged": True,
        "external_write_enabled": False,
        "online_write_performed": False,
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "schema_sha256": sha256_bytes(schema_bytes),
    }
    write_json(resolved_output / BUILD_RECEIPT_RELATIVE_PATH, receipt)

    verification_report = core_verification_report(
        generated_at=generated_at,
        overlay_version=overlay_version,
        binding=binding,
        catalog_index=catalog_index,
        records=records,
        evidence_rows=date_evidence,
        eligible_ids=eligible_ids,
        unusable_ids=unusable_ids,
        expected_count=expected_count,
    )
    write_json(resolved_output / VERIFICATION_REPORT_RELATIVE_PATH, verification_report)
    if verification_report["status"] != "COMPLETE":
        raise DispositionError(
            "core verification failed: " + ", ".join(verification_report["failures"])
        )

    command_rows = [
        command_event(
            generated_at=generated_at,
            overlay_version=overlay_version,
            operation="materialize_overlay",
            status="COMPLETE",
            exit_code=0,
            detail={
                "record_count": len(records),
                "eligible_count": len(eligible_ids),
                "unusable_count": len(unusable_ids),
                "base_catalog_index_sha256": catalog_meta["catalog_index_sha256"],
            },
        ),
        command_event(
            generated_at=generated_at,
            overlay_version=overlay_version,
            operation="verify_core",
            status="COMPLETE",
            exit_code=0,
            detail={"checks": verification_report["checks"], "failures": []},
        ),
    ]
    write_bytes(resolved_output / COMMANDS_RELATIVE_PATH, jsonl_bytes(command_rows))
    preliminary_secret_report = scan_overlay_secrets(resolved_output, generated_at)
    if preliminary_secret_report["status"] != "PASS":
        write_json(resolved_output / SECRET_SCAN_REPORT_RELATIVE_PATH, preliminary_secret_report)
        raise DispositionError(
            f"secret scan failed with {preliminary_secret_report['finding_count']} finding(s)"
        )
    command_rows.append(
        command_event(
            generated_at=generated_at,
            overlay_version=overlay_version,
            operation="secret_scan",
            status="PASS",
            exit_code=0,
            detail={
                "patterns": [name for name, _ in SECRET_PATTERNS],
                "finding_count": 0,
            },
        )
    )
    write_bytes(resolved_output / COMMANDS_RELATIVE_PATH, jsonl_bytes(command_rows))
    secret_report = scan_overlay_secrets(resolved_output, generated_at)
    write_json(resolved_output / SECRET_SCAN_REPORT_RELATIVE_PATH, secret_report)
    if secret_report["status"] != "PASS":
        raise DispositionError(f"secret scan failed with {secret_report['finding_count']} finding(s)")

    manifest = build_manifest(resolved_output, generated_at, binding)
    write_json(resolved_output / MANIFEST_RELATIVE_PATH, manifest)
    manifest_hash = sha256_file(resolved_output / MANIFEST_RELATIVE_PATH)
    write_bytes(
        resolved_output / MANIFEST_SIDECAR_RELATIVE_PATH,
        f"{manifest_hash}  {MANIFEST_RELATIVE_PATH.name}\n".encode("ascii"),
    )

    verification = verify_overlay(resolved_output, catalog_index, expected_count)
    if verification["status"] != "COMPLETE":
        raise DispositionError(
            "self-verification failed: " + "; ".join(verification["errors"][:10])
        )
    return {
        **receipt,
        "derived_manifest_sha256": manifest_hash,
        "verification_status": verification["status"],
    }


RECORD_KEYS = {
    "schema_version",
    "policy_version",
    "overlay_version",
    "generated_at",
    "product_id",
    "ordinal",
    "subject",
    "base_binding",
    "date_evidence",
    "record_disposition",
    "disposition",
    "disposition_is_final",
    "allow_rag",
    "fact_extraction",
    "asset_reuse",
    "publish_draft",
    "auto_listing",
    "promotion_required_for_publish",
    "external_write_enabled",
    "reason_codes",
}


def validate_record(record: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["record_not_object"]
    if set(record) != RECORD_KEYS:
        missing = sorted(RECORD_KEYS - set(record))
        extra = sorted(set(record) - RECORD_KEYS)
        errors.append(f"record_keys:missing={missing},extra={extra}")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version")
    if record.get("policy_version") != POLICY_VERSION:
        errors.append("policy_version")
    if not isinstance(record.get("product_id"), str) or not PRODUCT_ID_RE.fullmatch(
        record.get("product_id", "")
    ):
        errors.append("product_id")
    if not isinstance(record.get("ordinal"), int) or isinstance(record.get("ordinal"), bool):
        errors.append("ordinal")
    if record.get("record_disposition") not in CONTROL_MATRIX:
        errors.append("unknown_record_disposition")
    if record.get("disposition") not in CONTROL_MATRIX:
        errors.append("unknown_disposition")
    if record.get("record_disposition") != record.get("disposition"):
        errors.append("canonical_disposition_mismatch")
    if record.get("disposition_is_final") is not True:
        errors.append("disposition_is_final")
    if record.get("promotion_required_for_publish") != PUBLISH_PROMOTION:
        errors.append("promotion_required_for_publish")
    if record.get("external_write_enabled") is not False:
        errors.append("external_write_enabled")
    evidence = record.get("date_evidence")
    if not isinstance(evidence, dict):
        errors.append("date_evidence")
    else:
        expected_evidence_keys = {"field", "value", "semantics", "cutoff", "comparison"}
        if set(evidence) != expected_evidence_keys:
            errors.append("date_evidence_keys")
        if evidence.get("field") != DATE_FIELD:
            errors.append("date_evidence_field")
        if evidence.get("semantics") != DATE_SEMANTICS:
            errors.append("date_evidence_semantics")
        try:
            expected_disposition = disposition_for(evidence.get("value"), evidence.get("cutoff"))
        except DispositionError:
            expected_disposition = None
            errors.append("date_evidence_invalid_date")
        if expected_disposition and record.get("disposition") != expected_disposition:
            errors.append("disposition_date_mismatch")
        expected_comparison = "on_or_after_cutoff" if expected_disposition == UNUSABLE else "before_cutoff"
        if expected_disposition and evidence.get("comparison") != expected_comparison:
            errors.append("comparison")
    if record.get("disposition") in CONTROL_MATRIX:
        for key, expected in CONTROL_MATRIX[record["disposition"]].items():
            if record.get(key) is not expected:
                errors.append(f"control:{key}")
    if not isinstance(record.get("reason_codes"), list) or not record.get("reason_codes"):
        errors.append("reason_codes")
    return errors


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            raise DispositionError(f"{path}: blank JSONL line {line_number}")
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise DispositionError(f"{path}: invalid JSONL line {line_number}") from error
    return rows


def verify_manifest(overlay_root: Path, expected_base_sha256: str | None, errors: list[str]) -> None:
    manifest_path = overlay_root / MANIFEST_RELATIVE_PATH
    sidecar_path = overlay_root / MANIFEST_SIDECAR_RELATIVE_PATH
    if not manifest_path.is_file() or not sidecar_path.is_file():
        errors.append("manifest_or_sidecar_missing")
        return
    try:
        manifest = load_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"manifest_invalid:{error}")
        return
    sidecar_tokens = sidecar_path.read_text(encoding="ascii").split()
    actual_manifest_hash = sha256_file(manifest_path)
    if not sidecar_tokens or sidecar_tokens[0].lower() != actual_manifest_hash:
        errors.append("manifest_sidecar_hash_mismatch")

    entries = manifest.get("files")
    if not isinstance(entries, list):
        errors.append("manifest_files_invalid")
        return
    recorded: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
            errors.append("manifest_entry_invalid")
            continue
        relative = item["relative_path"]
        if relative in recorded:
            errors.append(f"manifest_duplicate:{relative}")
        recorded[relative] = item
    if list(recorded) != sorted(recorded):
        errors.append("manifest_path_order")

    excluded = {MANIFEST_RELATIVE_PATH.as_posix(), MANIFEST_SIDECAR_RELATIVE_PATH.as_posix()}
    actual_paths = {
        path.relative_to(overlay_root).as_posix()
        for path in overlay_root.rglob("*")
        if path.is_file() and path.relative_to(overlay_root).as_posix() not in excluded
    }
    if set(recorded) != actual_paths:
        errors.append(
            f"manifest_scope_mismatch:missing={sorted(actual_paths-set(recorded))},extra={sorted(set(recorded)-actual_paths)}"
        )
    for relative in sorted(set(recorded) & actual_paths):
        path = overlay_root / Path(relative)
        item = recorded[relative]
        if item.get("bytes") != path.stat().st_size:
            errors.append(f"manifest_size:{relative}")
        if item.get("sha256") != sha256_file(path):
            errors.append(f"manifest_hash:{relative}")
    if manifest.get("file_count") != len(entries):
        errors.append("manifest_file_count")
    if manifest.get("total_bytes") != sum(
        item.get("bytes", -1) for item in entries if isinstance(item, dict)
    ):
        errors.append("manifest_total_bytes")
    if manifest.get("status") != "COMPLETE":
        errors.append("manifest_status")
    if expected_base_sha256 and manifest.get("base_catalog_index_sha256") != expected_base_sha256:
        errors.append("manifest_base_binding")
    aggregate = "".join(
        f"{item.get('sha256')}  {item.get('relative_path')}\n" for item in entries
    ).encode("utf-8")
    if manifest.get("aggregate_sha256") != sha256_bytes(aggregate):
        errors.append("manifest_aggregate")


def verify_overlay(
    overlay_root: Path,
    catalog_index: Path | None = None,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
) -> dict[str, Any]:
    overlay_root = overlay_root.resolve(strict=False)
    errors: list[str] = []
    required = [
        POLICY_RELATIVE_PATH,
        SCHEMA_COPY_RELATIVE_PATH,
        BINDING_RELATIVE_PATH,
        DATE_EVIDENCE_RELATIVE_PATH,
        ELIGIBLE_IDS_RELATIVE_PATH,
        UNUSABLE_IDS_RELATIVE_PATH,
        OVERLAY_RELATIVE_PATH,
        INDEX_RELATIVE_PATH,
        BUILD_RECEIPT_RELATIVE_PATH,
        VERIFICATION_REPORT_RELATIVE_PATH,
        SECRET_SCAN_REPORT_RELATIVE_PATH,
        COMMANDS_RELATIVE_PATH,
        REPORT_RELATIVE_PATH,
        MANIFEST_RELATIVE_PATH,
        MANIFEST_SIDECAR_RELATIVE_PATH,
    ]
    for relative in required:
        if not (overlay_root / relative).is_file():
            errors.append(f"missing:{relative.as_posix()}")
    if errors:
        return {"status": "FAIL", "overlay_root": str(overlay_root), "errors": errors}

    try:
        policy = load_json(overlay_root / POLICY_RELATIVE_PATH)
        binding = load_json(overlay_root / BINDING_RELATIVE_PATH)
        index = load_json(overlay_root / INDEX_RELATIVE_PATH)
        receipt = load_json(overlay_root / BUILD_RECEIPT_RELATIVE_PATH)
        recorded_verification = load_json(overlay_root / VERIFICATION_REPORT_RELATIVE_PATH)
        recorded_secret_scan = load_json(overlay_root / SECRET_SCAN_REPORT_RELATIVE_PATH)
        commands = load_jsonl(overlay_root / COMMANDS_RELATIVE_PATH)
        records = load_jsonl(overlay_root / OVERLAY_RELATIVE_PATH)
        evidence_rows = load_jsonl(overlay_root / DATE_EVIDENCE_RELATIVE_PATH)
    except (OSError, ValueError, json.JSONDecodeError, DispositionError) as error:
        return {
            "status": "FAIL",
            "overlay_root": str(overlay_root),
            "errors": [f"overlay_parse:{error}"],
        }

    if policy.get("source_field") != DATE_FIELD or policy.get("source_field_semantics") != DATE_SEMANTICS:
        errors.append("policy_date_semantics")
    if policy.get("controls_by_disposition") != CONTROL_MATRIX:
        errors.append("policy_control_matrix")
    if policy.get("unknown_or_missing_handling") != "FAIL_CLOSED_ABORT":
        errors.append("policy_fail_closed")
    if policy.get("publish_promotion_required") != PUBLISH_PROMOTION:
        errors.append("policy_publish_promotion")
    if policy.get("external_write_enabled") is not False:
        errors.append("policy_external_write")
    if policy.get("cutoff") != binding.get("cutoff"):
        errors.append("policy_cutoff_binding")
    if policy.get("overlay_version") != binding.get("overlay_version"):
        errors.append("policy_overlay_version")
    if policy.get("generated_at") != binding.get("generated_at"):
        errors.append("policy_generated_at")

    base_path = (catalog_index or Path(binding.get("catalog_index_path", ""))).resolve(strict=False)
    try:
        catalog_rows, catalog_meta = load_catalog(base_path, expected_count)
    except (OSError, DispositionError) as error:
        errors.append(f"base_catalog:{error}")
        catalog_rows, catalog_meta = [], {}
    if catalog_meta:
        for key in (
            "catalog_index_sha256",
            "catalog_index_bytes",
            "catalog_index_rows",
            "catalog_run_id",
            "product_id_sequence_sha256",
        ):
            if binding.get(key) != catalog_meta.get(key):
                errors.append(f"base_binding:{key}")
    if binding.get("source_field") != DATE_FIELD or binding.get("source_field_semantics") != DATE_SEMANTICS:
        errors.append("binding_date_semantics")
    if binding.get("original_archive_mutated") is not False or binding.get("external_write_enabled") is not False:
        errors.append("binding_write_boundary")
    try:
        bound_base_root = Path(binding.get("base_run_root", "")).resolve(strict=False)
        if overlay_root.parent != bound_base_root.parent or overlay_root == bound_base_root or bound_base_root in overlay_root.parents:
            errors.append("overlay_not_versioned_sibling")
    except (OSError, TypeError, ValueError):
        errors.append("base_run_root_binding")

    if len(records) != expected_count:
        errors.append(f"record_count:{len(records)}/{expected_count}")
    if len(evidence_rows) != expected_count:
        errors.append(f"date_evidence_count:{len(evidence_rows)}/{expected_count}")
    if len(catalog_rows) == len(records):
        catalog_ids = [item["row"]["product_id"] for item in catalog_rows]
        record_ids = [record.get("product_id") if isinstance(record, dict) else None for record in records]
        if catalog_ids != record_ids:
            errors.append("record_product_sequence")
        cutoff = binding.get("cutoff")
        for catalog_item, record in zip(catalog_rows, records):
            product_id = catalog_item["row"]["product_id"]
            for item_error in validate_record(record):
                errors.append(f"record:{product_id}:{item_error}")
            if not isinstance(record, dict):
                continue
            if record.get("overlay_version") != binding.get("overlay_version"):
                errors.append(f"record:{product_id}:overlay_version")
            if record.get("generated_at") != binding.get("generated_at"):
                errors.append(f"record:{product_id}:generated_at")
            if record.get("ordinal") != catalog_item["row"]["ordinal"]:
                errors.append(f"record:{product_id}:ordinal_binding")
            if record.get("subject") != (
                catalog_item["row"].get("subject", "")
                if isinstance(catalog_item["row"].get("subject", ""), str)
                else ""
            ):
                errors.append(f"record:{product_id}:subject_binding")
            base = record.get("base_binding", {})
            if base != {
                "catalog_index_sha256": catalog_meta.get("catalog_index_sha256"),
                "catalog_index_line": catalog_item["line_number"],
                "catalog_row_sha256": catalog_item["row_sha256"],
            }:
                errors.append(f"record:{product_id}:base_binding")
            evidence = record.get("date_evidence", {})
            if evidence.get("value") != catalog_item["row"].get(DATE_FIELD) or evidence.get("cutoff") != cutoff:
                errors.append(f"record:{product_id}:date_binding")
    if len(catalog_rows) == len(evidence_rows):
        for catalog_item, evidence in zip(catalog_rows, evidence_rows):
            product_id = catalog_item["row"]["product_id"]
            try:
                expected_evidence = build_date_evidence(
                    catalog_item,
                    binding.get("cutoff"),
                    binding.get("overlay_version"),
                )
            except DispositionError as error:
                errors.append(f"date_evidence:{product_id}:policy:{error}")
                continue
            if evidence != expected_evidence:
                errors.append(f"date_evidence:{product_id}:binding")

    eligible_ids = [line for line in (overlay_root / ELIGIBLE_IDS_RELATIVE_PATH).read_text(encoding="utf-8").splitlines() if line]
    unusable_ids = [line for line in (overlay_root / UNUSABLE_IDS_RELATIVE_PATH).read_text(encoding="utf-8").splitlines() if line]
    expected_eligible = [
        record["product_id"]
        for record in records
        if isinstance(record, dict) and record.get("disposition") == HISTORICAL
    ]
    expected_unusable = [
        record["product_id"]
        for record in records
        if isinstance(record, dict) and record.get("disposition") == UNUSABLE
    ]
    if eligible_ids != expected_eligible:
        errors.append("eligible_id_list")
    if unusable_ids != expected_unusable:
        errors.append("unusable_id_list")
    if set(eligible_ids) & set(unusable_ids):
        errors.append("id_list_overlap")
    if len(set(eligible_ids + unusable_ids)) != expected_count:
        errors.append("id_list_coverage")

    if index.get("total_records") != len(records):
        errors.append("index_total_records")
    if index.get("eligible_records") != len(eligible_ids) or index.get("unusable_records") != len(unusable_ids):
        errors.append("index_disposition_counts")
    if index.get("record_file_sha256") != sha256_file(overlay_root / OVERLAY_RELATIVE_PATH):
        errors.append("index_record_hash")
    if index.get("base_catalog_index_sha256") != binding.get("catalog_index_sha256"):
        errors.append("index_base_binding")
    if index.get("cutoff") != binding.get("cutoff"):
        errors.append("index_cutoff_binding")
    if index.get("overlay_version") != binding.get("overlay_version"):
        errors.append("index_overlay_version")
    if index.get("coverage_complete") is not True or index.get("external_write_enabled") is not False:
        errors.append("index_boundary")
    if receipt.get("status") != "COMPLETE" or receipt.get("record_count") != len(records):
        errors.append("build_receipt_status_or_count")
    if receipt.get("eligible_count") != len(eligible_ids) or receipt.get("unusable_count") != len(unusable_ids):
        errors.append("build_receipt_disposition_counts")
    if receipt.get("base_catalog_index_sha256") != binding.get("catalog_index_sha256"):
        errors.append("build_receipt_base_binding")
    if receipt.get("overlay_version") != binding.get("overlay_version"):
        errors.append("build_receipt_overlay_version")
    if receipt.get("coverage_complete") is not True:
        errors.append("build_receipt_coverage")
    if receipt.get("original_catalog_unchanged") is not True:
        errors.append("build_receipt_original_catalog")
    if receipt.get("external_write_enabled") is not False or receipt.get("online_write_performed") is not False:
        errors.append("build_receipt_write_boundary")

    if recorded_verification.get("status") != "COMPLETE" or recorded_verification.get("failures") != []:
        errors.append("recorded_verification_status")
    recorded_checks = recorded_verification.get("checks")
    if not isinstance(recorded_checks, dict) or not recorded_checks or not all(
        value is True for value in recorded_checks.values()
    ):
        errors.append("recorded_verification_checks")
    if recorded_verification.get("base_catalog_index_sha256") != binding.get("catalog_index_sha256"):
        errors.append("recorded_verification_base_binding")
    if recorded_verification.get("overlay_version") != binding.get("overlay_version"):
        errors.append("recorded_verification_overlay_version")
    if recorded_verification.get("record_count") != len(records):
        errors.append("recorded_verification_count")
    if recorded_verification.get("external_write_enabled") is not False:
        errors.append("recorded_verification_write_boundary")

    fresh_secret_scan = scan_overlay_secrets(
        overlay_root,
        recorded_secret_scan.get("generated_at", binding.get("generated_at", utc_now())),
    )
    for key in (
        "scanner",
        "patterns",
        "excluded_relative_paths",
        "files_scanned",
        "bytes_scanned",
        "finding_count",
        "findings",
        "status",
        "external_write_enabled",
    ):
        if recorded_secret_scan.get(key) != fresh_secret_scan.get(key):
            errors.append(f"secret_scan_report:{key}")
    if recorded_secret_scan.get("status") != "PASS" or recorded_secret_scan.get("finding_count") != 0:
        errors.append("secret_scan_not_clean")

    expected_operations = ["materialize_overlay", "verify_core", "secret_scan"]
    if [row.get("operation") for row in commands if isinstance(row, dict)] != expected_operations:
        errors.append("commands_operation_sequence")
    for command_number, command in enumerate(commands, start=1):
        if not isinstance(command, dict):
            errors.append(f"commands:{command_number}:not_object")
            continue
        if command.get("overlay_version") != binding.get("overlay_version"):
            errors.append(f"commands:{command_number}:overlay_version")
        if command.get("exit_code") != 0 or command.get("status") not in {"COMPLETE", "PASS"}:
            errors.append(f"commands:{command_number}:status")
        if command.get("external_write_enabled") is not False or command.get("online_write_performed") is not False:
            errors.append(f"commands:{command_number}:write_boundary")

    schema_copy = overlay_root / SCHEMA_COPY_RELATIVE_PATH
    if binding.get("schema_sha256") != sha256_file(schema_copy):
        errors.append("schema_binding")
    verify_manifest(overlay_root, binding.get("catalog_index_sha256"), errors)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "status": "COMPLETE" if not errors else "FAIL",
        "overlay_root": str(overlay_root),
        "catalog_index": str(base_path),
        "expected_count": expected_count,
        "record_count": len(records),
        "eligible_count": len(eligible_ids),
        "unusable_count": len(unusable_ids),
        "errors": errors,
        "external_write_enabled": False,
    }


def fail_closed_check(product_id: str, reason: str) -> dict[str, Any]:
    return {
        "status": "UNKNOWN_FAIL_CLOSED",
        "product_id": product_id,
        "allow_rag": False,
        "fact_extraction": False,
        "asset_reuse": False,
        "publish_draft": False,
        "auto_listing": False,
        "external_write_enabled": False,
        "reason": reason,
    }


def check_product(
    overlay_root: Path,
    product_id: str,
    catalog_index: Path | None = None,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
) -> tuple[dict[str, Any], int]:
    verification = verify_overlay(overlay_root, catalog_index, expected_count)
    if verification["status"] != "COMPLETE":
        payload = fail_closed_check(product_id, "overlay_verification_failed")
        payload["verification_errors"] = verification["errors"]
        return payload, 3
    records = load_jsonl(overlay_root.resolve() / OVERLAY_RELATIVE_PATH)
    matches = [record for record in records if record.get("product_id") == product_id]
    if len(matches) != 1:
        return fail_closed_check(product_id, "product_id_not_found_or_not_unique"), 4
    return {"verification_status": "COMPLETE", **matches[0]}, 0


def gate_usage(
    overlay_root: Path,
    product_id: str,
    usage: str,
    catalog_index: Path | None = None,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
) -> tuple[dict[str, Any], int]:
    record_field = USAGE_TO_RECORD_FIELD.get(usage)
    if record_field is None:
        payload = fail_closed_check(product_id, "unknown_usage")
        payload.update({"usage": usage, "allowed": False})
        return payload, 4
    verification = verify_overlay(overlay_root, catalog_index, expected_count)
    if verification["status"] != "COMPLETE":
        payload = fail_closed_check(product_id, "overlay_verification_failed")
        payload.update(
            {
                "usage": usage,
                "allowed": False,
                "verification_errors": verification["errors"],
            }
        )
        return payload, 4
    records = load_jsonl(overlay_root.resolve() / OVERLAY_RELATIVE_PATH)
    matches = [record for record in records if record.get("product_id") == product_id]
    if len(matches) != 1:
        payload = fail_closed_check(product_id, "product_id_not_found_or_not_unique")
        payload.update({"usage": usage, "allowed": False})
        return payload, 4
    record = matches[0]
    allowed = record.get(record_field)
    if not isinstance(allowed, bool):
        payload = fail_closed_check(product_id, "usage_control_missing_or_invalid")
        payload.update({"usage": usage, "allowed": False})
        return payload, 4
    result = {
        "status": "ALLOWED" if allowed else "DENIED",
        "verification_status": "COMPLETE",
        "product_id": product_id,
        "usage": usage,
        "control_field": record_field,
        "allowed": allowed,
        "record_disposition": record["record_disposition"],
        "disposition": record["disposition"],
        "base_catalog_index_sha256": record["base_binding"]["catalog_index_sha256"],
        "promotion_required_for_publish": record["promotion_required_for_publish"],
        "external_write_enabled": False,
    }
    return result, 0 if allowed else 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build a versioned sibling disposition overlay")
    build.add_argument("--catalog-index", type=Path, required=True)
    build.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    build.add_argument("--output-root", type=Path)
    build.add_argument("--expected-count", type=int, default=DEFAULT_EXPECTED_COUNT)

    verify = subparsers.add_parser("verify", help="verify base binding, coverage, policy and manifest")
    verify.add_argument("--overlay-root", type=Path, required=True)
    verify.add_argument("--catalog-index", type=Path)
    verify.add_argument("--expected-count", type=int, default=DEFAULT_EXPECTED_COUNT)

    check = subparsers.add_parser("check", help="return one product disposition after full verification")
    check.add_argument("--overlay-root", type=Path, required=True)
    check.add_argument("--product-id", required=True)
    check.add_argument("--catalog-index", type=Path)
    check.add_argument("--expected-count", type=int, default=DEFAULT_EXPECTED_COUNT)

    gate = subparsers.add_parser("gate", help="verify the overlay and gate one product usage")
    gate.add_argument("--overlay-root", type=Path, required=True)
    gate.add_argument("--product-id", required=True)
    gate.add_argument("--usage", choices=GATE_USAGES, required=True)
    gate.add_argument("--catalog-index", type=Path)
    gate.add_argument("--expected-count", type=int, default=DEFAULT_EXPECTED_COUNT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "build":
            payload = build_overlay(
                args.catalog_index,
                cutoff=args.cutoff,
                output_root=args.output_root,
                expected_count=args.expected_count,
            )
            exit_code = 0
        elif args.command == "verify":
            payload = verify_overlay(args.overlay_root, args.catalog_index, args.expected_count)
            exit_code = 0 if payload["status"] == "COMPLETE" else 2
        elif args.command == "check":
            payload, exit_code = check_product(
                args.overlay_root,
                args.product_id,
                args.catalog_index,
                args.expected_count,
            )
        else:
            payload, exit_code = gate_usage(
                args.overlay_root,
                args.product_id,
                args.usage,
                args.catalog_index,
                args.expected_count,
            )
    except (OSError, DispositionError, json.JSONDecodeError) as error:
        payload = {
            "status": "FAIL_CLOSED",
            "error": str(error),
            "external_write_enabled": False,
        }
        exit_code = 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
