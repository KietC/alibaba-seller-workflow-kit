from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ENGINE_VERSION
from .config import Paths
from .review import FIELD_REVIEW_DECISIONS, REVIEW_CONTRACT_VERSION, review_state, validate_review_identity
from .store import load_product_bundle, open_database, validate_database_state
from .utils import (
    ensure_within,
    exclusive_file_lock,
    normalize_model,
    secret_findings,
    secret_text_findings,
    sha256_file,
    stable_json_text,
    write_json_atomic,
    write_text_atomic,
)


PACKAGE_STATUS = "PASS_LOCAL_REVIEW_PACKAGE_READY_NO_REMOTE_WRITE"
LOCAL_REVIEW_STATUS = "READY_FOR_LOCAL_HUMAN_REVIEW"
REMOTE_WRITE_STATUS = "DISABLED_NOT_REQUESTED"
RELEASE_STATUS = "NOT_RELEASED_V1_LOCAL_ONLY"
PACKAGE_CONTENT_NAMES = {
    "candidate_product.json",
    "field_provenance.jsonl",
    "match_decision.json",
    "similar_products.json",
    "commercial_suggestions.csv",
    "asset_manifest.csv",
    "fill_plan.preview.json",
    "review_state.json",
    "review_report.html",
}


def _load_metadata(paths: Paths) -> dict[str, Any]:
    connection = open_database(paths)
    try:
        return {
            row["key"]: json.loads(row["value_json"])
            for row in connection.execute("SELECT key, value_json FROM metadata ORDER BY key")
        }
    finally:
        connection.close()


def _plugin_source_tree_sha256() -> str:
    plugin_root = Path(__file__).resolve().parents[2]
    rows: list[str] = []
    for path in sorted(plugin_root.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix.lower() == ".pyc":
            continue
        rows.append(f"{sha256_file(path)}  {path.relative_to(plugin_root).as_posix()}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _read_review_log(paths: Paths) -> list[dict[str, Any]]:
    if not paths.review_decisions_path.exists():
        return []
    events: list[dict[str, Any]] = []
    previous_hash = "0" * 64
    for line_number, line in enumerate(paths.review_decisions_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Review log line {line_number} is not an object")
        stored_hash = str(value.get("event_hash") or "")
        stored_id = str(value.get("event_id") or "")
        unsigned = {key: item for key, item in value.items() if key not in {"event_hash", "event_id"}}
        expected_hash = hashlib.sha256(stable_json_text(unsigned).encode("utf-8")).hexdigest()
        if value.get("previous_event_hash") != previous_hash:
            raise ValueError(f"Review log chain mismatch at line {line_number}")
        if stored_hash != expected_hash or stored_id != expected_hash[:24]:
            raise ValueError(f"Review log event hash mismatch at line {line_number}")
        events.append(value)
        previous_hash = stored_hash
    return events


def _review_events(paths: Paths, canonical_model: str) -> list[dict[str, Any]]:
    return [event for event in _read_review_log(paths) if event.get("canonical_model") == canonical_model]


def _review_subject_hash(
    official: dict[str, Any],
    decision: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    source_report = metadata["source_report"]
    official_product_digest = hashlib.sha256(stable_json_text(official).encode("utf-8")).hexdigest()
    subject = {
        "engine_version": ENGINE_VERSION,
        "review_contract_version": REVIEW_CONTRACT_VERSION,
        "canonical_model": official["canonical_model"],
        "official_source_sha256": official["source"]["source_sha256"],
        "official_payload_sha256": official["source"]["payload_sha256"],
        "official_product_digest": official_product_digest,
        "website_archive_sha256": source_report["observed"]["website_archive_sha256"],
        "website_dist_tree_sha256": source_report["observed"]["website_dist_tree_sha256"],
        "website_assets_sha256": source_report["observed"]["website_assets_sha256"],
        "alibaba_catalog_sha256": source_report["observed"]["alibaba_catalog_sha256"],
        "alibaba_snapshot_tree_sha256": source_report["observed"]["alibaba_snapshot_tree_sha256"],
        "disposition_overlay_sha256": source_report["observed"]["disposition_overlay_sha256"],
        "plugin_source_tree_sha256": _plugin_source_tree_sha256(),
        "match_decision": decision,
    }
    return hashlib.sha256(stable_json_text(subject).encode("utf-8")).hexdigest()


def _review_state_for_model(paths: Paths, canonical_model: str, review_subject_hash: str) -> dict[str, Any]:
    events = _review_events(paths, canonical_model)
    current_events = [
        event
        for event in events
        if event.get("decision") in FIELD_REVIEW_DECISIONS
        and event.get("review_subject_hash") == review_subject_hash
    ]
    return review_state(review_subject_hash, events, current_events)


def _provenance_rows(official: dict[str, Any]) -> list[dict[str, Any]]:
    source = official["source"]
    common = {
        "canonical_model": official["canonical_model"],
        "source_type": source["source_type"],
        "source_path": source["source_path"],
        "source_sha256": source["source_sha256"],
        "source_release": source["release_id"],
        "approval_status": "needs_review",
        "ai_assisted_extraction": True,
        "observed_at_utc": source.get("observed_at_utc"),
        "source_disposition": source.get("source_disposition"),
    }
    rows: list[dict[str, Any]] = []
    for field, locator in (
        ("raw_model", "/product/xinghao"),
        ("name", "/product/name"),
        ("category", "/oneclass/name"),
        ("subcategory", "/twoclass/name"),
        ("short_description_text", "/product/smalltitle"),
        ("description_text", "/product/describe"),
        ("details_text", "/product/details"),
    ):
        rows.append({**common, "field": field, "value": official.get(field), "source_locator": locator})
    for attribute in official["attributes"]:
        rows.append(
            {
                **common,
                "field": f"specification[{attribute['position']}]",
                "value": {"name": attribute["name"], "value": attribute["raw_value"]},
                "source_locator": attribute["source_locator"],
                "value_state": attribute["value_state"],
            }
        )
    return rows


def _extract_commercial_rows(historical: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(
        item: dict[str, Any],
        field: str,
        value: Any,
        locator: str,
        risk_note: str = "Historical value; verify for the new product and current Alibaba schema.",
        copy_policy: str = "SUGGESTED_FROM_HISTORICAL",
    ) -> None:
        if value in (None, "", [], {}):
            return
        rows.append(
            {
                "rank": item["similarity"].get("rank", ""),
                "similarity_score": item["similarity"]["score"],
                "source_product_id": item["product_id"],
                "source_model": item["raw_model"],
                "source_subject": item["subject"],
                "field": field,
                "suggested_value": json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else value,
                "source_locator": locator,
                "source_disposition": item["record_disposition"],
                "copy_policy": copy_policy,
                "approval_status": "HUMAN_CONFIRM_REQUIRED",
                "approved_for_fill": False,
                "risk_note": risk_note,
                "source_observed_at": (item.get("safe_snapshot") or {}).get("captured_at"),
                "source_file_sha256": (item.get("safe_snapshot") or {}).get("source_file_sha256"),
                "comparison_status": "NOT_VALIDATED_EQUIVALENT",
            }
        )

    for rank, item in enumerate(historical, start=1):
        item["similarity"]["rank"] = rank
        safe = item.get("safe_snapshot") or {}
        product = safe.get("product") or {}
        trade = safe.get("trade") or {}
        logistics = trade.get("logistics") or {}
        price = product.get("price") or {}
        price_range = price.get("range") or {}
        add(item, "currency", item.get("currency"), "/catalog_observation/currency")
        add(item, "minimum_order_quantity", product.get("moq") or item.get("minimum_order_quantity"), "/public_page/detail_data_core/product/moq")
        add(item, "customs_moq", product.get("customs_moq"), "/public_page/detail_data_core/product/customsMoq")
        add(item, "box_moq", product.get("box_moq"), "/public_page/detail_data_core/product/boxMoq")
        add(item, "price_range_usd_low", price_range.get("dollarPriceRangeLow"), "/public_page/detail_data_core/product/price/productRangePrices/dollarPriceRangeLow")
        add(item, "price_range_usd_high", price_range.get("dollarPriceRangeHigh"), "/public_page/detail_data_core/product/price/productRangePrices/dollarPriceRangeHigh")
        add(item, "price_ladders", price.get("ladders"), "/public_page/detail_data_core/product/price/productLadderPrices")
        add(item, "sku_attributes", (product.get("sku") or {}).get("attributes"), "/public_page/detail_data_core/product/sku/skuAttrs")
        add(item, "sku_samples", (product.get("sku") or {}).get("samples"), "/public_page/detail_data_core/product/sku/skuSample")
        add(item, "sample_terms", product.get("sample"), "/public_page/detail_data_core/product/sampleInfo")
        add(
            item,
            "lead_time",
            trade.get("lead_time"),
            "/public_page/detail_data_core/trade/leadTimeInfo",
            "Historical lead time is highly templated in this corpus; independent confirmation required.",
        )
        add(item, "packaging_properties", logistics.get("packaging_properties"), "/public_page/detail_data_core/trade/logisticInfo/productPackagingProperties")
        add(item, "packaging_unit_size", logistics.get("unit_size"), "/public_page/detail_data_core/trade/logisticInfo/unitSize")
        add(item, "packaging_unit_weight", logistics.get("unit_weight"), "/public_page/detail_data_core/trade/logisticInfo/unitWeight")
        if str(logistics.get("unit_volume") or "") not in {"", "0", "0.0"}:
            add(item, "packaging_unit_volume", logistics.get("unit_volume"), "/public_page/detail_data_core/trade/logisticInfo/unitVolume")
        add(
            item,
            "shipping_template_id",
            logistics.get("shipping_template_id"),
            "/public_page/detail_data_core/trade/logisticInfo/productShippingTemplateId",
            "Account configuration reference only; never copy directly without current account verification.",
            "REFERENCE_ONLY_NOT_COPYABLE",
        )
        add(item, "trade_terms", trade.get("trade"), "/public_page/detail_data_core/trade/tradeInfo")
    return rows


def _candidate_product(official: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "sellerkit.candidate_product.v1",
        "identity": {
            "product_id": official.get("product_id"),
            "canonical_model": official["canonical_model"],
            "raw_model": official["raw_model"],
            "guid_advisory_only": official.get("guid_advisory"),
        },
        "taxonomy": {
            "website_category": official["category"],
            "website_subcategory": official["subcategory"],
            **official.get("taxonomy", {}),
            "alibaba_category_id": None,
            "alibaba_schema_hash": None,
            "mapping_status": "BLOCKED_UNMAPPED",
        },
        "content": {
            "name": official["name"],
            "short_description_html": official["short_description_html"],
            "short_description_text": official["short_description_text"],
            "description_html": official["description_html"],
            "description_text": official["description_text"],
            "details_html": official["details_html"],
            "details_text": official["details_text"],
        },
        "specifications": official["attributes"],
        "relations": official["materials"],
        "source": official["source"],
        "approval_status": "needs_review",
        "approved_for_remote_fill": False,
    }


def _asset_rows(official: dict[str, Any]) -> list[dict[str, Any]]:
    listing_image_roles = {"main_image", "gallery", "spec_value_link", "product_display"}
    return [
        {
            "model": official["raw_model"],
            "order": asset["order"],
            "role": asset["role"],
            "source_url": asset["source_url"],
            "source_basename": asset["source_basename"],
            "materialization": asset["verification_status"],
            "local_file": asset["local_file"],
            "local_file_exists": asset["local_file_exists"],
            "size_bytes": asset["size_bytes"],
            "sha256": asset["sha256"],
            "width": asset["width"],
            "height": asset["height"],
            "asset_class": "LISTING_IMAGE_CANDIDATE" if asset["role"] in listing_image_roles else "REFERENCE_MATERIAL",
            "required_for_listing": asset["role"] in {"main_image", "gallery"},
            "manual_upload_required": asset["role"] in listing_image_roles,
            "manual_action": (
                "REVIEW_THEN_UPLOAD_TO_ALIBABA_PHOTOBANK"
                if asset["role"] in listing_image_roles
                else "REFERENCE_ONLY_NO_AUTOMATIC_UPLOAD"
            ),
            "remote_asset_id": "",
            "approval_status": "HUMAN_CONFIRM_REQUIRED",
            "approved_for_fill": False,
        }
        for asset in official["assets"]
    ]


def _fill_plan(
    official: dict[str, Any],
    decision: dict[str, Any],
    assets: list[dict[str, Any]],
    commercial: list[dict[str, Any]],
    current_review_state: dict[str, Any],
    package_id: str,
) -> dict[str, Any]:
    blocked = [
        "V1_LOCAL_ONLY_NO_REMOTE_WRITE",
        "BLOCKED_ALIBABA_CATEGORY_SCHEMA_UNAVAILABLE",
        "BLOCKED_COMMERCIAL_FACTS_UNAPPROVED",
        "BLOCKED_ASSETS_NOT_PREUPLOADED",
    ]
    if any(
        row["required_for_listing"]
        and row["materialization"] == "REMOTE_REFERENCE_UNMATERIALIZED"
        for row in assets
    ):
        blocked.append("BLOCKED_REMOTE_PRODUCT_ASSETS_UNMATERIALIZED")
    return {
        "schema_version": "sellerkit.fill_plan.preview.v2",
        "package_id": package_id,
        "model": official["raw_model"],
        "match_status": decision["status"],
        "preview_only": True,
        "external_write_enabled": False,
        "browser_actions_allowed": False,
        "remote_draft_allowed": False,
        "publish_allowed": False,
        "field_actions": [
            {
                "field": "product_title",
                "proposed_value": official["name"],
                "source_locator": "/product/name",
                "approval_status": "needs_review",
            },
            {
                "field": "model",
                "proposed_value": official["raw_model"],
                "source_locator": "/product/xinghao",
                "approval_status": "needs_review",
            },
        ],
        "commercial_suggestion_count": len(commercial),
        "all_commercial_suggestions_require_human_confirmation": True,
        "asset_actions": assets,
        "blocked_reasons": sorted(set(blocked)),
        "build_status": PACKAGE_STATUS,
        "local_review_status": LOCAL_REVIEW_STATUS,
        "remote_write_status": REMOTE_WRITE_STATUS,
        "release_status": RELEASE_STATUS,
        "terminal_status": "LOCAL_REVIEW_PREVIEW_ONLY_NO_REMOTE_WRITE",
        "review_state": current_review_state,
    }


def _report_html(
    official: dict[str, Any],
    decision: dict[str, Any],
    historical: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    commercial: list[dict[str, Any]],
    fill_plan: dict[str, Any],
) -> str:
    esc = lambda value: html.escape(str(value if value is not None else ""))
    similar_rows = "".join(
        f"<tr><td>{index}</td><td>{esc(item['raw_model'])}</td><td>{esc(item['subject'])}</td>"
        f"<td>{item['similarity']['score']:.4f}</td><td>{esc(item['product_id'])}</td></tr>"
        for index, item in enumerate(historical, start=1)
    ) or "<tr><td colspan='5'>无可用历史相似品</td></tr>"
    spec_rows = "".join(
        f"<tr><td>{row['position']}</td><td>{esc(row['name'])}</td><td>{esc(row['raw_value'])}</td>"
        f"<td>{esc(row['value_state'])}</td></tr>"
        for row in official["attributes"]
    )
    asset_rows = "".join(
        f"<tr><td>{row['order']}</td><td>{esc(row['role'])}</td><td class='wrap'>{esc(row['source_url'])}</td>"
        f"<td>{esc(row['materialization'])}</td><td>{esc(row['sha256'])}</td></tr>"
        for row in assets
    ) or "<tr><td colspan='5'>无素材引用</td></tr>"
    commercial_rows = "".join(
        f"<tr><td>{esc(row['rank'])}</td><td>{esc(row['source_model'])}</td><td>{esc(row['field'])}</td>"
        f"<td class='wrap'>{esc(row['suggested_value'])}</td><td>{esc(row['copy_policy'])}</td>"
        f"<td>{esc(row['risk_note'])}</td></tr>"
        for row in commercial
    ) or "<tr><td colspan='6'>没有可提取的历史商业建议</td></tr>"
    blocked = "".join(f"<li>{esc(reason)}</li>" for reason in fill_plan["blocked_reasons"])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{esc(official['raw_model'])} 本地审核包</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:28px;color:#18212b}}h1,h2{{color:#0e4b5a}}.gate{{background:#fff0f0;border:2px solid #b42318;padding:16px}}.ok{{background:#eef8f0;border:1px solid #2f855a;padding:12px}}table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}th,td{{border:1px solid #ccd5db;padding:7px;vertical-align:top}}th{{background:#edf4f6}}.wrap{{overflow-wrap:anywhere;max-width:520px}}code{{font-size:12px}}</style></head>
<body><h1>SellerKit → Alibaba 本地审核包：{esc(official['raw_model'])}</h1>
<div class="gate"><strong>V1 本地审核边界。</strong> 本包不可执行 Browser 点击、文件上传、远端草稿、提交或发布；历史商业建议和素材仍需逐项人工确认。</div>
<p class="ok">本地构建状态：<strong>{PACKAGE_STATUS}</strong>。本地审核状态：<strong>{LOCAL_REVIEW_STATUS}</strong>。远端写入状态：<strong>{REMOTE_WRITE_STATUS}</strong>。发布状态：<strong>{RELEASE_STATUS}</strong>。</p>
<h2>候选与匹配</h2><p>官网名称：{esc(official['name'])}<br>官网分类：{esc(official['category'])} / {esc(official['subcategory'])}<br>匹配状态：{esc(decision['status'])}（缺失未自动确认）</p>
<table><thead><tr><th>排名</th><th>型号</th><th>历史标题</th><th>相似度</th><th>商品ID</th></tr></thead><tbody>{similar_rows}</tbody></table>
<h2>字段审核记录</h2><p>当前证据范围内记录数：{esc(fill_plan['review_state']['current_subject_event_count'])}。V1 记录仅用于本地审计，不自动晋升为 approved_internal。</p>
<h2>阻断项</h2><ul>{blocked}</ul>
<h2>官网规格（保序、不去重）</h2><table><thead><tr><th>序号</th><th>字段</th><th>值</th><th>状态</th></tr></thead><tbody>{spec_rows}</tbody></table>
<h2>素材清单（人工预传）</h2><table><thead><tr><th>序号</th><th>角色</th><th>来源URL</th><th>物化状态</th><th>SHA-256</th></tr></thead><tbody>{asset_rows}</tbody></table>
<h2>历史商业建议（全部需人工确认）</h2><table><thead><tr><th>排名</th><th>来源型号</th><th>字段</th><th>建议值</th><th>复制策略</th><th>风险</th></tr></thead><tbody>{commercial_rows}</tbody></table>
</body></html>"""


def _csv_payload(fieldnames: list[str], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    return "\ufeff" + buffer.getvalue()


def _assert_text_is_sanitized(text: str, label: str) -> None:
    findings = secret_text_findings(text, label)
    if findings:
        raise ValueError(f"Secret scan failed before package write for {label}: {findings[:5]}")


def compile_review_package(
    paths: Paths,
    model: str,
    *,
    collection: str = "ad_hoc",
) -> dict[str, Any]:
    if collection not in {"ad_hoc", "priority"}:
        raise ValueError("collection must be ad_hoc or priority")
    database_state = validate_database_state(paths)
    official, decision, historical = load_product_bundle(paths, model, preflight=False)
    metadata = _load_metadata(paths)
    review_subject_hash = _review_subject_hash(official, decision, metadata)
    current_review_state = _review_state_for_model(paths, official["canonical_model"], review_subject_hash)
    source_report = metadata["source_report"]
    seed = {
        "engine_version": ENGINE_VERSION,
        "model": official["canonical_model"],
        "official_source_sha256": official["source"]["source_sha256"],
        "payload_sha256": official["source"]["payload_sha256"],
        "catalog_sha256": source_report["observed"]["alibaba_catalog_sha256"],
        "overlay_sha256": source_report["observed"]["disposition_overlay_sha256"],
        "database_sha256": database_state["database_sha256"],
        "database_receipt_sha256": database_state["database_receipt_sha256"],
        "decision": decision,
        "review_subject_hash": review_subject_hash,
        "review_events": current_review_state.get("review_events", []),
    }
    package_id = hashlib.sha256(stable_json_text(seed).encode("utf-8")).hexdigest()
    package_root = paths.output_root / (
        "review_packages" if collection == "priority" else "ad_hoc_review_packages"
    )
    package_dir = ensure_within(
        package_root / f"{official['raw_model']}-{package_id[:12]}",
        paths.output_root,
    )
    package_dir.mkdir(parents=True, exist_ok=True)
    unexpected_existing = [
        path.name
        for path in package_dir.iterdir()
        if not path.is_file() or path.name not in PACKAGE_CONTENT_NAMES | {"receipt.json"}
    ]
    if unexpected_existing:
        raise ValueError(f"Package directory contains unexpected pre-existing entries: {sorted(unexpected_existing)}")

    for item in historical:
        item["safe_snapshot"]["approval_status"] = "HUMAN_CONFIRM_REQUIRED"
    invalid_historical = [
        item["product_id"]
        for item in historical
        if item.get("record_disposition") != "HISTORICAL_REFERENCE_ONLY"
    ]
    if invalid_historical:
        raise ValueError(f"Ineligible historical records reached package compilation: {invalid_historical}")
    assets = _asset_rows(official)
    commercial = _extract_commercial_rows(historical)
    waste_suggestion_count = sum(
        row.get("source_disposition") != "HISTORICAL_REFERENCE_ONLY" for row in commercial
    )
    if waste_suggestion_count:
        raise ValueError("Unusable or unknown records reached commercial suggestions")
    candidate = _candidate_product(official)
    provenance = _provenance_rows(official)
    fill_plan = _fill_plan(official, decision, assets, commercial, current_review_state, package_id)
    similar = {
        "schema_version": "sellerkit.similar_products.v1",
        "model": official["raw_model"],
        "selection_policy": "eligible historical records only; unusable waste excluded",
        "candidates": historical,
    }

    json_values = {
        "candidate_product.json": candidate,
        "match_decision.json": decision,
        "similar_products.json": similar,
        "fill_plan.preview.json": fill_plan,
        "review_state.json": current_review_state,
    }
    for name, value in json_values.items():
        findings = secret_findings(value)
        if findings:
            raise ValueError(f"Secret scan failed before package write for {name}: {findings[:5]}")
        write_json_atomic(package_dir / name, value)
    provenance_text = "".join(stable_json_text(row) for row in provenance)
    _assert_text_is_sanitized(provenance_text, "field_provenance.jsonl")
    write_text_atomic(package_dir / "field_provenance.jsonl", provenance_text)
    asset_fields = [
        "model", "order", "role", "asset_class", "required_for_listing", "source_url", "source_basename", "materialization", "local_file",
        "local_file_exists", "size_bytes", "sha256", "width", "height", "manual_upload_required",
        "manual_action", "remote_asset_id", "approval_status", "approved_for_fill",
    ]
    asset_csv = _csv_payload(asset_fields, assets)
    _assert_text_is_sanitized(asset_csv, "asset_manifest.csv")
    write_text_atomic(package_dir / "asset_manifest.csv", asset_csv)
    commercial_fields = [
        "rank", "similarity_score", "source_product_id", "source_model", "source_subject", "field",
        "suggested_value", "source_locator", "source_disposition", "copy_policy", "approval_status",
        "approved_for_fill", "risk_note",
        "source_observed_at", "source_file_sha256", "comparison_status",
    ]
    commercial_csv = _csv_payload(commercial_fields, commercial)
    _assert_text_is_sanitized(commercial_csv, "commercial_suggestions.csv")
    write_text_atomic(package_dir / "commercial_suggestions.csv", commercial_csv)
    report_html = _report_html(official, decision, historical, assets, commercial, fill_plan)
    _assert_text_is_sanitized(report_html, "review_report.html")
    write_text_atomic(package_dir / "review_report.html", report_html)

    content_names = sorted(path.name for path in package_dir.iterdir() if path.is_file() and path.name != "receipt.json")
    if set(content_names) != PACKAGE_CONTENT_NAMES:
        raise ValueError(f"Package content set does not match the V1 contract: {content_names}")
    file_hashes = {name: sha256_file(package_dir / name) for name in content_names}
    plugin_source_tree_sha256 = _plugin_source_tree_sha256()
    receipt_core = {
        "schema_version": "sellerkit.review_package_receipt.v2",
        "status": PACKAGE_STATUS,
        "build_status": PACKAGE_STATUS,
        "local_review_status": LOCAL_REVIEW_STATUS,
        "remote_write_status": REMOTE_WRITE_STATUS,
        "release_status": RELEASE_STATUS,
        "package_id": package_id,
        "package_collection": collection,
        "model": official["raw_model"],
        "generated_from_frozen_source_at": source_report["overlay"]["generated_at"],
        "engine_version": ENGINE_VERSION,
        "source_hashes": {
            "official_html": official["source"]["source_sha256"],
            "official_payload": official["source"]["payload_sha256"],
            "website_archive": source_report["observed"]["website_archive_sha256"],
            "website_dist_tree": source_report["observed"]["website_dist_tree_sha256"],
            "website_assets": source_report["observed"]["website_assets_sha256"],
            "alibaba_catalog": source_report["observed"]["alibaba_catalog_sha256"],
            "alibaba_snapshot_tree": source_report["observed"]["alibaba_snapshot_tree_sha256"],
            "disposition_overlay": source_report["observed"]["disposition_overlay_sha256"],
            "database": database_state["database_sha256"],
            "database_receipt": database_state["database_receipt_sha256"],
            "plugin_source_tree": plugin_source_tree_sha256,
        },
        "invariants": {
            "external_write_enabled": False,
            "browser_actions": 0,
            "remote_drafts": 0,
            "publish_requests": 0,
            "unusable_waste_records_in_suggestions": waste_suggestion_count,
            "all_commercial_suggestions_require_human_confirmation": True,
        },
        "review_state": current_review_state,
        "receipt_authentication": "UNSIGNED_LOCAL_INTEGRITY_ONLY_NOT_RELEASE_AUTHORIZATION",
    }
    package_digest_input = {"files": file_hashes, "receipt_core": receipt_core}
    package_digest = hashlib.sha256(stable_json_text(package_digest_input).encode("utf-8")).hexdigest()
    receipt = {
        **receipt_core,
        "files": file_hashes,
        "package_digest": package_digest,
        "package_digest_definition": "SHA256(stable_json({files,receipt_core}))",
    }
    write_json_atomic(package_dir / "receipt.json", receipt)
    return {**receipt, "package_path": str(package_dir), "receipt_sha256": sha256_file(package_dir / "receipt.json")}


def record_review_decision(
    paths: Paths,
    model: str,
    field: str,
    decision: str,
    reviewer_id: str,
    reviewer_role: str,
    rationale: str,
) -> dict[str, Any]:
    database_state = validate_database_state(paths)
    canonical_model = normalize_model(model)
    if not canonical_model:
        raise ValueError("model is required")
    allowed = FIELD_REVIEW_DECISIONS
    if decision not in allowed:
        raise ValueError(f"decision must be one of {sorted(allowed)}")
    reviewer_id, reviewer_role, rationale = validate_review_identity(reviewer_id, reviewer_role, rationale)
    field = str(field or "").strip()
    if not field:
        raise ValueError("field is required")
    official, match_decision, _ = load_product_bundle(paths, canonical_model, preflight=False)
    metadata = _load_metadata(paths)
    review_subject_hash = _review_subject_hash(official, match_decision, metadata)
    lock_path = paths.review_decisions_path.with_suffix(paths.review_decisions_path.suffix + ".lock")
    with exclusive_file_lock(lock_path):
        existing_events = _read_review_log(paths)
        existing = (
            paths.review_decisions_path.read_text(encoding="utf-8")
            if paths.review_decisions_path.exists()
            else ""
        )
        previous_hash = existing_events[-1]["event_hash"] if existing_events else "0" * 64
        event = {
            "schema_version": "sellerkit.review_decision.v2",
            "canonical_model": canonical_model,
            "raw_model": model,
            "field": field,
            "decision": decision,
            "reviewer_id": reviewer_id,
            "reviewer_role": reviewer_role,
            "rationale": rationale,
            "review_scope": "FIELD",
            "review_subject_hash": review_subject_hash,
            "identity_assurance": "SELF_ATTESTED_UNVERIFIED",
            "promotion_effective": False,
            "promotion_effective_reason": "V1_HAS_NO_APPROVED_INTERNAL_PROMOTION_WORKFLOW",
            "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "previous_event_hash": previous_hash,
            "external_write_enabled": False,
            "database_sha256": database_state["database_sha256"],
            "database_receipt_sha256": database_state["database_receipt_sha256"],
        }
        findings = secret_findings(event)
        if findings:
            raise ValueError(f"Secret scan failed for review event: {findings[:5]}")
        event_hash = hashlib.sha256(stable_json_text(event).encode("utf-8")).hexdigest()
        event["event_hash"] = event_hash
        event["event_id"] = event_hash[:24]
        write_text_atomic(paths.review_decisions_path, existing + stable_json_text(event))
        return event


def export_evidence_receipt(paths: Paths) -> dict[str, Any]:
    database_state = validate_database_state(paths)
    files: dict[str, str] = {}
    excluded_current_evidence_prefixes = (
        ("output", "ad_hoc_review_packages"),
        ("output", "superseded_review_packages"),
        ("output", "plugin_package", "superseded"),
    )
    for root in (
        paths.project_root / "artifacts",
        paths.work_root,
        paths.output_root,
        paths.project_root / "schemas",
        paths.project_root / "scripts",
        paths.project_root / "tests",
    ):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda value: str(value).lower()):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() != ".pyc":
                relative_path = path.relative_to(paths.project_root)
                normalized_parts = tuple(part.casefold() for part in relative_path.parts)
                if any(
                    normalized_parts[: len(prefix)] == prefix
                    for prefix in excluded_current_evidence_prefixes
                ):
                    continue
                files[str(relative_path).replace("\\", "/")] = sha256_file(path)
    receipt = {
        "schema_version": "sellerkit.evidence_receipt.v2",
        "status": "PASS_LOCAL_EVIDENCE_RECEIPT_BUILT_NO_REMOTE_WRITE",
        "build_status": "PASS_LOCAL_EVIDENCE_RECEIPT_BUILT_NO_REMOTE_WRITE",
        "local_review_status": LOCAL_REVIEW_STATUS,
        "remote_write_status": REMOTE_WRITE_STATUS,
        "release_status": RELEASE_STATUS,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "engine_version": ENGINE_VERSION,
        "files": files,
        "file_count": len(files),
        "external_write_enabled": False,
        "browser_actions": 0,
        "remote_drafts": 0,
        "publish_requests": 0,
        "plugin_source_tree_sha256": _plugin_source_tree_sha256(),
        "database_sha256": database_state["database_sha256"],
        "database_receipt_sha256": database_state["database_receipt_sha256"],
        "source_report": database_state["source_report"],
    }
    receipt["evidence_digest"] = hashlib.sha256(stable_json_text(files).encode("utf-8")).hexdigest()
    path = paths.evidence_root / "current_evidence_receipt.json"
    write_json_atomic(path, receipt)
    return {**receipt, "path": str(path), "receipt_sha256": sha256_file(path)}
