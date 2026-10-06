from __future__ import annotations

"""Local-only visual feature orchestration for SellerKit catalog comparison.

This module deliberately imports neither Torch/OpenCLIP nor Paddle/ONNX.  Those
frameworks live in dedicated worker processes because their CUDA runtimes are
not safe to load together in the long-running review process on this host.

The public functions in this file create *features and receipts only*.  They do
not label a pair as a match and they do not perform any browser or remote write.
"""

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = "sellerkit.visual_features.v1"
ALLOWED_DISPOSITION = "HISTORICAL_REFERENCE_ONLY"
REJECTED_DISPOSITION = "UNUSABLE_WASTE_RECORD"
from .config import EXPECTED, get_settings


DEFAULT_PROJECT_ROOT = Path(r"D:/SellerKit/synthetic/76fc1bc7aed5/sample")
DEFAULT_OPENCLIP_PYTHON = Path(
    r"D:/SellerKit/synthetic/88815e2edaf2/sample.exe"
)
DEFAULT_OPENCLIP_WEIGHT = Path(r"D:/SellerKit/synthetic/9ce8db3bf570/sample.safetensors")
DEFAULT_PPOCR_PYTHON = Path(
    r"D:/SellerKit/synthetic/de4f8ed433c8/sample.exe"
)
DEFAULT_PADDLE_VL_PYTHON = Path(r"D:/SellerKit/synthetic/06f0573f7932/sample.exe")
DEFAULT_PADDLE_VL_MODEL = Path(
    r"D:/SellerKit/synthetic/70dfd778942a/sample.6--66317acc4c9fc17bd154591ce650735cd2855f3e"
)

_MEDIA_PATH_MARKERS = (
    "product.mediaitems.",
    "product/mediaitems/",
    "json_ld_product.image",
    "json_ld.images",
    "jsonld.image",
)
_UI_PATH_MARKERS = (
    "seller",
    "company",
    "banner",
    "logo",
    "icon",
    "avatar",
    "accountportrait",
    "photourl",
    "companycard",
    "navigation",
    "header",
    "footer",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_text_atomic(path, _stable_json(value) + "\n")


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    _write_text_atomic(path, "".join(_stable_json(row) + "\n" for row in rows))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            rows.append(value)
    return rows


def _safe_under(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Local media path escapes archive root: {resolved}") from exc
    return resolved


def _normalize_model(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _find_prefixed_directory(root: Path, prefix: str) -> Path:
    matches = sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith(prefix))
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected one {prefix}* directory below {root}; found {len(matches)}")
    return matches[0]


def discover_alibaba_archive_root(parent: Path | None = None) -> Path:
    """Discover the explicitly configured frozen archive without embedding a locale-sensitive name."""

    parent = (parent or Path(get_settings()["paths"]["alibaba_archive_root"]).parent).resolve()
    matches: list[Path] = []
    for candidate in parent.iterdir():
        if not candidate.is_dir() or not candidate.name.endswith("_" + get_settings()["capture_snapshot"]):
            continue
        prefixes = {child.name[:2] for child in candidate.iterdir() if child.is_dir()}
        if {"03", "05"}.issubset(prefixes):
            matches.append(candidate.resolve())
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected one frozen Alibaba archive below {parent}; found {len(matches)}")
    return matches[0]


@dataclass(frozen=True)
class VisionRuntimePaths:
    project_root: Path = DEFAULT_PROJECT_ROOT
    openclip_python: Path = DEFAULT_OPENCLIP_PYTHON
    openclip_weight: Path = DEFAULT_OPENCLIP_WEIGHT
    ppocr_python: Path = DEFAULT_PPOCR_PYTHON
    paddle_vl_python: Path = DEFAULT_PADDLE_VL_PYTHON
    paddle_vl_model: Path = DEFAULT_PADDLE_VL_MODEL

    @property
    def scripts_root(self) -> Path:
        return self.project_root / "scripts"

    @property
    def website_manifest(self) -> Path:
        return self.project_root / "work" / "website_main_images.jsonl"

    @property
    def alibaba_historical(self) -> Path:
        return self.project_root / "work" / "alibaba_historical_sanitized.jsonl"

    @property
    def alibaba_presence_guard(self) -> Path:
        return self.project_root / "work" / "alibaba_presence_guard.jsonl"


def _coerce_local_path(row: dict[str, Any]) -> Path:
    for key in ("local_file", "local_path", "blob_path", "path", "image_path"):
        value = str(row.get(key) or "").strip()
        if value:
            return Path(value)
    raise ValueError("Image manifest row has no local path")


def _make_image_id(source: str, product_key: str, role: str, order: int, expected_sha256: str, path: Path) -> str:
    material = {
        "source": source,
        "product_key": product_key,
        "role": role,
        "order": order,
        "expected_sha256": expected_sha256,
        "path": str(path),
    }
    return hashlib.sha256(_stable_json(material).encode("utf-8")).hexdigest()[:32]


def _website_rows(manifest: Path, *, strict_counts: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_rows = _load_jsonl(manifest)
    selected: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, row in enumerate(raw_rows, 1):
        model = _normalize_model(row.get("canonical_model") or row.get("model") or row.get("raw_model"))
        if not model:
            exclusions.append({"source": "website", "ordinal": ordinal, "reason": "MISSING_MODEL"})
            continue
        if model in seen:
            raise ValueError(f"Duplicate website model in main-image manifest: {model}")
        seen.add(model)
        path = _coerce_local_path(row).resolve()
        if not path.is_file():
            exclusions.append({"source": "website", "product_key": model, "reason": "LOCAL_FILE_MISSING", "path": str(path)})
            continue
        expected_sha = str(row.get("sha256") or row.get("blob_sha256") or "").lower()
        role = str(row.get("role") or "main_image")
        if role != "main_image":
            exclusions.append({"source": "website", "product_key": model, "reason": "NOT_MAIN_IMAGE", "role": role})
            continue
        order = int(row.get("order") or 1)
        selected.append(
            {
                "image_id": _make_image_id("website", model, role, order, expected_sha, path),
                "source": "website",
                "product_key": model,
                "canonical_model": model,
                "role": role,
                "order": order,
                "local_path": str(path),
                "expected_sha256": expected_sha,
                "source_url": str(row.get("source_url") or row.get("url") or ""),
                "manifest_row": ordinal,
            }
        )
    if strict_counts and len(selected) != EXPECTED["website_products"]:
        raise ValueError(
            f"Website main-image gate requires {EXPECTED['website_products']} materialized rows; got {len(selected)} "
            f"({len(exclusions)} excluded)"
        )
    return selected, exclusions


def _source_variant_key(source_path: str) -> str:
    value = source_path.lower()
    return re.sub(r"_(?:50|80|120|200|350)x(?:50|80|120|200|350)\.(?:jpg|jpeg|png|webp|avif)$", "", value)


def _is_product_media_path(detail_path: str) -> bool:
    normalized = detail_path.lower().replace("[", ".").replace("]", "")
    return any(marker in normalized for marker in _MEDIA_PATH_MARKERS)


def _is_ui_context(detail_path: str, source_path: str) -> bool:
    text = f"{detail_path} {source_path}".lower().replace("_", "")
    return any(marker in text for marker in _UI_PATH_MARKERS) or "/tfs/" in source_path.lower()


def _alibaba_rows(
    historical_path: Path,
    presence_guard_path: Path,
    archive_root: Path,
    *,
    strict_counts: bool,
    high_df_ratio: float,
    high_df_min_products: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    historical = _load_jsonl(historical_path)
    presence = _load_jsonl(presence_guard_path)
    allowed_ids = {
        str(row.get("product_id") or "")
        for row in historical
        if row.get("record_disposition") == ALLOWED_DISPOSITION
    }
    waste_ids = {
        str(row.get("product_id") or "")
        for row in presence
        if row.get("record_disposition") == REJECTED_DISPOSITION
    }
    if "" in allowed_ids or "" in waste_ids or allowed_ids & waste_ids:
        raise ValueError("Alibaba disposition sets are malformed or overlap")
    if strict_counts and (len(allowed_ids), len(waste_ids)) != (EXPECTED["historical_reference"], EXPECTED["unusable_waste"]):
        raise ValueError(
            f"Disposition gate expected {EXPECTED['historical_reference']} allowed/{EXPECTED['unusable_waste']} waste; "
            f"got {len(allowed_ids)}/{len(waste_ids)}"
        )

    standard_root = _find_prefixed_directory(archive_root, "03")
    df_threshold = max(high_df_min_products, math.ceil(max(1, len(allowed_ids)) * high_df_ratio))
    raw_candidates: list[dict[str, Any]] = []
    ui_candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for product_id in sorted(allowed_ids, key=lambda value: (len(value), value)):
        manifest = standard_root / product_id / "asset_manifest.jsonl"
        if not manifest.is_file():
            exclusions.append({"source": "alibaba", "product_key": product_id, "reason": "ASSET_MANIFEST_MISSING"})
            continue
        for row_number, row in enumerate(_load_jsonl(manifest), 1):
            role = str(row.get("role") or "")
            if role not in {"gallery", "product_media"}:
                continue
            detail_path = str(row.get("detail_data_path") or "")
            source_path = str(row.get("source_path") or "")
            if role == "product_media" and not _is_product_media_path(detail_path):
                if _is_ui_context(detail_path, source_path):
                    ui_candidates.append(
                        {
                            "source": "alibaba",
                            "product_key": product_id,
                            "role": role,
                            "detail_data_path": detail_path,
                            "blob_sha256": str(row.get("blob_sha256") or "").lower(),
                        }
                    )
                else:
                    exclusions.append(
                        {"source": "alibaba", "product_key": product_id, "reason": "NON_PRODUCT_MEDIA_CONTEXT", "role": role, "detail_data_path": detail_path}
                    )
                continue
            if _is_ui_context(detail_path, source_path):
                ui_candidates.append(
                    {
                        "source": "alibaba",
                        "product_key": product_id,
                        "role": role,
                        "detail_data_path": detail_path,
                        "blob_sha256": str(row.get("blob_sha256") or "").lower(),
                    }
                )
                continue
            if row.get("download_status") != "downloaded":
                exclusions.append({"source": "alibaba", "product_key": product_id, "reason": "NOT_DOWNLOADED", "role": role})
                continue
            local_value = str(row.get("local_path") or "")
            if not local_value:
                exclusions.append({"source": "alibaba", "product_key": product_id, "reason": "LOCAL_PATH_MISSING", "role": role})
                continue
            local_path = _safe_under(archive_root / Path(local_value), archive_root)
            if not local_path.is_file():
                exclusions.append(
                    {"source": "alibaba", "product_key": product_id, "reason": "LOCAL_FILE_MISSING", "path": str(local_path)}
                )
                continue
            content_type = str(row.get("content_type") or "").lower()
            if content_type and not content_type.startswith("image/"):
                exclusions.append({"source": "alibaba", "product_key": product_id, "reason": "NON_IMAGE_CONTENT", "content_type": content_type})
                continue
            raw_candidates.append(
                {
                    "source": "alibaba",
                    "product_key": product_id,
                    "canonical_model": "",
                    "role": role,
                    "order": int(row.get("order") or row_number),
                    "local_path": str(local_path),
                    "expected_sha256": str(row.get("blob_sha256") or "").lower(),
                    "source_url": "",
                    "source_origin": str(row.get("source_origin") or ""),
                    "source_path": source_path,
                    "detail_data_path": detail_path,
                    "content_type": content_type,
                    "bytes": int(row.get("bytes") or local_path.stat().st_size),
                    "manifest_path": str(manifest),
                    "manifest_row": row_number,
                    "variant_key": _source_variant_key(source_path),
                }
            )

    ui_blob_products: dict[str, set[str]] = defaultdict(set)
    for row in ui_candidates:
        if row["blob_sha256"]:
            ui_blob_products[row["blob_sha256"]].add(row["product_key"])
    high_df_ui_count = 0
    for row in ui_candidates:
        document_frequency = len(ui_blob_products.get(row["blob_sha256"], ()))
        high_df = document_frequency >= df_threshold
        high_df_ui_count += int(high_df)
        exclusions.append(
            {
                "source": row["source"],
                "product_key": row["product_key"],
                "reason": "HIGH_DOCUMENT_FREQUENCY_UI_ASSET" if high_df else "UI_ASSET_CONTEXT",
                "role": row["role"],
                "detail_data_path": row["detail_data_path"],
                "document_frequency": document_frequency,
                "threshold": df_threshold,
            }
        )

    # Prefer one original/big asset per product/source URL family.  The discarded
    # 50/120/350 variants are retained in the receipt, not silently dropped.
    by_variant: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in raw_candidates:
        by_variant[(row["product_key"], row["variant_key"])].append(row)
    candidates: list[dict[str, Any]] = []
    for _, variants in sorted(by_variant.items()):
        variants.sort(key=lambda row: (row["role"] == "gallery", row["bytes"], -row["order"]), reverse=True)
        candidates.append(variants[0])
        for redundant in variants[1:]:
            exclusions.append(
                {
                    "source": "alibaba",
                    "product_key": redundant["product_key"],
                    "reason": "REDUNDANT_RESOLUTION_VARIANT",
                    "role": redundant["role"],
                    "detail_data_path": redundant["detail_data_path"],
                }
            )

    blob_products: dict[str, set[str]] = defaultdict(set)
    for row in candidates:
        if row["expected_sha256"]:
            blob_products[row["expected_sha256"]].add(row["product_key"])
    selected: list[dict[str, Any]] = []
    high_df_excluded = high_df_ui_count
    for row in candidates:
        document_frequency = len(blob_products.get(row["expected_sha256"], ()))
        # A high-DF product_media object is normally seller/UI chrome that slipped
        # through a changing JSON path. Gallery originals remain usable evidence.
        if row["role"] == "product_media" and document_frequency >= df_threshold:
            high_df_excluded += 1
            exclusions.append(
                {
                    "source": "alibaba",
                    "product_key": row["product_key"],
                    "reason": "HIGH_DOCUMENT_FREQUENCY_UI_ASSET",
                    "document_frequency": document_frequency,
                    "threshold": df_threshold,
                }
            )
            continue
        row["document_frequency"] = document_frequency
        row["image_id"] = _make_image_id(
            "alibaba", row["product_key"], row["role"], row["order"], row["expected_sha256"], Path(row["local_path"])
        )
        row.pop("variant_key", None)
        selected.append(row)

    product_models = {str(row.get("product_id") or ""): _normalize_model(row.get("canonical_model")) for row in historical}
    for row in selected:
        row["canonical_model"] = product_models.get(row["product_key"], "")
    stats = {
        "eligible_product_ids": len(allowed_ids),
        "waste_product_ids_excluded": len(waste_ids),
        "eligible_products_with_images": len({row["product_key"] for row in selected}),
        "raw_product_media_gallery_rows": len(raw_candidates),
        "selected_images": len(selected),
        "high_df_threshold_products": df_threshold,
        "high_df_ui_images_excluded": high_df_excluded,
    }
    return sorted(selected, key=lambda row: (row["product_key"], row["order"], row["image_id"])), exclusions, stats


def build_visual_input_index(
    *,
    output_path: Path,
    runtime: VisionRuntimePaths | None = None,
    website_manifest: Path | None = None,
    alibaba_archive_root: Path | None = None,
    strict_counts: bool = True,
    high_df_ratio: float = 0.05,
    high_df_min_products: int = 8,
) -> dict[str, Any]:
    """Index materialized website main images and eligible Alibaba gallery media."""

    runtime = runtime or VisionRuntimePaths()
    website_manifest = (website_manifest or runtime.website_manifest).resolve()
    archive_root = (alibaba_archive_root or discover_alibaba_archive_root()).resolve()
    website, website_exclusions = _website_rows(website_manifest, strict_counts=strict_counts)
    alibaba, alibaba_exclusions, alibaba_stats = _alibaba_rows(
        runtime.alibaba_historical,
        runtime.alibaba_presence_guard,
        archive_root,
        strict_counts=strict_counts,
        high_df_ratio=high_df_ratio,
        high_df_min_products=high_df_min_products,
    )
    exclusions = website_exclusions + alibaba_exclusions
    exclusion_counts = dict(sorted(Counter(row["reason"] for row in exclusions).items()))
    images = website + alibaba
    product_index: dict[str, dict[str, list[str]]] = {"website": defaultdict(list), "alibaba": defaultdict(list)}  # type: ignore[assignment]
    for image in images:
        product_index[image["source"]][image["product_key"]].append(image["image_id"])
    index = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "visual_input_index",
        "created_at_utc": _utc_now(),
        "feature_only": True,
        "decision_generated": False,
        "source_policy": {
            "website": "OFFICIAL_CURRENT_MAIN_IMAGE",
            "alibaba": ALLOWED_DISPOSITION,
            "rejected_disposition": REJECTED_DISPOSITION,
            "waste_records_used_for_visual_features": 0,
            "allowed_roles": ["main_image", "gallery", "product_media"],
            "high_document_frequency_ui_filter": {"ratio": high_df_ratio, "minimum_products": high_df_min_products},
        },
        "inputs": {
            "website_manifest": str(website_manifest),
            "website_manifest_sha256": _sha256_file(website_manifest),
            "alibaba_historical": str(runtime.alibaba_historical),
            "alibaba_historical_sha256": _sha256_file(runtime.alibaba_historical),
            "alibaba_presence_guard": str(runtime.alibaba_presence_guard),
            "alibaba_presence_guard_sha256": _sha256_file(runtime.alibaba_presence_guard),
            "alibaba_archive_root": str(archive_root),
        },
        "counts": {
            "website_products": len(product_index["website"]),
            "website_images": len(website),
            "alibaba_eligible_products": alibaba_stats["eligible_product_ids"],
            "alibaba_products_with_images": len(product_index["alibaba"]),
            "alibaba_images": len(alibaba),
            "alibaba_waste_records_excluded": alibaba_stats["waste_product_ids_excluded"],
            "total_images": len(images),
            "exclusions": len(exclusions),
        },
        "alibaba_media_stats": alibaba_stats,
        "exclusion_counts": exclusion_counts,
        "exclusion_examples": exclusions[:100],
        "product_index": {
            source: {key: values for key, values in sorted(groups.items())}
            for source, groups in product_index.items()
        },
        "images": sorted(images, key=lambda row: (row["source"], row["product_key"], row["order"], row["image_id"])),
    }
    _write_json_atomic(output_path, index)
    index["artifact_sha256"] = _sha256_file(output_path)
    return index


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _dhash(image: Any, hash_size: int = 8) -> str:
    gray = image.convert("L").resize((hash_size + 1, hash_size))
    pixels = list(gray.get_flattened_data() if hasattr(gray, "get_flattened_data") else gray.getdata())
    bits: list[bool] = []
    for y in range(hash_size):
        offset = y * (hash_size + 1)
        bits.extend(pixels[offset + x] > pixels[offset + x + 1] for x in range(hash_size))
    value = sum((1 << index) for index, bit in enumerate(bits) if bit)
    return f"{value:0{hash_size * hash_size // 4}x}"


def _phash(image: Any, hash_size: int = 8, highfreq_factor: int = 4) -> str:
    size = hash_size * highfreq_factor
    gray = image.convert("L").resize((size, size))
    pixels = list(gray.get_flattened_data() if hasattr(gray, "get_flattened_data") else gray.getdata())
    cosine = [
        [math.cos(math.pi * (2 * x + 1) * u / (2 * size)) for x in range(size)]
        for u in range(hash_size)
    ]
    row_dct = [[0.0] * size for _ in range(hash_size)]
    for u in range(hash_size):
        weights = cosine[u]
        for y in range(size):
            row_dct[u][y] = sum(weights[x] * pixels[y * size + x] for x in range(size))
    coefficients: list[float] = []
    for u in range(hash_size):
        for v in range(hash_size):
            coefficients.append(sum(row_dct[u][y] * cosine[v][y] for y in range(size)))
    threshold = _median(coefficients[1:])
    value = sum((1 << index) for index, coefficient in enumerate(coefficients) if coefficient > threshold)
    return f"{value:0{hash_size * hash_size // 4}x}"


def _cpu_feature_task(path_text: str) -> dict[str, Any]:
    """Top-level and picklable for Windows spawn pools."""

    path = Path(path_text)
    started = time.perf_counter()
    base = {"local_path": str(path), "sha256": "", "phash": "", "dhash": "", "status": "FAILED"}
    try:
        from PIL import Image

        digest = _sha256_file(path)
        with Image.open(path) as image:
            image.load()
            base.update(
                {
                    "status": "PASS",
                    "sha256": digest,
                    "bytes": path.stat().st_size,
                    "format": str(image.format or ""),
                    "mode": str(image.mode),
                    "width": int(image.width),
                    "height": int(image.height),
                    "phash": _phash(image),
                    "dhash": _dhash(image),
                }
            )
    except Exception as exc:
        base["error"] = f"{type(exc).__name__}: {exc}"
    base["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return base


def _calculate_unique_cpu_features(paths: list[str], workers: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    started = time.perf_counter()
    status = "PASS"
    degradation: str | None = None
    if workers <= 1:
        rows = [_cpu_feature_task(path) for path in paths]
        effective_workers = 1
    else:
        effective_workers = min(workers, len(paths), 61 if os.name == "nt" else workers)
        try:
            context = multiprocessing.get_context("spawn") if os.name == "nt" else None
            with ProcessPoolExecutor(max_workers=effective_workers, mp_context=context) as pool:
                rows = list(pool.map(_cpu_feature_task, paths, chunksize=max(1, len(paths) // (effective_workers * 4))))
        except Exception as exc:
            rows = [_cpu_feature_task(path) for path in paths]
            status = "DEGRADED_TO_SINGLE_PROCESS"
            degradation = f"{type(exc).__name__}: {exc}"
            effective_workers = 1
    elapsed = time.perf_counter() - started
    core = [
        {key: row.get(key) for key in ("local_path", "status", "sha256", "phash", "dhash", "width", "height", "format", "error")}
        for row in rows
    ]
    metrics = {
        "status": status,
        "requested_workers": workers,
        "effective_workers": effective_workers,
        "unique_images": len(paths),
        "elapsed_sec": round(elapsed, 6),
        "images_per_sec": round(len(paths) / max(elapsed, 1e-9), 3),
        "result_digest": hashlib.sha256(_stable_json(core).encode("utf-8")).hexdigest(),
        "degradation_reason": degradation,
    }
    return rows, metrics


def extract_cpu_features(index: dict[str, Any], *, output_path: Path, workers: int = 16) -> dict[str, Any]:
    images = list(index.get("images") or [])
    paths = sorted({str(row["local_path"]) for row in images})
    unique_features, metrics = _calculate_unique_cpu_features(paths, workers)
    by_path = {row["local_path"]: row for row in unique_features}
    rows: list[dict[str, Any]] = []
    sha_mismatches = 0
    for image in images:
        feature = dict(by_path[str(image["local_path"])])
        expected = str(image.get("expected_sha256") or "")
        integrity = "NOT_DECLARED"
        if expected:
            integrity = "PASS" if expected == feature.get("sha256") else "FAIL"
            sha_mismatches += int(integrity == "FAIL")
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "artifact_type": "cpu_image_feature",
                "image_id": image["image_id"],
                "source": image["source"],
                "product_key": image["product_key"],
                "role": image["role"],
                "expected_sha256": expected,
                "integrity_status": integrity,
                **feature,
            }
        )
    _write_jsonl_atomic(output_path, rows)
    return {
        "status": "PASS" if sha_mismatches == 0 and all(row["status"] == "PASS" for row in rows) else "FAIL_FEATURE_OR_INTEGRITY",
        "output_path": str(output_path),
        "output_sha256": _sha256_file(output_path),
        "image_rows": len(rows),
        "unique_files": len(paths),
        "sha256_mismatches": sha_mismatches,
        "failed_images": sum(row["status"] != "PASS" for row in rows),
        "execution": metrics,
    }


def benchmark_cpu_features(
    index: dict[str, Any],
    *,
    output_path: Path,
    worker_counts: Sequence[int] = (1, 8, 16, 32),
    limit: int = 128,
) -> dict[str, Any]:
    paths = sorted({str(row["local_path"]) for row in index.get("images") or []})[: max(1, limit)]
    runs = []
    for requested in worker_counts:
        _, metrics = _calculate_unique_cpu_features(paths, int(requested))
        runs.append(metrics)
    digests = {row["result_digest"] for row in runs}
    fastest = max(runs, key=lambda row: row["images_per_sec"]) if runs else None
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "cpu_feature_benchmark",
        "created_at_utc": _utc_now(),
        "sample_size": len(paths),
        "requested_worker_counts": list(worker_counts),
        "deterministic_across_worker_counts": len(digests) == 1,
        "fastest_requested_workers": fastest["requested_workers"] if fastest else None,
        "runs": runs,
    }
    _write_json_atomic(output_path, receipt)
    receipt["artifact_sha256"] = _sha256_file(output_path)
    return receipt


def _worker_call(
    *,
    python_executable: Path,
    script: Path,
    request: dict[str, Any],
    work_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    work_root.mkdir(parents=True, exist_ok=True)
    request_path = work_root / f"request-{script.stem}.json"
    response_path = work_root / f"response-{script.stem}.json"
    request = {**request, "response_path": str(response_path)}
    _write_json_atomic(request_path, request)
    environment = os.environ.copy()
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "NO_PROXY": "*",
        }
    )
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(python_executable), str(script), "--request", str(request_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=environment,
        )
        response = json.loads(response_path.read_text(encoding="utf-8")) if response_path.is_file() else {}
        return {
            **response,
            "worker_returncode": completed.returncode,
            "worker_elapsed_sec": round(time.perf_counter() - started, 3),
            "worker_stdout_tail": completed.stdout[-4000:],
            "worker_stderr_tail": completed.stderr[-4000:],
            "request_path": str(request_path),
            "request_sha256": _sha256_file(request_path),
            "response_path": str(response_path) if response_path.is_file() else "",
            "response_sha256": _sha256_file(response_path) if response_path.is_file() else "",
        }
    except Exception as exc:
        return {
            "status": "DEGRADED_WORKER_UNAVAILABLE",
            "error": f"{type(exc).__name__}: {exc}",
            "worker_elapsed_sec": round(time.perf_counter() - started, 3),
            "request_path": str(request_path),
            "request_sha256": _sha256_file(request_path),
        }


def run_openclip_features(
    index: dict[str, Any],
    *,
    output_root: Path,
    runtime: VisionRuntimePaths | None = None,
    batch_size: int = 128,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    runtime = runtime or VisionRuntimePaths()
    if not runtime.openclip_python.is_file() or not runtime.openclip_weight.is_file():
        return {
            "status": "DEGRADED_OPENCLIP_RUNTIME_MISSING",
            "python_exists": runtime.openclip_python.is_file(),
            "weight_exists": runtime.openclip_weight.is_file(),
        }
    embedding_path = output_root / "openclip_embeddings.jsonl"
    similarity_path = output_root / "openclip_product_similarity.jsonl"
    request = {
        "schema_version": SCHEMA_VERSION,
        "mode": "batch",
        "model_name": "ViT-B-32",
        "weight_path": str(runtime.openclip_weight),
        "batch_size": batch_size,
        "allow_cpu": False,
        "images": [
            {key: row[key] for key in ("image_id", "source", "product_key", "local_path")}
            for row in index.get("images") or []
        ],
        "product_index": index.get("product_index") or {},
        "embedding_output_path": str(embedding_path),
        "similarity_output_path": str(similarity_path),
        "feature_only": True,
        "decision_generated": False,
    }
    response = _worker_call(
        python_executable=runtime.openclip_python,
        script=runtime.scripts_root / "vision_openclip_worker.py",
        request=request,
        work_root=output_root / "worker_openclip",
        timeout_seconds=timeout_seconds,
    )
    if response.get("status") != "PASS":
        response["degraded_to"] = "SHA256_PHASH_DHASH_ONLY"
    return response


def run_ocr_features(
    index: dict[str, Any],
    *,
    output_root: Path,
    engine: str = "ppocrv4",
    runtime: VisionRuntimePaths | None = None,
    paddle_vl_smoke_receipt: Path | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    runtime = runtime or VisionRuntimePaths()
    if engine not in {"ppocrv4", "paddleocr-vl"}:
        raise ValueError(f"Unsupported OCR engine: {engine}")
    python_executable = runtime.ppocr_python if engine == "ppocrv4" else runtime.paddle_vl_python
    if not python_executable.is_file():
        return {"status": "DEGRADED_OCR_RUNTIME_MISSING", "engine": engine, "python": str(python_executable)}
    if engine == "paddleocr-vl" and paddle_vl_smoke_receipt is None:
        return {
            "status": "BLOCKED_REAL_PRODUCT_SMOKE_REQUIRED",
            "engine": engine,
            "reason": "PaddleOCR-VL batch inference is fail-closed until a real-product Gate0 receipt passes",
        }
    output_path = output_root / f"ocr_{engine}.jsonl"
    request = {
        "schema_version": SCHEMA_VERSION,
        "mode": "batch",
        "engine": engine,
        "images": [{key: row[key] for key in ("image_id", "source", "product_key", "local_path")} for row in index.get("images") or []],
        "output_path": str(output_path),
        "model_path": str(runtime.paddle_vl_model) if engine == "paddleocr-vl" else "",
        "smoke_receipt": str(paddle_vl_smoke_receipt) if paddle_vl_smoke_receipt else "",
        "smoke_receipt_sha256": _sha256_file(paddle_vl_smoke_receipt) if paddle_vl_smoke_receipt else "",
        "feature_only": True,
        "decision_generated": False,
    }
    response = _worker_call(
        python_executable=python_executable,
        script=runtime.scripts_root / "vision_ocr_worker.py",
        request=request,
        work_root=output_root / f"worker_ocr_{engine}",
        timeout_seconds=timeout_seconds,
    )
    if response.get("status") != "PASS":
        response["degraded_to"] = "OCR_FEATURE_OMITTED"
    return response


def run_paddle_vl_real_product_smoke(
    index: dict[str, Any],
    *,
    output_root: Path,
    runtime: VisionRuntimePaths | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Load PaddleOCR-VL on two indexed product images and emit its enablement gate.

    A subsequent ``run_ocr_features(..., engine="paddleocr-vl")`` must bind to
    the exact response receipt SHA-256.  Merely finding the model files never
    enables batch use.
    """

    runtime = runtime or VisionRuntimePaths()
    if not runtime.paddle_vl_python.is_file() or not runtime.paddle_vl_model.is_dir():
        return {
            "status": "DEGRADED_PADDLEOCR_VL_RUNTIME_MISSING",
            "python_exists": runtime.paddle_vl_python.is_file(),
            "model_exists": runtime.paddle_vl_model.is_dir(),
        }
    sample = select_gate0_images(index, website_count=1, alibaba_count=1)
    if len({row["source"] for row in sample}) != 2:
        return {"status": "BLOCKED_REAL_PRODUCT_SMOKE_SAMPLE_INCOMPLETE", "sample_count": len(sample)}
    request = {
        "schema_version": SCHEMA_VERSION,
        "mode": "smoke",
        "engine": "paddleocr-vl",
        "images": [{key: row[key] for key in ("image_id", "source", "product_key", "local_path")} for row in sample],
        "output_path": str(output_root / "paddleocr_vl_real_product_smoke.jsonl"),
        "model_path": str(runtime.paddle_vl_model),
        "max_new_tokens": 256,
        "feature_only": True,
        "decision_generated": False,
    }
    return _worker_call(
        python_executable=runtime.paddle_vl_python,
        script=runtime.scripts_root / "vision_ocr_worker.py",
        request=request,
        work_root=output_root / "worker_paddleocr_vl_smoke",
        timeout_seconds=timeout_seconds,
    )


def _hamming(left: str, right: str) -> int | None:
    if not left or not right or len(left) != len(right):
        return None
    return (int(left, 16) ^ int(right, 16)).bit_count()


def build_product_visual_similarity(
    index: dict[str, Any],
    *,
    cpu_feature_path: Path,
    output_path: Path,
    openclip_similarity_path: Path | None = None,
    compatible_output_path: Path | None = None,
) -> dict[str, Any]:
    """Build exhaustive visual evidence rows without producing a match decision."""

    cpu_rows = _load_jsonl(cpu_feature_path)
    cpu_by_id = {row["image_id"]: row for row in cpu_rows}
    groups = index.get("product_index") or {}
    website_groups = groups.get("website") or {}
    alibaba_groups = groups.get("alibaba") or {}
    clip: dict[tuple[str, str], dict[str, Any]] = {}
    if openclip_similarity_path and openclip_similarity_path.is_file():
        for row in _load_jsonl(openclip_similarity_path):
            clip[(str(row.get("website_product_key")), str(row.get("alibaba_product_key")))] = row
    rows: list[dict[str, Any]] = []
    compatible_rows: list[dict[str, Any]] = []
    for website_key, website_ids in sorted(website_groups.items()):
        website_features = [cpu_by_id[item] for item in website_ids if item in cpu_by_id]
        for alibaba_key, alibaba_ids in sorted(alibaba_groups.items(), key=lambda item: (len(item[0]), item[0])):
            alibaba_features = [cpu_by_id[item] for item in alibaba_ids if item in cpu_by_id]
            exact_pairs: list[tuple[str, str]] = []
            phash_best: tuple[int, str, str] | None = None
            dhash_best: tuple[int, str, str] | None = None
            for left in website_features:
                for right in alibaba_features:
                    if left.get("sha256") and left.get("sha256") == right.get("sha256"):
                        exact_pairs.append((left["image_id"], right["image_id"]))
                    pd = _hamming(str(left.get("phash") or ""), str(right.get("phash") or ""))
                    dd = _hamming(str(left.get("dhash") or ""), str(right.get("dhash") or ""))
                    if pd is not None and (phash_best is None or pd < phash_best[0]):
                        phash_best = (pd, left["image_id"], right["image_id"])
                    if dd is not None and (dhash_best is None or dd < dhash_best[0]):
                        dhash_best = (dd, left["image_id"], right["image_id"])
            clip_row = clip.get((website_key, alibaba_key), {})
            phash_similarity = max(0.0, 1.0 - phash_best[0] / 64.0) if phash_best else 0.0
            dhash_similarity = max(0.0, 1.0 - dhash_best[0] / 64.0) if dhash_best else 0.0
            clip_value = clip_row.get("max_cosine")
            clip_similarity = max(0.0, min(1.0, float(clip_value))) if clip_value is not None else None
            if exact_pairs:
                visual_score = 1.0
                score_definition = "EXACT_SHA256"
            elif clip_similarity is not None:
                visual_score = 0.70 * clip_similarity + 0.20 * phash_similarity + 0.10 * dhash_similarity
                score_definition = "0.70*openclip_cosine_clamped+0.20*phash_similarity+0.10*dhash_similarity"
            else:
                visual_score = 0.65 * phash_similarity + 0.35 * dhash_similarity
                score_definition = "HASH_ONLY_DEGRADED:0.65*phash_similarity+0.35*dhash_similarity"
            visual_score = round(max(0.0, min(1.0, visual_score)), 8)
            evidence_row = {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_type": "product_visual_similarity_feature",
                    "feature_only": True,
                    "decision_generated": False,
                    "website_product_key": website_key,
                    "alibaba_product_key": alibaba_key,
                    "official_model": website_key,
                    "alibaba_product_id": alibaba_key,
                    "visual_score": visual_score,
                    "score": visual_score,
                    "visual_method": "SELLERKIT_VISUAL_RETRIEVAL_V1",
                    "score_semantics": "RETRIEVAL_FEATURE_NOT_PROBABILITY_NOT_MATCH_DECISION",
                    "score_definition": score_definition,
                    "score_components": {
                        "exact_sha256": 1.0 if exact_pairs else 0.0,
                        "openclip_cosine_clamped": clip_similarity,
                        "phash_similarity": round(phash_similarity, 8),
                        "dhash_similarity": round(dhash_similarity, 8),
                    },
                    "website_image_count": len(website_features),
                    "alibaba_image_count": len(alibaba_features),
                    "exact_sha256": bool(exact_pairs),
                    "exact_sha256_pair_count": len(exact_pairs),
                    "exact_sha256_pair_example": list(exact_pairs[0]) if exact_pairs else None,
                    "phash_min_hamming": phash_best[0] if phash_best else None,
                    "phash_best_pair": list(phash_best[1:]) if phash_best else None,
                    "dhash_min_hamming": dhash_best[0] if dhash_best else None,
                    "dhash_best_pair": list(dhash_best[1:]) if dhash_best else None,
                    "openclip_status": clip_row.get("status", "OMITTED"),
                    "openclip_max_cosine": clip_row.get("max_cosine"),
                    "openclip_mean_top3_cosine": clip_row.get("mean_top3_cosine"),
                    "openclip_best_pair": clip_row.get("best_pair"),
                }
            rows.append(evidence_row)
            compatible_rows.append(
                {
                    "official_model": website_key,
                    "alibaba_product_id": alibaba_key,
                    "visual_score": visual_score,
                    "visual_method": "SELLERKIT_VISUAL_RETRIEVAL_V1",
                    "score_semantics": "RETRIEVAL_FEATURE_NOT_PROBABILITY_NOT_MATCH_DECISION",
                    "score_definition": score_definition,
                    "feature_only": True,
                    "decision_generated": False,
                }
            )
    _write_jsonl_atomic(output_path, rows)
    compatible_output_path = compatible_output_path or output_path.with_name("full_match_visual_scores.jsonl")
    _write_jsonl_atomic(compatible_output_path, compatible_rows)
    return {
        "status": "PASS_FEATURES_ONLY_NO_MATCH_DECISION",
        "pair_rows": len(rows),
        "expected_pair_rows": len(website_groups) * len(alibaba_groups),
        "openclip_included": bool(clip),
        "output_path": str(output_path),
        "output_sha256": _sha256_file(output_path),
        "compatible_output_path": str(compatible_output_path),
        "compatible_output_sha256": _sha256_file(compatible_output_path),
        "compatible_rows": len(compatible_rows),
        "decision_generated": False,
    }


def _ocr_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 2
    }


def build_product_ocr_similarity(
    index: dict[str, Any],
    *,
    ocr_feature_path: Path,
    historical_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Build complete OCR retrieval scalars for the configured eligible pairs.

    OCR is evidence only.  Known catalog model identifiers are compared with
    text read from product imagery; the score is not a probability and cannot
    create a match decision.
    """

    ocr_rows = _load_jsonl(ocr_feature_path)
    failures = [row for row in ocr_rows if row.get("status") != "PASS"]
    if failures:
        raise ValueError(f"OCR feature input contains {len(failures)} failed images")
    ocr_by_id = {str(row.get("image_id") or ""): row for row in ocr_rows}
    historical = _load_jsonl(historical_path)
    historical_models = {
        str(row.get("product_id") or ""): _normalize_model(
            row.get("canonical_model") or row.get("raw_model")
        )
        for row in historical
        if row.get("record_disposition") == ALLOWED_DISPOSITION
    }
    groups = index.get("product_index") or {}
    website_groups = groups.get("website") or {}
    alibaba_groups = groups.get("alibaba") or {}
    if set(alibaba_groups) != set(historical_models):
        raise ValueError("OCR product index is not exactly the configured eligible Alibaba records")

    def aggregate(image_ids: Sequence[str]) -> str:
        return "\n".join(
            str(ocr_by_id.get(str(image_id), {}).get("text") or "").strip()
            for image_id in image_ids
            if str(ocr_by_id.get(str(image_id), {}).get("text") or "").strip()
        )

    website_text = {key: aggregate(ids) for key, ids in website_groups.items()}
    alibaba_text = {key: aggregate(ids) for key, ids in alibaba_groups.items()}
    rows: list[dict[str, Any]] = []
    observed_pairs = 0
    model_hit_pairs = 0
    for official_model, _website_ids in sorted(website_groups.items()):
        left_text = website_text[official_model]
        left_compact = _normalize_model(left_text)
        left_tokens = _ocr_tokens(left_text)
        for product_id, _alibaba_ids in sorted(alibaba_groups.items(), key=lambda item: (len(item[0]), item[0])):
            right_text = alibaba_text[product_id]
            right_compact = _normalize_model(right_text)
            right_tokens = _ocr_tokens(right_text)
            alibaba_model = historical_models[product_id]
            official_model_hit = bool(official_model and official_model in right_compact)
            alibaba_model_hit = bool(alibaba_model and alibaba_model in left_compact)
            model_hit = official_model_hit or alibaba_model_hit
            union = left_tokens | right_tokens
            token_jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
            observed = bool(left_text or right_text)
            if model_hit:
                score = 1.0
            elif observed:
                score = min(0.75, 0.75 * token_jaccard)
            else:
                score = 0.0
            observed_pairs += int(observed)
            model_hit_pairs += int(model_hit)
            rows.append(
                {
                    "official_model": official_model,
                    "alibaba_product_id": product_id,
                    "ocr_score": round(score, 8),
                    "observed": observed,
                    "ocr_method": "OCR_MODEL_CONTAINMENT_AND_TOKEN_JACCARD_V1",
                    "score_semantics": "RETRIEVAL_FEATURE_NOT_PROBABILITY_NOT_MATCH_DECISION",
                    "official_model_found_in_alibaba_ocr": official_model_hit,
                    "alibaba_model_found_in_website_ocr": alibaba_model_hit,
                    "token_jaccard": round(token_jaccard, 8),
                    "website_ocr_text_sha256": hashlib.sha256(left_text.encode("utf-8")).hexdigest(),
                    "alibaba_ocr_text_sha256": hashlib.sha256(right_text.encode("utf-8")).hexdigest(),
                    "feature_only": True,
                    "decision_generated": False,
                }
            )
    _write_jsonl_atomic(output_path, rows)
    return {
        "status": "PASS_OCR_RETRIEVAL_FEATURES_ONLY",
        "rows": len(rows),
        "expected_rows": len(website_groups) * len(alibaba_groups),
        "observed_pairs": observed_pairs,
        "model_hit_pairs": model_hit_pairs,
        "output_path": str(output_path),
        "output_sha256": _sha256_file(output_path),
        "score_semantics": "RETRIEVAL_FEATURE_NOT_PROBABILITY_NOT_MATCH_DECISION",
        "decision_generated": False,
    }


def select_gate0_images(index: dict[str, Any], *, website_count: int = 4, alibaba_count: int = 8) -> list[dict[str, Any]]:
    """Deterministic size/role/product-stratified real-product sample."""

    output: list[dict[str, Any]] = []
    for source, count in (("website", website_count), ("alibaba", alibaba_count)):
        candidates = [dict(row) for row in index.get("images") or [] if row.get("source") == source]
        candidates.sort(key=lambda row: (Path(row["local_path"]).stat().st_size, row["product_key"], row["image_id"]))
        if not candidates:
            continue
        positions = sorted({round(index * (len(candidates) - 1) / max(1, count - 1)) for index in range(count)})
        seen_products: set[str] = set()
        for position in positions:
            row = candidates[position]
            if row["product_key"] in seen_products:
                replacement = next((item for item in candidates if item["product_key"] not in seen_products), row)
                row = replacement
            seen_products.add(row["product_key"])
            output.append(row)
    return output


def run_gate0(
    index: dict[str, Any],
    *,
    output_root: Path,
    runtime: VisionRuntimePaths | None = None,
    include_openclip: bool = True,
    include_ppocr: bool = False,
) -> dict[str, Any]:
    runtime = runtime or VisionRuntimePaths()
    sample = select_gate0_images(index)
    sample_index = {
        **index,
        "images": sample,
        "product_index": {
            source: {
                key: [row["image_id"] for row in sample if row["source"] == source and row["product_key"] == key]
                for key in sorted({row["product_key"] for row in sample if row["source"] == source})
            }
            for source in ("website", "alibaba")
        },
    }
    output_root.mkdir(parents=True, exist_ok=True)
    cpu = extract_cpu_features(sample_index, output_path=output_root / "gate0_cpu_features.jsonl", workers=min(8, len(sample)))
    openclip = run_openclip_features(sample_index, output_root=output_root / "openclip", runtime=runtime, batch_size=32) if include_openclip else {"status": "OMITTED_BY_CALLER"}
    ppocr = run_ocr_features(sample_index, output_root=output_root / "ppocr", engine="ppocrv4", runtime=runtime) if include_ppocr else {"status": "OMITTED_BY_CALLER"}
    required_pass = cpu["status"] == "PASS" and (not include_openclip or openclip.get("status") == "PASS")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "gate0_real_product_sample",
        "created_at_utc": _utc_now(),
        "status": "PASS_GATE0_FEATURES" if required_pass else "DEGRADED_GATE0_FEATURES",
        "real_product_images": True,
        "sample_count": len(sample),
        "sample_image_ids": [row["image_id"] for row in sample],
        "sample_sources": dict(Counter(row["source"] for row in sample)),
        "cpu": cpu,
        "openclip": openclip,
        "ppocr": ppocr,
        "feature_only": True,
        "decision_generated": False,
    }
    receipt_path = output_root / "gate0_receipt.json"
    _write_json_atomic(receipt_path, receipt)
    receipt["receipt_path"] = str(receipt_path)
    receipt["receipt_sha256"] = _sha256_file(receipt_path)
    return receipt


def run_visual_feature_pipeline(
    *,
    index_path: Path,
    output_root: Path,
    runtime: VisionRuntimePaths | None = None,
    cpu_workers: int = 16,
    include_openclip: bool = True,
    include_ppocr: bool = False,
) -> dict[str, Any]:
    """Run CPU/OpenCLIP/OCR features and exhaustive product-pair visual evidence."""

    runtime = runtime or VisionRuntimePaths()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    cpu_path = output_root / "cpu_image_features.jsonl"
    cpu = extract_cpu_features(index, output_path=cpu_path, workers=cpu_workers)
    openclip = run_openclip_features(index, output_root=output_root / "openclip", runtime=runtime) if include_openclip else {"status": "OMITTED_BY_CALLER"}
    ppocr = run_ocr_features(index, output_root=output_root / "ppocr", engine="ppocrv4", runtime=runtime) if include_ppocr else {"status": "OMITTED_BY_CALLER"}
    clip_path_text = str(openclip.get("similarity_output_path") or "")
    clip_path = Path(clip_path_text) if clip_path_text else None
    similarity = build_product_visual_similarity(
        index,
        cpu_feature_path=cpu_path,
        output_path=output_root / "product_visual_similarity.jsonl",
        openclip_similarity_path=clip_path,
        compatible_output_path=output_root / "full_match_visual_scores.jsonl",
    )
    ocr_similarity = (
        build_product_ocr_similarity(
            index,
            ocr_feature_path=Path(str(ppocr["output_path"])),
            historical_path=runtime.project_root / "work" / "alibaba_historical_sanitized.jsonl",
            output_path=output_root / "full_match_ocr_scores.jsonl",
        )
        if include_ppocr and ppocr.get("status") == "PASS"
        else {"status": "OMITTED_OR_DEGRADED"}
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "visual_feature_pipeline_receipt",
        "created_at_utc": _utc_now(),
        "status": "PASS_FEATURES_ONLY_NO_MATCH_DECISION" if cpu["status"] == "PASS" else "DEGRADED_FEATURES_ONLY",
        "feature_only": True,
        "decision_generated": False,
        "index_path": str(index_path),
        "index_sha256": _sha256_file(index_path),
        "cpu": cpu,
        "openclip": openclip,
        "ppocr": ppocr,
        "ocr_similarity": ocr_similarity,
        "product_similarity": similarity,
        "framework_isolation": {
            "orchestrator_imports_torch_openclip_paddle_onnx": False,
            "openclip_worker_process": str(runtime.scripts_root / "vision_openclip_worker.py"),
            "ocr_worker_process": str(runtime.scripts_root / "vision_ocr_worker.py"),
        },
    }
    receipt_path = output_root / "visual_feature_receipt.json"
    _write_json_atomic(receipt_path, receipt)
    receipt["receipt_path"] = str(receipt_path)
    receipt["receipt_sha256"] = _sha256_file(receipt_path)
    return receipt


def _main() -> int:
    parser = argparse.ArgumentParser(description="SellerKit local visual feature orchestration (no match decisions)")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    index_parser = sub.add_parser("index")
    index_parser.add_argument("--website-manifest", type=Path)
    index_parser.add_argument("--alibaba-archive-root", type=Path)
    index_parser.add_argument("--out", type=Path, required=True)
    index_parser.add_argument("--allow-incomplete", action="store_true")
    benchmark_parser = sub.add_parser("benchmark")
    benchmark_parser.add_argument("--index", type=Path, required=True)
    benchmark_parser.add_argument("--out", type=Path, required=True)
    benchmark_parser.add_argument("--limit", type=int, default=128)
    gate_parser = sub.add_parser("gate0")
    gate_parser.add_argument("--index", type=Path, required=True)
    gate_parser.add_argument("--out-root", type=Path, required=True)
    gate_parser.add_argument("--with-ppocr", action="store_true")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--index", type=Path, required=True)
    run_parser.add_argument("--out-root", type=Path, required=True)
    run_parser.add_argument("--cpu-workers", type=int, default=16)
    run_parser.add_argument("--without-openclip", action="store_true")
    run_parser.add_argument("--with-ppocr", action="store_true")
    args = parser.parse_args()
    runtime = VisionRuntimePaths(project_root=args.project_root.resolve())
    if args.command == "index":
        result = build_visual_input_index(
            output_path=args.out,
            runtime=runtime,
            website_manifest=args.website_manifest,
            alibaba_archive_root=args.alibaba_archive_root,
            strict_counts=not args.allow_incomplete,
        )
    else:
        index = json.loads(args.index.read_text(encoding="utf-8"))
        if args.command == "benchmark":
            result = benchmark_cpu_features(index, output_path=args.out, limit=args.limit)
        elif args.command == "gate0":
            result = run_gate0(index, output_root=args.out_root, runtime=runtime, include_ppocr=args.with_ppocr)
        else:
            result = run_visual_feature_pipeline(
                index_path=args.index,
                output_root=args.out_root,
                runtime=runtime,
                cpu_workers=args.cpu_workers,
                include_openclip=not args.without_openclip,
                include_ppocr=args.with_ppocr,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
