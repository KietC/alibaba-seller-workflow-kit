#!/usr/bin/env python3
"""Find local product assets and evaluate Alibaba publish-page constraints.

This script is read-only with respect to source assets. It writes only the JSON
path explicitly passed with --output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path(
    r"D:/SellerKit/synthetic/14f92d7e36a0/sample"
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}
DOCUMENT_EXTENSIONS = {".pdf", ".pptx", ".xlsx"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_SECONDS = 45.0

APPROVED_INTERNAL_SOURCE = "APPROVED_INTERNAL_SOURCE"
HISTORICAL_REFERENCE_ONLY = "HISTORICAL_REFERENCE_ONLY"
HARD_REJECT_DISPOSITIONS = {
    "UNUSABLE_WASTE_RECORD",
    "BLOCKED_UNCLASSIFIED",
}
KNOWN_DISPOSITIONS = {
    APPROVED_INTERNAL_SOURCE,
    HISTORICAL_REFERENCE_ONLY,
    *HARD_REJECT_DISPOSITIONS,
}
ARCHIVE_MARKER = "已上架商品全量归档_"


class DispositionPolicyError(ValueError):
    """Raised when an Alibaba archive source has no usable policy manifest."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        raise DispositionPolicyError("Disposition manifest must contain an object or array.")
    if "product_id" in payload:
        return [payload]
    for key in ("records", "products", "dispositions", "product_dispositions"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        if isinstance(rows, dict):
            payload = rows
            break
    rows = []
    for product_id, value in payload.items():
        if isinstance(value, str):
            rows.append({"product_id": product_id, "record_disposition": value})
        elif isinstance(value, dict):
            rows.append({"product_id": product_id, **value})
    return rows


def load_disposition_manifest(path: Path) -> tuple[dict[str, str | None], dict[str, Any]]:
    if not path.is_file():
        raise DispositionPolicyError(f"Disposition manifest does not exist: {path}")
    try:
        if path.suffix.lower() == ".jsonl":
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
        else:
            rows = _manifest_rows(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DispositionPolicyError(f"Cannot read disposition manifest: {exc}") from exc

    dispositions: dict[str, str | None] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise DispositionPolicyError(f"Disposition row {index} is not an object.")
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            raise DispositionPolicyError(f"Disposition row {index} has no product_id.")
        primary = row.get("record_disposition")
        legacy = row.get("disposition")
        if primary not in (None, "") and legacy not in (None, "") and str(primary) != str(legacy):
            raise DispositionPolicyError(
                f"Disposition row {index} has conflicting record_disposition/disposition values."
            )
        disposition = primary if primary not in (None, "") else legacy
        normalized = str(disposition).strip().upper() if disposition not in (None, "") else None
        if product_id in dispositions:
            raise DispositionPolicyError(f"Duplicate disposition product_id: {product_id}")
        dispositions[product_id] = normalized
    return dispositions, {
        "path": str(path),
        "sha256": sha256_file(path),
        "product_records": len(dispositions),
    }


def is_archive_path(path: Path, archive_root: Path | None = None) -> bool:
    if archive_root is not None:
        try:
            path.resolve().relative_to(archive_root.resolve())
            return True
        except ValueError:
            pass
    return ARCHIVE_MARKER.casefold() in str(path).casefold()


def infer_product_id(path: Path, explicit_product_id: str | None = None) -> str | None:
    if explicit_product_id:
        return str(explicit_product_id).strip()
    for part in reversed(path.parts):
        if re.fullmatch(r"\d{8,20}", part):
            return part
    return None


def disposition_decision(
    product_id: str | None,
    dispositions: dict[str, str | None],
) -> dict[str, Any]:
    if not product_id:
        disposition = None
        reason = "archive_product_id_unknown"
    elif product_id not in dispositions:
        disposition = None
        reason = "disposition_missing"
    else:
        disposition = dispositions[product_id]
        if disposition == APPROVED_INTERNAL_SOURCE:
            reason = "approved_internal_source"
        elif disposition == HISTORICAL_REFERENCE_ONLY:
            reason = "historical_reference_only_publish_blocked"
        elif disposition in HARD_REJECT_DISPOSITIONS:
            reason = f"record_disposition_{disposition.lower()}"
        else:
            reason = "record_disposition_unknown"
    return {
        "product_id": product_id,
        "record_disposition": disposition,
        "upload_allowed": disposition == APPROVED_INTERNAL_SOURCE,
        "rag_allowed": disposition in {APPROVED_INTERNAL_SOURCE, HISTORICAL_REFERENCE_ONLY},
        "forensic_reference_allowed": disposition == HISTORICAL_REFERENCE_ONLY,
        "reason": reason,
    }


def exact_model_pattern(model: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(model)}(?![A-Za-z0-9])", re.IGNORECASE)


def classify_role(path: Path) -> str:
    text = str(path).lower()
    if "产品主图" in text:
        return "main_image"
    if "产品渲染图" in text:
        return "render_image"
    if "产品单张" in text:
        return "product_sheet"
    if "产品说明书" in text or "说明书" in text:
        return "manual"
    if "产品视频" in text or path.suffix.lower() in VIDEO_EXTENSIONS:
        return "product_video"
    if "公司" in text or "企业" in text:
        return "company_asset"
    return "other"


def image_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"width": None, "height": None, "mode": None, "read_error": None}
    try:
        data = path.read_bytes()
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            result.update(
                width=int.from_bytes(data[16:20], "big"),
                height=int.from_bytes(data[20:24], "big"),
                mode="png",
            )
            return result
        if data.startswith(b"\xff\xd8"):
            position = 2
            start_of_frame = {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }
            while position + 8 < len(data):
                if data[position] != 0xFF:
                    position += 1
                    continue
                while position < len(data) and data[position] == 0xFF:
                    position += 1
                if position >= len(data):
                    break
                marker = data[position]
                position += 1
                if marker in {0x01, *range(0xD0, 0xDA)}:
                    continue
                if position + 2 > len(data):
                    break
                segment_length = int.from_bytes(data[position:position + 2], "big")
                if marker in start_of_frame and position + 7 <= len(data):
                    result.update(
                        height=int.from_bytes(data[position + 3:position + 5], "big"),
                        width=int.from_bytes(data[position + 5:position + 7], "big"),
                        mode="jpeg",
                    )
                    return result
                if segment_length < 2:
                    break
                position += segment_length
        raise ValueError("unsupported_or_invalid_image")
    except Exception as exc:
        result["read_error"] = f"{type(exc).__name__}: {exc}"
    return result


def video_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "duration_seconds": None,
        "width": None,
        "height": None,
        "codec": None,
        "probe_error": None,
    }
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        result["probe_error"] = "ffprobe_not_found"
        return result
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        payload = json.loads(completed.stdout)
        stream = (payload.get("streams") or [{}])[0]
        result.update(
            duration_seconds=float(payload.get("format", {}).get("duration")),
            width=stream.get("width"),
            height=stream.get("height"),
            codec=stream.get("codec_name"),
        )
    except Exception as exc:
        result["probe_error"] = f"{type(exc).__name__}: {exc}"
    return result


def evaluate_image(path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    width = metadata.get("width")
    height = metadata.get("height")
    square = bool(width and height and abs(width - height) / max(width, height) <= 0.08)
    minimum_dimensions = bool(width and height and width > 640 and height > 640)
    recommended_dimensions = bool(width and height and width <= 1000 and height <= 1000)
    extension_ok = path.suffix.lower() in IMAGE_EXTENSIONS
    size_ok = path.stat().st_size <= MAX_IMAGE_BYTES
    upload_ready_main = all((extension_ok, size_ok, square, minimum_dimensions))
    reasons = []
    if not extension_ok:
        reasons.append("unsupported_extension")
    if not size_ok:
        reasons.append("over_5mb")
    if not square:
        reasons.append("not_square")
    if not minimum_dimensions:
        reasons.append("dimensions_not_over_640")
    if upload_ready_main and not recommended_dimensions:
        reasons.append("over_recommended_1000px")
    return {
        "square_within_8_percent": square,
        "minimum_dimensions_ok": minimum_dimensions,
        "recommended_dimensions_ok": recommended_dimensions,
        "size_ok": size_ok,
        "upload_ready_main": upload_ready_main,
        "reasons": reasons,
    }


def evaluate_video(path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    duration = metadata.get("duration_seconds")
    size_ok = path.stat().st_size <= MAX_VIDEO_BYTES
    duration_ok = duration is not None and duration <= MAX_VIDEO_SECONDS
    reasons = []
    if not size_ok:
        reasons.append("over_100mb")
    if duration is None:
        reasons.append("duration_unknown")
    elif not duration_ok:
        reasons.append("over_45_seconds")
    return {
        "size_ok": size_ok,
        "duration_ok": duration_ok,
        "upload_ready_main_video": bool(size_ok and duration_ok),
        "reasons": reasons,
    }


def candidate_score(candidate: dict[str, Any]) -> int:
    source_policy = candidate.get("source_policy", {})
    if source_policy.get("archive_source") and not source_policy.get("upload_allowed"):
        return 0
    score = 0
    role = candidate["role"]
    if role == "main_image":
        score += 50
    elif role == "render_image":
        score += 25
    elif role == "product_sheet":
        score += 15
    elif role == "product_video":
        score += 20
    if candidate.get("compliance", {}).get("upload_ready_main"):
        score += 60
    if candidate.get("compliance", {}).get("upload_ready_main_video"):
        score += 60
    if candidate["model_in_filename"]:
        score += 25
    return score


def resolve_assets(
    model: str,
    root: Path,
    *,
    disposition_manifest: Path | None = None,
    archive_source: bool = False,
    archive_root: Path | None = None,
    product_id: str | None = None,
) -> dict[str, Any]:
    if not root.exists():
        raise FileNotFoundError(f"Asset root does not exist: {root}")
    archive_source = bool(archive_source or is_archive_path(root, archive_root))
    archive_detected = archive_source
    if archive_source and disposition_manifest is None:
        raise DispositionPolicyError(
            "Alibaba archive source requires --disposition-manifest; refusing unclassified assets."
        )
    dispositions: dict[str, str | None] = {}
    policy_metadata: dict[str, Any] | None = None
    if disposition_manifest is not None:
        dispositions, policy_metadata = load_disposition_manifest(disposition_manifest)

    pattern = exact_model_pattern(model)
    candidates = []
    scanned_files = 0
    hard_rejected_product_ids: set[str] = set()
    publish_rejected_product_ids: set[str] = set()
    hard_rejection_reasons: Counter[str] = Counter()
    publish_rejection_reasons: Counter[str] = Counter()
    hard_rejected_files = 0
    publish_rejected_files = 0
    historical_reference_files = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        scanned_files += 1
        if path.suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | DOCUMENT_EXTENSIONS:
            continue
        relative = path.relative_to(root)
        if not pattern.search(str(relative)):
            continue
        stat = path.stat()
        item: dict[str, Any] = {
            "path": str(path),
            "relative_path": str(relative),
            "extension": path.suffix.lower(),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "role": classify_role(path),
            "model_in_filename": bool(pattern.search(path.name)),
            "metadata": {},
            "compliance": {},
        }
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            item["metadata"] = image_metadata(path)
            item["compliance"] = evaluate_image(path, item["metadata"])
        elif path.suffix.lower() in VIDEO_EXTENSIONS:
            item["metadata"] = video_metadata(path)
            item["compliance"] = evaluate_video(path, item["metadata"])

        candidate_is_archive = bool(archive_source or is_archive_path(path, archive_root))
        if candidate_is_archive:
            archive_detected = True
            if disposition_manifest is None:
                raise DispositionPolicyError(
                    "Alibaba archive candidate requires a disposition manifest; refusing unclassified assets."
                )
            item_product_id = infer_product_id(path, product_id)
            decision = disposition_decision(item_product_id, dispositions)
            item["source_policy"] = {"archive_source": True, **decision}
            if not decision["upload_allowed"]:
                publish_rejected_files += 1
                publish_rejection_reasons[decision["reason"]] += 1
                if item_product_id:
                    publish_rejected_product_ids.add(item_product_id)
                compliance = item["compliance"]
                if "upload_ready_main" in compliance:
                    compliance["upload_ready_main"] = False
                if "upload_ready_main_video" in compliance:
                    compliance["upload_ready_main_video"] = False
                compliance.setdefault("reasons", []).append(decision["reason"])
            if decision["record_disposition"] == HISTORICAL_REFERENCE_ONLY:
                historical_reference_files += 1
            if not decision["rag_allowed"]:
                hard_rejected_files += 1
                hard_rejection_reasons[decision["reason"]] += 1
                if item_product_id:
                    hard_rejected_product_ids.add(item_product_id)
        else:
            item["source_policy"] = {
                "archive_source": False,
                "product_id": None,
                "record_disposition": None,
                "upload_allowed": True,
                "rag_allowed": True,
                "forensic_reference_allowed": False,
                "reason": "local_non_alibaba_source",
            }
        item["score"] = candidate_score(item)
        candidates.append(item)

    candidates.sort(key=lambda item: (-item["score"], item["relative_path"].lower()))
    ready_images = [item for item in candidates if item["compliance"].get("upload_ready_main")]
    ready_videos = [item for item in candidates if item["compliance"].get("upload_ready_main_video")]
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(root),
        "model": model,
        "disposition_policy": {
            "required": archive_detected,
            "provided": disposition_manifest is not None,
            **(policy_metadata or {}),
        },
        "rules": {
            "main_image_minimum_count": 4,
            "main_image_maximum_count": 6,
            "main_image_max_bytes": MAX_IMAGE_BYTES,
            "main_image_min_width_exclusive": 640,
            "main_image_min_height_exclusive": 640,
            "main_video_max_bytes": MAX_VIDEO_BYTES,
            "main_video_max_seconds": MAX_VIDEO_SECONDS,
        },
        "summary": {
            "scanned_files": scanned_files,
            "matched_candidates": len(candidates),
            "upload_ready_main_images": len(ready_images),
            "missing_main_images": max(0, 4 - len(ready_images)),
            "upload_ready_main_videos": len(ready_videos),
            "archive_source": archive_detected,
            "rejected_products": len(hard_rejected_product_ids),
            "rejected_files": hard_rejected_files,
            "rejection_reasons": dict(sorted(hard_rejection_reasons.items())),
            "publish_rejected_products": len(publish_rejected_product_ids),
            "publish_rejected_files": publish_rejected_files,
            "publish_rejection_reasons": dict(sorted(publish_rejection_reasons.items())),
            "historical_reference_files": historical_reference_files,
        },
        "candidates": candidates,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact product model, for example MODEL-001")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Marketing asset root")
    parser.add_argument("--output", type=Path, required=True, help="JSON report path")
    parser.add_argument("--disposition-manifest", type=Path)
    parser.add_argument(
        "--archive-source",
        action="store_true",
        help="Treat --root as an Alibaba archive source and require a disposition policy.",
    )
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--product-id", help="Explicit Alibaba product ID for a product-scoped root")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = resolve_assets(
            args.model.strip(),
            args.root,
            disposition_manifest=args.disposition_manifest,
            archive_source=args.archive_source,
            archive_root=args.archive_root,
            product_id=args.product_id,
        )
    except DispositionPolicyError as exc:
        report = {
            "schema_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_root": str(args.root),
            "model": args.model.strip(),
            "status": "FAIL_CLOSED",
            "error": str(exc),
            "summary": {
                "rejected_products": 0,
                "rejected_files": 0,
                "rejection_reasons": {"disposition_policy_unavailable": 1},
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False), file=sys.stderr)
        print(args.output, file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
