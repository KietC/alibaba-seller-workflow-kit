#!/usr/bin/env python3
"""Inspect the current SellerKit RAG manifest without loading the model/index."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from alibaba_asset_resolver import (  # noqa: E402
    DispositionPolicyError,
    HISTORICAL_REFERENCE_ONLY,
    disposition_decision,
    infer_product_id,
    is_archive_path,
    load_disposition_manifest,
)


DEFAULT_MANIFEST = Path(
    r"D:/SellerKit/synthetic/748973e2b820/sample.json"
)


def has_non_ascii(value: str) -> bool:
    return any(ord(char) > 127 for char in value)


def included_file_record(line: str) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return line.strip(), {}
    if not isinstance(payload, dict):
        return line.strip(), {}
    for key in ("path", "file", "source_path", "relative_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), payload
    return line.strip(), payload


def inspect_manifest(
    path: Path,
    *,
    disposition_manifest: Path | None = None,
    archive_root: Path | None = None,
) -> dict[str, Any]:
    if archive_root is not None and disposition_manifest is None:
        raise DispositionPolicyError(
            "--archive-root requires --disposition-manifest; refusing unclassified RAG sources."
        )
    dispositions: dict[str, str | None] = {}
    policy_metadata: dict[str, Any] | None = None
    if disposition_manifest is not None:
        dispositions, policy_metadata = load_disposition_manifest(disposition_manifest)

    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    included_path = Path(manifest["included_files_jsonl"])
    counts = {"total": 0, "alibaba_12": 0, "marketing_01": 0, "MODEL-001": 0}
    samples: dict[str, list[str]] = {"alibaba_12": [], "MODEL-001": []}
    archive_files = 0
    rag_eligible_archive_files = 0
    historical_reference_files = 0
    rejected_files = 0
    rejected_product_ids: set[str] = set()
    rejection_reasons: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    rejected_samples: list[dict[str, Any]] = []
    if included_path.exists():
        for line in included_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if not line.strip():
                continue
            counts["total"] += 1
            source_path, source_record = included_file_record(line)
            lowered = line.lower()
            if "\\12_alibaba\\" in lowered:
                counts["alibaba_12"] += 1
                if len(samples["alibaba_12"]) < 5:
                    samples["alibaba_12"].append(line[:500])
            if "\\01_sellerkit产品与营销素材\\" in lowered:
                counts["marketing_01"] += 1
            if "MODEL-001" in lowered:
                counts["MODEL-001"] += 1
                if len(samples["MODEL-001"]) < 5:
                    samples["MODEL-001"].append(line[:500])

            if is_archive_path(Path(source_path), archive_root):
                archive_files += 1
                if disposition_manifest is None:
                    raise DispositionPolicyError(
                        "Alibaba archive records are present but no disposition manifest was provided."
                    )
                product_id = str(source_record.get("product_id") or "").strip() or infer_product_id(
                    Path(source_path)
                )
                decision = disposition_decision(product_id, dispositions)
                disposition_counts[str(decision["record_disposition"] or "MISSING_OR_UNKNOWN")] += 1
                if decision["rag_allowed"]:
                    rag_eligible_archive_files += 1
                    if decision["record_disposition"] == HISTORICAL_REFERENCE_ONLY:
                        historical_reference_files += 1
                else:
                    rejected_files += 1
                    rejection_reasons[decision["reason"]] += 1
                    if product_id:
                        rejected_product_ids.add(product_id)
                    if len(rejected_samples) < 10:
                        rejected_samples.append(
                            {
                                "path": source_path,
                                "product_id": product_id,
                                "record_disposition": decision["record_disposition"],
                                "reason": decision["reason"],
                            }
                        )

    faiss_path = str(manifest.get("vector_index", ""))
    recommendations = []
    if counts["alibaba_12"] == 0:
        recommendations.append("rebuild_or_increment_index_with_12_Alibaba_rules")
    if rejected_files:
        recommendations.append("rebuild_index_without_rejected_archive_records")
    if has_non_ascii(faiss_path):
        recommendations.append("copy_faiss_index_to_checksum_keyed_ascii_temp_directory_before_read")
    recommendations.append("keep_product_facts_alibaba_rules_and_business_policies_logically_separate")
    recommendations.append("run_fixed_retrieval_eval_before_using_generated_fields")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(path),
        "manifest_created_at": manifest.get("created_at"),
        "model": manifest.get("model"),
        "stats": manifest.get("stats", {}),
        "included_file_counts": counts,
        "disposition_policy": {
            "required": archive_root is not None or archive_files > 0,
            "provided": disposition_manifest is not None,
            **(policy_metadata or {}),
        },
        "archive_policy_filter": {
            "archive_files": archive_files,
            "rag_eligible_archive_files": rag_eligible_archive_files,
            "historical_reference_files": historical_reference_files,
            "rejected_products": len(rejected_product_ids),
            "rejected_files": rejected_files,
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
            "disposition_counts": dict(sorted(disposition_counts.items())),
            "rejected_samples": rejected_samples,
            "status": "PASS" if rejected_files == 0 else "REJECTED_ARCHIVE_RECORDS_PRESENT",
        },
        "samples": samples,
        "faiss_index": {
            "path": faiss_path,
            "exists": Path(faiss_path).exists(),
            "contains_non_ascii": has_non_ascii(faiss_path),
            "known_current_runtime_result": "faiss.read_index failed on the Unicode path during 1999-12-16 diagnostic",
        },
        "ready_for_alibaba_autofill": False,
        "ready_for_local_rag_reference": rejected_files == 0,
        "recommendations": recommendations,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--disposition-manifest", type=Path)
    parser.add_argument(
        "--archive-root",
        type=Path,
        help="Alibaba archive root; specifying it requires a disposition manifest.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = inspect_manifest(
            args.manifest,
            disposition_manifest=args.disposition_manifest,
            archive_root=args.archive_root,
        )
    except DispositionPolicyError as exc:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "manifest_path": str(args.manifest),
            "status": "FAIL_CLOSED",
            "error": str(exc),
            "archive_policy_filter": {
                "rejected_products": 0,
                "rejected_files": 0,
                "rejection_reasons": {"disposition_policy_unavailable": 1},
                "status": "FAIL_CLOSED",
            },
            "ready_for_alibaba_autofill": False,
            "ready_for_local_rag_reference": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["archive_policy_filter"], ensure_ascii=False), file=sys.stderr)
        print(args.output, file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["included_file_counts"], ensure_ascii=False))
    print(args.output)
    return 3 if report["archive_policy_filter"]["rejected_files"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
