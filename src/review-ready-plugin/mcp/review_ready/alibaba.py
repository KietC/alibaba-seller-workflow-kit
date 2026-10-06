from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .config import Paths
from .utils import normalize_model, normalize_space, secret_findings, sha256_bytes, sha256_file


ALLOWED_DISPOSITION = "HISTORICAL_REFERENCE_ONLY"
REJECTED_DISPOSITION = "UNUSABLE_WASTE_RECORD"


def _safe_properties(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed = ("attrName", "attrNameId", "attrValue", "attrValueId")
    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key) for key in allowed if item.get(key) not in (None, "")}
        if row:
            output.append(row)
    return output


def _safe_price(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    ranges = value.get("productRangePrices") if isinstance(value.get("productRangePrices"), dict) else {}
    ladders = value.get("productLadderPrices") if isinstance(value.get("productLadderPrices"), list) else []
    return {
        "batch_num": value.get("batchNum"),
        "sale_type": value.get("saleType"),
        "unit": value.get("unit"),
        "unit_even": value.get("unitEven"),
        "range": {
            key: ranges.get(key)
            for key in (
                "dollarPriceRangeLow",
                "dollarPriceRangeHigh",
                "priceRangeLow",
                "priceRangeHigh",
            )
            if ranges.get(key) is not None
        },
        "ladders": [
            {
                key: row.get(key)
                for key in ("minQuantity", "maxQuantity", "price", "dollarPrice")
                if isinstance(row, dict) and row.get(key) is not None
            }
            for row in ladders
            if isinstance(row, dict)
        ],
    }


def _safe_sku(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    attrs: list[dict[str, Any]] = []
    for attr in value.get("skuAttrs") or []:
        if not isinstance(attr, dict):
            continue
        attrs.append(
            {
                "id": attr.get("id"),
                "name": attr.get("name"),
                "type": attr.get("type"),
                "values": [
                    {key: item.get(key) for key in ("id", "name", "type", "color") if item.get(key) not in (None, "")}
                    for item in (attr.get("values") or [])
                    if isinstance(item, dict)
                ],
            }
        )
    samples: list[dict[str, Any]] = []
    for sample in value.get("skuSample") or []:
        if not isinstance(sample, dict):
            continue
        samples.append(
            {
                "id": sample.get("id"),
                "price": sample.get("price"),
                "values": [
                    {key: item.get(key) for key in ("id", "name", "propName", "type", "color") if item.get(key) not in (None, "")}
                    for item in (sample.get("values") or [])
                    if isinstance(item, dict)
                ],
            }
        )
    return {"attributes": attrs, "samples": samples}


def _safe_sample(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in ("enable", "freeSample", "maxQuantity", "price", "priceFrom", "priceTo")
        if value.get(key) is not None
    }


def _safe_trade(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    logistic = value.get("logisticInfo") if isinstance(value.get("logisticInfo"), dict) else {}
    trade_info = value.get("tradeInfo") if isinstance(value.get("tradeInfo"), dict) else {}
    packaging = _safe_properties(logistic.get("productPackagingProperties"))
    return {
        "lead_time": value.get("leadTimeInfo") if isinstance(value.get("leadTimeInfo"), dict) else {},
        "warehouse_lead_time": value.get("warehouseLeadTimeInfo")
        if isinstance(value.get("warehouseLeadTimeInfo"), dict)
        else {},
        "logistics": {
            "packaging_properties": packaging,
            "shipping_template_id": logistic.get("productShippingTemplateId"),
            "unit_size": logistic.get("unitSize"),
            "unit_volume": logistic.get("unitVolume"),
            "unit_weight": logistic.get("unitWeight"),
        },
        "trade": {
            key: trade_info.get(key)
            for key in ("batchNum", "hasSpu", "quantityUnitStr", "saleType", "tradeAvailable", "tradePriceType")
            if trade_info.get(key) is not None
        },
    }


def _safe_json_ld(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    brand = value.get("brand") if isinstance(value.get("brand"), dict) else {}
    offers = value.get("offers") if isinstance(value.get("offers"), dict) else {}
    images = value.get("image") if isinstance(value.get("image"), list) else []
    return {
        "name": value.get("name"),
        "description": value.get("description"),
        "sku": value.get("sku"),
        "mpn": value.get("mpn"),
        "brand_name": brand.get("name"),
        "images": [str(item) for item in images if isinstance(item, str)],
        "offer": {
            key: offers.get(key)
            for key in ("price", "priceCurrency", "availability")
            if offers.get(key) is not None
        },
    }


def _load_catalog_with_hashes(path: Path) -> list[tuple[dict[str, Any], str]]:
    records: list[tuple[dict[str, Any], str]] = []
    with path.open("rb") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            stripped = raw_line.rstrip(b"\r\n")
            if not stripped:
                continue
            try:
                record = json.loads(stripped.decode("utf-8"))
            except Exception as exc:
                raise ValueError(f"Invalid catalog JSON at line {line_number}") from exc
            records.append((record, sha256_bytes(stripped)))
    return records


def _load_dispositions(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Invalid disposition JSON at line {line_number}")
            output.append(value)
    return output


def parse_alibaba_products(paths: Paths) -> list[dict[str, Any]]:
    catalog = _load_catalog_with_hashes(paths.alibaba_catalog)
    dispositions = _load_dispositions(paths.disposition_records)
    if len(catalog) != len(dispositions):
        raise ValueError("Catalog and disposition row counts differ")
    catalog_hash = sha256_file(paths.alibaba_catalog)
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for line_number, ((catalog_row, row_hash), disposition) in enumerate(zip(catalog, dispositions), start=1):
        product_id = str(catalog_row.get("product_id") or "")
        if not product_id or product_id in seen_ids:
            raise ValueError(f"Missing or duplicate product_id at catalog line {line_number}")
        seen_ids.add(product_id)
        binding = disposition.get("base_binding") or {}
        if str(disposition.get("product_id") or "") != product_id:
            raise ValueError(f"Disposition product mismatch at line {line_number}")
        if binding.get("catalog_index_line") != line_number:
            raise ValueError(f"Disposition catalog line mismatch for {product_id}")
        if binding.get("catalog_index_sha256") != catalog_hash:
            raise ValueError(f"Disposition catalog hash mismatch for {product_id}")
        if binding.get("catalog_row_sha256") != row_hash:
            raise ValueError(f"Disposition catalog row hash mismatch for {product_id}")
        status = str(disposition.get("disposition") or disposition.get("record_disposition") or "")
        if status not in {ALLOWED_DISPOSITION, REJECTED_DISPOSITION}:
            raise ValueError(f"Unknown disposition {status!r} for {product_id}")

        identity = {
            "product_id": product_id,
            "ordinal": catalog_row.get("ordinal"),
            "canonical_model": normalize_model(catalog_row.get("model")),
            "raw_model": normalize_space(catalog_row.get("model")),
            "record_disposition": status,
            "historical_observation_only": True,
            "approved_fact": False,
            "allow_rag": bool(disposition.get("allow_rag")),
            "fact_extraction": bool(disposition.get("fact_extraction")),
            "asset_reuse": bool(disposition.get("asset_reuse")),
            "publish_draft": False,
            "auto_listing": False,
            "external_write_enabled": False,
            "source": {
                "catalog_path": str(paths.alibaba_catalog),
                "catalog_sha256": catalog_hash,
                "catalog_line": line_number,
                "catalog_row_sha256": row_hash,
                "overlay_path": str(paths.disposition_records),
                "overlay_version": disposition.get("overlay_version"),
            },
        }

        if status == REJECTED_DISPOSITION:
            output.append(
                {
                    **identity,
                    "subject": "",
                    "display_status": "",
                    "detail_url": "",
                    "group_names": [],
                    "currency": None,
                    "minimum_order_quantity": None,
                    "sku_min_price": None,
                    "sku_max_price": None,
                    "quality_score": None,
                    "gmt_modified": None,
                    "safe_snapshot": None,
                    "exclusion_reason": "UNUSABLE_WASTE_RECORD_MINIMAL_PRESENCE_GUARD_ONLY",
                    "waste_fields_retained": ["product_id", "ordinal", "raw_model", "canonical_model", "record_disposition", "source"],
                }
            )
            continue

        if not all(
            [
                disposition.get("allow_rag") is True,
                disposition.get("fact_extraction") is True,
                disposition.get("publish_draft") is False,
                disposition.get("auto_listing") is False,
                disposition.get("external_write_enabled") is False,
            ]
        ):
            raise ValueError(f"Historical disposition controls are not fail-closed for {product_id}")

        base = {
            **identity,
            "subject": normalize_space(catalog_row.get("subject")),
            "display_status": catalog_row.get("display_status"),
            "detail_url": catalog_row.get("detail_url"),
            "group_names": catalog_row.get("group_names") or [],
            "currency": catalog_row.get("currency"),
            "minimum_order_quantity": catalog_row.get("min_order_quantity"),
            "sku_min_price": catalog_row.get("sku_min_price"),
            "sku_max_price": catalog_row.get("sku_max_price"),
            "quality_score": catalog_row.get("quality_score"),
            "gmt_modified": catalog_row.get("gmt_modified"),
        }
        snapshot_path = paths.alibaba_snapshots_root / product_id / "product_snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if str(snapshot.get("product_id") or "") != product_id:
            raise ValueError(f"Snapshot product mismatch for {product_id}")
        if snapshot.get("historical_observation_only") is not True or snapshot.get("approved_fact") is not False:
            raise ValueError(f"Snapshot source flags are not historical-only for {product_id}")
        public_page = snapshot.get("public_page") if isinstance(snapshot.get("public_page"), dict) else {}
        detail_core = public_page.get("detail_data_core") if isinstance(public_page.get("detail_data_core"), dict) else {}
        product = detail_core.get("product") if isinstance(detail_core.get("product"), dict) else {}
        trade = detail_core.get("trade") if isinstance(detail_core.get("trade"), dict) else {}
        safe_snapshot = {
            "captured_at": snapshot.get("captured_at"),
            "title": public_page.get("title"),
            "canonical": public_page.get("canonical"),
            "h1": public_page.get("h1"),
            "description_text": public_page.get("description_text"),
            "json_ld": _safe_json_ld(public_page.get("json_ld_product")),
            "product": {
                "subject": product.get("subject"),
                "category_id": product.get("productCategoryId"),
                "leaf_category_id": product.get("productLeafId"),
                "moq": product.get("moq"),
                "customs_moq": product.get("customsMoq"),
                "box_moq": product.get("boxMoq"),
                "quantity_unit": product.get("quantityUnit"),
                "deliver_place": product.get("deliverPlace"),
                "price": _safe_price(product.get("price")),
                "custom_price": _safe_price(product.get("customPrice")),
                "properties": _safe_properties(product.get("productBasicProperties")),
                "key_industry_properties": _safe_properties(product.get("productKeyIndustryProperties")),
                "other_properties": _safe_properties(product.get("productOtherProperties")),
                "sku": _safe_sku(product.get("sku")),
                "sample": _safe_sample(product.get("sampleInfo") or product.get("sample")),
                "light_customization": product.get("productLightCustomizationList")
                if isinstance(product.get("productLightCustomizationList"), list)
                else [],
            },
            "trade": _safe_trade(trade),
            "source_file": str(snapshot_path),
            "source_file_sha256": sha256_file(snapshot_path),
            "source_json_pointer": "/public_page",
            "record_disposition": status,
            "historical_observation_only": True,
            "approved_fact": False,
            "approval_status": "HUMAN_CONFIRM_REQUIRED",
        }
        findings = secret_findings(safe_snapshot)
        if findings:
            raise ValueError(f"Sensitive field survived whitelist for {product_id}: {findings[:3]}")
        base["safe_snapshot"] = safe_snapshot
        output.append(base)

    return sorted(output, key=lambda item: int(item["ordinal"] or 0))


def alibaba_stats(products: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [item for item in products if item["record_disposition"] == ALLOWED_DISPOSITION]
    waste = [item for item in products if item["record_disposition"] == REJECTED_DISPOSITION]
    public_range_covered = 0
    structured_ladder_covered = 0
    catalog_sku_price_covered = 0
    sku_attrs = 0
    sku_samples = 0
    lead_time = 0
    packaging = 0
    for item in eligible:
        safe = item["safe_snapshot"] or {}
        product = safe.get("product") or {}
        price = product.get("price") or {}
        if price.get("range"):
            public_range_covered += 1
        if price.get("ladders"):
            structured_ladder_covered += 1
        if item.get("sku_min_price") and item.get("sku_max_price"):
            catalog_sku_price_covered += 1
        if (product.get("sku") or {}).get("attributes"):
            sku_attrs += 1
        if (product.get("sku") or {}).get("samples"):
            sku_samples += 1
        trade = safe.get("trade") or {}
        if trade.get("lead_time"):
            lead_time += 1
        if (trade.get("logistics") or {}).get("packaging_properties"):
            packaging += 1
    return {
        "product_count": len(products),
        "unique_product_id_count": len({item["product_id"] for item in products}),
        "non_empty_model_count": sum(bool(item["canonical_model"]) for item in products),
        "unique_model_count": len({item["canonical_model"] for item in products if item["canonical_model"]}),
        "historical_reference_count": len(eligible),
        "unusable_waste_count": len(waste),
        "historical_price_coverage": structured_ladder_covered,
        "historical_structured_ladder_price_coverage": structured_ladder_covered,
        "historical_catalog_sku_price_coverage": catalog_sku_price_covered,
        "historical_public_price_range_coverage": public_range_covered,
        "historical_sku_attribute_coverage": sku_attrs,
        "historical_sku_sample_coverage": sku_samples,
        "historical_lead_time_coverage": lead_time,
        "historical_packaging_coverage": packaging,
    }


def historical_search_text(item: dict[str, Any]) -> str:
    safe = item.get("safe_snapshot") or {}
    product = safe.get("product") or {}
    properties = product.get("properties") or []
    property_text = " ".join(
        f"{row.get('attrName', '')} {html.unescape(str(row.get('attrValue', '')))}" for row in properties
    )
    return " ".join(
        str(part or "")
        for part in (
            item.get("subject"),
            item.get("raw_model"),
            " ".join(item.get("group_names") or []),
            safe.get("title"),
            safe.get("h1"),
            safe.get("description_text"),
            product.get("subject"),
            property_text,
        )
    )
