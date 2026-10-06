from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from . import ENGINE_VERSION
from .alibaba import (
    ALLOWED_DISPOSITION,
    REJECTED_DISPOSITION,
    alibaba_stats,
    historical_search_text,
    parse_alibaba_products,
)
from .config import EXPECTED, Paths, get_settings, validate_path_contract
from .utils import (
    jaccard,
    load_json,
    normalize_model,
    secret_findings,
    sha256_file,
    stable_json_text,
    tree_manifest_sha256,
    tokens,
    write_json_atomic,
    write_text_atomic,
)
from .website import official_stats, parse_official_products


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_project_layout(paths: Paths) -> None:
    validate_path_contract(paths)
    for directory in (
        paths.project_root / "artifacts" / "original",
        paths.work_root,
        paths.evidence_root,
        paths.output_root / "review_packages",
        paths.project_root / "schemas",
        paths.project_root / "tests",
        paths.project_root / "scripts",
        paths.website_image_blob_root,
        paths.vision_work_root,
        paths.full_match_work_root,
        paths.full_match_output_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    aliases = paths.work_root / "aliases.jsonl"
    if not aliases.exists():
        write_text_atomic(aliases, "")
    if not paths.review_decisions_path.exists():
        write_text_atomic(paths.review_decisions_path, "")


def inspect_sources(paths: Paths) -> dict[str, Any]:
    validate_path_contract(paths)
    required_files = {
        "website_archive": paths.website_archive,
        "old_website_archive": paths.old_website_archive,
        "website_release_receipt": paths.website_release_receipt,
        "website_pages": paths.website_pages_report,
        "website_assets": paths.website_assets_report,
        "alibaba_catalog": paths.alibaba_catalog,
        "disposition_overlay": paths.disposition_records,
        "disposition_index": paths.disposition_index,
    }
    missing = [name for name, path in required_files.items() if not path.is_file()]
    if missing:
        return {
            "status": "BLOCKED_SOURCE_MISSING",
            "missing": missing,
            "external_write_enabled": False,
        }
    website_dist_tree = tree_manifest_sha256(paths.website_replica_root / "dist")
    snapshot_files = list(paths.alibaba_snapshots_root.rglob("product_snapshot.json"))
    alibaba_snapshot_tree = tree_manifest_sha256(paths.alibaba_snapshots_root, snapshot_files)
    observed = {
        "website_archive_sha256": sha256_file(paths.website_archive),
        "website_archive_size_bytes": paths.website_archive.stat().st_size,
        "old_website_archive_sha256": sha256_file(paths.old_website_archive),
        "old_website_archive_size_bytes": paths.old_website_archive.stat().st_size,
        "website_release_receipt_sha256": sha256_file(paths.website_release_receipt),
        "website_pages_sha256": sha256_file(paths.website_pages_report),
        "website_assets_sha256": sha256_file(paths.website_assets_report),
        "website_assets_size_bytes": paths.website_assets_report.stat().st_size,
        "alibaba_catalog_sha256": sha256_file(paths.alibaba_catalog),
        "disposition_overlay_sha256": sha256_file(paths.disposition_records),
        "website_dist_tree_sha256": website_dist_tree["sha256"],
        "website_dist_file_count": website_dist_tree["file_count"],
        "website_dist_total_bytes": website_dist_tree["total_bytes"],
        "alibaba_snapshot_tree_sha256": alibaba_snapshot_tree["sha256"],
        "alibaba_snapshot_file_count": alibaba_snapshot_tree["file_count"],
        "alibaba_snapshot_total_bytes": alibaba_snapshot_tree["total_bytes"],
    }
    index = load_json(paths.disposition_index)
    release = load_json(paths.website_release_receipt)
    errors: list[str] = []
    for key in (
        "website_archive_sha256",
        "website_archive_size_bytes",
        "old_website_archive_sha256",
        "old_website_archive_size_bytes",
        "website_release_receipt_sha256",
        "website_pages_sha256",
        "website_assets_sha256",
        "website_assets_size_bytes",
        "alibaba_catalog_sha256",
        "disposition_overlay_sha256",
        "website_dist_tree_sha256",
        "website_dist_file_count",
        "website_dist_total_bytes",
        "alibaba_snapshot_tree_sha256",
        "alibaba_snapshot_file_count",
        "alibaba_snapshot_total_bytes",
    ):
        if observed[key] != EXPECTED[key]:
            errors.append(f"{key}: expected {EXPECTED[key]}, observed {observed[key]}")
    release_package = ((release.get("packages") or {}).get("release_r3") or {})
    release_source = release.get("source") or {}
    if release.get("release_id") != get_settings()["website_release_id"]:
        errors.append("website release receipt has an unexpected release_id")
    if release_package.get("sha256") != observed["website_archive_sha256"]:
        errors.append("website release receipt archive hash binding does not match")
    if release_package.get("bytes") != observed["website_archive_size_bytes"]:
        errors.append("website release receipt archive size binding does not match")
    if release_source.get("tree_sha256") != EXPECTED["website_dist_tree_sha256"]:
        errors.append("website release receipt dist tree hash does not match")
    if release_source.get("tree_sha256") != observed["website_dist_tree_sha256"]:
        errors.append("live website dist tree does not match the frozen release receipt")
    if release_source.get("file_count") != observed["website_dist_file_count"]:
        errors.append("live website dist file count does not match the frozen release receipt")
    if ((release.get("deployment") or {}).get("external_write_performed")) is not False:
        errors.append("website release receipt does not prove zero external write")
    if index.get("base_catalog_index_sha256") != observed["alibaba_catalog_sha256"]:
        errors.append("overlay base catalog binding does not match current catalog")
    if index.get("coverage_complete") is not True:
        errors.append("disposition overlay coverage is not complete")
    return {
        "status": "PASS_FROZEN_SOURCES_VERIFIED" if not errors else "BLOCKED_SOURCE_DRIFT",
        "observed": observed,
        "expected": {
            key: EXPECTED[key]
            for key in (
                "website_archive_sha256",
                "website_archive_size_bytes",
                "old_website_archive_sha256",
                "old_website_archive_size_bytes",
                "website_release_receipt_sha256",
                "website_pages_sha256",
                "website_assets_sha256",
                "website_assets_size_bytes",
                "alibaba_catalog_sha256",
                "disposition_overlay_sha256",
                "website_dist_tree_sha256",
                "website_dist_file_count",
                "website_dist_total_bytes",
                "alibaba_snapshot_tree_sha256",
                "alibaba_snapshot_file_count",
                "alibaba_snapshot_total_bytes",
            )
        },
        "website_release": {
            "release_id": release.get("release_id"),
            "created_at_utc": release.get("created_at_utc"),
            "dist_tree_sha256": release_source.get("tree_sha256"),
            "result": release.get("result"),
            "external_write_performed": (release.get("deployment") or {}).get("external_write_performed"),
        },
        "overlay": {
            "version": index.get("overlay_version"),
            "generated_at": index.get("generated_at"),
            "cutoff": index.get("cutoff"),
            "date_field": index.get("source_field"),
            "date_semantics": index.get("source_field_semantics"),
        },
        "errors": errors,
        "external_write_enabled": False,
    }


def _similarity(official: dict[str, Any], historical: dict[str, Any]) -> dict[str, float]:
    official_title = tokens(f"{official['name']} {official['category']} {official['subcategory']}")
    historical_title = tokens(f"{historical['subject']} {' '.join(historical.get('group_names') or [])}")
    official_specs = tokens(
        " ".join(
            f"{row['name']} {row['raw_value']}" for row in official["attributes"] if not row["is_empty"]
        )
    )
    historical_all = tokens(historical_search_text(historical))
    category = jaccard(tokens(f"{official['category']} {official['subcategory']}"), historical_all)
    title = jaccard(official_title, historical_title)
    specs = jaccard(official_specs, historical_all)
    overall = jaccard(official_title | official_specs, historical_all)
    score = round(0.40 * title + 0.15 * category + 0.30 * specs + 0.15 * overall, 6)
    return {
        "score": score,
        "title_jaccard": round(title, 6),
        "category_jaccard": round(category, 6),
        "spec_jaccard": round(specs, 6),
        "overall_jaccard": round(overall, 6),
    }


def _load_aliases(paths: Paths) -> list[dict[str, Any]]:
    aliases: list[dict[str, Any]] = []
    alias_path = paths.work_root / "aliases.jsonl"
    if not alias_path.exists():
        return aliases
    for line_number, line in enumerate(alias_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Invalid alias row {line_number}")
        value["left_model_normalized"] = normalize_model(value.get("left_model"))
        value["right_model_normalized"] = normalize_model(value.get("right_model"))
        findings = secret_findings(value)
        if findings:
            raise ValueError(f"Secret scan failed for alias row {line_number}: {findings[:5]}")
        aliases.append(value)
    if aliases:
        raise ValueError(
            "V1 has no authoritative alias registry; aliases.jsonl must remain empty"
        )
    return []


def _match_decisions(
    official_products: list[dict[str, Any]],
    alibaba_products: list[dict[str, Any]],
    aliases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_model: dict[str, list[dict[str, Any]]] = {}
    eligible = [item for item in alibaba_products if item["record_disposition"] == ALLOWED_DISPOSITION]
    for item in alibaba_products:
        if item["canonical_model"]:
            by_model.setdefault(item["canonical_model"], []).append(item)
    alias_map: dict[str, set[str]] = {}
    for row in aliases:
        if row.get("review_status") != "APPROVED":
            continue
        left = row.get("left_model_normalized")
        right = row.get("right_model_normalized")
        if left and right:
            alias_map.setdefault(left, set()).add(right)
            alias_map.setdefault(right, set()).add(left)

    decisions: list[dict[str, Any]] = []
    for official in official_products:
        model = official["canonical_model"]
        exact = by_model.get(model, [])
        alias_hits = [
            item for alias in alias_map.get(model, set()) for item in by_model.get(alias, [])
        ]
        model_relation_hits = [
            item
            for item in eligible
            if item["canonical_model"]
            and item["canonical_model"] != model
            and min(len(model), len(item["canonical_model"])) >= 4
            and (model.startswith(item["canonical_model"]) or item["canonical_model"].startswith(model))
        ]

        ranked: list[dict[str, Any]] = []
        if not any(item["record_disposition"] == ALLOWED_DISPOSITION for item in exact):
            for item in eligible:
                components = _similarity(official, item)
                ranked.append(
                    {
                        "product_id": item["product_id"],
                        "model": item["raw_model"],
                        "subject": item["subject"],
                        "record_disposition": item["record_disposition"],
                        **components,
                    }
                )
            ranked.sort(key=lambda row: (-row["score"], row["product_id"]))
        conflict_threshold = 0.20
        if exact:
            if any(item["record_disposition"] == ALLOWED_DISPOSITION for item in exact):
                status = "CONFIRMED_PRESENT"
            else:
                status = "CONFIRMED_PRESENT_UNUSABLE_REFERENCE"
        elif alias_hits:
            status = "AMBIGUOUS_ALIAS"
        elif model_relation_hits:
            status = "VARIANT_OR_ACCESSORY"
        elif ranked and ranked[0]["score"] >= conflict_threshold:
            status = "BLOCKED_INSUFFICIENT_EVIDENCE"
        else:
            status = "CANDIDATE_MISSING"
        decisions.append(
            {
                "canonical_model": model,
                "raw_model": official["raw_model"],
                "status": status,
                "exact_product_ids": [item["product_id"] for item in exact],
                "alias_product_ids": [item["product_id"] for item in alias_hits],
                "model_relation_candidates": [
                    {
                        "product_id": item["product_id"],
                        "model": item["raw_model"],
                        "relation": "NORMALIZED_MODEL_PREFIX_RELATION_REVIEW_REQUIRED",
                    }
                    for item in model_relation_hits
                ],
                "similar_products": ranked[:5],
                "missing_is_confirmed": False,
                "exact_difference_candidate": not bool(exact),
                "conflict_threshold": conflict_threshold,
                "conflict_threshold_status": "UNVALIDATED_HEURISTIC_REVIEW_ONLY",
                "classification_effect": "QUEUE_FOR_HUMAN_REVIEW_ONLY_NEVER_CONFIRM_MISSING_OR_RELEASE",
                "status_reason": {
                    "CONFIRMED_PRESENT": "Exact normalized model exists in an eligible historical record.",
                    "CONFIRMED_PRESENT_UNUSABLE_REFERENCE": "Exact normalized model exists only in an unusable waste record; content is excluded.",
                    "AMBIGUOUS_ALIAS": "An approved alias relation exists and requires human resolution.",
                    "VARIANT_OR_ACCESSORY": "A normalized model prefix relation suggests a variant or accessory; human resolution required.",
                    "BLOCKED_INSUFFICIENT_EVIDENCE": "No exact match, but historical similarity exceeds the conflict threshold; absence cannot be inferred.",
                    "CANDIDATE_MISSING": "No exact or approved-alias match; this remains an unconfirmed review candidate.",
                }[status],
                "manual_review_required": status != "CONFIRMED_PRESENT",
            }
        )
    return decisions


def _write_derivatives(
    paths: Paths,
    official_products: list[dict[str, Any]],
    alibaba_products: list[dict[str, Any]],
    source_report: dict[str, Any],
) -> dict[str, str]:
    official_path = paths.work_root / "official_current.jsonl"
    historical_path = paths.work_root / "alibaba_historical_sanitized.jsonl"
    presence_path = paths.work_root / "alibaba_presence_guard.jsonl"
    official_text = "".join(stable_json_text(item) for item in official_products)
    historical_records = [
        item for item in alibaba_products if item["record_disposition"] == ALLOWED_DISPOSITION
    ]
    historical_text = "".join(stable_json_text(item) for item in historical_records)
    presence_text = "".join(
        stable_json_text(
            {
                "product_id": item["product_id"],
                "canonical_model": item["canonical_model"],
                "raw_model": item["raw_model"],
                "record_disposition": item["record_disposition"],
                "presence_dedup_only": item["record_disposition"] == REJECTED_DISPOSITION,
            }
        )
        for item in alibaba_products
    )
    for value, label in ((historical_records, "historical"), (official_products, "official")):
        findings = secret_findings(value)
        if findings:
            raise ValueError(f"Secret scan failed for {label} derivatives: {findings[:5]}")
    write_text_atomic(official_path, official_text)
    write_text_atomic(historical_path, historical_text)
    write_text_atomic(presence_path, presence_text)
    source_manifest = {
        "schema_version": "1.0.0",
        "engine_version": ENGINE_VERSION,
        "source_report": source_report,
        "artifacts": {
            "website_archive": {
                "path": str(paths.website_archive),
                "sha256": source_report["observed"]["website_archive_sha256"],
                "size_bytes": source_report["observed"]["website_archive_size_bytes"],
            },
            "old_website_archive": {
                "path": str(paths.old_website_archive),
                "sha256": source_report["observed"]["old_website_archive_sha256"],
                "size_bytes": source_report["observed"]["old_website_archive_size_bytes"],
                "role": "preserved_historical_only",
            },
            "website_release_receipt": {
                "path": str(paths.website_release_receipt),
                "sha256": source_report["observed"]["website_release_receipt_sha256"],
            },
            "website_pages": {
                "path": str(paths.website_pages_report),
                "sha256": source_report["observed"]["website_pages_sha256"],
            },
            "website_assets": {
                "path": str(paths.website_assets_report),
                "sha256": source_report["observed"]["website_assets_sha256"],
                "size_bytes": source_report["observed"]["website_assets_size_bytes"],
            },
            "website_live_dist_tree": {
                "path": str(paths.website_replica_root / "dist"),
                "sha256": source_report["observed"]["website_dist_tree_sha256"],
                "file_count": source_report["observed"]["website_dist_file_count"],
                "total_bytes": source_report["observed"]["website_dist_total_bytes"],
            },
            "alibaba_catalog": {
                "path": str(paths.alibaba_catalog),
                "sha256": source_report["observed"]["alibaba_catalog_sha256"],
            },
            "disposition_overlay": {
                "path": str(paths.disposition_records),
                "sha256": source_report["observed"]["disposition_overlay_sha256"],
            },
            "alibaba_product_snapshot_tree": {
                "path": str(paths.alibaba_snapshots_root),
                "sha256": source_report["observed"]["alibaba_snapshot_tree_sha256"],
                "file_count": source_report["observed"]["alibaba_snapshot_file_count"],
                "total_bytes": source_report["observed"]["alibaba_snapshot_total_bytes"],
            },
        },
        "derivatives": {
            "official_current": {"path": str(official_path), "sha256": sha256_file(official_path)},
            "alibaba_historical_sanitized": {
                "path": str(historical_path),
                "sha256": sha256_file(historical_path),
            },
            "alibaba_presence_guard": {"path": str(presence_path), "sha256": sha256_file(presence_path)},
        },
        "external_write_enabled": False,
    }
    source_pointer_path = paths.project_root / "artifacts" / "original" / "source_pointers.json"
    write_json_atomic(source_pointer_path, source_manifest)
    return {
        "official_current": str(official_path),
        "alibaba_historical_sanitized": str(historical_path),
        "alibaba_presence_guard": str(presence_path),
        "source_pointers": str(source_pointer_path),
    }


def _create_database(
    database_path: Path,
    official_products: list[dict[str, Any]],
    alibaba_products: list[dict[str, Any]],
    aliases: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    source_report: dict[str, Any],
) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".review-ready-", suffix=".sqlite3.partial", dir=database_path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE official_products (
                canonical_model TEXT PRIMARY KEY,
                raw_model TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                product_json TEXT NOT NULL
            );
            CREATE TABLE official_specs (
                canonical_model TEXT NOT NULL REFERENCES official_products(canonical_model),
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                raw_value TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                PRIMARY KEY(canonical_model, position)
            );
            CREATE TABLE official_assets (
                canonical_model TEXT NOT NULL REFERENCES official_products(canonical_model),
                asset_order INTEGER NOT NULL,
                role TEXT NOT NULL,
                source_url TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                asset_json TEXT NOT NULL,
                PRIMARY KEY(canonical_model, asset_order, source_url)
            );
            CREATE TABLE alibaba_products (
                product_id TEXT PRIMARY KEY,
                ordinal INTEGER NOT NULL UNIQUE,
                canonical_model TEXT NOT NULL,
                raw_model TEXT NOT NULL,
                subject TEXT NOT NULL,
                record_disposition TEXT NOT NULL,
                approved_fact INTEGER NOT NULL CHECK(approved_fact = 0),
                product_json TEXT NOT NULL
            );
            CREATE INDEX idx_alibaba_model ON alibaba_products(canonical_model);
            CREATE INDEX idx_alibaba_disposition ON alibaba_products(record_disposition);
            CREATE TABLE aliases (
                left_model TEXT NOT NULL,
                right_model TEXT NOT NULL,
                relation TEXT NOT NULL,
                review_status TEXT NOT NULL,
                alias_json TEXT NOT NULL,
                PRIMARY KEY(left_model, right_model, relation)
            );
            CREATE TABLE match_decisions (
                canonical_model TEXT PRIMARY KEY REFERENCES official_products(canonical_model),
                status TEXT NOT NULL,
                decision_json TEXT NOT NULL
            );
            """
        )
        metadata = {
            "engine_version": ENGINE_VERSION,
            "source_report": source_report,
            "external_write_enabled": False,
        }
        connection.executemany(
            "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
            ((key, _compact(value)) for key, value in sorted(metadata.items())),
        )
        for product in official_products:
            connection.execute(
                "INSERT INTO official_products VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    product["canonical_model"],
                    product["raw_model"],
                    product["name"],
                    product["category"],
                    product["subcategory"],
                    product["source"]["source_sha256"],
                    _compact(product),
                ),
            )
            connection.executemany(
                "INSERT INTO official_specs VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        product["canonical_model"],
                        row["position"],
                        row["name"],
                        row["raw_value"],
                        _compact(row),
                    )
                    for row in product["attributes"]
                ),
            )
            connection.executemany(
                "INSERT INTO official_assets VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        product["canonical_model"],
                        row["order"],
                        row["role"],
                        row["source_url"],
                        row["sha256"],
                        _compact(row),
                    )
                    for row in product["assets"]
                ),
            )
        connection.executemany(
            "INSERT INTO alibaba_products VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (
                (
                    row["product_id"],
                    row["ordinal"],
                    row["canonical_model"],
                    row["raw_model"],
                    row["subject"],
                    row["record_disposition"],
                    _compact(row),
                )
                for row in alibaba_products
            ),
        )
        connection.executemany(
            "INSERT INTO aliases VALUES (?, ?, ?, ?, ?)",
            (
                (
                    row.get("left_model_normalized", ""),
                    row.get("right_model_normalized", ""),
                    str(row.get("relation") or "alias"),
                    str(row.get("review_status") or "PENDING"),
                    _compact(row),
                )
                for row in aliases
            ),
        )
        connection.executemany(
            "INSERT INTO match_decisions VALUES (?, ?, ?)",
            ((row["canonical_model"], row["status"], _compact(row)) for row in decisions),
        )
        connection.commit()
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {result}")
        connection.close()
        os.replace(temporary, database_path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def build_database(paths: Paths) -> dict[str, Any]:
    ensure_project_layout(paths)
    source_report = inspect_sources(paths)
    if source_report["status"] != "PASS_FROZEN_SOURCES_VERIFIED":
        raise ValueError(f"Frozen source verification failed: {source_report['errors']}")
    official_products = parse_official_products(paths)
    alibaba_products = parse_alibaba_products(paths)
    official_summary = official_stats(official_products)
    alibaba_summary = alibaba_stats(alibaba_products)
    aliases = _load_aliases(paths)
    decisions = _match_decisions(official_products, alibaba_products, aliases)

    actual = {
        "website_products": official_summary["product_count"],
        "website_specs": official_summary["attribute_count"],
        "website_non_empty_specs": official_summary["non_empty_attribute_count"],
        "website_empty_specs": official_summary["empty_attribute_count"],
        "website_main_image_references": official_summary["main_image_reference_count"],
        "website_main_image_materialized": official_summary["main_image_materialized_count"],
        "website_gallery_references": official_summary["gallery_reference_count"],
        "website_gallery_materialized": official_summary["gallery_materialized_count"],
        "website_core_images_remote_unmaterialized": official_summary["core_product_image_remote_unmaterialized_count"],
        "alibaba_products": alibaba_summary["product_count"],
        "alibaba_non_empty_models": alibaba_summary["non_empty_model_count"],
        "historical_reference": alibaba_summary["historical_reference_count"],
        "unusable_waste": alibaba_summary["unusable_waste_count"],
        "historical_structured_ladder_price_coverage": alibaba_summary["historical_structured_ladder_price_coverage"],
        "historical_catalog_sku_price_coverage": alibaba_summary["historical_catalog_sku_price_coverage"],
        "historical_public_price_range_coverage": alibaba_summary["historical_public_price_range_coverage"],
        "historical_sku_attribute_coverage": alibaba_summary["historical_sku_attribute_coverage"],
        "historical_sku_sample_coverage": alibaba_summary["historical_sku_sample_coverage"],
        "historical_lead_time_coverage": alibaba_summary["historical_lead_time_coverage"],
        "historical_packaging_coverage": alibaba_summary["historical_packaging_coverage"],
        "exact_intersection": sum(bool(row["exact_product_ids"]) for row in decisions),
        "exact_missing_candidates": sum(not row["exact_product_ids"] for row in decisions),
    }
    errors = [f"{key}: expected {EXPECTED[key]}, observed {value}" for key, value in actual.items() if value != EXPECTED[key]]
    if errors:
        raise ValueError(f"Frozen data gate failed: {errors}")

    derivatives = _write_derivatives(paths, official_products, alibaba_products, source_report)
    _create_database(paths.database_path, official_products, alibaba_products, aliases, decisions, source_report)
    result = {
        "schema_version": "sellerkit.database_build_receipt.v2",
        "status": "PASS_V1_DATABASE_BUILT_NO_REMOTE_WRITE",
        "engine_version": ENGINE_VERSION,
        "database_path": str(paths.database_path),
        "database_sha256": sha256_file(paths.database_path),
        "official": official_summary,
        "alibaba": alibaba_summary,
        "reconciliation": actual,
        "source_report": source_report,
        "derivatives": derivatives,
        "external_write_enabled": False,
        "browser_actions": 0,
        "remote_drafts": 0,
        "publish_requests": 0,
        "local_review_status": "READY_FOR_LOCAL_HUMAN_REVIEW",
        "remote_write_status": "DISABLED_NOT_REQUESTED",
        "release_status": "NOT_RELEASED_V1_LOCAL_ONLY",
    }
    write_json_atomic(paths.evidence_root / "database_build_receipt.json", result)
    return result


def open_database(paths: Paths) -> sqlite3.Connection:
    if not paths.database_path.is_file():
        raise FileNotFoundError(f"Database not built: {paths.database_path}")
    connection = sqlite3.connect(paths.database_path)
    connection.row_factory = sqlite3.Row
    return connection


def validate_database_state(paths: Paths) -> dict[str, Any]:
    validate_path_contract(paths)
    source_report = inspect_sources(paths)
    if source_report.get("status") != "PASS_FROZEN_SOURCES_VERIFIED":
        raise ValueError(f"Current frozen-source preflight failed: {source_report.get('errors')}")
    receipt_path = paths.evidence_root / "database_build_receipt.json"
    if not paths.database_path.is_file() or not receipt_path.is_file():
        raise FileNotFoundError("Database and database_build_receipt.json must both exist")
    receipt = load_json(receipt_path)
    if secret_findings(receipt):
        raise ValueError("Secret scan failed for database receipt")
    database_sha256 = sha256_file(paths.database_path)
    if receipt.get("status") != "PASS_V1_DATABASE_BUILT_NO_REMOTE_WRITE":
        raise ValueError("Database receipt status is not a successful local-only build")
    if receipt.get("engine_version") != ENGINE_VERSION:
        raise ValueError("Database receipt engine version does not match current engine")
    if receipt.get("database_sha256") != database_sha256:
        raise ValueError("Database hash does not match database_build_receipt.json")
    if receipt.get("source_report") != source_report:
        raise ValueError("Database receipt is not bound to the current frozen-source preflight")

    connection = sqlite3.connect(paths.database_path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError(f"SQLite integrity check failed during preflight: {integrity}")
        metadata_row = connection.execute(
            "SELECT value_json FROM metadata WHERE key = 'source_report'"
        ).fetchone()
        if metadata_row is None or json.loads(metadata_row[0]) != source_report:
            raise ValueError("SQLite metadata is not bound to the current frozen-source preflight")
    finally:
        connection.close()
    return {
        "status": "PASS_DATABASE_AND_SOURCE_CHAIN_VERIFIED",
        "database_sha256": database_sha256,
        "database_receipt_sha256": sha256_file(receipt_path),
        "source_report": source_report,
        "engine_version": ENGINE_VERSION,
    }


def load_product_bundle(
    paths: Paths,
    model: str,
    *,
    preflight: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if preflight:
        validate_database_state(paths)
    canonical = normalize_model(model)
    connection = open_database(paths)
    try:
        official_row = connection.execute(
            "SELECT product_json FROM official_products WHERE canonical_model = ?", (canonical,)
        ).fetchone()
        if official_row is None:
            raise KeyError(f"Unknown official model: {model}")
        decision_row = connection.execute(
            "SELECT decision_json FROM match_decisions WHERE canonical_model = ?", (canonical,)
        ).fetchone()
        official = json.loads(official_row[0])
        decision = json.loads(decision_row[0]) if decision_row else {}
        historical: list[dict[str, Any]] = []
        for candidate in decision.get("similar_products") or []:
            row = connection.execute(
                "SELECT product_json FROM alibaba_products WHERE product_id = ?", (candidate["product_id"],)
            ).fetchone()
            if row:
                value = json.loads(row[0])
                value["similarity"] = candidate
                historical.append(value)
        return official, decision, historical
    finally:
        connection.close()
