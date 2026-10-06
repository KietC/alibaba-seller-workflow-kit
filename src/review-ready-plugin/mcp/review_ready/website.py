from __future__ import annotations

from .config import get_settings

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin

from .config import Paths
from .utils import (
    extract_numbers_and_units,
    ensure_within,
    iter_urls,
    load_json,
    load_jsonl,
    normalize_model,
    normalize_space,
    parse_jsonish,
    sha256_file,
    strip_html,
    url_basename,
)


PAYLOAD_PATTERN = re.compile(
    r"<script\b(?=[^>]*\bid=[\"']product-detail-payload[\"'])[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
PRODUCT_ORIGIN = "https://catalog.example.invalid"
CORE_PRODUCT_IMAGE_ROLES = {"main_image", "gallery", "spec_value_link"}
MATERIALIZED_IMAGE_ROLES = {"main_image", "gallery"}


def _absolute_url(value: str) -> str:
    value = normalize_space(value)
    if not value:
        return ""
    return urljoin(f"{PRODUCT_ORIGIN}/", value)


def _load_asset_registry(paths: Paths) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_url: dict[str, dict[str, Any]] = {}
    by_page: dict[str, list[dict[str, Any]]] = {}
    for row in load_json(paths.website_assets_report):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        if url:
            by_url[url] = row
        for page in str(row.get("referenced_by") or "").split(" | "):
            if page:
                by_page.setdefault(page, []).append(row)
    return by_url, by_page


def _read_image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None, None


def _load_materialized_image_registry(paths: Paths) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Load and independently verify URL-bound website image materializations.

    The manifests are derived evidence.  A row is trusted only after its local
    path is proven to stay inside the project CAS and its declared SHA-256 is
    recomputed from the file.  Missing manifests are allowed so the initial
    website parse can still bootstrap the materialization workflow.
    """

    registry: dict[tuple[str, str, str], dict[str, Any]] = {}
    hash_cache: dict[Path, str] = {}
    for expected_role, manifest_path in (
        ("main_image", paths.website_main_images_path),
        ("gallery", paths.website_gallery_images_path),
    ):
        if not manifest_path.is_file():
            continue
        for row_number, row in enumerate(load_jsonl(manifest_path), start=1):
            role = str(row.get("role") or "")
            source_url = str(row.get("source_url") or "")
            product_id = str(row.get("product_id") or "")
            declared_sha256 = str(row.get("sha256") or "").lower()
            if role != expected_role:
                raise ValueError(f"{manifest_path}:{row_number} role mismatch: {role!r}")
            if row.get("release_id") != get_settings()["website_release_id"]:
                raise ValueError(f"{manifest_path}:{row_number} is not bound to approved current")
            if row.get("status") != "MATERIALIZED" or not source_url or not product_id:
                raise ValueError(f"{manifest_path}:{row_number} is not a complete materialized reference")
            if not re.fullmatch(r"[0-9a-f]{64}", declared_sha256):
                raise ValueError(f"{manifest_path}:{row_number} has invalid SHA-256")
            local_text = str(row.get("local_file") or "")
            if not local_text:
                raise ValueError(f"{manifest_path}:{row_number} has no local_file")
            local_path = ensure_within(Path(local_text), paths.website_image_blob_root)
            if not local_path.is_file():
                raise ValueError(f"{manifest_path}:{row_number} local file is missing: {local_path}")
            if local_path not in hash_cache:
                hash_cache[local_path] = sha256_file(local_path)
            if hash_cache[local_path] != declared_sha256:
                raise ValueError(f"{manifest_path}:{row_number} SHA-256 mismatch: {local_path}")
            width, height = _read_image_size(local_path)
            if width is None or height is None:
                raise ValueError(f"{manifest_path}:{row_number} is not a decodable image: {local_path}")
            declared_width = row.get("width")
            declared_height = row.get("height")
            if declared_width not in (None, width) or declared_height not in (None, height):
                raise ValueError(f"{manifest_path}:{row_number} decoded dimensions disagree with manifest")
            key = (role, product_id, source_url)
            if key in registry and registry[key]["sha256"] != declared_sha256:
                raise ValueError(f"Conflicting materialized image rows for {key}")
            registry[key] = {
                **row,
                "local_file": str(local_path),
                "local_file_exists": True,
                "sha256": declared_sha256,
                "size_bytes": local_path.stat().st_size,
                "width": width,
                "height": height,
            }
    return registry


def _material_urls(payload: dict[str, Any], product: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def add(role: str, value: Any) -> None:
        if isinstance(value, str) and value.strip():
            parsed = parse_jsonish(value, value)
        else:
            parsed = value
        if isinstance(parsed, str):
            parts = re.split(r",(?=https?://)", parsed)
            for part in parts:
                part = part.strip()
                if part.startswith(("http://", "https://", "/")):
                    found.append((role, _absolute_url(part)))
            return
        for url in iter_urls(parsed):
            found.append((role, _absolute_url(url)))

    add("main_image", product.get("picture"))
    add("gallery", product.get("ablums"))
    add("product_file", product.get("fileshow"))
    add("product_list_media", product.get("prolist"))
    add("selection_guide", product.get("howselect"))
    add("installation_video", product.get("instructionvideo"))
    add("spec_value_link", product.get("proattribute"))
    for key, role in (
        ("flyers", "flyer"),
        ("catalogs", "catalog"),
        ("series", "series"),
        ("instructions", "instruction"),
        ("displays", "product_display"),
        ("installationVideos", "installation_video"),
    ):
        add(role, payload.get(key, []))
    return found


def _asset_records(
    paths: Paths,
    payload: dict[str, Any],
    product: dict[str, Any],
    page_url: str,
    by_url: dict[str, dict[str, Any]],
    by_page: dict[str, list[dict[str, Any]]],
    materialized_images: dict[tuple[str, str, str], dict[str, Any]],
    hash_cache: dict[Path, str],
) -> list[dict[str, Any]]:
    candidates = _material_urls(payload, product)
    known_urls = {url for _, url in candidates}
    for observed in by_page.get(page_url, []):
        if observed.get("category") not in {"images", "media", "documents"}:
            continue
        url = str(observed.get("url") or "")
        if "/upload/" not in url.lower() or url in known_urls:
            continue
        candidates.append(("page_observed_asset", url))
        known_urls.add(url)

    deduped: dict[str, dict[str, Any]] = {}
    for order, (role, url) in enumerate(candidates, start=1):
        if not url or url in deduped:
            continue
        registry = by_url.get(url, {})
        core_product_image = role in CORE_PRODUCT_IMAGE_ROLES
        materialized = materialized_images.get((role, str(product.get("id") or ""), url), {})
        local_text = (
            str(materialized.get("local_file") or "")
            if role in MATERIALIZED_IMAGE_ROLES
            else ""
            if core_product_image
            else str(registry.get("local_file") or "")
        )
        allowed_root = paths.website_image_blob_root if materialized else paths.website_candidate_root / "assets"
        local_path = (
            ensure_within(Path(local_text), allowed_root)
            if local_text
            else None
        )
        exists = bool(local_path and local_path.is_file())
        file_hash = ""
        size_bytes: int | None = None
        width: int | None = None
        height: int | None = None
        if exists and local_path is not None:
            if local_path not in hash_cache:
                hash_cache[local_path] = sha256_file(local_path)
            file_hash = hash_cache[local_path]
            size_bytes = local_path.stat().st_size
            width, height = _read_image_size(local_path)
        deduped[url] = {
            "order": order,
            "role": role,
            "source_url": url,
            "source_basename": url_basename(url),
            "local_file": str(local_path) if local_path else "",
            "local_file_exists": exists,
            "size_bytes": size_bytes,
            "sha256": file_hash,
            "width": width,
            "height": height,
            "content_type": str(materialized.get("content_type") or ""),
            "image_format": str(materialized.get("image_format") or ""),
            "acquisition_method": str(materialized.get("acquisition_method") or ""),
            "materialization_reference_id": str(materialized.get("reference_id") or ""),
            "manual_upload_required": True,
            "remote_asset_id": "",
            "verification_status": (
                "LOCAL_URL_BOUND_VERIFIED"
                if materialized and exists and file_hash
                else "REMOTE_REFERENCE_UNMATERIALIZED"
                if core_product_image
                else "LOCAL_PAGE_REFERENCE_UNBOUND"
                if role == "page_observed_asset" and exists and file_hash
                else "LOCAL_URL_BOUND_VERIFIED"
                if exists and file_hash
                else "REMOTE_REFERENCE_UNMATERIALIZED"
            ),
        }
    return list(deduped.values())


def _safe_materials(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    allowed = ("id", "name", "picture", "file", "youtubeurl", "isyoutube", "addtime")
    for source_key, output_key in (
        ("flyers", "flyers"),
        ("catalogs", "catalogs"),
        ("series", "series"),
        ("instructions", "instructions"),
        ("displays", "displays"),
        ("installationVideos", "installation_videos"),
    ):
        rows: list[dict[str, Any]] = []
        for item in payload.get(source_key) or []:
            if not isinstance(item, dict):
                continue
            row = {key: item.get(key) for key in allowed if item.get(key) not in (None, "")}
            if row:
                rows.append(row)
        output[output_key] = rows
    return output


def parse_official_products(paths: Paths) -> list[dict[str, Any]]:
    by_url, by_page = _load_asset_registry(paths)
    materialized_images = _load_materialized_image_registry(paths)
    release_receipt = load_json(paths.website_release_receipt)
    release_created_at = str(release_receipt.get("created_at_utc") or "")
    products: list[dict[str, Any]] = []
    seen_models: dict[str, Path] = {}
    hash_cache: dict[Path, str] = {}
    for html_path in sorted(paths.website_products_root.rglob("index.html"), key=lambda item: str(item).lower()):
        raw = html_path.read_text(encoding="utf-8")
        match = PAYLOAD_PATTERN.search(raw)
        if not match:
            continue
        payload = json.loads(match.group(1))
        product = payload.get("product") or {}
        raw_model = normalize_space(product.get("xinghao"))
        canonical_model = normalize_model(raw_model)
        if not canonical_model:
            raise ValueError(f"Missing product model in {html_path}")
        if canonical_model in seen_models:
            raise ValueError(f"Duplicate canonical model {canonical_model}: {seen_models[canonical_model]} and {html_path}")
        seen_models[canonical_model] = html_path

        attributes_value = parse_jsonish(product.get("proattribute"), [])
        if not isinstance(attributes_value, list):
            raise ValueError(f"proattribute is not a list for {raw_model}")
        attributes: list[dict[str, Any]] = []
        for position, attribute in enumerate(attributes_value, start=1):
            if not isinstance(attribute, dict):
                continue
            raw_value = normalize_space(attribute.get("value"))
            numbers, units = extract_numbers_and_units(raw_value)
            attributes.append(
                {
                    "position": position,
                    "attribute_id": attribute.get("id"),
                    "name": normalize_space(attribute.get("name")),
                    "name_key": normalize_space(attribute.get("name")).casefold(),
                    "raw_value": raw_value,
                    "is_empty": raw_value == "",
                    "value_state": "EMPTY" if raw_value == "" else "PRESENT",
                    "numeric_values": numbers,
                    "unit_tokens": units,
                    "is_cover": bool(attribute.get("iscover")),
                    "name_link_raw": normalize_space(attribute.get("namelink")),
                    "value_link_raw": normalize_space(attribute.get("valuelink")),
                    "source_locator": f"/product/proattribute/{position - 1}",
                }
            )

        relative = html_path.relative_to(paths.website_legacy_root)
        route = "/" + "/".join(relative.parts[:-1])
        route_leaf = unquote(relative.parts[-2])
        if normalize_model(route_leaf) != canonical_model:
            raise ValueError(f"Route/model mismatch for {raw_model}: {route_leaf}")
        model_attributes = [
            row for row in attributes if row["name_key"] == "model no." and normalize_model(row["raw_value"]) == canonical_model
        ]
        if not model_attributes:
            raise ValueError(f"Model No. attribute mismatch for {raw_model}")
        page_url = f"{PRODUCT_ORIGIN}{route}"
        source_hash = sha256_file(html_path)
        payload_hash = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()
        assets = _asset_records(
            paths,
            payload,
            product,
            page_url,
            by_url,
            by_page,
            materialized_images,
            hash_cache,
        )
        products.append(
            {
                "schema_version": "sellerkit.website_product.v1",
                "product_id": product.get("id"),
                "canonical_model": canonical_model,
                "raw_model": raw_model,
                "guid_advisory": normalize_space(product.get("guid")),
                "name": normalize_space(product.get("name")),
                "category": normalize_space((payload.get("oneclass") or {}).get("name")),
                "subcategory": normalize_space((payload.get("twoclass") or {}).get("name")),
                "taxonomy": {
                    "category_id": (payload.get("oneclass") or {}).get("id"),
                    "category_slug": relative.parts[1] if len(relative.parts) > 1 else "",
                    "subcategory_id": (payload.get("twoclass") or {}).get("id"),
                    "subcategory_slug": relative.parts[2] if len(relative.parts) > 2 else "",
                },
                "short_description_html": str(product.get("smalltitle") or ""),
                "short_description_text": strip_html(product.get("smalltitle")),
                "description_html": str(product.get("describe") or ""),
                "description_text": strip_html(product.get("describe")),
                "details_html": str(product.get("details") or ""),
                "details_text": strip_html(product.get("details")),
                "attributes": attributes,
                "assets": assets,
                "materials": _safe_materials(payload),
                "source": {
                    "source_type": "official_current",
                    "source_path": str(html_path),
                    "source_route": route,
                    "source_url": page_url,
                    "source_sha256": source_hash,
                    "payload_sha256": payload_hash,
                    "source_locator": "script#product-detail-payload",
                    "release_id": get_settings()["website_release_id"],
                    "observed_at_utc": release_created_at,
                    "capture_snapshot": get_settings()["capture_snapshot"],
                    "source_disposition": "OFFICIAL_CURRENT_REVIEW_REQUIRED",
                    "approval_status": "needs_review",
                },
            }
        )
    return sorted(products, key=lambda item: item["canonical_model"])


def official_stats(products: list[dict[str, Any]]) -> dict[str, Any]:
    attributes = [attribute for product in products for attribute in product["attributes"]]
    assets = [asset for product in products for asset in product["assets"]]
    main_images = [asset for asset in assets if asset["role"] == "main_image"]
    galleries = [asset for asset in assets if asset["role"] == "gallery"]
    core_images = main_images + galleries
    return {
        "product_count": len(products),
        "unique_model_count": len({product["canonical_model"] for product in products}),
        "attribute_count": len(attributes),
        "non_empty_attribute_count": sum(not item["is_empty"] for item in attributes),
        "empty_attribute_count": sum(item["is_empty"] for item in attributes),
        "category_count": len({product["category"] for product in products}),
        "subcategory_count": len({product["subcategory"] for product in products}),
        "asset_reference_count": len(assets),
        "locally_verified_supplemental_asset_count": sum(
            asset["verification_status"] == "LOCAL_URL_BOUND_VERIFIED" for asset in assets
        ),
        "main_image_reference_count": len(main_images),
        "main_image_materialized_count": sum(
            asset["verification_status"] == "LOCAL_URL_BOUND_VERIFIED" for asset in main_images
        ),
        "gallery_reference_count": len(galleries),
        "gallery_materialized_count": sum(
            asset["verification_status"] == "LOCAL_URL_BOUND_VERIFIED" for asset in galleries
        ),
        "core_product_image_remote_unmaterialized_count": sum(
            asset["verification_status"] == "REMOTE_REFERENCE_UNMATERIALIZED" for asset in core_images
        ),
    }
