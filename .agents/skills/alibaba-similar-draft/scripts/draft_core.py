from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse


class GuardError(RuntimeError):
    """Fail-closed validation error."""


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}

# Production browser provenance is intentionally not implemented in this
# release.  A caller-controlled adapter JSON and its self-consistent hash are
# not an attestation boundary: setting a readiness boolean must never promote
# a local exact-READ probe to live execution authority.  A future release must
# replace this constant with verification of a host-produced, non-forgeable
# attestation receipt; changing adapter metadata alone is insufficient.
# 本版未实现生产浏览器来源认证；调用方可改的 JSON 和自洽哈希不算认证。
# 只改 readiness 标志不能让本地只读探测获得在线执行权限。
# 未来必须核验平台产生的不可伪造回执，不能只改适配器元数据。
PRODUCTION_BROWSER_ATTESTATION_VERIFIER_IMPLEMENTED = False
REJECTED_DISPOSITIONS = {
    "UNUSABLE_WASTE_RECORD",
    "BLOCKED_UNCLASSIFIED",
}
HISTORICAL_DISPOSITION = "HISTORICAL_REFERENCE_ONLY"
DEFAULT_CUTOFF = date(2000, 1, 2)
# The browser plan and both approval gates use the same reviewed aliases.
# The registry has no `moq` alias: its logical field is minimum_order_quantity.
# 两个审批门禁共用已审核字段别名；MOQ 的正式逻辑名为 minimum_order_quantity。
FIELD_REGISTRY_ALIASES = {"price": "trade_price"}
QUALIFIED_COMMERCIAL_REGISTRY_FIELDS = {
    "trade_price": "price",
    "minimum_order_quantity": "minimum_order_quantity",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise GuardError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise GuardError(f"JSONL row is not an object at {path}:{line_number}")
            yield value


def parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def percentile(values: list[float], probability: float) -> float | None:
    values = sorted(v for v in values if math.isfinite(v))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def product_properties(row: dict[str, Any]) -> list[dict[str, Any]]:
    product = (row.get("safe_snapshot") or {}).get("product") or {}
    props = product.get("properties") or []
    return [item for item in props if isinstance(item, dict)]


def property_blob(row: dict[str, Any]) -> str:
    values: list[str] = []
    for item in product_properties(row):
        values.extend([str(item.get("attrName") or ""), str(item.get("attrValue") or "")])
    values.extend([str(row.get("subject") or ""), str(row.get("title") or "")])
    return normalized_text(" ".join(values))


def extract_trade(row: dict[str, Any]) -> dict[str, Any]:
    """Return the normalized trade object, preferring the sanitized snapshot schema."""

    snapshot = row.get("safe_snapshot")
    candidates = []
    if isinstance(snapshot, dict):
        candidates.append(snapshot.get("trade"))
    candidates.append(row.get("trade"))

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        nested = candidate.get("trade")
        if isinstance(nested, dict) and nested:
            return nested
        # Compatibility with older/flattened rows whose trade fields were not
        # wrapped in a second ``trade`` object.
        if any(
            key in candidate
            for key in ("tradeAvailable", "tradePriceType", "quantityUnitStr", "saleType")
        ):
            return candidate
    return {}


def extract_price(row: dict[str, Any]) -> dict[str, Any]:
    product = (row.get("safe_snapshot") or {}).get("product") or {}
    price = product.get("price") or product.get("custom_price") or {}
    bulk_observed: list[float] = []
    range_data = price.get("range") or {}
    for key in ("dollarPriceRangeLow", "dollarPriceRangeHigh"):
        number = as_float(range_data.get(key))
        if number is not None:
            bulk_observed.append(number)
    for ladder in price.get("ladders") or []:
        number = as_float((ladder or {}).get("dollarPrice"))
        if number is not None:
            bulk_observed.append(number)
    sample_observed: list[float] = []
    for sample in ((product.get("sku") or {}).get("samples") or []):
        number = as_float((sample or {}).get("price"))
        if number is not None:
            sample_observed.append(number)

    bulk_observed = sorted(set(bulk_observed))
    sample_observed = sorted(set(sample_observed))
    low = min(bulk_observed) if bulk_observed else None
    high = max(bulk_observed) if bulk_observed else None
    sample_low = min(sample_observed) if sample_observed else None
    sample_high = max(sample_observed) if sample_observed else None
    trade = extract_trade(row)
    return {
        "currency": row.get("currency") or "USD",
        "unit": price.get("unit") or trade.get("quantityUnitStr"),
        "price_basis": "BULK_ONLY",
        "low": low,
        "high": high,
        "midpoint": ((low + high) / 2) if low is not None and high is not None else None,
        "observations": bulk_observed,
        "bulk": {
            "low": low,
            "high": high,
            "midpoint": ((low + high) / 2) if low is not None and high is not None else None,
            "observations": bulk_observed,
        },
        "sample": {
            "low": sample_low,
            "high": sample_high,
            "observations": sample_observed,
            "excluded_from_bulk": True,
        },
    }


def extract_candidate(
    row: dict[str, Any],
    rank: int | None,
    retrieval_score: float,
    visual_score: float,
    model_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    product = (row.get("safe_snapshot") or {}).get("product") or {}
    blob = property_blob(row)
    signals = {
        "deck_mount": any(token in blob for token in ("deck mount", "deck-mounted", "台面/台盆上安装", "座台")),
        "single_hole": any(token in blob for token in ("single hole", "single-hole", "单孔")),
        "stainless": any(token in blob for token in ("sus304", "stainless steel 304", "不锈钢")),
        "pre_rinse_complete_faucet": (
            any(token in blob for token in ("pre rinse faucet", "pre-rinse faucet", "commercial kitchen faucet", "预冲洗"))
            and not any(token in blob for token in ("hose only", "spray gun only", "reel only", "软管", "喷枪组件"))
        ),
        "category_match": str(product.get("leaf_category_id") or "") in {"100007076", "100007079"},
    }
    model_reference = model_reference or {}
    single_handle_confirmed = bool(model_reference.get("single_handle_confirmed")) or any(
        token in blob
        for token in (
            "single handle",
            "single-handle",
            "single lever",
            "single-lever",
            "单柄",
            "单手柄",
        )
    )
    configuration_conflicts = {
        "dual_handle": bool(model_reference.get("dual_handle")) or any(
            token in blob
            for token in (
                "dual handle",
                "dual-handle",
                "double handle",
                "double-handle",
                "two handle",
                "two-handle",
                "双柄",
                "双手柄",
            )
        ),
        "add_on_spout": bool(model_reference.get("add_on_spout")) or any(
            token in blob
            for token in (
                "add-on spout",
                "add on spout",
                "swing spout",
                "pot filler",
                "6 inch",
                "6-inch",
                "6英寸",
                "加装嘴",
            )
        ),
    }
    strict_price_signals = {
        "single_handle_confirmed": single_handle_confirmed,
        "no_dual_handle_conflict": not configuration_conflicts["dual_handle"],
        "no_add_on_spout_conflict": not configuration_conflicts["add_on_spout"],
    }
    structured = (
        0.25 * signals["deck_mount"]
        + 0.25 * signals["single_hole"]
        + 0.20 * signals["stainless"]
        + 0.20 * signals["pre_rinse_complete_faucet"]
        + 0.10 * signals["category_match"]
    )
    final_score = 0.80 * structured + 0.10 * max(0.0, min(1.0, retrieval_score)) + 0.10 * max(0.0, min(1.0, visual_score))
    price = extract_price(row)
    moq = as_float(product.get("moq") if product.get("moq") is not None else row.get("minimum_order_quantity"))
    unit = normalized_text(price.get("unit"))
    price_eligible = (
        all(signals.values())
        and all(strict_price_signals.values())
        and price["low"] is not None
        and price["high"] is not None
        and (not unit or unit in {"unit", "units", "piece", "pieces"})
    )
    trade = extract_trade(row)
    reasons = [name for name, present in signals.items() if present]
    reasons.extend(name for name, present in strict_price_signals.items() if present)
    missing = [name for name, present in signals.items() if not present]
    if not single_handle_confirmed:
        missing.append("single_handle_not_confirmed")
    missing.extend(name for name, present in configuration_conflicts.items() if present)
    return {
        "rank_seed": rank,
        "product_id": str(row.get("product_id") or ""),
        "model": row.get("canonical_model") or row.get("raw_model") or "",
        "subject": row.get("subject") or row.get("title") or "",
        "detail_url": row.get("detail_url") or "",
        "gmt_modified_proxy": row.get("gmt_modified"),
        "record_disposition": row.get("record_disposition"),
        "allow_rag": row.get("allow_rag") is True,
        "category_id": product.get("leaf_category_id"),
        "minimum_order_quantity": moq,
        "trade_price_type": trade.get("tradePriceType") or "FOB_OBSERVED",
        "trade_available": trade.get("tradeAvailable"),
        "price": price,
        "signals": signals,
        "strict_price_signals": strict_price_signals,
        "configuration_conflicts": configuration_conflicts,
        "configuration_reference": model_reference,
        "structured_score": round(structured, 6),
        "retrieval_score": round(retrieval_score, 6),
        "visual_score": round(visual_score, 6),
        "final_score": round(final_score, 6),
        "price_eligible": price_eligible,
        "evidence_reasons": reasons,
        "conflicts_or_missing": missing,
        "source_file": row.get("source_file"),
        "source_file_sha256": row.get("source_file_sha256"),
        "review_status": "HUMAN_CONFIRM_REQUIRED",
        "copy_allowed": False,
    }


def verify_overlay(workspace: Path) -> dict[str, Any]:
    """Verify and content-rebind a versioned disposition overlay.

    The original overlay receipts intentionally preserve absolute provenance
    paths from the machine that built them.  Those strings are not authority
    after migration.  This verifier locates exactly one versioned sibling,
    validates every manifest entry and the catalog content hash, and returns a
    receipt bound to the current workspace path.  It never edits the sealed
    overlay and never weakens the cutoff/disposition controls.
    """

    workspace = workspace.resolve()
    overlay_roots = sorted(
        path.resolve()
        for path in workspace.glob("*__record_disposition_overlay__*")
        if path.is_dir()
    )
    if not overlay_roots:
        raise GuardError("no versioned disposition overlay found")
    # The version suffix is a UTC sortable build identifier.  Select by that
    # sealed identifier, never by filesystem mtime (which changes on copy).
    overlay = max(overlay_roots, key=lambda path: path.name)
    separator = "__record_disposition_overlay__"
    if separator not in overlay.name:
        raise GuardError("overlay directory is not a versioned sibling")
    base_root = workspace / overlay.name.split(separator, 1)[0]
    catalog_candidates = sorted(path.resolve() for path in base_root.rglob("catalog_index.jsonl") if path.is_file())
    if len(catalog_candidates) != 1:
        raise GuardError(f"expected exactly one catalog_index.jsonl under {base_root}, found {len(catalog_candidates)}")
    catalog = catalog_candidates[0]

    manifest_path = overlay / "03_audit" / "derived_manifest.json"
    sidecar_path = overlay / "03_audit" / "derived_manifest.json.sha256"
    binding_path = overlay / "01_date_evidence" / "base_catalog_binding.json"
    build_receipt_path = overlay / "03_audit" / "build_receipt.json"
    verification_path = overlay / "03_audit" / "verification_report.json"
    overlay_path = overlay / "02_overlay" / "record_disposition.jsonl"
    consumed_evidence_paths = (binding_path, overlay_path, build_receipt_path, verification_path)
    for required in (manifest_path, sidecar_path, *consumed_evidence_paths):
        if not required.is_file():
            raise GuardError(f"overlay evidence is missing: {required}")

    sidecar_tokens = sidecar_path.read_text(encoding="utf-8").strip().split()
    if not sidecar_tokens or sidecar_tokens[0].lower() != sha256_file(manifest_path):
        raise GuardError("derived overlay manifest sidecar hash mismatch")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("status") != "COMPLETE":
        raise GuardError("derived overlay manifest is not COMPLETE")
    files = manifest.get("files") or []
    if len(files) != int(manifest.get("file_count") or -1):
        raise GuardError("derived overlay manifest file count mismatch")
    seen_relative: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise GuardError("derived overlay manifest contains a non-object entry")
        relative = str(entry.get("relative_path") or "").replace("\\", "/")
        parts = Path(relative).parts
        if not relative or Path(relative).is_absolute() or ".." in parts or relative in seen_relative:
            raise GuardError(f"unsafe or duplicate overlay manifest path: {relative!r}")
        seen_relative.add(relative)
        candidate = (overlay / Path(*parts)).resolve()
        try:
            candidate.relative_to(overlay)
        except ValueError as exc:
            raise GuardError(f"overlay manifest path escapes root: {relative}") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise GuardError(f"overlay manifest file is missing or linked: {relative}")
        declared_bytes = entry.get("bytes")
        if type(declared_bytes) is not int or declared_bytes < 0 or candidate.stat().st_size != declared_bytes:
            raise GuardError(f"overlay manifest byte count mismatch: {relative}")
        if sha256_file(candidate) != str(entry.get("sha256") or "").lower():
            raise GuardError(f"overlay manifest hash mismatch: {relative}")

    # A valid seal over unrelated files cannot authorize unsealed inputs.  The
    # manifest is sealed by its sidecar, while the external catalog is bound
    # by the sealed binding file below.  Every other file consumed here must be
    # covered by an individually size/hash-verified entry in this manifest.
    required_relative = {path.relative_to(overlay).as_posix() for path in consumed_evidence_paths}
    missing_consumed = sorted(required_relative - seen_relative)
    if missing_consumed:
        raise GuardError(f"overlay manifest omits consumed evidence: {missing_consumed}")

    binding = read_json(binding_path)
    build_receipt = read_json(build_receipt_path)
    verification = read_json(verification_path)
    if not all(isinstance(value, dict) for value in (binding, build_receipt, verification)):
        raise GuardError("overlay binding or verification evidence is not a JSON object")
    expected_count = int(binding.get("expected_count") or -1)
    if expected_count < 1 or binding.get("cutoff") != DEFAULT_CUTOFF.isoformat():
        raise GuardError("overlay expected count or cutoff is invalid")
    if catalog.stat().st_size != int(binding.get("catalog_index_bytes") or -1):
        raise GuardError("relocated catalog byte count does not match sealed binding")
    catalog_hash = sha256_file(catalog)
    if catalog_hash != str(binding.get("catalog_index_sha256") or "").lower():
        raise GuardError("relocated catalog hash does not match sealed binding")

    catalog_rows = list(read_jsonl(catalog))
    overlay_rows = list(read_jsonl(overlay_path))
    if len(catalog_rows) != expected_count or len(overlay_rows) != expected_count:
        raise GuardError("catalog or overlay record count changed after sealing")
    catalog_ids = [str(row.get("product_id") or "") for row in catalog_rows]
    overlay_ids = [str(row.get("product_id") or "") for row in overlay_rows]
    if any(not value for value in catalog_ids) or len(set(catalog_ids)) != expected_count:
        raise GuardError("catalog product IDs are empty or duplicated")
    if overlay_ids != catalog_ids:
        raise GuardError("overlay product ID sequence does not match relocated catalog")

    checks = verification.get("checks") or {}
    if (
        verification.get("status") != "COMPLETE"
        or verification.get("failures")
        or not checks
        or not all(value is True for value in checks.values())
        or build_receipt.get("status") != "COMPLETE"
        or build_receipt.get("coverage_complete") is not True
        or build_receipt.get("external_write_enabled") is not False
        or build_receipt.get("online_write_performed") is not False
        or build_receipt.get("original_catalog_unchanged") is not True
    ):
        raise GuardError("sealed overlay verification controls are not complete")

    eligible_count = sum(
        1
        for row in overlay_rows
        if (row.get("record_disposition") or row.get("disposition")) == HISTORICAL_DISPOSITION
        and row.get("allow_rag") is True
    )
    unusable_count = expected_count - eligible_count
    observed_counts = [build_receipt.get(key) for key in ("eligible_count", "unusable_count", "record_count")]
    if any(type(value) is not int for value in observed_counts) or observed_counts != [eligible_count, unusable_count, expected_count]:
        raise GuardError("overlay eligibility totals changed after sealing")

    recorded_overlay_root = str(build_receipt.get("overlay_root") or "")
    return {
        "schema_version": "sellerkit.portable_overlay_verification.v1",
        "status": "COMPLETE",
        "errors": [],
        "verification_mode": "CONTENT_HASH_REBOUND",
        "overlay_root": str(overlay),
        "recorded_overlay_root_sha256": sha256_bytes(recorded_overlay_root.encode("utf-8")),
        "relocation_rebound": bool(recorded_overlay_root and Path(recorded_overlay_root) != overlay),
        "catalog_index": str(catalog),
        "catalog_index_sha256": catalog_hash,
        "derived_manifest_sha256": sha256_file(manifest_path),
        "record_count": expected_count,
        "eligible_count": eligible_count,
        "unusable_count": unusable_count,
        "cutoff": binding.get("cutoff"),
        "external_write_enabled": False,
        "online_write_performed": False,
    }


def load_pair_rank(path: Path, target_model: str) -> dict[str, tuple[int, float]]:
    ranks: dict[str, tuple[int, float]] = {}
    if not path.exists():
        return ranks
    for row in read_jsonl(path):
        official = row.get("official") or {}
        if str(official.get("canonical_model") or "").upper() != target_model.upper():
            continue
        product = row.get("alibaba") or {}
        product_id = str(product.get("product_id") or "")
        rank = int(row.get("rank") or 999999)
        retrieval = as_float(row.get("retrieval_score")) or 0.0
        ranks[product_id] = (rank, retrieval)
    return ranks


def load_visual_scores(path: Path, target_model: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    if not path.exists():
        return scores
    schemas = (
        ("official_model", "alibaba_product_id", "visual_score"),
        ("website_product_key", "alibaba_product_key", "mean_top3_cosine"),
    )
    for row in read_jsonl(path):
        schema = next(
            (
                candidate
                for candidate in schemas
                if all(key in row for key in candidate)
            ),
            None,
        )
        if schema is None:
            continue
        model_key, product_key, score_key = schema
        if str(row.get(model_key) or "").strip().upper() != target_model.strip().upper():
            continue
        product_id = str(row.get(product_key) or "").strip()
        value = as_float(row.get(score_key))
        if product_id and value is not None:
            scores[product_id] = value
    return scores


def load_official_product(path: Path, target_model: str) -> dict[str, Any]:
    matches = [row for row in read_jsonl(path) if str(row.get("canonical_model") or "").upper() == target_model.upper()]
    if len(matches) != 1:
        raise GuardError(f"expected one official row for {target_model}, found {len(matches)}")
    return matches[0]


def load_official_model_configurations(path: Path) -> dict[str, dict[str, Any]]:
    """Build an exact-model configuration index from current official rows.

    Historical Alibaba text frequently omits whether a pre-rinse faucet has an
    add-on spout or uses dual handles.  The current official product row can
    disqualify that historical model from the strict price lane, but it remains
    review-only evidence and never supplies a commercial value.
    """

    configurations: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        model = str(row.get("canonical_model") or "").strip().upper()
        if not model or model in configurations:
            continue
        attrs: dict[str, list[str]] = {}
        for item in row.get("attributes") or []:
            name = str((item or {}).get("name") or "").strip()
            value = str((item or {}).get("raw_value") or "").strip()
            if name and value:
                attrs.setdefault(name, []).append(value)
        temperature = " ".join(attrs.get("Temperature Control") or []).lower()
        add_on_values = attrs.get("Add-on Swing Nozzle") or []
        add_on_normalized = [normalized_text(value) for value in add_on_values]
        add_on_spout = any(
            value not in {"", "no", "none", "not applicable", "n/a", "/", "无", "否"}
            for value in add_on_normalized
        )
        source = row.get("source") or {}
        configurations[model] = {
            "model": model,
            "single_handle_confirmed": any(
                token in temperature for token in ("single-handle", "single handle", "single-lever", "single lever")
            ),
            "dual_handle": any(
                token in temperature for token in ("dual-handles", "dual handles", "dual-handle", "dual handle")
            ),
            "add_on_spout": add_on_spout,
            "add_on_spout_values": add_on_values,
            "temperature_control_values": attrs.get("Temperature Control") or [],
            "source_path": source.get("source_path") or str(path),
            "source_sha256": source.get("source_sha256"),
            "source_disposition": source.get("source_disposition") or "OFFICIAL_CURRENT_REVIEW_REQUIRED",
        }
    return configurations


def build_generic_title_candidate(official: dict[str, Any]) -> dict[str, Any]:
    """Build a review-only title from this product's own category and facts.

    Candidate generation deliberately has no product-family fallback.  In
    particular, it must never turn a missing category into a faucet title.
    Claims are admitted only when the source row carries an explicit approved
    title-claim decision with content and evidence bindings.
    """

    category_term = re.sub(r"\s+", " ", str(official.get("category") or "")).strip()
    if not category_term:
        raise GuardError("official product has no target category; refusing to guess a title family")

    facts: list[dict[str, str]] = []
    seen_values: set[str] = {normalized_text(category_term)}
    excluded_names = {
        "model no.",
        "model no",
        "model",
        "product size",
        "height",
        "hole size",
        "packing method",
    }
    preferred_names = (
        "Body Material",
        "Material",
        "Style",
        "Body Mount",
        "Installation Type",
        "Usage",
    )
    attributes = [row for row in official.get("attributes") or [] if isinstance(row, dict)]
    ordered_attributes = sorted(
        attributes,
        key=lambda row: (
            0 if str(row.get("name") or "") in preferred_names else 1,
            preferred_names.index(str(row.get("name") or ""))
            if str(row.get("name") or "") in preferred_names
            else int(row.get("position") or 10**9),
        ),
    )
    for row in ordered_attributes:
        name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
        value = re.sub(r"\s+", " ", str(row.get("raw_value") or "")).strip()
        normalized_value = normalized_text(value)
        if (
            not name
            or not value
            or normalized_text(name) in excluded_names
            or normalized_value in seen_values
            or len(value) > 70
            or len(value.split()) > 8
        ):
            continue
        facts.append({"attribute": name, "value": value})
        seen_values.add(normalized_value)
        if len(facts) == 3:
            break

    claims: list[dict[str, str]] = []
    for raw in official.get("title_claim_decisions") or []:
        if not isinstance(raw, dict) or raw.get("use_in_title") is not True:
            continue
        text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()
        decision_id = str(raw.get("decision_id") or "").strip()
        source_sha256 = str(raw.get("source_sha256") or "").strip().lower()
        if (
            raw.get("status") != "APPROVED"
            or not text
            or not decision_id
            or not re.fullmatch(r"[a-f0-9]{64}", source_sha256)
        ):
            raise GuardError("title claim marked for use lacks an explicit approved evidence binding")
        claims.append(
            {
                "text": text,
                "decision_id": decision_id,
                "source_sha256": source_sha256,
            }
        )

    terms: list[str] = []
    for value in [*(row["value"] for row in facts), category_term, *(row["text"] for row in claims)]:
        normalized_value = normalized_text(value)
        if normalized_value and normalized_value not in {normalized_text(term) for term in terms}:
            terms.append(value)
    title = " ".join(terms).strip()
    if not title:
        raise GuardError("category/fact/claim title derivation produced an empty value")
    return {
        "candidate_value": title,
        "category_term": category_term,
        "fact_terms": facts,
        "claim_terms": claims,
        "derivation": "TARGET_CATEGORY_PLUS_OFFICIAL_FACTS_PLUS_EXPLICIT_APPROVED_CLAIMS",
    }


def official_field_candidates(official: dict[str, Any], source_path: Path) -> list[dict[str, Any]]:
    attrs: dict[str, list[str]] = {}
    for row in official.get("attributes") or []:
        name = str((row or {}).get("name") or "").strip()
        value = str((row or {}).get("raw_value") or "").strip()
        if name and value:
            attrs.setdefault(name, []).append(value)
    mapping = [
        ("model", "Model No."),
        ("style", "Style"),
        ("body_mount", "Body Mount"),
        ("hole_size", "Hole Size"),
        ("height", "Height"),
        ("body_material", "Body Material"),
        ("temperature_control", "Temperature Control"),
        ("hose", "Hose"),
        ("wall_bracket", "Wall Bracket"),
    ]
    source = official.get("source") or {}
    bound_source_path = str(source.get("source_path") or source_path)
    disposition = source.get("source_disposition") or "UNKNOWN"
    fields: list[dict[str, Any]] = []
    title_decision = build_generic_title_candidate(official)
    fields.append(
        {
            "field": "product_title",
            "candidate_value": title_decision["candidate_value"],
            "evidence_value": official.get("name"),
            "source_path": bound_source_path,
            "source_sha256": source.get("source_sha256"),
            "source_disposition": disposition,
            "decision": "PENDING",
            "write_allowed": False,
            "title_candidate_context": title_decision,
            "note": "Category/fact/approved-claim candidate only; review against the current dynamic schema and locale rules.",
        }
    )
    for logical_name, attr_name in mapping:
        values = attrs.get(attr_name) or []
        fields.append(
            {
                "field": logical_name,
                "candidate_value": values[0] if len(values) == 1 else values,
                "evidence_value": values,
                "source_path": bound_source_path,
                "source_sha256": source.get("source_sha256"),
                "source_disposition": disposition,
                "decision": "PENDING",
                "write_allowed": False,
                "note": "Conflicting values require explicit selection." if len(set(values)) > 1 else "Official-current candidate; source still requires review.",
            }
        )

    spray_values = list(dict.fromkeys(attrs.get("Spray Valve") or []))
    dual_modes = list(dict.fromkeys(attrs.get("Dual-function Spray Valve Flow Mode") or []))
    single_modes = list(dict.fromkeys(attrs.get("Single-function Spray Valve Flow Mode") or []))

    # Do not carry a known product's spray model or material into a generic
    # candidate.  Resolution is allowed only when the current target's own
    # structured attributes establish one dual option as standard and one
    # single option as optional.  The model codes, when present, are parsed
    # from those same raw attribute values rather than supplied as constants.
    dual_spray_values = [value for value in spray_values if "dual" in value.casefold()]
    single_spray_values = [value for value in spray_values if "single" in value.casefold()]
    standard_dual_modes = [value for value in dual_modes if "standard" in value.casefold()]
    optional_single_modes = [value for value in single_modes if "optional" in value.casefold()]
    dual_has_conflicting_marker = any("optional" in value.casefold() for value in dual_modes)
    single_has_conflicting_marker = any("standard" in value.casefold() for value in single_modes)
    all_spray_values_classified = len(dual_spray_values) + len(single_spray_values) == len(spray_values)
    spray_semantics_resolved = bool(
        len(dual_spray_values) == 1
        and len(single_spray_values) == 1
        and len(standard_dual_modes) == 1
        and len(optional_single_modes) == 1
        and not dual_has_conflicting_marker
        and not single_has_conflicting_marker
        and all_spray_values_classified
    )

    def model_number_from_mode(value: str) -> str | None:
        match = re.search(
            r"\bmodel\s*(?:no\.?|number)?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)",
            value,
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else None

    if spray_semantics_resolved:
        standard_value = dual_spray_values[0]
        optional_value = single_spray_values[0]
        standard_model = model_number_from_mode(standard_dual_modes[0])
        optional_model = model_number_from_mode(optional_single_modes[0])
        spray_candidate_parts = [f"{standard_value} (Standard)"]
        if standard_model:
            spray_candidate_parts.append(standard_model)
        spray_candidate: Any = ", ".join(spray_candidate_parts)
        standard_label = f"{standard_value}{f' [{standard_model}]' if standard_model else ''}"
        optional_label = f"{optional_value}{f' [{optional_model}]' if optional_model else ''}"
        spray_note = (
            f"Resolved only from this target's structured attributes: {standard_label} is standard; "
            f"{optional_label} is optional and must not be written as the installed standard feature."
        )
    else:
        spray_candidate = None
        spray_note = (
            "Standard/optional spray-valve semantics are not uniquely established by this target's structured "
            "attributes; candidate_value is intentionally empty and explicit sourced selection is required."
        )
    fields.append(
        {
            "field": "spray_valve",
            "candidate_value": spray_candidate,
            "evidence_value": {
                "spray_valve_options": spray_values,
                "dual_function_flow_mode": dual_modes,
                "single_function_flow_mode": single_modes,
            },
            "source_path": bound_source_path,
            "source_sha256": source.get("source_sha256"),
            "source_disposition": disposition,
            "decision": "PENDING",
            "write_allowed": False,
            "configuration_resolution": (
                "STANDARD_DUAL_OPTIONAL_SINGLE" if spray_semantics_resolved else "UNRESOLVED"
            ),
            "note": spray_note,
        }
    )
    return fields


def inspect_source_inventory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "MISSING_INVENTORY", "sources": []}
    data = read_json(path)
    sources = data.get("sources") or []
    rows = []
    for source in sources:
        source_path = Path(str(source.get("path") or ""))
        exists = source_path.is_file()
        observed_hash = sha256_file(source_path) if exists else None
        rows.append(
            {
                "source_id": source.get("source_id"),
                "path": str(source_path),
                "exists": exists,
                "recorded_sha256": source.get("sha256"),
                "observed_sha256": observed_hash,
                "hash_match": bool(exists and source.get("sha256") and observed_hash == source.get("sha256")),
            }
        )
    return {
        "status": "PASS" if rows and all(row["hash_match"] for row in rows) else "SOURCE_CHAIN_INCOMPLETE",
        "sources": rows,
    }


def build_asset_candidates(workspace: Path, target_model: str, workers: int) -> list[dict[str, Any]]:
    review_root = workspace / "sellerkit-alibaba-review-ready-v1"
    source_rows: list[dict[str, Any]] = []
    for relative in ("work/website_main_images.jsonl", "work/website_gallery_images.jsonl"):
        path = review_root / relative
        for row in read_jsonl(path):
            if str(row.get("canonical_model") or "").upper() == target_model.upper():
                source_rows.append(
                    {
                        "asset_id": row.get("reference_id") or row.get("sha256"),
                        "category": row.get("role"),
                        "local_path": row.get("local_file"),
                        "recorded_sha256": row.get("sha256"),
                        "source_url": row.get("source_url"),
                        "source_type": "OFFICIAL_CURRENT_IMAGE",
                        "exact_model": True,
                        "default_decision": "PENDING",
                    }
                )
    product_lock_paths = list(
        workspace.glob(f"{target_model}*/local_matching/product_lock/product_lock_manifest.json")
    )
    if len(product_lock_paths) > 1:
        raise GuardError(f"multiple product-lock manifests found for {target_model}")
    if product_lock_paths:
        product_lock_path = product_lock_paths[0].resolve()
        product_lock = read_json(product_lock_path)
        if not isinstance(product_lock, dict):
            raise GuardError(f"invalid product-lock manifest: {product_lock_path}")
        if str(product_lock.get("model") or "").upper() != target_model.upper():
            raise GuardError(f"product-lock model mismatch: {product_lock_path}")
        manifest_hash = sha256_file(product_lock_path)
        immutable = product_lock.get("immutable") is True
        operation = str(product_lock.get("operation") or "")
        for view in product_lock.get("views") or []:
            if not isinstance(view, dict):
                raise GuardError(f"invalid product-lock view: {product_lock_path}")
            source_path = Path(str(view.get("source_path") or ""))
            source_exists = source_path.is_file()
            source_observed = sha256_file(source_path) if source_exists else None
            source_recorded = str(view.get("source_sha256") or "").lower() or None
            source_hash_match = bool(source_exists and source_recorded and source_observed == source_recorded)
            derivation_asserted = bool(
                immutable
                and view.get("pixel_identical_after_lossless_crop") is True
                and "lossless alpha crop only" in operation.lower()
            )
            source_size = view.get("source_size")
            if (
                isinstance(source_size, list)
                and len(source_size) == 2
                and all(isinstance(value, int) and value > 0 for value in source_size)
            ):
                source_size_label = f"{source_size[0]}x{source_size[1]}"
            else:
                source_size_label = "dimension-unspecified"
            source_rows.append(
                {
                    "asset_id": f"{target_model}-product-lock-{view.get('view') or 'view'}",
                    "category": f"product_lock_{view.get('view') or 'view'}",
                    "local_path": view.get("product_layer_path"),
                    "recorded_sha256": view.get("product_layer_sha256"),
                    "source_url": None,
                    "source_type": "HASH_BOUND_PRODUCT_LOCK",
                    "exact_model": True,
                    "default_decision": "PENDING" if derivation_asserted else "REJECT",
                    "provenance_manifest_path": str(product_lock_path),
                    "provenance_manifest_sha256": manifest_hash,
                    "provenance_operation": operation,
                    "provenance_source_path": str(source_path),
                    "provenance_source_recorded_sha256": source_recorded,
                    "provenance_source_observed_sha256": source_observed,
                    "provenance_source_exists": source_exists,
                    "provenance_source_hash_match": source_hash_match,
                    "provenance_source_size": source_size if isinstance(source_size, list) else None,
                    "source_chain_status": (
                        "COMPLETE" if source_hash_match else "ORIGINAL_SOURCE_MISSING_HASH_BOUND_DERIVATION"
                    ),
                    "provenance_note": (
                        f"Lossless-crop manifest is hash-bound, but the original {source_size_label} source is unavailable; "
                        "human review is mandatory."
                        if not source_hash_match
                        else "Original and derived hashes are both verified."
                    ),
                }
            )

    def inspect(row: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(row.get("local_path") or ""))
        exists = path.is_file()
        observed = sha256_file(path) if exists else None
        result = dict(row)
        result.update(
            {
                "exists": exists,
                "observed_sha256": observed,
                "hash_match": bool(exists and (not row.get("recorded_sha256") or observed == row.get("recorded_sha256"))),
                "bytes": path.stat().st_size if exists else None,
                "review_status": row.get("default_decision"),
                "upload_allowed": False,
            }
        )
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        inspected = list(executor.map(inspect, source_rows))
    deduped: dict[str, dict[str, Any]] = {}
    for row in inspected:
        key = row.get("observed_sha256") or str(row.get("local_path") or row.get("asset_id"))
        if key not in deduped:
            deduped[key] = row
    return list(deduped.values())


def classify_endpoint(
    policy: dict[str, Any],
    url: str,
    method: str,
    *,
    body_present: bool = False,
    headers: dict[str, Any] | None = None,
    redirected: bool = False,
    credential_present: bool = False,
    storage_accessed: bool = False,
    bundle_sha256: str | None = None,
    action: str | None = None,
    dom_intent: str | None = None,
    blockers: list[Any] | None = None,
    evidence_id: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    method = method.upper().strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query, keep_blank_values=True)
    safe_query_keys = {"behavior", "itemId", "optType", "pubType", "step"}
    query_shape = {
        "keys": sorted(query),
        "safe_values": {
            key: sorted(set(query[key]))
            for key in sorted(query)
            if key in safe_query_keys
        },
        "sensitive_value_sha256": {
            key: sorted({sha256_bytes(value.encode("utf-8")) for value in query[key]})
            for key in sorted(query)
            if key not in safe_query_keys
        },
    }
    result = {
        "url_shape": {
            "scheme": parsed.scheme.lower(),
            "host": host,
            "port": parsed.port,
            "path": parsed.path,
            "query": query_shape,
        },
        "method": method,
        "classification": None,
        "decision": "DENY",
        "rule_id": None,
        "reason": None,
    }
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or host not in set(policy.get("allowed_domains") or [])
        or parsed.username is not None
        or parsed.password is not None
    ):
        result.update(classification="DENY_ORIGIN", reason="scheme, port, or host is not exact allowlisted origin")
        return result
    if redirected:
        result.update(classification="DENY_REDIRECT", reason="redirected requests are never allowlisted")
        return result
    normalized_headers = {str(key).lower(): value for key, value in (headers or {}).items()}
    sensitive_headers = {"cookie", "authorization", "x-csrf-token", "x-xsrf-token", "x-auth-token"}
    if credential_present or storage_accessed or sensitive_headers.intersection(normalized_headers):
        result.update(classification="DENY_CREDENTIAL_SOURCE", reason="credential or browser storage use is forbidden")
        return result
    if {"x-http-method-override", "x-method-override"}.intersection(normalized_headers):
        result.update(classification="DENY_METHOD_SHAPE", reason="method override header is forbidden")
        return result
    raw_path = parsed.path
    decoded = unquote(raw_path)
    twice_decoded = unquote(decoded)
    lowered_raw = raw_path.lower()
    if (
        ".." in decoded
        or ".." in twice_decoded
        or "//" in decoded
        or "\\" in decoded
        or any(token in lowered_raw for token in ("%2f", "%5c", "%2e", "%25"))
    ):
        result.update(classification="DENY_PATH_NORMALIZATION", reason="ambiguous or encoded path")
        return result
    for key in ("_method", "methodOverride", "httpMethod"):
        if key in query:
            result.update(classification="DENY_METHOD_SHAPE", reason="method override is forbidden")
            return result
    for key, values in query.items():
        if len(values) > 1 and len(set(values)) > 1:
            result.update(classification="DENY_QUERY", reason=f"conflicting repeated query key: {key}")
            return result
    if method in READ_METHODS and body_present:
        result.update(classification="DENY_METHOD_SHAPE", reason="read method with body is forbidden")
        return result
    if blockers:
        result.update(classification="DENY_GATE", reason="page blockers are present")
        return result
    if action and dom_intent and normalized_text(action) != normalized_text(dom_intent):
        result.update(classification="DENY_ACTION_MISMATCH", reason="requested action does not match live DOM intent")
        return result
    if normalized_text(dom_intent) in {"stepsubmit", "final submit", "publish", "submit"}:
        result.update(classification="FINAL_WRITE", decision="HARD_DENY", reason="final UI intent is permanently forbidden")
        return result
    if method in WRITE_METHODS and content_type and normalized_text(content_type).split(";", 1)[0] not in {
        "application/json",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    }:
        result.update(classification="DENY_DEFAULT", reason="unexpected write content type")
        return result
    target = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
    # A final-write route is permanently denied before method/evidence-state
    # handling.  No policy typo, UNKNOWN method, or future adapter candidate may
    # weaken this invariant.
    for rule in policy.get("rules") or []:
        if rule.get("classification") != "FINAL_WRITE":
            continue
        final_methods = {str(item).upper() for item in rule.get("methods") or []}
        if final_methods and method not in final_methods and "UNKNOWN" not in final_methods:
            continue
        if re.search(str(rule.get("url_regex") or r"a^"), target):
            result.update(
                classification="FINAL_WRITE",
                decision="HARD_DENY",
                rule_id=rule.get("id"),
                reason="final write is permanently forbidden",
            )
            return result
    for rule in policy.get("rules") or []:
        methods = {str(item).upper() for item in rule.get("methods") or []}
        if methods and method not in methods and "UNKNOWN" not in methods:
            continue
        if re.search(str(rule.get("url_regex") or r"a^"), target):
            classification = rule.get("classification")
            state = rule.get("evidence_state", "ACTIVE")
            if rule.get("query_allowlist") is not None and not set(query).issubset(set(rule.get("query_allowlist") or [])):
                result.update(classification="DENY_QUERY", rule_id=rule.get("id"), reason="query key is not allowlisted")
                return result
            allowed_values = rule.get("query_values") or {}
            for key, allowed in allowed_values.items():
                if key in query and not set(query[key]).issubset(set(allowed or [])):
                    result.update(classification="DENY_QUERY", rule_id=rule.get("id"), reason=f"query value is not allowlisted: {key}")
                    return result
            expected_bundle = rule.get("bundle_sha256")
            if expected_bundle and bundle_sha256 != expected_bundle:
                result.update(classification="DENY_EVIDENCE_DRIFT", rule_id=rule.get("id"), reason="bundle body hash missing or mismatched")
                return result
            if rule.get("evidence_id_required") is True and not evidence_id:
                result.update(classification="DENY_DEFAULT", rule_id=rule.get("id"), reason="missing evidence id")
                return result
            if methods == {"UNKNOWN"}:
                result.update(
                    classification="DENY_METHOD_UNVERIFIED",
                    decision="DENY",
                    rule_id=rule.get("id"),
                    reason="route is frozen but HTTP method is unverified",
                    policy_classification=classification,
                )
                return result
            decision = "HARD_DENY" if classification == "FINAL_WRITE" else "DENY"
            if classification == "READ" and state == "LIVE_VERIFIED_ACTIVE":
                decision = "ALLOW"
            elif classification == "REVERSIBLE_DRAFT_WRITE" and state == "LIVE_VERIFIED_ACTIVE" and rule.get("ui_only") is True:
                decision = "ALLOW_UI_ONLY"
            result.update(classification=classification, decision=decision, rule_id=rule.get("id"), reason=state)
            return result
    if method in WRITE_METHODS:
        result.update(classification=policy.get("default_write_classification", "DENY_UNVERIFIED_WRITE"), reason="no active exact write rule")
    else:
        result.update(classification=policy.get("default_read_classification", "DENY_UNVERIFIED_ROUTE"), reason="read route not live-verified")
    return result


def approved_asset_sequence_sha256(assets: Iterable[dict[str, Any]]) -> str:
    sequence = [
        {
            "asset_id": row.get("asset_id"),
            "role": row.get("role"),
            "upload_order": row.get("upload_order"),
            "sha256": row.get("sha256"),
        }
        for row in sorted(assets, key=lambda item: (int(item.get("upload_order") or 0), str(item.get("asset_id") or "")))
    ]
    return sha256_bytes(canonical_json(sequence).encode("utf-8"))


def _valid_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "")))


def _approval_errors(document: dict[str, Any], scope: str, candidate_package_sha256: Any, label: str) -> list[str]:
    errors: list[str] = []
    approval = document.get("approval") or {}
    if approval.get("status") != "APPROVED" or approval.get("explicit") is not True:
        errors.append(f"{label} approval is not explicit APPROVED")
    if approval.get("scope") != scope:
        errors.append(f"{label} approval scope mismatch")
    if approval.get("origin") != "HUMAN_REVIEWED_CANDIDATE_PACKAGE":
        errors.append(f"{label} approval origin mismatch")
    for key in ("decision_id", "approved_by", "approved_at"):
        if not approval.get(key):
            errors.append(f"{label} approval missing {key}")
    if not _valid_sha256(candidate_package_sha256):
        errors.append(f"{label} candidate package hash is invalid")
    if approval.get("approval_package_sha256") != candidate_package_sha256:
        errors.append(f"{label} approval package hash binding mismatch")
    return errors


def canonical_registry_field(field: str) -> str:
    return FIELD_REGISTRY_ALIASES.get(field, field)


def duplicate_registry_targets(fields: Iterable[str]) -> dict[str, list[str]]:
    grouped: dict[str, set[str]] = {}
    for field in fields:
        grouped.setdefault(canonical_registry_field(field), set()).add(field)
    return {target: sorted(names) for target, names in grouped.items() if len(names) > 1}


def canonical_commercial_field(field: str) -> str | None:
    return QUALIFIED_COMMERCIAL_REGISTRY_FIELDS.get(canonical_registry_field(field))


def commercial_registry_fields(registry: dict[str, Any] | None = None) -> set[str]:
    if registry is None:
        registry = read_json(Path(__file__).resolve().parent.parent / "references" / "publish-field-registry.v1.json")
    names = {
        str(row["logical_name"])
        for row in registry.get("fields") or []
        if row.get("source_policy") == "APPROVED_COMMERCIAL_DECISION"
    }
    return names | {alias for alias, target in FIELD_REGISTRY_ALIASES.items() if target in names}


def _finite_commercial_number(value: Any) -> bool:
    # bool is an int subclass in Python; equality alone would accept True == 1.
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _commercial_value_equal(value: Any, expected: Any) -> bool:
    if isinstance(value, bool) or isinstance(expected, bool):
        return False
    if type(value) in (int, float) or type(expected) in (int, float):
        return _finite_commercial_number(value) and _finite_commercial_number(expected) and value == expected
    if isinstance(expected, str):
        return isinstance(value, str) and value == expected
    if isinstance(expected, list):
        return isinstance(value, list) and len(value) == len(expected) and all(
            _commercial_value_equal(actual, reference) for actual, reference in zip(value, expected)
        )
    if isinstance(expected, dict):
        return isinstance(value, dict) and value.keys() == expected.keys() and all(
            _commercial_value_equal(value[key], expected[key]) for key in expected
        )
    return False


def commercial_value_matches(field: str, value: Any, commercial: dict[str, Any]) -> bool:
    """Bind numeric value and any supplied commercial context to the sealed file."""
    canonical_field = canonical_commercial_field(field)
    if canonical_field == "price":
        expected = commercial.get("price") or {}
        if not _finite_commercial_number(expected.get("approved_value")):
            return False
        if isinstance(value, dict):
            # Compact or full price objects are supported, but supplied keys
            # cannot introduce a new currency/unit/term or alternate value.
            return "approved_value" in value and value.keys() <= expected.keys() and all(
                _commercial_value_equal(actual, expected[key]) for key, actual in value.items()
            )
        return _commercial_value_equal(value, expected["approved_value"])
    if canonical_field == "minimum_order_quantity":
        expected = commercial.get("minimum_order_quantity") or {}
        if not _finite_commercial_number(expected.get("value")):
            return False
        return _commercial_value_equal(value, expected if isinstance(value, dict) else expected["value"])
    return False


def _validate_commercial_decision(path: Path, target_model: str) -> list[str]:
    errors: list[str] = []
    try:
        decision = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"commercial decision is not readable JSON: {exc}"]
    from local_schema_validation import local_schema_errors

    schema_errors = local_schema_errors(
        decision, Path(__file__).resolve().parent.parent / "references" / "schemas" / "approved-commercial-decision.schema.json"
    )
    if schema_errors:
        errors = [f"commercial decision canonical schema: {error}" for error in schema_errors]
        if any("candidate_product_ids" in error for error in schema_errors):
            errors.append("price requires an array of at least three distinct approved comparable products")
        return errors
    if not isinstance(decision, dict) or decision.get("schema_version") != "sellerkit.approved-commercial-decision.v1":
        return ["commercial decision schema is unsupported"]
    if str(decision.get("target_model") or "").upper() != target_model:
        errors.append("commercial decision target model mismatch")
    if decision.get("status") != "APPROVED" or not decision.get("approved_by") or not decision.get("approved_at"):
        errors.append("commercial decision lacks explicit approval audit fields")
    price = decision.get("price") or {}
    ids = {str(value) for value in price.get("candidate_product_ids") or [] if str(value).isdigit()}
    if len(ids) < 3:
        errors.append("price requires at least three distinct approved comparable products")
    if price.get("pricing_method") != "WEIGHTED_P25_P50_P75":
        errors.append("price pricing_method must be WEIGHTED_P25_P50_P75")
    if not price.get("currency") or not price.get("unit") or not price.get("trade_term"):
        errors.append("price unit/currency/trade-term normalization is incomplete")
    for key in ("p25", "p50", "p75", "approved_value"):
        if not _finite_commercial_number(price.get(key)) or price[key] < 0:
            errors.append(f"commercial price missing valid {key}")
    p25, p50, p75, approved_value = (
        price.get(key) if _finite_commercial_number(price.get(key)) else None
        for key in ("p25", "p50", "p75", "approved_value")
    )
    if None not in (p25, p50, p75) and not (p25 <= p50 <= p75):
        errors.append("commercial price percentiles must satisfy p25 <= p50 <= p75")
    if None not in (p25, p75, approved_value) and not (p25 <= approved_value <= p75):
        errors.append("commercial approved price must stay inside p25/p75")
    moq = decision.get("minimum_order_quantity") or {}
    if not _finite_commercial_number(moq.get("value")) or moq["value"] <= 0 or not moq.get("unit"):
        errors.append("commercial MOQ is incomplete")
    if decision.get("historical_values_are_quote") is not False:
        errors.append("commercial decision must state historical_values_are_quote=false")
    if decision.get("final_write_enabled") is not False:
        errors.append("commercial decision final_write_enabled must be false")
    return errors


def validate_approved_manifest(
    manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    *,
    manifest_path: Path | None = None,
    asset_manifest_path: Path | None = None,
) -> dict[str, Any]:
    del asset_manifest_path  # Reserved for a future receipt binding; no implicit write authorization.
    errors: list[str] = []
    approved_fields: set[str] = set()
    approved_assets: list[dict[str, Any]] = []
    source_by_id: dict[str, dict[str, Any]] = {}
    dynamic_required: set[str] = set()
    target_model = str(manifest.get("target_model") or "").strip().upper()

    source_schema_ok = manifest.get("schema_version") == "alibaba.approved-source-manifest.v1"
    if not source_schema_ok:
        errors.append("unsupported canonical source manifest schema")
    else:
        if not target_model:
            errors.append("target_model is required")
        bindings = manifest.get("bindings") or {}
        candidate_package_sha256 = bindings.get("candidate_package_sha256")
        errors.extend(
            _approval_errors(
                manifest,
                "SIMILAR_DRAFT_SOURCE_USE_ONLY",
                candidate_package_sha256,
                "source manifest",
            )
        )
        if str(bindings.get("target_model_normalized") or "").strip().upper() != target_model:
            errors.append("source manifest target binding mismatch")
        if not str(bindings.get("template_product_id") or "").isdigit():
            errors.append("source manifest template product id is required")
        for key in ("source_inventory_sha256", "field_registry_sha256", "schema_bundle_sha256"):
            if not _valid_sha256(bindings.get(key)):
                errors.append(f"source manifest binding has invalid {key}")
        write_policy = manifest.get("write_policy") or {}
        if (
            write_policy.get("candidate_external_write_enabled") is not False
            or write_policy.get("draft_save_requires_separate_approval") is not True
            or write_policy.get("final_write_enabled") is not False
            or write_policy.get("raw_disposition_may_grant_approval") is not False
        ):
            errors.append("source manifest write policy is unsafe")

        sources = manifest.get("sources") or []
        if manifest.get("source_count") != len(sources) or manifest.get("rejected_source_count") != 0:
            errors.append("source manifest counts are inconsistent")
        commercial_paths: set[Path] = set()
        for source in sources:
            source_id = str((source or {}).get("source_id") or "")
            if not source_id or source_id in source_by_id:
                errors.append(f"duplicate or empty approved source id: {source_id!r}")
                continue
            source_by_id[source_id] = source
            if source.get("record_disposition") != "APPROVED_INTERNAL_SOURCE":
                errors.append(f"source is not APPROVED_INTERNAL_SOURCE: {source_id}")
            if not source.get("decision_id"):
                errors.append(f"source missing decision id: {source_id}")
            usage = source.get("usage_policy") or {}
            if (
                usage.get("allow_rag") is not True
                or usage.get("fact_extraction") is not True
                or usage.get("publish_draft") is not True
                or usage.get("auto_listing") is not False
                or usage.get("external_disclosure") is not False
                or usage.get("final_write") is not False
            ):
                errors.append(f"source usage policy is unsafe: {source_id}")
            fields = {str(value) for value in source.get("approved_fields") or [] if str(value)}
            approved_fields.update(fields)
            path = Path(str(source.get("path") or ""))
            if not path.is_absolute() or not path.is_file():
                errors.append(f"source path is not an existing absolute file: {source_id}")
            elif (
                not _valid_sha256(source.get("sha256"))
                or sha256_file(path) != str(source.get("sha256")).lower()
                or source.get("bytes") is None
                or path.stat().st_size != int(source.get("bytes"))
            ):
                errors.append(f"source hash/size mismatch: {source_id}")
            elif source.get("source_role") == "COMMERCIAL_DECISION":
                commercial_paths.add(path)

        duplicate_targets = duplicate_registry_targets(approved_fields)
        if duplicate_targets:
            errors.append(f"duplicate approved registry targets after alias normalization: {duplicate_targets}")

        dynamic_schema = manifest.get("dynamic_schema") or {}
        if dynamic_schema.get("status") != "CURRENT_SCHEMA_BOUND":
            errors.append("source manifest lacks a current dynamic schema binding")
        else:
            try:
                from dynamic_schema_gate import validate_dynamic_schema_capture

                capture = dynamic_schema.get("capture") or {}
                capture_path = Path(str(capture.get("path") or ""))
                if (
                    not capture_path.is_absolute()
                    or not capture_path.is_file()
                    or capture_path.stat().st_size != int(capture.get("bytes") or -1)
                    or sha256_file(capture_path) != str(capture.get("sha256") or "").lower()
                ):
                    raise GuardError("dynamic schema capture file binding mismatch")
                observed_dynamic_schema = validate_dynamic_schema_capture(
                    capture_path,
                    expected_target_model=target_model,
                    expected_template_product_id=str(bindings.get("template_product_id") or ""),
                    expected_run_id=str(manifest.get("run_id") or ""),
                )
                for key in (
                    "capture_id",
                    "category_id",
                    "product_type",
                    "captured_at",
                    "expires_at",
                    "field_ids",
                    "required_field_ids",
                    "field_set_sha256",
                    "required_field_set_sha256",
                    "source_kind",
                    "final_write_enabled",
                ):
                    if dynamic_schema.get(key) != observed_dynamic_schema.get(key):
                        errors.append(f"dynamic schema manifest binding mismatch for {key}")
                if dynamic_schema.get("raw_payload") != observed_dynamic_schema.get("raw_payload"):
                    errors.append("dynamic schema raw payload binding mismatch")
                dynamic_required = set(observed_dynamic_schema["required_field_ids"])
            except (GuardError, OSError, TypeError, ValueError) as exc:
                errors.append(f"dynamic schema verification failed: {exc}")
        field_policy = manifest.get("draft_field_policy") or {}
        partial_mode = field_policy.get("mode") == "PARTIAL_DRAFT_FILL_APPROVED_ONLY"
        if partial_mode:
            blocked_fields = {str(value) for value in field_policy.get("blocked_fields") or [] if str(value)}
            if not _valid_sha256(field_policy.get("explicit_user_instruction_sha256")):
                errors.append("partial draft policy has invalid user instruction hash")
            if (
                field_policy.get("save_incomplete_draft_allowed") is not True
                or field_policy.get("publish_ready_claim_allowed") is not False
                or field_policy.get("final_write_enabled") is not False
            ):
                errors.append("partial draft policy is unsafe")
            overlap = sorted(
                {canonical_registry_field(field) for field in blocked_fields}
                & {canonical_registry_field(field) for field in approved_fields}
            )
            if overlap:
                errors.append(f"partial draft blocked fields were also approved: {overlap}")
            required = dynamic_required - blocked_fields
        else:
            required = dynamic_required
        missing = sorted(required - approved_fields)
        if missing:
            errors.append(f"missing current-schema required fields: {missing}")
        commercial_fields = commercial_registry_fields()
        approved_commercial_fields = commercial_fields & approved_fields
        approved_field_values = manifest.get("approved_field_values") or {}
        qualified_commercial_documents: dict[Path, dict[str, Any]] = {}
        if approved_commercial_fields:
            for path in commercial_paths:
                qualification_errors = _validate_commercial_decision(path, target_model)
                errors.extend(qualification_errors)
                if not qualification_errors:
                    qualified_commercial_documents[path] = read_json(path)
        for field in sorted(approved_commercial_fields):
            if canonical_commercial_field(field) is None:
                errors.append(f"commercial field has no supported qualified decision schema and must be blocked: {field}")
                continue
            field_sources = [
                source
                for source in source_by_id.values()
                if field in set(source.get("approved_fields") or [])
            ]
            if not any(
                source.get("source_role") == "COMMERCIAL_DECISION"
                and Path(str(source.get("path") or "")) in commercial_paths
                for source in field_sources
            ):
                errors.append(f"{field} requires an approved COMMERCIAL_DECISION source")
            # An unrelated qualified commercial source must not launder a
            # fact-source approval for this particular commercial field.
            for source in field_sources:
                if source.get("source_role") != "COMMERCIAL_DECISION":
                    errors.append(f"{field} source must be COMMERCIAL_DECISION: {source.get('source_id')}")
                    continue
                document = qualified_commercial_documents.get(Path(str(source.get("path") or "")))
                if document is not None and source.get("decision_id") != document.get("decision_id"):
                    errors.append(f"commercial decision_id mismatch for {field}: {source.get('source_id')}")
            field_decision = approved_field_values.get(field)
            if isinstance(field_decision, dict):
                value_source = source_by_id.get(str(field_decision.get("source_id") or "")) or {}
                document = qualified_commercial_documents.get(Path(str(value_source.get("path") or "")))
                if value_source.get("source_role") == "COMMERCIAL_DECISION" and document is not None:
                    if not commercial_value_matches(field, field_decision.get("value"), document):
                        errors.append(f"commercial value mismatch for {field}")

        if partial_mode:
            writable_fields = {
                field
                for field in approved_fields
                if not field.startswith(("main_image.", "detail_image.", "main_video.", "detail_video."))
            }
            missing_values = sorted(writable_fields - set(approved_field_values))
            if missing_values:
                errors.append(f"partial draft missing approved field values: {missing_values}")
            for field, decision in approved_field_values.items():
                if field not in approved_fields:
                    errors.append(f"approved field value is outside approved source scope: {field}")
                    continue
                source = source_by_id.get(str((decision or {}).get("source_id") or ""))
                if source is None or field not in set(source.get("approved_fields") or []):
                    errors.append(f"approved field value source binding mismatch: {field}")
                    continue
                if (decision or {}).get("decision_id") != source.get("decision_id"):
                    errors.append(f"approved field value decision binding mismatch: {field}")
                value = (decision or {}).get("value")
                expected_value_hash = sha256_bytes(canonical_json(value).encode("utf-8"))
                if (decision or {}).get("value_sha256") != expected_value_hash:
                    errors.append(f"approved field value hash mismatch: {field}")
                if value is None or (isinstance(value, str) and not value.strip()):
                    errors.append(f"approved field value is empty: {field}")

    asset_schema_ok = asset_manifest.get("schema_version") == "alibaba.approved-asset-manifest.v1"
    if not asset_schema_ok:
        errors.append("unsupported canonical asset manifest schema")
    else:
        asset_target_model = str(asset_manifest.get("target_model") or "").strip().upper()
        if asset_target_model != target_model:
            errors.append("asset manifest target model mismatch")
        asset_bindings = asset_manifest.get("bindings") or {}
        candidate_package_sha256 = asset_bindings.get("candidate_package_sha256")
        errors.extend(
            _approval_errors(
                asset_manifest,
                "SIMILAR_DRAFT_ASSET_USE_ONLY",
                candidate_package_sha256,
                "asset manifest",
            )
        )
        source_bindings = manifest.get("bindings") or {}
        if candidate_package_sha256 != source_bindings.get("candidate_package_sha256"):
            errors.append("source and asset candidate package bindings differ")
        for key in ("field_registry_sha256", "schema_bundle_sha256"):
            if asset_bindings.get(key) != source_bindings.get(key) or not _valid_sha256(asset_bindings.get(key)):
                errors.append(f"asset manifest binding mismatch for {key}")
        if str(asset_bindings.get("target_model_normalized") or "").strip().upper() != target_model:
            errors.append("asset manifest target binding mismatch")
        observed_source_manifest_hash = (
            sha256_file(manifest_path)
            if manifest_path is not None and manifest_path.is_file()
            else sha256_bytes(canonical_json(manifest).encode("utf-8"))
        )
        if asset_bindings.get("approved_source_manifest_sha256") != observed_source_manifest_hash:
            errors.append("approved source manifest hash binding mismatch")
        write_policy = asset_manifest.get("write_policy") or {}
        if (
            write_policy.get("candidate_external_write_enabled") is not False
            or write_policy.get("upload_requires_current_probe_receipt") is not True
            or write_policy.get("draft_save_requires_separate_approval") is not True
            or write_policy.get("final_write_enabled") is not False
        ):
            errors.append("asset manifest write policy is unsafe")

        approved_assets = [row for row in asset_manifest.get("assets") or [] if isinstance(row, dict)]
        main_images = [row for row in approved_assets if row.get("role") == "MAIN_IMAGE"]
        main_videos = [row for row in approved_assets if row.get("role") == "MAIN_VIDEO"]
        if asset_manifest.get("asset_count") != len(approved_assets):
            errors.append("asset_count does not match assets")
        if asset_manifest.get("main_image_count") != len(main_images) or not 4 <= len(main_images) <= 6:
            errors.append("main image count must match and be between four and six")
        if asset_manifest.get("main_video_count") != len(main_videos) or len(main_videos) > 1:
            errors.append("main video count is inconsistent")
        if asset_manifest.get("asset_sequence_sha256") != approved_asset_sequence_sha256(approved_assets):
            errors.append("asset sequence hash mismatch")
        asset_ids = [str(row.get("asset_id") or "") for row in approved_assets]
        upload_orders = [row.get("upload_order") for row in approved_assets]
        asset_hashes = [str(row.get("sha256") or "").lower() for row in approved_assets]
        asset_paths = [str(Path(str(row.get("path") or "")).resolve()).casefold() for row in approved_assets]
        if len(asset_ids) != len(set(asset_ids)) or not all(asset_ids):
            errors.append("asset ids are empty or duplicated")
        if len(upload_orders) != len(set(upload_orders)):
            errors.append("asset upload orders are duplicated")
        if len(asset_hashes) != len(set(asset_hashes)) or not all(asset_hashes):
            errors.append("asset content hashes are empty or duplicated")
        if len(asset_paths) != len(set(asset_paths)) or not all(asset_paths):
            errors.append("asset paths are empty or duplicated")

        for row in approved_assets:
            asset_id = str(row.get("asset_id") or "")
            if row.get("record_disposition") != "APPROVED_INTERNAL_SOURCE":
                errors.append(f"asset is not APPROVED_INTERNAL_SOURCE: {asset_id}")
            identity = row.get("product_identity") or {}
            if (
                str(identity.get("target_model") or "").strip().upper() != target_model
                or identity.get("match_status") != "MATCHED"
                or not identity.get("decision_id")
            ):
                errors.append(f"asset identity is not approved for target model: {asset_id}")
            upload_approval = row.get("upload_approval") or {}
            if (
                upload_approval.get("approved") is not True
                or not upload_approval.get("decision_id")
                or not upload_approval.get("approved_by")
                or not upload_approval.get("approved_at")
            ):
                errors.append(f"asset upload approval is incomplete: {asset_id}")
            compliance = row.get("compliance") or {}
            for key in ("format_ok", "size_ok", "dimensions_ok", "identity_ok", "policy_ok", "upload_ready"):
                if compliance.get(key) is not True:
                    errors.append(f"asset compliance {key} is not true: {asset_id}")
            if row.get("derived_from_template") is not False or row.get("derived_from_historical_record") is not False:
                errors.append(f"asset inherits forbidden template/historical media: {asset_id}")
            source = source_by_id.get(str(row.get("source_id") or ""))
            if source is None or source.get("source_role") != "APPROVED_ASSET_ORIGIN":
                errors.append(f"asset source id is not an approved asset origin: {asset_id}")
            elif row.get("source_decision_id") != source.get("decision_id"):
                errors.append(f"asset source decision binding mismatch: {asset_id}")
            path = Path(str(row.get("path") or ""))
            if not path.is_absolute() or not path.is_file():
                errors.append(f"asset path is not an existing absolute file: {asset_id}")
            elif (
                not _valid_sha256(row.get("sha256"))
                or sha256_file(path) != str(row.get("sha256")).lower()
                or row.get("bytes") is None
                or path.stat().st_size != int(row.get("bytes"))
            ):
                errors.append(f"asset hash/size mismatch: {asset_id}")

    return {
        "schema_version": "sellerkit.preflight_receipt.v1",
        "generated_at": utc_now(),
        "target_model": manifest.get("target_model"),
        "approved_field_count": len(approved_fields),
        "approved_asset_count": len(approved_assets),
        "dynamic_required_field_ids": sorted(dynamic_required),
        "dynamic_required_field_set_sha256": (manifest.get("dynamic_schema") or {}).get("required_field_set_sha256"),
        "draft_profile": "PARTIAL_DRAFT_FILL_APPROVED_ONLY" if source_schema_ok and partial_mode else "FULL_DRAFT",
        "errors": errors,
        "status": "PASS" if not errors else "BLOCKED_PREFLIGHT",
    }


def _parse_probe_timestamp(value: Any, label: str, errors: list[str]) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        errors.append(f"missing {label}")
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"invalid {label}")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{label} must include a timezone")
        return None
    return parsed.astimezone(timezone.utc)


def _probe_object_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _effective_probe_read_policy(
    probe: dict[str, Any],
    policy: dict[str, Any],
    *,
    binding_context: dict[str, Any],
    read_requests: list[Any],
    page_fingerprint: str,
    completed_at: datetime | None,
    errors: list[str],
) -> tuple[dict[str, Any], str | None]:
    """Validate a fresh exact-READ overlay without mutating global policy.

    The overlay is deliberately weaker than a production browser attestation:
    it only makes the public verification path structurally reachable.  A
    separate adapter-readiness gate still blocks production when the bundled
    adapter is not reviewed/implemented.
    """

    overlay = binding_context.get("probe_read_activation")
    if overlay is None:
        return policy, None
    start_error_count = len(errors)
    if not isinstance(overlay, dict):
        errors.append("probe READ activation request is not an object")
        return policy, None

    if overlay.get("schema_version") != "sellerkit.alibaba.probe-read-activation-request.v1":
        errors.append("unsupported probe READ activation request schema_version")
    if overlay.get("status") != "PENDING_CURRENT_SESSION_EXACT_READ_ACTIVATION":
        errors.append("probe READ activation request status is not pending exact READ activation")
    if overlay.get("authority") != "EXACT_CAPTURED_READS_ONLY_NOT_GLOBAL_POLICY":
        errors.append("probe READ activation request authority is invalid")
    if (
        overlay.get("global_policy_unchanged") is not True
        or overlay.get("draft_write_enabled") is not False
        or overlay.get("final_write_enabled") is not False
        or overlay.get("errors") != []
    ):
        errors.append("probe READ activation request weakens policy or contains errors")

    body = copy.deepcopy(overlay)
    declared_body_sha256 = str(body.pop("body_sha256", "") or "").lower()
    if declared_body_sha256 != _probe_object_sha256(body):
        errors.append("probe READ activation request body_sha256 mismatch")

    generated_at = _parse_probe_timestamp(
        overlay.get("generated_at"), "probe_read_activation.generated_at", errors
    )
    expires_at = _parse_probe_timestamp(
        overlay.get("expires_at"), "probe_read_activation.expires_at", errors
    )
    now = datetime.now(timezone.utc)
    if generated_at is not None:
        if generated_at > now + timedelta(minutes=5):
            errors.append("probe READ activation request is generated in the future")
        if completed_at is not None and generated_at < completed_at:
            errors.append("probe READ activation request predates probe completion")
    if generated_at is not None and expires_at is not None:
        if expires_at < now:
            errors.append("probe READ activation request is expired")
        if expires_at <= generated_at or expires_at - generated_at > timedelta(minutes=15):
            errors.append("probe READ activation request lifetime must be positive and at most fifteen minutes")

    context_hashes = binding_context.get("hashes") if isinstance(binding_context.get("hashes"), dict) else {}
    scope = overlay.get("scope") if isinstance(overlay.get("scope"), dict) else {}
    network = probe.get("network") if isinstance(probe.get("network"), dict) else {}
    expected_run_id = str(probe.get("run_id") or "")
    if scope.get("run_id") != expected_run_id:
        errors.append("probe READ activation request run_id mismatch")
    if scope.get("target_model") != probe.get("target_model"):
        errors.append("probe READ activation request target_model mismatch")
    if str(scope.get("template_product_id") or "") != str(probe.get("template_product_id") or ""):
        errors.append("probe READ activation request template_product_id mismatch")
    if scope.get("browser_session_sha256") != network.get("interception_session_sha256"):
        errors.append("probe READ activation request browser session mismatch")
    if scope.get("page_fingerprint_sha256") != page_fingerprint:
        errors.append("probe READ activation request page fingerprint mismatch")

    bindings = overlay.get("bindings") if isinstance(overlay.get("bindings"), dict) else {}
    for overlay_key, context_key in (
        ("global_endpoint_policy_sha256", "endpoint_policy_sha256"),
        ("run_spec_sha256", "run_spec_sha256"),
        ("action_plan_sha256", "action_plan_sha256"),
        ("adapter_sha256", "adapter_sha256"),
        ("probe_source_sha256", "probe_source_sha256"),
    ):
        if bindings.get(overlay_key) != context_hashes.get(context_key):
            errors.append(f"probe READ activation request binding mismatch: {overlay_key}")

    rows = overlay.get("request_activations")
    if not isinstance(rows, list) or len(rows) != len(read_requests) or not rows:
        errors.append("probe READ activation request count does not match captured READ requests")
        rows = []
    effective_rules: list[dict[str, Any]] = []
    seen_request_keys: set[tuple[str, str]] = set()
    for index, request in enumerate(read_requests):
        if not isinstance(request, dict) or index >= len(rows) or not isinstance(rows[index], dict):
            continue
        row = rows[index]
        method = str(request.get("method") or "").upper()
        url = str(request.get("url") or "")
        key = (method, url)
        if key in seen_request_keys:
            errors.append(f"probe READ activation request contains duplicate exact request: {index}")
        seen_request_keys.add(key)
        if method not in READ_METHODS or row.get("method") != method or row.get("url") != url:
            errors.append(f"probe READ activation request {index} does not bind exact READ method/url")
        if row.get("classification") != "READ" or row.get("decision") != "ALLOW":
            errors.append(f"probe READ activation request {index} is not READ/ALLOW")
        if row.get("source_request_sha256") != _probe_object_sha256(request):
            errors.append(f"probe READ activation request {index} source request hash mismatch")

        baseline = classify_endpoint(
            policy,
            url,
            method,
            evidence_id="CURRENT_SESSION_READ_OVERLAY_CANDIDATE",
        )
        baseline_class = baseline.get("classification")
        policy_class = baseline.get("policy_classification")
        if baseline.get("decision") == "HARD_DENY" or baseline_class == "FINAL_WRITE":
            errors.append(f"probe READ activation request {index} targets a final-write route")
        elif baseline_class == "REVERSIBLE_DRAFT_WRITE" or policy_class == "REVERSIBLE_DRAFT_WRITE":
            errors.append(f"probe READ activation request {index} targets a draft-write route")
        elif not (
            baseline_class == "READ"
            or (baseline_class == "DENY_METHOD_UNVERIFIED" and policy_class == "READ")
        ):
            errors.append(f"probe READ activation request {index} is not a known READ route")

        parsed = urlparse(url)
        target = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        effective_rules.append(
            {
                "id": f"session-read-{index}",
                "classification": "READ",
                "methods": [method],
                "method_evidence": "CURRENT_SESSION_VERIFIED",
                "evidence_state": "LIVE_VERIFIED_ACTIVE",
                "url_regex": f"^{re.escape(target)}$",
                "query_allowlist": sorted(query),
                "query_values": {key: sorted(set(values)) for key, values in sorted(query.items())},
            }
        )

    overlay_sha256 = str(context_hashes.get("probe_read_activation_request_sha256") or "")
    if overlay_sha256 and not _valid_sha256(overlay_sha256):
        errors.append("probe READ activation request file hash is invalid")
    if len(errors) != start_error_count:
        return policy, overlay_sha256 or None
    effective_policy = copy.deepcopy(policy)
    effective_policy["rules"] = effective_rules + list(effective_policy.get("rules") or [])
    return effective_policy, overlay_sha256 or _probe_object_sha256(overlay)


def finalize_probe_verification_gate(
    result: dict[str, Any],
    *,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Separate proof of zero writes from authority to continue execution.

    A read-only probe can conclusively prove that it caused no write while its
    observed requests are still denied or unknown under the bound endpoint
    policy.  Only an exact ``ALLOW`` decision for every captured request grants
    controlled-execution authority.  ``ALLOW_UI_ONLY`` is deliberately not a
    read authorization and therefore also fails this gate.
    """

    unique_errors = sorted(set(errors if errors is not None else (result.get("errors") or [])))
    classifications = result.get("request_classifications")
    authorization_errors: list[str] = []
    zero_write_confirmed = not unique_errors
    if not zero_write_confirmed:
        authorization_errors.append("zero-write probe verification failed")
    if not isinstance(classifications, list) or not classifications:
        authorization_errors.append("request classifications are empty")
        classifications = []
    for index, row in enumerate(classifications):
        decision = row.get("decision") if isinstance(row, dict) else None
        if decision != "ALLOW":
            authorization_errors.append(
                f"request classification {index} is not ALLOW: {decision or 'UNKNOWN'}"
            )

    result["errors"] = unique_errors
    result["zero_write_confirmed"] = zero_write_confirmed
    result["authorization_errors"] = sorted(set(authorization_errors))
    result["execution_authorized"] = zero_write_confirmed and not authorization_errors
    result["status"] = "PASS_ZERO_WRITE" if zero_write_confirmed else "BLOCKED_PROBE"
    adapter_ready = result.get("adapter_production_execution_ready") is True
    production_errors: list[str] = []
    if not result["execution_authorized"]:
        production_errors.append("exact READ execution authorization is absent")
    if not adapter_ready:
        production_errors.append("browser adapter production execution readiness is not certified")
    result["production_authorization_errors"] = sorted(set(production_errors))
    result["production_execution_authorized"] = not production_errors
    return result


def verify_probe(
    probe: dict[str, Any],
    policy: dict[str, Any],
    *,
    binding_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless a fresh, actively intercepted v2 probe is bound to real inputs.

    ``binding_context`` is supplied by the CLI from the actual run spec, action
    plan, adapter and their file hashes.  Hashes merely asserted inside the
    browser receipt are never treated as authority.
    """

    errors: list[str] = []
    classifications: list[dict[str, Any]] = []
    context = binding_context if isinstance(binding_context, dict) else {}
    context_hashes = context.get("hashes") if isinstance(context.get("hashes"), dict) else {}
    target_model = str(probe.get("target_model") or "").strip()
    template_product_id = str(probe.get("template_product_id") or "").strip()
    probe_run_id = str(probe.get("run_id") or "").strip()

    if probe.get("schema_version") != "alibaba.probe-receipt.v2":
        errors.append("unsupported or missing strict probe schema_version")
    if not target_model:
        errors.append("target_model is required")
    if not template_product_id.isdigit():
        errors.append("numeric template_product_id is required")
    if not probe_run_id:
        errors.append("run_id is required")
    if probe.get("mode") != "READ_ONLY_PROBE":
        errors.append("probe mode is not READ_ONLY_PROBE")
    if probe.get("result") != "READ_ONLY_VERIFIED":
        errors.append("probe result is not READ_ONLY_VERIFIED")
    if probe.get("safe_to_continue_read_only") is not True:
        errors.append("read-only continuation was not explicitly verified")
    if probe.get("safe_to_write") is not False or probe.get("external_write_enabled") is not False:
        errors.append("probe must not grant external write authority")
    if probe.get("final_write_count") != 0 or probe.get("submit_triggered") is not False:
        errors.append("probe contains final-write activity or unknown final-write state")
    if probe.get("errors") != []:
        errors.append("probe contains browser-side errors or an unknown error list")

    current_url = str(probe.get("current_url") or "")
    parsed = urlparse(current_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    expected_query = {
        "pubType": ["similarPost"],
        "behavior": ["copyNew"],
        "itemId": [template_product_id],
    }
    if (
        parsed.scheme != "https"
        or parsed.netloc != "post.alibaba.com"
        or parsed.path != "/product/publish.htm"
        or parsed.params
        or parsed.fragment
    ):
        errors.append("probe URL must use exact https://post.alibaba.com/product/publish.htm")
    if query != expected_query:
        errors.append("probe URL query must exactly bind similarPost/copyNew/template itemId")

    route = probe.get("route") if isinstance(probe.get("route"), dict) else {}
    if route.get("origin") != "https://post.alibaba.com" or route.get("path") != "/product/publish.htm":
        errors.append("route origin/path binding mismatch")
    if route.get("query") != {
        "pubType": "similarPost",
        "behavior": "copyNew",
        "itemId": template_product_id,
    }:
        errors.append("route query binding mismatch")
    if (
        route.get("origin_match") is not True
        or route.get("path_match") is not True
        or route.get("query_match") is not True
        or route.get("redirect_origin_changed") is not False
        or route.get("redirect_count") != 0
    ):
        errors.append("route was redirected, ambiguous, or not exactly matched")

    page = probe.get("page") if isinstance(probe.get("page"), dict) else {}
    if (
        page.get("page_kind") != "icbu_product_publish_similar"
        or page.get("page_kind_match") is not True
    ):
        errors.append("page kind was not proven as the similar-product publish form")
    if (
        page.get("login_active") is not True
        or page.get("login_challenge_present") is not False
    ):
        errors.append("Alibaba login is inactive, challenged, or unknown")
    if page.get("captcha_present") is not False:
        errors.append("CAPTCHA is present or its state is unknown")
    if page.get("credential_values_recorded") is not False:
        errors.append("credential values were recorded or the state is unknown")
    fingerprint = str(page.get("fingerprint_sha256") or "")
    material = page.get("fingerprint_material")
    if not _valid_sha256(fingerprint):
        errors.append("page fingerprint must be a SHA-256 digest")
    elif not isinstance(material, dict) or fingerprint.lower() != _probe_object_sha256(material):
        errors.append("page fingerprint does not hash the supplied fingerprint material")
    elif (
        material.get("page_kind") != page.get("page_kind")
        or material.get("title_sha256") != _probe_object_sha256(page.get("title"))
        or material.get("aria_snapshot_sha256") != page.get("aria_snapshot_sha256")
        or material.get("dom_snapshot_sha256") != page.get("dom_snapshot_sha256")
        or material.get("bundle_hashes") != page.get("bundle_hashes")
    ):
        errors.append("page fingerprint material is not bound to current page evidence")
    if page.get("fingerprint_accepted") is not True:
        errors.append("page fingerprint was not accepted")
    for key in (
        "raw_probe_sha256",
        "redacted_probe_sha256",
        "aria_snapshot_sha256",
        "dom_snapshot_sha256",
        "screenshot_sha256",
    ):
        if not _valid_sha256(page.get(key)):
            errors.append(f"invalid page evidence digest: {key}")
    bundle_hashes = page.get("bundle_hashes")
    if not isinstance(bundle_hashes, list) or not bundle_hashes or not all(_valid_sha256(v) for v in bundle_hashes):
        errors.append("non-empty SHA-256 bundle evidence is required")

    started_at = _parse_probe_timestamp(probe.get("started_at"), "started_at", errors)
    captured_at = _parse_probe_timestamp(probe.get("captured_at"), "captured_at", errors)
    completed_at = _parse_probe_timestamp(probe.get("completed_at"), "completed_at", errors)

    network = probe.get("network") if isinstance(probe.get("network"), dict) else {}
    interception_at = _parse_probe_timestamp(network.get("interception_started_at"), "interception_started_at", errors)
    navigation_at = _parse_probe_timestamp(network.get("navigation_started_at"), "navigation_started_at", errors)
    capture_completed_at = _parse_probe_timestamp(network.get("capture_completed_at"), "capture_completed_at", errors)
    ordered_times = [started_at, interception_at, navigation_at, captured_at, capture_completed_at, completed_at]
    if all(value is not None for value in ordered_times):
        concrete_times = [value for value in ordered_times if value is not None]
        if concrete_times != sorted(concrete_times):
            errors.append("probe timing must be start <= interception <= navigation <= capture <= completion")
        elif completed_at is not None and started_at is not None and (completed_at - started_at).total_seconds() > 1800:
            errors.append("probe duration exceeds 30 minutes")
    if completed_at is not None:
        age_seconds = (datetime.now(timezone.utc) - completed_at).total_seconds()
        if age_seconds < -300:
            errors.append("probe completion time is in the future")
        elif age_seconds > 900:
            errors.append("probe is older than 15 minutes")

    if (
        network.get("interception_mode") != "ACTIVE_FAIL_CLOSED"
        or network.get("interception_active") is not True
        or network.get("network_guard_installed_before_navigation") is not True
        or network.get("requests_complete") is not True
        or not _valid_sha256(network.get("interception_session_sha256"))
    ):
        errors.append("active fail-closed interception was not proven before navigation")
    if network.get("allowed_methods") != ["GET", "HEAD", "OPTIONS"]:
        errors.append("zero-write allowed_methods must be exactly GET/HEAD/OPTIONS")
    if (
        network.get("write_requests_observed") != 0
        or network.get("write_requests_allowed") != 0
        or network.get("unknown_write_count") != 0
        or network.get("final_write_count") != 0
        or network.get("all_observed_writes_blocked") is not True
        or network.get("blocked_write_attempts") != []
    ):
        errors.append("zero-write probe observed, allowed, or ambiguously recorded a write")
    if network.get("request_bodies_recorded") is not False or network.get("credential_values_recorded") is not False:
        errors.append("network body/credential values were recorded or state is unknown")

    read_requests = network.get("read_requests")
    if not isinstance(read_requests, list) or not read_requests:
        errors.append("non-empty network read evidence is required")
        read_requests = []
    if network.get("read_request_count") != len(read_requests):
        errors.append("read_request_count does not match read_requests evidence")
    effective_policy, read_activation_sha256 = _effective_probe_read_policy(
        probe,
        policy,
        binding_context=context,
        read_requests=read_requests,
        page_fingerprint=fingerprint,
        completed_at=completed_at,
        errors=errors,
    )
    observed_methods: list[str] = []
    exact_page_read = False
    for index, request in enumerate(read_requests):
        if not isinstance(request, dict):
            errors.append(f"read request {index} is not an object")
            continue
        method = str(request.get("method") or "").upper()
        request_url = str(request.get("url") or "")
        observed_methods.append(method)
        if method not in READ_METHODS:
            errors.append(f"read request {index} has a non-read method")
        response_status = request.get("response_status")
        if isinstance(response_status, bool) or not isinstance(response_status, int) or not 200 <= response_status < 400:
            errors.append(f"read request {index} lacks a successful response status")
        request_at = _parse_probe_timestamp(request.get("captured_at"), f"read_requests[{index}].captured_at", errors)
        if request_at is not None and navigation_at is not None and capture_completed_at is not None:
            if not navigation_at <= request_at <= capture_completed_at:
                errors.append(f"read request {index} falls outside the captured navigation window")
        if method == "GET" and request_url == current_url and isinstance(response_status, int) and 200 <= response_status < 400:
            exact_page_read = True
        classifications.append(classify_endpoint(effective_policy, request_url, method))
    if not exact_page_read:
        errors.append("no successful GET evidence exists for the exact current publish URL")
    expected_observed_methods = [method for method in ("GET", "HEAD", "OPTIONS") if method in set(observed_methods)]
    if network.get("observed_methods") != expected_observed_methods:
        errors.append("observed_methods does not exactly match read request evidence")

    counts = probe.get("counts") if isinstance(probe.get("counts"), dict) else {}
    for before, after, label in (
        (counts.get("online_before"), counts.get("online_after"), "online"),
        (counts.get("draft_before"), counts.get("draft_after"), "draft"),
    ):
        if isinstance(before, bool) or not isinstance(before, int) or before < 0:
            errors.append(f"invalid {label} count before probe")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            errors.append(f"invalid {label} count after probe")
        if before != after:
            errors.append(f"{label} count changed during zero-write probe")

    bindings = probe.get("bindings") if isinstance(probe.get("bindings"), dict) else {}
    required_binding_hashes = (
        "run_spec_sha256",
        "action_plan_sha256",
        "field_registry_sha256",
        "schema_bundle_sha256",
        "probe_tool_sha256",
        "adapter_sha256",
        "endpoint_policy_sha256",
    )
    for key in required_binding_hashes:
        if not _valid_sha256(bindings.get(key)):
            errors.append(f"invalid probe binding digest: {key}")
    run_spec = context.get("run_spec") if isinstance(context.get("run_spec"), dict) else {}
    action_plan = context.get("action_plan") if isinstance(context.get("action_plan"), dict) else {}
    adapter = context.get("adapter") if isinstance(context.get("adapter"), dict) else {}
    if not context or not run_spec or not action_plan or not adapter:
        errors.append("actual run spec, action plan, endpoint policy and adapter binding context is required")
    for binding_key, context_key in (
        ("run_spec_sha256", "run_spec_sha256"),
        ("action_plan_sha256", "action_plan_sha256"),
        ("adapter_sha256", "adapter_sha256"),
        ("endpoint_policy_sha256", "endpoint_policy_sha256"),
    ):
        if bindings.get(binding_key) != context_hashes.get(context_key):
            errors.append(f"probe {binding_key} does not match the actual bound file")
    if context_hashes.get("endpoint_policy_sha256") != context.get("loaded_policy_sha256"):
        errors.append("loaded endpoint policy hash does not match binding context")
    if context.get("loaded_policy") != policy:
        errors.append("loaded endpoint policy document does not match verifier policy")

    plan_target = action_plan.get("target") if isinstance(action_plan.get("target"), dict) else {}
    plan_bindings = action_plan.get("bindings") if isinstance(action_plan.get("bindings"), dict) else {}
    plan_three_party = plan_bindings.get("three_party") if isinstance(plan_bindings.get("three_party"), dict) else {}
    plan_policy = plan_bindings.get("endpoint_policy") if isinstance(plan_bindings.get("endpoint_policy"), dict) else {}
    plan_adapter = plan_bindings.get("adapter") if isinstance(plan_bindings.get("adapter"), dict) else {}
    if plan_target.get("model") != target_model or str(plan_target.get("template_product_id") or "") != template_product_id:
        errors.append("action plan target does not match probe target")
    if str(plan_target.get("run_id") or "") != probe_run_id:
        errors.append("action plan run_id does not match probe run_id")
    if bindings.get("action_plan_id") != action_plan.get("plan_id"):
        errors.append("action plan id binding mismatch")
    if bindings.get("action_plan_generated_at") != action_plan.get("generated_at"):
        errors.append("action plan generated_at binding mismatch")
    if plan_three_party.get("run_spec_sha256") != bindings.get("run_spec_sha256"):
        errors.append("action plan is not bound to the probe run spec")
    if plan_policy.get("sha256") != bindings.get("endpoint_policy_sha256"):
        errors.append("action plan endpoint policy binding mismatch")
    if plan_adapter.get("sha256") != bindings.get("adapter_sha256"):
        errors.append("action plan adapter binding mismatch")
    runtime_requirements = action_plan.get("runtime_requirements") or {}
    if (
        runtime_requirements.get("current_probe_required") is not True
        or runtime_requirements.get("current_probe_must_be_newer_than_plan") is not True
        or runtime_requirements.get("fail_on_unknown_write_request") is not True
    ):
        errors.append("action plan lacks strict current-probe runtime requirements")

    if str(run_spec.get("target_model") or "").strip() != target_model:
        errors.append("run spec target model binding mismatch")
    if str(run_spec.get("template_product_id") or "") != template_product_id:
        errors.append("run spec template product id binding mismatch")
    if str(run_spec.get("run_id") or "") != probe_run_id:
        errors.append("run spec run_id binding mismatch")
    if (run_spec.get("endpoint_policy") or {}).get("sha256") != bindings.get("endpoint_policy_sha256"):
        errors.append("run spec endpoint policy binding mismatch")
    if adapter.get("adapter_id") != bindings.get("adapter_id") or adapter.get("adapter_version") != bindings.get("adapter_version"):
        errors.append("adapter identity/version binding mismatch")
    adapter_runtime = adapter.get("runtime_resolution") or {}
    if (
        adapter_runtime.get("current_probe_required") is not True
        or adapter_runtime.get("probe_origin") != "https://post.alibaba.com"
        or adapter_runtime.get("probe_path") != "/product/publish.htm"
    ):
        errors.append("adapter does not require the exact current publish-page probe")

    run_generated_at = _parse_probe_timestamp(run_spec.get("generated_at"), "run_spec.generated_at", errors)
    plan_generated_at = _parse_probe_timestamp(action_plan.get("generated_at"), "action_plan.generated_at", errors)
    if run_generated_at is not None and plan_generated_at is not None and run_generated_at > plan_generated_at:
        errors.append("action plan predates its run spec")
    if plan_generated_at is not None and interception_at is not None and plan_generated_at >= interception_at:
        errors.append("current probe must be newer than the action plan")

    adapter_readiness = adapter.get("readiness") if isinstance(adapter.get("readiness"), dict) else {}
    adapter_declares_production_ready = adapter_readiness.get("production_execution_ready") is True
    adapter_production_ready = (
        PRODUCTION_BROWSER_ATTESTATION_VERIFIER_IMPLEMENTED
        and adapter_declares_production_ready
    )
    result = {
        "schema_version": "sellerkit.browser_probe_receipt.v3",
        "generated_at": utc_now(),
        "probe_captured_at": probe.get("captured_at"),
        "input_probe_sha256": (
            context_hashes.get("probe_source_sha256")
            if _valid_sha256(context_hashes.get("probe_source_sha256"))
            else _probe_object_sha256(probe)
        ),
        "target_model": probe.get("target_model"),
        "template_product_id": probe.get("template_product_id"),
        "browser_session_sha256": network.get("interception_session_sha256"),
        "page_fingerprint": fingerprint if _valid_sha256(fingerprint) else None,
        "bindings": {
            "run_spec_sha256": bindings.get("run_spec_sha256"),
            "action_plan_sha256": bindings.get("action_plan_sha256"),
            "action_plan_id": bindings.get("action_plan_id"),
            "endpoint_policy_sha256": bindings.get("endpoint_policy_sha256"),
            "adapter_sha256": bindings.get("adapter_sha256"),
            "probe_read_activation_request_sha256": read_activation_sha256,
        },
        "request_classifications": classifications,
        "errors": sorted(set(errors)),
        "adapter_production_execution_ready": adapter_production_ready,
    }
    return finalize_probe_verification_gate(result)


EMPTY_READBACK_SHA256 = sha256_bytes(canonical_json("").encode("utf-8"))


def _fill_target_key(value: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, dict):
        errors.append(f"{label} target is not an object")
        return None
    kind = value.get("kind")
    logical_name = str(value.get("logical_name") or "").strip()
    if kind not in {"FIELD_REGISTRY", "CATEGORY_PROPERTY_RUNTIME_KEY", "LOCALIZED_FIELD"} or not logical_name:
        errors.append(f"{label} target identity is invalid")
        return None
    if kind == "CATEGORY_PROPERTY_RUNTIME_KEY" and not str(value.get("approved_property_name") or "").strip():
        errors.append(f"{label} category target is missing approved_property_name")
        return None
    if kind == "LOCALIZED_FIELD":
        required = {
            "locale", "live_language_label", "direction", "expected_sha256",
            "max_length_rule_hash", "max_length_rule", "identity_sha256",
        }
        missing = sorted(name for name in required if not value.get(name))
        if missing:
            errors.append(f"{label} localized target is incomplete: {missing}")
            return None
        rule = value.get("max_length_rule")
        if not isinstance(rule, dict):
            errors.append(f"{label} localized max length rule is invalid")
            return None
        computed_rule_hash = sha256_bytes(canonical_json(rule).encode("utf-8"))
        if str(value.get("max_length_rule_hash") or "").lower() != computed_rule_hash:
            errors.append(f"{label} localized max length rule hash mismatch")
            return None
        identity = {
            "logical_name": value.get("logical_name"),
            "locale": value.get("locale"),
            "live_language_label": value.get("live_language_label"),
            "direction": value.get("direction"),
            "expected_sha256": value.get("expected_sha256"),
            "max_length_rule_hash": value.get("max_length_rule_hash"),
        }
        if str(value.get("identity_sha256") or "").lower() != sha256_bytes(
            canonical_json(identity).encode("utf-8")
        ):
            errors.append(f"{label} localized identity hash mismatch")
            return None
    return canonical_json(value)


def _phase_operations(action_plan: dict[str, Any], phase_name: str, errors: list[str]) -> list[dict[str, Any]]:
    matches = [
        phase
        for phase in action_plan.get("phases") or []
        if isinstance(phase, dict) and phase.get("name") == phase_name
    ]
    if len(matches) != 1:
        errors.append(f"action plan must contain exactly one {phase_name} phase")
        return []
    operations = matches[0].get("operations")
    if not isinstance(operations, list):
        errors.append(f"action plan {phase_name} operations are not a list")
        return []
    return [operation for operation in operations if isinstance(operation, dict)]


def _expected_fill_contract(
    action_plan: dict[str, Any], errors: list[str]
) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, str], str, str, str]:
    expected_fields: dict[str, dict[str, str]] = {}
    for index, operation in enumerate(
        _phase_operations(action_plan, "WRITE_APPROVED_FIELD_VALUES", errors)
    ):
        if operation.get("operation") != "WRITE_APPROVED_FIELD_VALUE":
            errors.append(f"unexpected write operation at index {index}")
            continue
        key = _fill_target_key(operation.get("target"), f"write operation {index}", errors)
        if key is None:
            continue
        if key in expected_fields:
            errors.append(f"duplicate action-plan write target: {key}")
            continue
        expected_sha256 = str(operation.get("value_sha256") or "").lower()
        readback_sha256 = str((operation.get("readback") or {}).get("expected_sha256") or "").lower()
        approval = operation.get("approval") or {}
        source_id = str(approval.get("source_id") or "")
        decision_id = str(approval.get("decision_id") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            errors.append(f"invalid expected value hash for action-plan target: {key}")
        if readback_sha256 != expected_sha256:
            errors.append(f"action-plan readback hash mismatch for target: {key}")
        if not source_id or not decision_id:
            errors.append(f"action-plan approval binding is incomplete for target: {key}")
        target = operation.get("target") or {}
        if target.get("kind") == "LOCALIZED_FIELD":
            rule = target.get("max_length_rule") or {}
            if str(target.get("expected_sha256") or "").lower() != expected_sha256:
                errors.append(f"localized target value hash mismatch: {key}")
            if str(rule.get("candidate_value_sha256") or "").lower() != expected_sha256:
                errors.append(f"localized length rule value hash mismatch: {key}")
        expected_fields[key] = {
            "expected_sha256": expected_sha256,
            "source_id": source_id,
            "decision_id": decision_id,
        }

    expected_clears: dict[str, str] = {}
    for index, operation in enumerate(
        _phase_operations(action_plan, "CLEAR_INHERITED_FIELDS", errors)
    ):
        if operation.get("operation") != "CLEAR_INHERITED_FIELD":
            errors.append(f"unexpected clear operation at index {index}")
            continue
        key = _fill_target_key(operation.get("target"), f"clear operation {index}", errors)
        if key is None:
            continue
        if key in expected_clears:
            errors.append(f"duplicate action-plan clear target: {key}")
            continue
        if (operation.get("readback") or {}).get("expected_state") != "EMPTY":
            errors.append(f"action-plan clear target does not require EMPTY readback: {key}")
        reason = str(operation.get("reason") or "")
        if reason not in {
            "REGISTRY_CLEAR_BEFORE_FILL", "BLOCKED_FIELD_MUST_REMAIN_EMPTY",
            "LOCALIZED_CLEAR_BEFORE_FILL",
        }:
            errors.append(f"action-plan clear reason is invalid for target: {key}")
        expected_clears[key] = reason

    uploaded_logical_names = {
        str((operation.get("target") or {}).get("logical_name") or "")
        for operation in _phase_operations(action_plan, "UPLOAD_APPROVED_ASSETS", errors)
        if operation.get("operation") == "UPLOAD_APPROVED_ASSET"
    }
    expected_post_refresh_empty: dict[str, str] = {}
    for key, reason in expected_clears.items():
        target = json.loads(key)
        written_later = key in expected_fields
        uploaded_later = (
            target.get("kind") == "FIELD_REGISTRY"
            and str(target.get("logical_name") or "") in uploaded_logical_names
        )
        if not written_later and not uploaded_later:
            expected_post_refresh_empty[key] = reason

    field_set_rows = [
        {
            "target": json.loads(key),
            "expected_sha256": expected_fields[key]["expected_sha256"],
            "source_id": expected_fields[key]["source_id"],
            "decision_id": expected_fields[key]["decision_id"],
        }
        for key in sorted(expected_fields)
    ]
    clear_set_rows = [
        {"target": json.loads(key), "reason": expected_clears[key]}
        for key in sorted(expected_clears)
    ]
    post_refresh_empty_set_rows = [
        {"target": json.loads(key), "reason": expected_post_refresh_empty[key]}
        for key in sorted(expected_post_refresh_empty)
    ]
    return (
        expected_fields,
        expected_clears,
        expected_post_refresh_empty,
        sha256_bytes(canonical_json(field_set_rows).encode("utf-8")),
        sha256_bytes(canonical_json(clear_set_rows).encode("utf-8")),
        sha256_bytes(canonical_json(post_refresh_empty_set_rows).encode("utf-8")),
    )


def _verify_fill_bindings(
    document: dict[str, Any],
    label: str,
    *,
    expected_bindings: dict[str, str],
    errors: list[str],
) -> None:
    bindings = document.get("bindings")
    if not isinstance(bindings, dict):
        errors.append(f"{label} bindings are missing")
        return
    for name, expected in expected_bindings.items():
        observed = str(bindings.get(name) or "").lower()
        if observed != expected:
            errors.append(f"{label} binding mismatch: {name}")


def _verify_field_readbacks(
    document: dict[str, Any],
    label: str,
    expected_fields: dict[str, dict[str, str]],
    errors: list[str],
) -> None:
    rows = document.get("field_readbacks")
    if not isinstance(rows, list):
        errors.append(f"{label} field_readbacks must be a list")
        return
    observed: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"{label} field_readback {index} is not an object")
            continue
        key = _fill_target_key(row.get("target"), f"{label} field_readback {index}", errors)
        if key is None:
            continue
        if key in observed:
            errors.append(f"{label} duplicate field readback: {key}")
            continue
        observed[key] = row

    missing = sorted(set(expected_fields) - set(observed))
    extra = sorted(set(observed) - set(expected_fields))
    for key in missing:
        errors.append(f"{label} missing expected field readback: {key}")
    for key in extra:
        errors.append(f"{label} unexpected field readback: {key}")
    for key in sorted(set(expected_fields) & set(observed)):
        expected = expected_fields[key]
        row = observed[key]
        expected_sha256 = str(row.get("expected_sha256") or "").lower()
        observed_sha256 = str(row.get("observed_sha256") or "").lower()
        if expected_sha256 != expected["expected_sha256"]:
            errors.append(f"{label} expected hash does not match action plan: {key}")
        if observed_sha256 != expected["expected_sha256"]:
            errors.append(f"{label} observed hash does not match expected value: {key}")
        if row.get("source_id") != expected["source_id"]:
            errors.append(f"{label} source binding mismatch: {key}")
        if row.get("decision_id") != expected["decision_id"]:
            errors.append(f"{label} decision binding mismatch: {key}")
        target = json.loads(key)
        if target.get("kind") == "LOCALIZED_FIELD":
            rule = target.get("max_length_rule") or {}
            localized_expected = {
                "observed_locale": target.get("locale"),
                "observed_live_language_label": target.get("live_language_label"),
                "observed_direction": target.get("direction"),
                "observed_count": rule.get("observed_count"),
                "observed_maximum": rule.get("maximum"),
                "observed_unit": rule.get("unit"),
                "max_length_rule_hash": target.get("max_length_rule_hash"),
                "current_page_fingerprint_sha256": rule.get("page_fingerprint_sha256"),
                "post_write_counter_readback": True,
            }
            for name, expected_value in localized_expected.items():
                if row.get(name) != expected_value:
                    errors.append(f"{label} localized {name} mismatch: {key}")
            observed_count = row.get("observed_count")
            maximum = row.get("observed_maximum")
            if (
                isinstance(observed_count, bool)
                or not isinstance(observed_count, int)
                or isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or observed_count > maximum
            ):
                errors.append(f"{label} localized live counter exceeds or lacks the live maximum: {key}")


def _verify_fill_capture_chain(
    fill_execution: dict[str, Any],
    post_refresh: dict[str, Any],
    probe_receipt: dict[str, Any],
    errors: list[str],
) -> None:
    """Bind the immediate and post-refresh ledgers to one ordered browser capture.

    The current in-app-browser contract exposes a session digest and a stable
    page fingerprint, but no signed browser attestation.  This verifier therefore
    enforces the strongest available local relationship: both ledgers must name
    the same browser session and the same page fingerprint, and that fingerprint
    must be the one accepted by the current zero-write probe.
    """

    fill_captured_at = _parse_probe_timestamp(
        fill_execution.get("captured_at"), "fill execution captured_at", errors
    )
    refresh_captured_at = _parse_probe_timestamp(
        post_refresh.get("captured_at"), "post-refresh captured_at", errors
    )
    probe_generated_at = _parse_probe_timestamp(
        probe_receipt.get("generated_at"), "probe verification generated_at", errors
    )
    probe_captured_at = _parse_probe_timestamp(
        probe_receipt.get("probe_captured_at"), "probe verification probe_captured_at", errors
    )
    now = datetime.now(timezone.utc)
    if fill_captured_at is not None and fill_captured_at > now:
        errors.append("fill execution captured_at is in the future")
    if refresh_captured_at is not None and refresh_captured_at > now:
        errors.append("post-refresh captured_at is in the future")
    if (
        fill_captured_at is not None
        and refresh_captured_at is not None
        and refresh_captured_at < fill_captured_at
    ):
        errors.append("post-refresh captured_at is earlier than fill execution captured_at")
    probe_ready_at = max(
        (value for value in (probe_generated_at, probe_captured_at) if value is not None),
        default=None,
    )
    if (
        fill_captured_at is not None
        and probe_ready_at is not None
        and fill_captured_at < probe_ready_at
    ):
        errors.append("fill execution captured_at predates the verified current probe")
    if (
        fill_captured_at is not None
        and probe_ready_at is not None
        and (fill_captured_at - probe_ready_at).total_seconds() > 1800
    ):
        errors.append("fill execution started more than 30 minutes after the verified probe")
    if (
        fill_captured_at is not None
        and refresh_captured_at is not None
        and (refresh_captured_at - fill_captured_at).total_seconds() > 1800
    ):
        errors.append("post-refresh capture is more than 30 minutes after fill execution")

    for name, label in (
        ("browser_session_sha256", "browser session"),
        ("page_fingerprint_sha256", "page fingerprint"),
    ):
        fill_value = str(fill_execution.get(name) or "").lower()
        refresh_value = str(post_refresh.get(name) or "").lower()
        if not _valid_sha256(fill_value):
            errors.append(f"fill execution {label} binding is invalid")
        if not _valid_sha256(refresh_value):
            errors.append(f"post-refresh {label} binding is invalid")
        if fill_value and refresh_value and fill_value != refresh_value:
            errors.append(f"fill execution and post-refresh {label} bindings differ")

    accepted_probe_fingerprint = str(probe_receipt.get("page_fingerprint") or "").lower()
    accepted_probe_session = str(
        probe_receipt.get("browser_session_sha256") or ""
    ).lower()
    fill_browser_session = str(
        fill_execution.get("browser_session_sha256") or ""
    ).lower()
    refresh_browser_session = str(
        post_refresh.get("browser_session_sha256") or ""
    ).lower()
    if not _valid_sha256(accepted_probe_session):
        errors.append("probe verification browser session binding is invalid")
    else:
        if fill_browser_session != accepted_probe_session:
            errors.append("fill execution browser session does not match the current probe")
        if refresh_browser_session != accepted_probe_session:
            errors.append("post-refresh browser session does not match the current probe")
    fill_page_fingerprint = str(
        fill_execution.get("page_fingerprint_sha256") or ""
    ).lower()
    refresh_page_fingerprint = str(
        post_refresh.get("page_fingerprint_sha256") or ""
    ).lower()
    if _valid_sha256(accepted_probe_fingerprint):
        if fill_page_fingerprint != accepted_probe_fingerprint:
            errors.append("fill execution page fingerprint does not match the current probe")
        if refresh_page_fingerprint != accepted_probe_fingerprint:
            errors.append("post-refresh page fingerprint does not match the current probe")


def _verify_clear_readbacks(
    document: dict[str, Any],
    label: str,
    expected_clears: dict[str, str],
    captured_phase: str,
    errors: list[str],
) -> None:
    rows = document.get("cleared_targets")
    if not isinstance(rows, list):
        errors.append(f"{label} cleared_targets must be a list")
        return
    observed: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"{label} cleared target {index} is not an object")
            continue
        key = _fill_target_key(row.get("target"), f"{label} cleared target {index}", errors)
        if key is None:
            continue
        if key in observed:
            errors.append(f"{label} duplicate cleared target: {key}")
            continue
        observed[key] = row
    for key in sorted(set(expected_clears) - set(observed)):
        errors.append(f"{label} missing expected cleared target: {key}")
    for key in sorted(set(observed) - set(expected_clears)):
        errors.append(f"{label} unexpected cleared target: {key}")
    for key in sorted(set(expected_clears) & set(observed)):
        row = observed[key]
        if row.get("reason") != expected_clears[key]:
            errors.append(f"{label} clear reason does not match action plan: {key}")
        if row.get("captured_phase") != captured_phase:
            errors.append(f"{label} clear readback was captured in the wrong phase: {key}")
        if row.get("expected_state") != "EMPTY" or row.get("observed_state") != "EMPTY":
            errors.append(f"{label} target was not proven empty: {key}")
        if str(row.get("expected_sha256") or "").lower() != EMPTY_READBACK_SHA256:
            errors.append(f"{label} expected empty hash mismatch: {key}")
        if str(row.get("observed_sha256") or "").lower() != EMPTY_READBACK_SHA256:
            errors.append(f"{label} observed empty hash mismatch: {key}")


def verify_fill_receipt(
    fill_execution: dict[str, Any],
    post_refresh: dict[str, Any] | None = None,
    action_plan: dict[str, Any] | None = None,
    run_spec: dict[str, Any] | None = None,
    probe_receipt: dict[str, Any] | None = None,
    *,
    action_plan_sha256: str | None = None,
    run_spec_sha256: str | None = None,
    probe_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify exact fill execution and persistence against hash-bound inputs.

    Legacy single-document receipts deliberately fail closed.  A PASS requires
    an action plan, run spec, current probe receipt, the immediate execution
    ledger, and an independent post-refresh ledger.
    """

    errors: list[str] = []
    if not isinstance(post_refresh, dict):
        errors.append("post-refresh verification input is required")
        post_refresh = {}
    if not isinstance(action_plan, dict):
        errors.append("browser action plan input is required")
        action_plan = {}
    if not isinstance(run_spec, dict):
        errors.append("run spec input is required")
        run_spec = {}
    if not isinstance(probe_receipt, dict):
        errors.append("current probe receipt input is required")
        probe_receipt = {}

    actual_hashes = {
        "action_plan_sha256": str(action_plan_sha256 or "").lower(),
        "run_spec_sha256": str(run_spec_sha256 or "").lower(),
        "probe_receipt_sha256": str(probe_receipt_sha256 or "").lower(),
    }
    for name, value in actual_hashes.items():
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            errors.append(f"actual {name} is required")

    plan_without_body_hash = dict(action_plan)
    declared_plan_body_sha256 = str(plan_without_body_hash.pop("plan_body_sha256", "") or "").lower()
    computed_plan_body_sha256 = sha256_bytes(
        canonical_json(plan_without_body_hash).encode("utf-8")
    )
    if declared_plan_body_sha256 != computed_plan_body_sha256:
        errors.append("action plan body hash mismatch")

    plan_three_party = ((action_plan.get("bindings") or {}).get("three_party") or {})
    if str(plan_three_party.get("run_spec_sha256") or "").lower() != actual_hashes["run_spec_sha256"]:
        errors.append("action plan run_spec_sha256 does not match the supplied run spec")
    plan_three_party_body = dict(plan_three_party)
    declared_three_party_binding = str(plan_three_party_body.pop("binding_sha256", "") or "").lower()
    computed_three_party_binding = sha256_bytes(
        canonical_json(plan_three_party_body).encode("utf-8")
    )
    if declared_three_party_binding != computed_three_party_binding:
        errors.append("action plan three-party binding hash mismatch")
    plan_endpoint_policy = ((action_plan.get("bindings") or {}).get("endpoint_policy") or {})
    plan_adapter = ((action_plan.get("bindings") or {}).get("adapter") or {})
    probe_bindings = probe_receipt.get("bindings") or {}
    if str(probe_bindings.get("run_spec_sha256") or "").lower() != actual_hashes["run_spec_sha256"]:
        errors.append("probe receipt run_spec_sha256 does not match the supplied run spec")
    if str(probe_bindings.get("action_plan_sha256") or "").lower() != actual_hashes["action_plan_sha256"]:
        errors.append("probe receipt action_plan_sha256 does not match the supplied action plan")
    if probe_bindings.get("action_plan_id") != action_plan.get("plan_id"):
        errors.append("probe receipt action_plan_id does not match the supplied action plan")
    if probe_receipt.get("schema_version") != "sellerkit.browser_probe_receipt.v3":
        errors.append("probe verification receipt is not the execution-gated v3 contract")
    if probe_receipt.get("status") != "PASS_ZERO_WRITE":
        errors.append("probe verification receipt is not PASS_ZERO_WRITE")
    if probe_receipt.get("errors") != []:
        errors.append("probe verification receipt contains errors")
    if probe_receipt.get("zero_write_confirmed") is not True:
        errors.append("probe verification receipt did not confirm zero write")
    if probe_receipt.get("execution_authorized") is not True:
        errors.append("probe verification receipt does not authorize controlled execution")
    if probe_receipt.get("production_execution_authorized") is not True:
        errors.append("probe verification receipt does not authorize production execution")
    if probe_receipt.get("adapter_production_execution_ready") is not True:
        errors.append("probe verification receipt adapter production readiness is not certified")
    if probe_receipt.get("production_authorization_errors") != []:
        errors.append("probe verification receipt contains production authorization errors")
    if probe_receipt.get("authorization_errors") != []:
        errors.append("probe verification receipt contains execution authorization errors")
    probe_classifications = probe_receipt.get("request_classifications")
    if not isinstance(probe_classifications, list) or not probe_classifications:
        errors.append("probe verification receipt has no request classifications")
    else:
        for index, classification in enumerate(probe_classifications):
            decision = classification.get("decision") if isinstance(classification, dict) else None
            if decision != "ALLOW":
                errors.append(
                    f"probe request classification {index} is not ALLOW: {decision or 'UNKNOWN'}"
                )
    for key in ("input_probe_sha256", "page_fingerprint"):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(probe_receipt.get(key) or "")):
            errors.append(f"probe verification receipt has invalid {key}")
    if str(probe_bindings.get("endpoint_policy_sha256") or "").lower() != str(
        plan_endpoint_policy.get("sha256") or ""
    ).lower():
        errors.append("probe receipt endpoint policy does not match the action plan")
    if str(probe_bindings.get("adapter_sha256") or "").lower() != str(
        plan_adapter.get("sha256") or ""
    ).lower():
        errors.append("probe receipt adapter does not match the action plan")
    if str((run_spec.get("endpoint_policy") or {}).get("sha256") or "").lower() != str(
        plan_endpoint_policy.get("sha256") or ""
    ).lower():
        errors.append("run spec endpoint policy does not match the action plan")

    (
        expected_fields,
        expected_clears,
        expected_post_refresh_empty,
        field_set_sha256,
        clear_set_sha256,
        post_refresh_empty_set_sha256,
    ) = _expected_fill_contract(action_plan, errors)
    expected_bindings = {
        **actual_hashes,
        "action_plan_body_sha256": computed_plan_body_sha256,
        "expected_field_set_sha256": field_set_sha256,
        "expected_clear_set_sha256": clear_set_sha256,
        "expected_post_refresh_empty_set_sha256": post_refresh_empty_set_sha256,
    }
    _verify_fill_bindings(
        fill_execution, "fill execution", expected_bindings=expected_bindings, errors=errors
    )
    _verify_fill_bindings(
        post_refresh, "post-refresh", expected_bindings=expected_bindings, errors=errors
    )

    plan_target = action_plan.get("target") or {}
    expected_model = str(plan_target.get("model") or "")
    expected_template_id = str(plan_target.get("template_product_id") or "")
    expected_run_id = str(plan_target.get("run_id") or "")
    for label, document in (
        ("run spec", run_spec),
        ("probe receipt", probe_receipt),
        ("fill execution", fill_execution),
        ("post-refresh", post_refresh),
    ):
        if document.get("target_model") != expected_model:
            errors.append(f"{label} target_model mismatch")
        if str(document.get("template_product_id") or "") != expected_template_id:
            errors.append(f"{label} template_product_id mismatch")
    for label, document in (("fill execution", fill_execution), ("post-refresh", post_refresh)):
        if str(document.get("run_id") or "") != expected_run_id:
            errors.append(f"{label} run_id mismatch")

    _verify_fill_capture_chain(fill_execution, post_refresh, probe_receipt, errors)

    _verify_field_readbacks(fill_execution, "fill execution", expected_fields, errors)
    _verify_field_readbacks(post_refresh, "post-refresh", expected_fields, errors)
    _verify_clear_readbacks(
        fill_execution,
        "fill execution",
        expected_clears,
        "CLEAR_INHERITED_FIELDS",
        errors,
    )
    _verify_clear_readbacks(
        post_refresh,
        "post-refresh",
        expected_post_refresh_empty,
        "POST_REFRESH_REOPEN",
        errors,
    )

    expected_save_count = plan_target.get("maximum_draft_save_count")
    for label, document in (("fill execution", fill_execution), ("post-refresh", post_refresh)):
        if document.get("draft_save_count") != expected_save_count or expected_save_count != 1:
            errors.append(f"{label} draft_save_count must equal the action-plan limit of one")
        if document.get("final_write_count") != 0:
            errors.append(f"{label} final_write_count must be zero")
        if document.get("final_submit_triggered") is not False:
            errors.append(f"{label} final submit state is not explicitly false")
        if document.get("non_target_field_diffs") != []:
            errors.append(f"{label} non-target field diffs are not empty")

    if fill_execution.get("draft_id") != post_refresh.get("draft_id"):
        errors.append("post-refresh draft_id does not match the saved draft")
    if fill_execution.get("run_id") != post_refresh.get("run_id"):
        errors.append("post-refresh run_id does not match the fill execution")
    non_target_hashes = {
        str(fill_execution.get("non_target_snapshot_before_sha256") or "").lower(),
        str(fill_execution.get("non_target_snapshot_after_sha256") or "").lower(),
        str(post_refresh.get("non_target_snapshot_before_sha256") or "").lower(),
        str(post_refresh.get("non_target_snapshot_after_sha256") or "").lower(),
    }
    if len(non_target_hashes) != 1 or "" in non_target_hashes:
        errors.append("normalized non-target snapshots changed across fill or refresh")
    if post_refresh.get("refresh_performed") is not True:
        errors.append("post-refresh verification did not prove a refresh")
    if post_refresh.get("same_draft_reopened") is not True:
        errors.append("post-refresh verification did not reopen the same draft")
    if post_refresh.get("persistence_verified") is not True:
        errors.append("post-refresh persistence is not verified")

    unique_errors = sorted(set(errors))
    return {
        "schema_version": "sellerkit.fill_receipt_verification.v2",
        "generated_at": utc_now(),
        "target_model": expected_model or None,
        "template_product_id": expected_template_id or None,
        "bindings": expected_bindings,
        "expected_field_count": len(expected_fields),
        "expected_clear_count": len(expected_clears),
        "expected_post_refresh_empty_count": len(expected_post_refresh_empty),
        "fill_readback_count": len(fill_execution.get("field_readbacks") or []),
        "post_refresh_readback_count": len(post_refresh.get("field_readbacks") or []),
        "draft_save_count": fill_execution.get("draft_save_count"),
        "final_write_count": fill_execution.get("final_write_count"),
        "errors": unique_errors,
        "status": "PASS" if not unique_errors else "FAIL",
    }
