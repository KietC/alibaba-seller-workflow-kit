# Archive tools are local utilities, not authority to collect private platform data.
# 归档工具仅是本地工具，不代表获得采集平台私有数据的权限。
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.generator import BytesGenerator
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup


SCHEMA_VERSION = "1.0.0"
ALLOWED_HOST_SUFFIXES = (
    ".alicdn.com",
    ".alibaba.com",
    ".alibabacloud.com",
)
PRODUCT_MEDIA_PATH_MARKERS = ("/kf/", "/imgextra/", "/product/")
ASSET_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".avif",
    ".gif",
    ".svg",
    ".mp4",
    ".webm",
    ".mov",
    ".m3u8",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
}
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "auth",
    "authorization",
    "cookie",
    "ctoken",
    "refresh_token",
    "session",
    "sign",
    "signature",
    "token",
    "x-xsrf-token",
    "xsrf",
}
SECRET_PATTERNS = {
    "sensitive_query": re.compile(
        rb"(?i)(?:[?&]|&amp;|%3[fF]|%26)"
        rb"(?:access_token|refresh_token|x-xsrf-token|ctoken|session|cookie|authorization|signature|sign|token)"
        rb"(?:%3[dD]|=)[^&\s\"'<>]{8,}"
    ),
    "cookie_header": re.compile(rb"(?im)^[^\S\r\n]*(?:set-)?cookie\s*:\s*\S+"),
    "bearer": re.compile(rb"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}"),
}
PUBLIC_TRANSIENT_QUERY_ALLOWLIST = {
    "alibaba_public_favorite_guest_ctoken": re.compile(
        rb"(?i)us-favorite\.alibaba\.com/favorite2/ajax/checkFavorites\.htm"
        rb"[^\"'<>]{0,500}(?:[?&]|&amp;)ctoken=[^&\s\"'<>]{8,}"
    ),
    "alibaba_public_review_jsonp_sign": re.compile(
        rb"(?i)acs\.h\.alibaba\.com/h5/mtop\.alibaba\.icbu\.review\.new\.shopreview\.scatter/"
        rb"[^\"'<>]{0,1000}(?:[?&]|&amp;)sign=[^&\s\"'<>]{8,}"
    ),
    "alibaba_public_aplus_analytics_sign": re.compile(
        rb"(?is)(?=.*dmtrack_b=)(?=.*(?:[?&]|&amp;|%3f|%26)sign(?:=|%3d))(?=.*plugins=\"aplus_plugin_icbu/)"
    ),
}
PUBLIC_QUERY_URL_RE = re.compile(
    rb"(?i)(?:(?<=\")|(?<=')|(?<=\s)|^)(?P<url>(?:https?:)?//[^\s\"'<>]+)"
)
SCRIPT_OPEN_TAG_RE = re.compile(
    # Only identify starts. A single-pass quote-aware lexer below consumes
    # complete opening tags without rescanning every unterminated start.
    rb"(?i)<script\b"
)
QUOTED_ATTRIBUTE_RE = re.compile(
    rb"(?is)\s(?P<name>[a-z_:][a-z0-9_:.-]*)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
PUBLIC_QUERY_REASONS = {
    "alibaba_public_favorite_guest_ctoken": "Current ctoken parameter is bound to the exact public favorite-state URL host and path.",
    "alibaba_public_review_jsonp_sign": "Current sign parameter is bound to the exact public review JSONP URL host and versioned path.",
    "alibaba_public_aplus_analytics_sign": "Current sign parameter is inside dmtrack_b on the same script opening tag as the aplus plugin marker.",
}
TEXT_SCAN_SUFFIXES = {".json", ".jsonl", ".html", ".mhtml", ".md", ".csv", ".txt", ".svg", ".xml", ".js", ".mjs"}
CAPTURE_ARTIFACTS = (
    "public_response.html",
    "public_rendered.html",
    "public_page_dom.json",
    "public_fullpage.png",
    "capture_receipt.json",
)
DERIVED_ARTIFACTS = (
    "product_snapshot.json",
    "asset_manifest.jsonl",
    "public_page.mhtml",
)
REQUIRED_PRODUCT_ARTIFACTS = CAPTURE_ARTIFACTS + DERIVED_ARTIFACTS
EVIDENCE_MANIFEST_PATH = Path("07_审计") / "evidence_manifest.json"
EVIDENCE_MANIFEST_SHA_PATH = Path("07_审计") / "evidence_manifest.json.sha256"
COMMAND_LOG_PATH = Path("07_审计") / "commands.jsonl"
REQUEST_LEDGER_PATH = Path("07_审计") / "public_request_ledger.jsonl"
BROWSER_CAPTURE_CHECKPOINT_PATH = Path("00_运行状态") / "browser_capture_checkpoint.json"


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def text_dump(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def jsonl_dump(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def jsonl_append(path: Path, row: dict[str, Any]) -> None:
    """Append one durable audit record without rewriting earlier records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_url(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().replace("\\/", "/")
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return None


def safe_public_url(raw: str) -> str:
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def url_receipt(raw: str) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(raw)
    return {
        "origin": f"{parsed.scheme}://{parsed.netloc}",
        "path": parsed.path or "/",
        "url_sha256": sha256_bytes(raw.encode("utf-8")),
    }


def host_allowed(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == suffix[1:] or host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def flatten_json_ld(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from flatten_json_ld(item)
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from flatten_json_ld(item)


def is_product_json_ld(item: dict[str, Any]) -> bool:
    raw_type = item.get("@type")
    return raw_type == "Product" or (isinstance(raw_type, list) and "Product" in raw_type)


def parse_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rows.extend(flatten_json_ld(payload))
    return rows


def parse_json_ld_texts(texts: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in texts:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rows.extend(flatten_json_ld(payload))
    return rows


def iter_srcset(raw: str | None) -> Iterable[str]:
    if not raw:
        return
    for candidate in raw.split(","):
        url = candidate.strip().split(" ", 1)[0]
        if url:
            yield url


def classify_asset(url: str, tag_name: str, context: str, gallery_urls: set[str]) -> str:
    clean = safe_public_url(url)
    if clean in gallery_urls:
        return "gallery"
    lowered = context.lower()
    suffix = Path(urllib.parse.urlsplit(clean).path).suffix.lower()
    if tag_name in {"video", "source"} or suffix in {".mp4", ".webm", ".mov", ".m3u8"}:
        return "video"
    if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip"}:
        return "document"
    if any(word in lowered for word in ("description", "detail", "rich-text", "module_product_detail")):
        return "description"
    if any(word in lowered for word in ("recommend", "related", "you-may", "hot-sale")):
        return "recommendation"
    if any(marker in urllib.parse.urlsplit(clean).path.lower() for marker in PRODUCT_MEDIA_PATH_MARKERS):
        return "product_media"
    return "page_ui"


def collect_assets(soup: BeautifulSoup, gallery_urls: list[str]) -> list[dict[str, Any]]:
    gallery_set = {safe_public_url(url) for url in gallery_urls}
    candidates: list[tuple[str, str, str]] = []
    attr_map = {
        "img": ("src", "data-src", "data-lazy-src", "data-original"),
        "video": ("src", "poster"),
        "source": ("src",),
        "a": ("href",),
    }
    for tag_name, attrs in attr_map.items():
        for tag in soup.find_all(tag_name):
            context = " ".join(
                str(value)
                for value in (
                    tag.get("class", []),
                    tag.get("id", ""),
                    tag.parent.get("class", []) if tag.parent else [],
                    tag.parent.get("id", "") if tag.parent else "",
                )
            )
            for attr in attrs:
                url = normalize_url(tag.get(attr))
                if url:
                    candidates.append((url, tag_name, context))
            for attr in ("srcset", "data-srcset"):
                for raw in iter_srcset(tag.get(attr)):
                    url = normalize_url(raw)
                    if url:
                        candidates.append((url, tag_name, context))

    for url in gallery_urls:
        candidates.append((url, "json_ld", "gallery"))

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw, tag_name, context in candidates:
        try:
            clean = safe_public_url(raw)
        except ValueError:
            continue
        if clean in seen or not host_allowed(clean):
            continue
        suffix = Path(urllib.parse.urlsplit(clean).path).suffix.lower()
        if tag_name == "a" and suffix not in ASSET_EXTENSIONS:
            continue
        seen.add(clean)
        role = classify_asset(clean, tag_name, context, gallery_set)
        receipt = url_receipt(raw)
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "asset_id": f"asset_{len(rows) + 1:04d}",
                "role": role,
                "order": len(rows) + 1,
                "source_origin": receipt["origin"],
                "source_path": receipt["path"],
                "source_url_sha256": receipt["url_sha256"],
                "download_url": clean,
                "download_status": "pending",
            }
        )
    return rows


def collect_assets_from_dom(dom: dict[str, Any], gallery_urls: list[str]) -> list[dict[str, Any]]:
    gallery_set = {safe_public_url(url) for url in gallery_urls}
    candidates: list[tuple[str, str, str]] = []
    for image in dom.get("images", []):
        context = " ".join(str(image.get(key) or "") for key in ("class_name", "parent_class", "alt"))
        for key in ("current_src", "src", "data_src"):
            url = normalize_url(image.get(key))
            if url:
                candidates.append((url, "img", context))
        for raw in iter_srcset(image.get("srcset")):
            url = normalize_url(raw)
            if url:
                candidates.append((url, "img", context))
    for video in dom.get("videos", []):
        for key in ("src", "poster"):
            url = normalize_url(video.get(key))
            if url:
                candidates.append((url, "video", str(video.get("class_name") or "")))
        for raw in video.get("sources", []):
            url = normalize_url(raw)
            if url:
                candidates.append((url, "source", str(video.get("class_name") or "")))
    for raw in dom.get("document_links", []):
        url = normalize_url(raw)
        if url:
            candidates.append((url, "a", "document"))
    for url in gallery_urls:
        candidates.append((url, "json_ld", "gallery"))

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw, tag_name, context in candidates:
        try:
            clean = safe_public_url(raw)
        except ValueError:
            continue
        if clean in seen or not host_allowed(clean):
            continue
        seen.add(clean)
        role = classify_asset(clean, tag_name, context, gallery_set)
        receipt = url_receipt(raw)
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "asset_id": f"asset_{len(rows) + 1:04d}",
                "role": role,
                "order": len(rows) + 1,
                "source_origin": receipt["origin"],
                "source_path": receipt["path"],
                "source_url_sha256": receipt["url_sha256"],
                "download_url": clean,
                "download_status": "pending",
            }
        )
    return rows


def first_product_json_ld(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        if is_product_json_ld(row):
            return row
    return {}


def extract_assigned_json(text: str, marker: str) -> dict[str, Any]:
    index = text.find(marker)
    if index < 0:
        return {}
    start = index + len(marker)
    while start < len(text) and text[start].isspace():
        start += 1
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def sanitize_public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_public_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_public_value(item) for item in value]
    if isinstance(value, str):
        normalized = normalize_url(value)
        if normalized:
            try:
                return safe_public_url(normalized)
            except ValueError:
                return value
    return value


def collect_assets_from_detail_data(detail_data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[tuple[str, str]] = []

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + (str(key),))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + (str(index),))
        elif isinstance(value, str):
            url = normalize_url(value)
            if url and host_allowed(url):
                candidates.append((url, ".".join(path).lower()))

    walk(detail_data, ())
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw, context in candidates:
        clean = safe_public_url(raw)
        if clean in seen:
            continue
        seen.add(clean)
        suffix = Path(urllib.parse.urlsplit(clean).path).suffix.lower()
        if "mediaitems" in context and any(word in context for word in (".big", ".originalurl")):
            role = "gallery"
        elif "description" in context or "productdetail" in context:
            role = "description"
        elif "video" in context or suffix in {".mp4", ".webm", ".mov", ".m3u8"}:
            role = "video"
        elif suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip"}:
            role = "document"
        elif any(marker in urllib.parse.urlsplit(clean).path.lower() for marker in PRODUCT_MEDIA_PATH_MARKERS):
            role = "product_media"
        else:
            role = "page_ui"
        receipt = url_receipt(raw)
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "asset_id": f"asset_{len(rows) + 1:04d}",
                "role": role,
                "order": len(rows) + 1,
                "source_origin": receipt["origin"],
                "source_path": receipt["path"],
                "source_url_sha256": receipt["url_sha256"],
                "download_url": clean,
                "download_status": "pending",
                "detail_data_path": context,
            }
        )
    return rows


def merge_asset_rows(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in primary + secondary:
        key = (row["source_origin"], row["source_path"])
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item["asset_id"] = f"asset_{len(merged) + 1:04d}"
        item["order"] = len(merged) + 1
        merged.append(item)
    return merged


def sanitize_rendered_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        script.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
                continue
            if attr.lower() not in {"src", "href", "action", "poster"}:
                continue
            value = tag.attrs.get(attr)
            if not isinstance(value, str):
                continue
            normalized = normalize_url(value)
            if normalized:
                tag.attrs[attr] = safe_public_url(normalized)
    return "<!DOCTYPE html>\n" + str(soup)


def product_dir(run_root: Path, product_id: str) -> Path:
    return run_root / "03_商品标准化" / product_id


def extract_product(run_root: Path, product_id: str) -> dict[str, Any]:
    target = product_dir(run_root, product_id)
    html_path = target / "public_rendered.html"
    html = html_path.read_text(encoding="utf-8", errors="replace")
    raw_soup = BeautifulSoup(html, "html.parser")
    response_path = target / "public_response.html"
    response_html = response_path.read_text(encoding="utf-8", errors="replace") if response_path.exists() else html
    detail_data = extract_assigned_json(response_html, "window.detailData =")
    safe_detail_data = sanitize_public_value(detail_data)
    detail_path = target / "public_detail_data.json"
    if safe_detail_data:
        json_dump(detail_path, safe_detail_data)
    sanitized_html = sanitize_rendered_html(html)
    sanitized_path = target / "public_rendered_sanitized.html"
    text_dump(sanitized_path, sanitized_html)
    html = sanitized_html
    soup = BeautifulSoup(html, "html.parser")
    dom_path = target / "public_page_dom.json"
    dom = json.loads(dom_path.read_text(encoding="utf-8")) if dom_path.exists() else {}
    ld_rows = parse_json_ld_texts(dom.get("json_ld", [])) or parse_json_ld(raw_soup)
    product = first_product_json_ld(ld_rows)
    raw_images = product.get("image", []) if isinstance(product, dict) else []
    if isinstance(raw_images, str):
        raw_images = [raw_images]
    gallery_urls = [safe_public_url(url) for raw in raw_images if (url := normalize_url(str(raw)))]
    dom_assets = collect_assets_from_dom(dom, gallery_urls) if dom else collect_assets(soup, gallery_urls)
    detail_assets = collect_assets_from_detail_data(detail_data) if detail_data else []
    assets = merge_asset_rows(detail_assets, dom_assets)

    catalog_row: dict[str, Any] = {}
    index_path = run_root / "01_商品索引" / "catalog_index.jsonl"
    for line in index_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if str(row.get("product_id")) == str(product_id):
            catalog_row = row
            break

    description_text = None
    if isinstance(product, dict):
        description_text = product.get("description")
    if not description_text:
        description = soup.select_one('[class*="description"], [id*="description"], [class*="detail"]')
        description_text = description.get_text(" ", strip=True)[:200_000] if description else None

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "run_id": catalog_row.get("run_id"),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "product_id": str(product_id),
        "historical_observation_only": True,
        "approved_fact": False,
        "catalog_observation": catalog_row,
        "public_page": {
            "title": dom.get("title") or (soup.title.get_text(" ", strip=True) if soup.title else None),
            "canonical": dom.get("canonical") or ((soup.select_one('link[rel="canonical"]') or {}).get("href") if soup.select_one('link[rel="canonical"]') else None),
            "h1": dom.get("h1") or (soup.h1.get_text(" ", strip=True) if soup.h1 else None),
            "description_text": description_text,
            "json_ld_product": product,
            "json_ld_record_count": len(ld_rows),
            "detail_data_core": {
                "product": safe_detail_data.get("globalData", {}).get("product", {}),
                "trade": safe_detail_data.get("globalData", {}).get("trade", {}),
                "seller": safe_detail_data.get("globalData", {}).get("seller", {}),
                "seo": safe_detail_data.get("globalData", {}).get("seo", {}),
            } if safe_detail_data else {},
        },
        "asset_count": len(assets),
        "source_artifacts": {
            "rendered_html": str(html_path.relative_to(run_root)),
            "rendered_html_sha256": sha256_file(html_path),
            "sanitized_rendered_html": str(sanitized_path.relative_to(run_root)),
            "sanitized_rendered_html_sha256": sha256_file(sanitized_path),
            "screenshot": str((target / "public_fullpage.png").relative_to(run_root)),
            "structured_dom": str(dom_path.relative_to(run_root)) if dom_path.exists() else None,
            "response_html": str(response_path.relative_to(run_root)) if response_path.exists() else None,
            "response_html_sha256": sha256_file(response_path) if response_path.exists() else None,
            "public_detail_data": str(detail_path.relative_to(run_root)) if detail_path.exists() else None,
        },
    }
    json_dump(target / "product_snapshot.json", snapshot)
    manifest_path = target / "asset_manifest.jsonl"
    if manifest_path.exists():
        previous = {
            (row.get("source_origin"), row.get("source_path")): row
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
            for row in (json.loads(line),)
        }
        for asset in assets:
            old = previous.get((asset.get("source_origin"), asset.get("source_path")))
            if old and old.get("download_status") in {"downloaded", "excluded_non_product"}:
                for key in ("download_status", "blob_sha256", "bytes", "content_type", "local_path"):
                    if key in old:
                        asset[key] = old[key]
                asset.pop("download_url", None)
    jsonl_dump(manifest_path, assets)
    return {"product_id": product_id, "asset_count": len(assets), "gallery_count": len(gallery_urls)}


def suffix_for(content_type: str | None, url: str) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
    if media_type in mapping:
        return mapping[media_type]
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    if suffix in ASSET_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(media_type)
    return ".jpg" if guessed in {".jpe", ".jpeg"} else (guessed or ".bin")


@dataclass
class DownloadResult:
    sha256: str
    bytes: int
    content_type: str | None
    path: Path


def download_to_store(
    url: str,
    store: Path,
    quarantine: Path,
    max_bytes: int,
    retries: int = 3,
) -> DownloadResult:
    if not host_allowed(url):
        raise ValueError(f"host not allowed: {urllib.parse.urlsplit(url).hostname}")
    quarantine.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        descriptor, temp_name = tempfile.mkstemp(prefix="asset_", suffix=".partial", dir=quarantine)
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/*,video/*,application/pdf,*/*;q=0.6",
                    "Referer": "https://www.alibaba.com/",
                },
            )
            digest = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(request, timeout=45) as response, temp_path.open("wb") as handle:
                content_type = response.headers.get("Content-Type")
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError(f"content-length {length} exceeds cap {max_bytes}")
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"stream exceeded cap {max_bytes}")
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            sha = digest.hexdigest()
            suffix = suffix_for(content_type, url)
            target = store / sha[:2] / f"{sha}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, target)
            return DownloadResult(sha256=sha, bytes=total, content_type=content_type, path=target)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
            temp_path.unlink(missing_ok=True)
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {408, 429, 500, 502, 503, 504}
            if not retryable or attempt == retries:
                break
            retry_after = None
            if isinstance(exc, urllib.error.HTTPError):
                retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(8.0, 0.75 * (2 ** (attempt - 1)))
            time.sleep(delay)
    raise RuntimeError(f"download failed after {retries} attempts: {type(last_error).__name__}: {last_error}")


def download_product_assets(run_root: Path, product_id: str, max_bytes: int) -> dict[str, Any]:
    target = product_dir(run_root, product_id)
    manifest_path = target / "asset_manifest.jsonl"
    rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    store = run_root / "05_素材库" / "sha256"
    quarantine = run_root / "09_quarantine"
    success = 0
    failure = 0
    for row in rows:
        if row.get("role") in {"page_ui", "recommendation"}:
            row["download_status"] = "excluded_non_product"
            row.pop("download_url", None)
            continue
        url = row.get("download_url")
        try:
            result = download_to_store(url, store, quarantine, max_bytes)
            row.update(
                {
                    "download_status": "downloaded",
                    "blob_sha256": result.sha256,
                    "bytes": result.bytes,
                    "content_type": result.content_type,
                    "local_path": str(result.path.relative_to(run_root)),
                }
            )
            success += 1
        except Exception as exc:
            row.update(
                {
                    "download_status": "failed",
                    "error_code": type(exc).__name__,
                    "error_summary": str(exc)[:300],
                }
            )
            failure += 1
        finally:
            row.pop("download_url", None)
    jsonl_dump(manifest_path, rows)
    return {"product_id": product_id, "downloaded": success, "failed": failure, "total": len(rows)}


def download_all_assets(run_root: Path, product_ids: list[str], max_bytes: int, workers: int) -> dict[str, Any]:
    manifests: dict[str, list[dict[str, Any]]] = {}
    references: dict[tuple[str, str], list[tuple[str, int]]] = {}
    source_urls: dict[tuple[str, str], str] = {}
    resolved: dict[tuple[str, str], dict[str, Any]] = {}
    validated_existing: dict[str, bool] = {}
    excluded = 0

    for product_id in product_ids:
        path = product_dir(run_root, product_id) / "asset_manifest.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        manifests[product_id] = rows
        for index, row in enumerate(rows):
            key = (row.get("source_origin"), row.get("source_path"))
            if row.get("role") in {"page_ui", "recommendation"}:
                row["download_status"] = "excluded_non_product"
                row.pop("download_url", None)
                excluded += 1
                continue
            references.setdefault(key, []).append((product_id, index))
            if row.get("download_status") == "downloaded" and row.get("blob_sha256"):
                local_path = str(row.get("local_path") or "")
                blob_path = run_root / Path(local_path) if local_path else None
                expected_sha = str(row.get("blob_sha256") or "").lower()
                expected_size = int(row.get("bytes", -1))
                cache_key = f"{blob_path}|{expected_size}|{expected_sha}" if blob_path is not None else ""
                if cache_key not in validated_existing:
                    validated_existing[cache_key] = bool(
                        blob_path is not None
                        and blob_path.is_file()
                        and blob_path.stat().st_size == expected_size
                        and blob_path.stem.lower() == expected_sha
                        and sha256_file(blob_path) == expected_sha
                    )
                reusable = validated_existing[cache_key]
                if reusable:
                    resolved[key] = {
                        name: row.get(name)
                        for name in ("download_status", "blob_sha256", "bytes", "content_type", "local_path")
                    }
            source_url = row.get("download_url") or f"{row.get('source_origin') or ''}{row.get('source_path') or ''}"
            if key not in resolved and normalize_url(source_url) and host_allowed(source_url):
                source_urls.setdefault(key, safe_public_url(source_url))

    store = run_root / "05_素材库" / "sha256"
    quarantine = run_root / "09_quarantine"

    def job(key: tuple[str, str], url: str) -> tuple[tuple[str, str], dict[str, Any]]:
        try:
            result = download_to_store(url, store, quarantine, max_bytes)
            return key, {
                "download_status": "downloaded",
                "blob_sha256": result.sha256,
                "bytes": result.bytes,
                "content_type": result.content_type,
                "local_path": str(result.path.relative_to(run_root)),
            }
        except Exception as exc:
            return key, {
                "download_status": "failed",
                "error_code": type(exc).__name__,
                "error_summary": str(exc)[:300],
            }

    pending = {key: url for key, url in source_urls.items() if key not in resolved}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(job, key, url) for key, url in pending.items()]
        for future in as_completed(futures):
            key, result = future.result()
            resolved[key] = result

    for key, refs in references.items():
        result = resolved.get(key, {"download_status": "failed", "error_code": "missing_download_url"})
        for product_id, index in refs:
            row = manifests[product_id][index]
            row.update(result)
            row.pop("download_url", None)

    for product_id, rows in manifests.items():
        jsonl_dump(product_dir(run_root, product_id) / "asset_manifest.jsonl", rows)

    downloaded_unique = sum(result.get("download_status") == "downloaded" for result in resolved.values())
    failed_unique = sum(result.get("download_status") == "failed" for result in resolved.values())
    return {
        "products": len(manifests),
        "unique_asset_references": len(references),
        "new_download_jobs": len(pending),
        "downloaded_unique": downloaded_unique,
        "failed_unique": failed_unique,
        "excluded_rows": excluded,
    }


def build_mhtml(run_root: Path, product_id: str) -> dict[str, Any]:
    target = product_dir(run_root, product_id)
    captured_html_path = target / "public_rendered.html"
    sanitized_html_path = target / "public_rendered_sanitized.html"
    if sanitized_html_path.exists():
        html = sanitized_html_path.read_text(encoding="utf-8", errors="replace")
    else:
        html = sanitize_rendered_html(captured_html_path.read_text(encoding="utf-8", errors="replace"))
    snapshot = json.loads((target / "product_snapshot.json").read_text(encoding="utf-8"))
    page_url = snapshot.get("catalog_observation", {}).get("detail_url") or snapshot.get("public_page", {}).get("canonical")
    if page_url:
        page_url = safe_public_url(normalize_url(page_url) or page_url)
    else:
        page_url = f"https://www.alibaba.com/product-detail/{product_id}.html"

    message = MIMEMultipart("related", type="text/html")
    message["Subject"] = f"Alibaba product forensic snapshot {product_id}"
    message["MIME-Version"] = "1.0"
    message["Snapshot-Content-Location"] = page_url
    html_part = MIMEText(html, "html", "utf-8")
    html_part["Content-Location"] = page_url
    message.attach(html_part)

    attached: set[str] = set()
    manifest_path = target / "asset_manifest.jsonl"
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        local = row.get("local_path")
        sha = row.get("blob_sha256")
        if not local or not sha or sha in attached:
            continue
        path = run_root / local
        if not path.exists():
            continue
        content_type = (row.get("content_type") or "application/octet-stream").split(";", 1)[0]
        maintype, _, subtype = content_type.partition("/")
        part = MIMEApplication(path.read_bytes(), _subtype=subtype or "octet-stream")
        part.replace_header("Content-Type", content_type)
        part["Content-ID"] = f"<{sha}@sellerkit-local>"
        part["Content-Location"] = row["source_origin"] + row["source_path"]
        message.attach(part)
        attached.add(sha)

    out = target / "public_page.mhtml"
    tmp = out.with_suffix(out.suffix + ".partial")
    with tmp.open("wb") as handle:
        BytesGenerator(handle, maxheaderlen=0).flatten(message)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, out)
    return {"product_id": product_id, "mhtml_bytes": out.stat().st_size, "mhtml_sha256": sha256_file(out), "attachments": len(attached)}


def load_catalog_rows(run_root: Path) -> list[dict[str, Any]]:
    path = run_root / "01_商品索引" / "catalog_index.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize_request_ledger(run_root: Path) -> dict[str, Any]:
    path = run_root / REQUEST_LEDGER_PATH
    rows: list[dict[str, Any]] = []
    invalid_lines: list[int] = []
    if path.is_file():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines.append(line_number)
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                invalid_lines.append(line_number)

    def counts(key: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in rows:
            value = str(row.get(key))
            result[value] = result.get(value, 0) + 1
        return dict(sorted(result.items()))

    captured_product_ids = sorted(
        {
            str(row.get("product_id"))
            for row in rows
            if row.get("outcome") == "captured" and row.get("product_id") is not None
        }
    )
    captured_set_bytes = (
        ("\n".join(captured_product_ids) + "\n").encode("utf-8")
        if captured_product_ids
        else b""
    )
    return {
        "path": REQUEST_LEDGER_PATH.as_posix(),
        "row_count": len(rows),
        "invalid_line_numbers": invalid_lines,
        "methods": counts("method"),
        "http_statuses": counts("http_status"),
        "outcomes": counts("outcome"),
        "external_write_rows": sum(1 for row in rows if row.get("external_write") is True),
        "captured_product_id_count": len(captured_product_ids),
        "captured_product_ids": captured_product_ids,
        "captured_product_id_set_sha256": sha256_bytes(captured_set_bytes),
    }


def reconcile_completed_browser_checkpoint(
    run_root: Path,
    rows: list[dict[str, Any]],
    checkpoint: dict[str, Any],
) -> dict[str, Any] | None:
    if checkpoint.get("status") != "COMPLETE":
        return None

    path = run_root / BROWSER_CAPTURE_CHECKPOINT_PATH
    prior: dict[str, Any] = {}
    if path.is_file():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            prior = {}

    archived_as: str | None = None
    if prior and prior.get("status") != "COMPLETE":
        generation_dir = path.parent / "checkpoint_generations"
        generation_dir.mkdir(parents=True, exist_ok=True)
        prior_digest = sha256_file(path)
        generation_path = generation_dir / f"browser_capture_checkpoint.{prior_digest[:16]}.json"
        if not generation_path.exists():
            shutil.copy2(path, generation_path)
        archived_as = generation_path.relative_to(run_root).as_posix()

    helper_path = Path(__file__).resolve().with_name("browser_capture_resume.mjs")
    product_sequence = "".join(f"{row['product_id']}\n" for row in rows).encode("utf-8")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": checkpoint.get("run_id"),
        "updated_at": checkpoint.get("generated_at"),
        "status": "COMPLETE",
        "reason": None,
        "total_products": len(rows),
        "captured_count": checkpoint.get("captured_count"),
        "remaining_count": checkpoint.get("remaining_count"),
        "catalog_product_id_sequence_sha256": sha256_bytes(product_sequence),
        "capture_helper_path": str(helper_path),
        "capture_helper_sha256": sha256_file(helper_path) if helper_path.is_file() else None,
        "last_attempted_ordinal": len(rows) if rows else None,
        "next_zero_based_index": None,
        "next_ordinal": None,
        "next_product_id": None,
        "stop_on_challenge": True,
        "external_writes": False,
        "completion_basis": "All catalog products have verified current captured receipts and required capture artifacts.",
        "prior_checkpoint_archived_as": archived_as,
    }
    json_dump(path, payload)
    return payload


def build_resume_checkpoint(run_root: Path, *, write: bool = True) -> dict[str, Any]:
    rows = load_catalog_rows(run_root)
    catalog_index_path = run_root / "01_商品索引" / "catalog_index.jsonl"
    catalog_freeze_path = run_root / "01_商品索引" / "catalog_freeze.json"
    capture_helper_path = Path(__file__).resolve().with_name("browser_capture_resume.mjs")
    completed: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    failed_receipts: list[dict[str, Any]] = []
    error_counts: dict[str, int] = {}

    for ordinal, row in enumerate(rows, 1):
        product_id = str(row["product_id"])
        target = product_dir(run_root, product_id)
        receipt_path = target / "capture_receipt.json"
        receipt: dict[str, Any] = {}
        if receipt_path.exists():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                receipt = {"status": "failed", "error_code": "invalid_capture_receipt"}
        capture_complete = receipt.get("status") == "captured" and all(
            (target / name).is_file() and (target / name).stat().st_size > 0
            for name in CAPTURE_ARTIFACTS
        )
        if capture_complete:
            completed.append({"ordinal": ordinal, "product_id": product_id})
            continue

        error_code = str(receipt.get("error_code") or "not_attempted")
        if receipt:
            failed_receipts.append(
                {
                    "ordinal": ordinal,
                    "product_id": product_id,
                    "status": receipt.get("status"),
                    "error_code": error_code,
                    "captured_at": receipt.get("captured_at"),
                }
            )
        error_counts[error_code] = error_counts.get(error_code, 0) + 1
        raw_url = row.get("detail_url") or row.get("detailUrl") or row.get("product_detail_url")
        public_url = safe_public_url(raw_url) if isinstance(raw_url, str) and normalize_url(raw_url) else None
        remaining.append(
            {
                "ordinal": ordinal,
                "zero_based_index": ordinal - 1,
                "product_id": product_id,
                "public_url": public_url,
                "last_error_code": error_code,
            }
        )

    challenge_blocked = error_counts.get("challenge_detected", 0) > 0
    next_item = remaining[0] if remaining else None
    completed_set_bytes = "".join(
        f"{item['ordinal']}\0{item['product_id']}\n" for item in completed
    ).encode("utf-8")
    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rows[0].get("run_id") if rows else None,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "local_time_zone": "America/New_York",
        "mode": "read_only",
        "total_products": len(rows),
        "captured_count": len(completed),
        "remaining_count": len(remaining),
        "failed_receipt_count": len(failed_receipts),
        "challenge_receipt_count": error_counts.get("challenge_detected", 0),
        "not_attempted_count": error_counts.get("not_attempted", 0),
        "error_code_counts": error_counts,
        "catalog_bindings": {
            "catalog_index_path": str(catalog_index_path.relative_to(run_root)),
            "catalog_index_bytes": catalog_index_path.stat().st_size,
            "catalog_index_sha256": sha256_file(catalog_index_path),
            "catalog_freeze_path": str(catalog_freeze_path.relative_to(run_root)),
            "catalog_freeze_bytes": catalog_freeze_path.stat().st_size,
            "catalog_freeze_sha256": sha256_file(catalog_freeze_path),
            "completed_set_sha256": sha256_bytes(completed_set_bytes),
        },
        "capture_helper": {
            "path": str(capture_helper_path),
            "sha256": sha256_file(capture_helper_path) if capture_helper_path.is_file() else None,
            "available": capture_helper_path.is_file(),
        },
        "status": (
            "COMPLETE"
            if not remaining
            else "BLOCKED_HUMAN_VERIFICATION"
            if challenge_blocked
            else "PARTIAL"
        ),
        "next_ordinal": next_item["ordinal"] if next_item else None,
        "next_zero_based_index": next_item["zero_based_index"] if next_item else None,
        "next_product_id": next_item["product_id"] if next_item else None,
        "resume_policy": {
            "skip_verified_captured": True,
            "stop_on_challenge": True,
            "recommended_batch_size": 10,
            "requires_user_completed_challenge": challenge_blocked,
            "challenge_action": (
                "Complete the visible CAPTCHA manually in the in-app browser, then resume at next_zero_based_index."
                if challenge_blocked
                else None
            ),
        },
        "postprocess_commands": [
            "python scripts/product_archive.py extract-captured --run-root <RUN_ROOT>",
            "python scripts/product_archive.py download-all --run-root <RUN_ROOT> --asset-concurrency 8",
            "python scripts/product_archive.py mhtml-captured --run-root <RUN_ROOT> --product-concurrency 4",
            "python scripts/product_archive.py checkpoint --run-root <RUN_ROOT>",
            "python scripts/product_archive.py evidence-manifest --run-root <RUN_ROOT>",
            "python scripts/product_archive.py audit-secrets --run-root <RUN_ROOT>",
            "python scripts/product_archive.py verify --run-root <RUN_ROOT> --expected-count <APPROVED_COUNT>",
        ],
        "failed_receipts": failed_receipts,
        "remaining_products": remaining,
    }
    if write:
        json_dump(run_root / "00_运行状态" / "resume_checkpoint.json", checkpoint)
        reconcile_completed_browser_checkpoint(run_root, rows, checkpoint)
    return checkpoint


def evidence_source(relative_path: str) -> tuple[str, str, str]:
    normalized = relative_path.replace("\\", "/")
    name = Path(normalized).name
    if normalized.startswith("01_商品索引/raw_pages/"):
        return ("Alibaba authenticated read-only management API", "browser network response capture", "raw catalog evidence")
    if normalized.startswith("01_商品索引/"):
        return ("Alibaba authenticated read-only management API", "catalog freezer", "catalog identity and count evidence")
    if normalized.startswith("03_商品标准化/") and name in CAPTURE_ARTIFACTS:
        return ("Alibaba public product page", "in-app browser read-only capture", "public page evidence")
    if normalized.startswith("03_商品标准化/"):
        return ("derived from captured public product evidence", "product_archive.py", "normalized or offline product evidence")
    if normalized.startswith("05_素材库/sha256/"):
        return ("allowlisted Alibaba public CDN", "product_archive.py streamed downloader", "content-addressed product asset")
    if normalized.startswith("09_quarantine/"):
        return ("failed or incomplete acquisition", "product_archive.py", "quarantined evidence")
    return ("local archive workflow", "product_archive.py", "supporting evidence")


def iter_evidence_files(run_root: Path) -> list[Path]:
    scope = (
        "01_商品索引",
        "02_商品原始API",
        "03_商品标准化",
        "04_素材清单",
        "05_素材库",
        "06_页面兜底",
        "09_quarantine",
    )
    files: list[Path] = []
    for directory in scope:
        root = run_root / directory
        if not root.exists():
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and (
                directory == "09_quarantine"
                or not path.name.endswith((".partial", ".part"))
            )
        )
    return sorted(files, key=lambda path: path.relative_to(run_root).as_posix())


def evidence_timestamp(
    run_root: Path,
    path: Path,
    relative: str,
    freeze: dict[str, Any],
    receipt_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    filesystem_mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    timestamp = {
        "filesystem_mtime_utc": filesystem_mtime,
        "event_time_utc": filesystem_mtime,
        "timestamp_source": "filesystem_mtime_only_event_time_unavailable",
    }
    if relative.startswith("01_商品索引/") and freeze.get("captured_at"):
        timestamp.update(
            {
                "event_time_utc": freeze["captured_at"],
                "timestamp_source": "catalog_freeze.captured_at",
            }
        )
        return timestamp
    parts = relative.split("/")
    if len(parts) >= 3 and parts[0] == "03_商品标准化":
        product_id = parts[1]
        if product_id not in receipt_cache:
            receipt_path = run_root / "03_商品标准化" / product_id / "capture_receipt.json"
            try:
                receipt_cache[product_id] = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                receipt_cache[product_id] = {}
        receipt = receipt_cache[product_id]
        if parts[-1] in CAPTURE_ARTIFACTS and receipt.get("captured_at"):
            timestamp.update(
                {
                    "event_time_utc": receipt["captured_at"],
                    "timestamp_source": "capture_receipt.captured_at",
                }
            )
    return timestamp


def build_evidence_manifest(run_root: Path) -> dict[str, Any]:
    checkpoint = build_resume_checkpoint(run_root, write=False)
    freeze_path = run_root / "01_商品索引" / "catalog_freeze.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    receipt_cache: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    aggregate = hashlib.sha256()
    total_bytes = 0
    for path in iter_evidence_files(run_root):
        relative = path.relative_to(run_root).as_posix()
        size = path.stat().st_size
        digest = sha256_file(path)
        source, producer, relationship = evidence_source(relative)
        entry = {
            "relative_path": relative,
            "size_bytes": size,
            "sha256": digest,
            "source": source,
            "produced_by": producer,
            "relationship": relationship,
            **evidence_timestamp(run_root, path, relative, freeze, receipt_cache),
        }
        entries.append(entry)
        aggregate.update(f"{relative}\0{size}\0{digest}\n".encode("utf-8"))
        total_bytes += size

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": checkpoint.get("run_id"),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "local_time_zone": "America/New_York",
        "objective": "Read-only forensic archive of the locally authorized Alibaba.com product set.",
        "manifest_scope": [
            "01_商品索引",
            "02_商品原始API",
            "03_商品标准化",
            "04_素材清单",
            "05_素材库",
            "06_页面兜底",
            "09_quarantine",
        ],
        "excluded_mutable_outputs": ["00_运行状态", "07_审计", "08_汇总表"],
        "total_products": checkpoint["total_products"],
        "captured_products": checkpoint["captured_count"],
        "remaining_products": checkpoint["remaining_count"],
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "aggregate_sha256": aggregate.hexdigest(),
        "files": entries,
    }
    manifest_path = run_root / EVIDENCE_MANIFEST_PATH
    json_dump(manifest_path, manifest)
    manifest_sha = sha256_file(manifest_path)
    text_dump(
        run_root / EVIDENCE_MANIFEST_SHA_PATH,
        f"{manifest_sha}  {EVIDENCE_MANIFEST_PATH.as_posix()}\n",
    )
    return {
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "aggregate_sha256": manifest["aggregate_sha256"],
        "captured_products": manifest["captured_products"],
        "remaining_products": manifest["remaining_products"],
    }


def command_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or result.stderr).strip()
    return value.splitlines()[0] if value else None


def inventory_file(run_root: Path, path: Path) -> dict[str, Any]:
    return {
        "relative_path": path.relative_to(run_root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "filesystem_mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def build_case_files(run_root: Path) -> dict[str, Any]:
    checkpoint = build_resume_checkpoint(run_root)
    freeze_path = run_root / "01_商品索引" / "catalog_freeze.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    script_root = Path(__file__).resolve().parents[1]
    script_candidates = [
        Path(__file__).resolve(),
        Path(__file__).resolve().with_name("browser_capture_resume.mjs"),
        script_root / "schemas" / "asset_manifest.schema.json",
        script_root / "schemas" / "product_snapshot.schema.json",
        script_root / "schemas" / "run_receipt.schema.json",
    ]
    script_bindings = [
        {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in script_candidates
        if path.is_file()
    ]

    referenced_assets: set[str] = set()
    rendered_mismatch = 0
    legacy_non_png_screenshots = 0
    for row in load_catalog_rows(run_root):
        product_id = str(row["product_id"])
        target = product_dir(run_root, product_id)
        receipt_path = target / "capture_receipt.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            receipt = {}
        rendered_path = target / "public_rendered.html"
        if receipt.get("status") == "captured" and rendered_path.is_file() and receipt.get("rendered_sha256"):
            rendered_mismatch += sha256_file(rendered_path) != str(receipt["rendered_sha256"]).lower()
        screenshot_path = target / "public_fullpage.png"
        if screenshot_path.is_file():
            with screenshot_path.open("rb") as handle:
                legacy_non_png_screenshots += handle.read(8) != b"\x89PNG\r\n\x1a\n"
        manifest_path = target / "asset_manifest.jsonl"
        if manifest_path.is_file():
            for line in manifest_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                local_path = str(item.get("local_path") or "").replace("\\", "/")
                if item.get("download_status") == "downloaded" and local_path:
                    referenced_assets.add(local_path)

    partials = sorted(path for path in (run_root / "09_quarantine").rglob("*") if path.is_file())
    blob_root = run_root / "05_素材库" / "sha256"
    blob_files = sorted(path for path in blob_root.rglob("*") if path.is_file()) if blob_root.exists() else []
    orphans = [path for path in blob_files if path.relative_to(run_root).as_posix() not in referenced_assets]
    partial_inventory = [inventory_file(run_root, path) for path in partials]
    orphan_inventory = [inventory_file(run_root, path) for path in orphans]

    failures = [
        {
            "id": "F-001",
            "status": "confirmed_incident",
            "observation": "Captured rendered DOM was overwritten by a sanitizing post-process in the legacy acquisition generation.",
            "measured_count": rendered_mismatch,
            "disposition": "Preserve legacy receipts and recapture into a new generation; never rewrite a legacy receipt to hide the mismatch.",
        },
        {
            "id": "F-002",
            "status": "confirmed_format_mismatch",
            "observation": "Legacy screenshot files named .png do not carry a PNG signature.",
            "measured_count": legacy_non_png_screenshots,
            "disposition": "Preserve legacy files; the persistent capture helper now emits true CDP PNG bytes for new generations.",
        },
        {
            "id": "F-003",
            "status": "resolved" if checkpoint["challenge_receipt_count"] == 0 else "active_external_gate",
            "observation": "Alibaba public-page CAPTCHA can interrupt unattended continuation; the authoritative current captures are no longer blocked.",
            "measured_count": checkpoint["challenge_receipt_count"],
            "disposition": "User completes the visible challenge manually; resume with a one-product canary at the bound checkpoint.",
        },
        {
            "id": "F-004",
            "status": "preserved_pending_review",
            "observation": "Quarantined partial downloads remain preserved.",
            "measured_count": len(partial_inventory),
            "disposition": "No deletion without a second explicit confirmation after inventory review.",
        },
        {
            "id": "F-005",
            "status": "preserved_pending_review",
            "observation": "Content-addressed blobs not referenced by current product manifests remain preserved.",
            "measured_count": len(orphan_inventory),
            "disposition": "No deletion without a second explicit confirmation after inventory review.",
        },
    ]
    failed_hypotheses_path = run_root / "00_运行状态" / "notes" / "failed_hypotheses.jsonl"
    jsonl_dump(failed_hypotheses_path, failures)

    cleanup_plan = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "policy": "NO_DELETE_WITHOUT_SECOND_EXPLICIT_CONFIRMATION",
        "current_action": "preserve_all",
        "quarantine": {
            "file_count": len(partial_inventory),
            "total_bytes": sum(item["size_bytes"] for item in partial_inventory),
            "files": partial_inventory,
        },
        "orphan_blobs": {
            "file_count": len(orphan_inventory),
            "total_bytes": sum(item["size_bytes"] for item in orphan_inventory),
            "definition": "Files below 05_素材库/sha256 not referenced by any current downloaded asset manifest row.",
            "files": orphan_inventory,
        },
        "approval_gate": [
            "Verify the final approved product-set evidence manifest first.",
            "Review this exact inventory and select explicit paths.",
            "Obtain a second explicit user confirmation before deletion.",
        ],
    }
    cleanup_path = run_root / "00_运行状态" / "cleanup_plan.json"
    json_dump(cleanup_path, cleanup_plan)

    profile_generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ledger = summarize_request_ledger(run_root)
    catalog_product_ids = {str(row["product_id"]) for row in load_catalog_rows(run_root)}
    ledger_captured_ids = set(ledger["captured_product_ids"])
    ledger_current_capture_coverage_complete = (
        checkpoint.get("status") == "COMPLETE"
        and not ledger["invalid_line_numbers"]
        and ledger_captured_ids == catalog_product_ids
        and set(ledger["methods"]).issubset({"GET", "HEAD"})
        and ledger["external_write_rows"] == 0
    )
    profile = {
        "schema_version": SCHEMA_VERSION,
        "run_id": checkpoint.get("run_id"),
        "generated_at": profile_generated_at,
        "local_time_zone": "America/New_York",
        "objective": "Read-only forensic archive of the approved Alibaba.com product set for local learning and later human-reviewed listing automation.",
        "target": {
            "management_origin": "https://i.alibaba.com",
            "public_origin": "https://www.alibaba.com",
            "catalog_expected_count": None,
        },
        "authorization_boundary": {
            "management_data": "read_only",
            "public_page_methods": ["GET", "HEAD"],
            "product_create_update_delete": False,
            "deployment": False,
            "external_write_operations_observed": ledger["external_write_rows"],
            "current_capture_request_ledger_coverage_complete": ledger_current_capture_coverage_complete,
            "request_ledger": {
                key: ledger[key]
                for key in (
                    "path",
                    "row_count",
                    "invalid_line_numbers",
                    "methods",
                    "http_statuses",
                    "outcomes",
                    "external_write_rows",
                    "captured_product_id_count",
                    "captured_product_id_set_sha256",
                )
            },
            "evidence_limit": "Current authoritative captures are request-ledger-covered for the approved catalog product IDs; preserved prior capture generations may predate request-level ledger coverage.",
        },
        "run_timestamps": {
            "catalog_capture_utc": freeze.get("captured_at"),
            "case_profile_generated_utc": profile_generated_at,
            "completed_utc": checkpoint.get("generated_at") if checkpoint.get("status") == "COMPLETE" else None,
        },
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.replace("\n", " "),
            "node": command_version(["node", "--version"]),
            "powershell": command_version(
                ["powershell", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"]
            ),
            "browser": "Codex in-app browser; exact runtime version is not exposed to local Python",
        },
        "catalog_bindings": checkpoint["catalog_bindings"],
        "script_bindings": script_bindings,
        "progress": {
            "captured_count": checkpoint["captured_count"],
            "remaining_count": checkpoint["remaining_count"],
            "challenge_receipt_count": checkpoint["challenge_receipt_count"],
            "not_attempted_count": checkpoint["not_attempted_count"],
            "status": checkpoint["status"],
        },
        "audit_files": {
            "resume_checkpoint": "00_运行状态/resume_checkpoint.json",
            "commands": COMMAND_LOG_PATH.as_posix(),
            "failed_hypotheses": failed_hypotheses_path.relative_to(run_root).as_posix(),
            "cleanup_plan": cleanup_path.relative_to(run_root).as_posix(),
            "request_ledger": "07_审计/public_request_ledger.jsonl",
            "evidence_manifest": EVIDENCE_MANIFEST_PATH.as_posix(),
        },
        "command_log_completeness": "The persistent log begins with this hardened CLI generation; earlier browser and shell actions are not reconstructed or fabricated.",
        "cleanup_policy": cleanup_plan["policy"],
    }
    profile_path = run_root / "00_运行状态" / "case_profile.json"
    json_dump(profile_path, profile)
    return {
        "case_profile": str(profile_path),
        "cleanup_plan": str(cleanup_path),
        "failed_hypotheses": str(failed_hypotheses_path),
        "quarantine_files": len(partial_inventory),
        "orphan_blobs": len(orphan_inventory),
        "rendered_receipt_mismatches": rendered_mismatch,
        "legacy_non_png_screenshots": legacy_non_png_screenshots,
    }


def scan_secrets(root: Path) -> list[dict[str, Any]]:
    return scan_secrets_report(root)["findings"]


def public_transient_query_spans(data: bytes, *, html_context: bool = True) -> dict[tuple[int, int], str]:
    """Bind exact spans; standalone bytes default to an explicit HTML contract."""
    exemptions: dict[tuple[int, int], str] = {}

    def has_nested_query_delimiter(value: bytes) -> bool:
        # Decode the bytes, not just one spelling of nested percent escapes.
        # A bounded undecidable representation loses the exemption fail-closed.
        for _ in range(64):
            if b"?" in value or b"&" in value or b"#" in value:
                return True
            decoded = urllib.parse.unquote_to_bytes(value)
            if decoded == value:
                return False
            value = decoded
        return True

    for url_match in PUBLIC_QUERY_URL_RE.finditer(data):
        raw_url = url_match.group("url")
        try:
            parsed = urllib.parse.urlsplit(html.unescape(raw_url.decode("utf-8")))
            if parsed.username is not None or parsed.password is not None:
                continue
        except (UnicodeError, ValueError):
            continue
        policy = None
        if parsed.hostname == "us-favorite.alibaba.com" and parsed.path == "/favorite2/ajax/checkFavorites.htm":
            policy = ("alibaba_public_favorite_guest_ctoken", b"ctoken")
        elif parsed.hostname == "acs.h.alibaba.com" and re.fullmatch(
            r"/h5/mtop\.alibaba\.icbu\.review\.new\.shopreview\.scatter/[0-9]+(?:\.[0-9]+)*/?", parsed.path
        ):
            policy = ("alibaba_public_review_jsonp_sign", b"sign")
        if policy is None or not PUBLIC_TRANSIENT_QUERY_ALLOWLIST[policy[0]].search(raw_url):
            continue
        query_start = raw_url.find(b"?")
        fragment_start = raw_url.find(b"#")
        query_end = fragment_start if fragment_start >= 0 else len(raw_url)
        if query_start < 0 or query_start >= query_end:
            continue
        for query_match in SECRET_PATTERNS["sensitive_query"].finditer(raw_url):
            # Public URL exceptions apply only to literal query delimiters/keys,
            # not nested encoded URLs or a second '?' inside a query value.
            key_match = re.match(rb"(?i)(?:[?&]|&amp;)(ctoken|sign)=", query_match.group())
            if key_match is None or key_match.group(1).lower() != policy[1]:
                continue
            if has_nested_query_delimiter(query_match.group()[key_match.end():]):
                # Include split hex digits and repeated percent-encoding.
                # The generic detector may span a literal/encoded second parameter;
                # never exempt a multi-parameter/ambiguous value as one token.
                continue
            if query_match.start() < query_start or query_match.end() > query_end:
                continue
            if query_match.group().startswith(b"?") and query_match.start() != query_start:
                continue
            offset = url_match.start("url")
            exemptions[(offset + query_match.start(), offset + query_match.end())] = policy[0]

    policy_id = "alibaba_public_aplus_analytics_sign"
    # APLUS is HTML script semantics, not a generic text/tag-shaped exception.
    # Non-HTML callers still retain the exact Favorite/Review public URL rules.
    if html_context is not True:
        return exemptions
    from html.parser import HTMLParser

    # Implementation-sensitive, fail-closed runtime guard. Older/unknown
    # stdlib start-tag implementations may backtrack on malformed attributes;
    # do not run them or authorize APLUS until a supported runtime is installed.
    checker_code = getattr(getattr(HTMLParser, "check_for_whole_start_tag", None), "__code__", None)
    if checker_code is None or "locatetagend" not in checker_code.co_names or "locatestarttagend_tolerant" in checker_code.co_names:
        return exemptions

    class ScriptContextParser(HTMLParser):
        # Conservative scripting-enabled HTML context. These elements do not
        # expose script-looking text as opening tags; plaintext never resumes.
        CDATA_CONTENT_ELEMENTS = (
            "script", "style", "textarea", "title", "xmp", "iframe",
            "noembed", "noframes", "noscript", "plaintext",
        )

        def __init__(self) -> None:
            super().__init__()
            self.script_tags: list[tuple[int, int, list[str]]] = []
            self.foreign: list[str] = []
            self.plaintext = False
            self.script_comment_escaped = False
            self.unsupported_script_escape = False
            self.line_offsets = [0] + [index + 1 for index, byte in enumerate(data) if byte == 10]

        def handle_starttag(self, tag_name: str, attrs: list[tuple[str, str | None]]) -> None:
            if self.plaintext:
                return
            if tag_name == "plaintext":
                self.plaintext = True
            if tag_name in {"svg", "math", "template"}:
                self.foreign.append(tag_name)
            if tag_name == "script":
                self.script_comment_escaped = False
            if tag_name == "script" and not self.foreign:
                raw_tag = self.get_starttag_text()
                if raw_tag is not None and raw_tag.endswith(">"):
                    line, column = self.getpos()
                    start = self.line_offsets[line - 1] + column
                    self.script_tags.append((start, start + len(raw_tag), [name for name, _ in attrs]))

        def handle_endtag(self, tag_name: str) -> None:
            if self.foreign and tag_name == self.foreign[-1]:
                self.foreign.pop()

        def handle_data(self, text: str) -> None:
            if self.cdata_elem != "script":
                return
            # HTMLParser does not implement HTML's script double-escaped
            # tokenizer state. Do not trust any APLUS tag from such a document.
            # Complete input is fed once; raw-script callbacks preserve these
            # literal markers, without decoding script character references.
            for marker in re.finditer(r"<!--|-->|<script(?=[\t\n\r\f />])", text, re.IGNORECASE | re.ASCII):
                if marker.group() == "<!--":
                    self.script_comment_escaped = True
                elif marker.group() == "-->":
                    self.script_comment_escaped = False
                elif self.script_comment_escaped:
                    self.unsupported_script_escape = True

        def handle_startendtag(self, tag_name: str, attrs: list[tuple[str, str | None]]) -> None:
            self.handle_starttag(tag_name, attrs)
            if tag_name in self.CDATA_CONTENT_ELEMENTS:
                # In HTML a self-closing slash does not end script/rawtext.
                self.set_cdata_mode(tag_name)
            elif tag_name != "template":
                # HTML ignores a template opening's self-closing slash:
                # contents remain inert until a real </template> token.
                self.handle_endtag(tag_name)

    def attribute_spans(tag: bytes) -> dict[bytes, list[tuple[int, int] | None]]:
        # Anchored cursor: each byte, including a failing trailing whitespace
        # run, is consumed once. Never retry a greedy whitespace search.
        attributes: dict[bytes, list[tuple[int, int] | None]] = {}
        whitespace = b" \t\r\n\f"
        index = 1
        while index < len(tag) and tag[index] not in whitespace + b"/>":
            index += 1
        while index < len(tag):
            while index < len(tag) and tag[index] in whitespace:
                index += 1
            if index >= len(tag) or tag[index] == 62:
                break
            if tag[index] in (47, 61):
                index += 1
                continue
            start = index
            while index < len(tag) and tag[index] not in whitespace + b"/=>":
                index += 1
            name = tag[start:index].lower()
            while index < len(tag) and tag[index] in whitespace:
                index += 1
            span = None
            if index < len(tag) and tag[index] == 61:
                index += 1
                while index < len(tag) and tag[index] in whitespace:
                    index += 1
                if index < len(tag) and tag[index] in (34, 39):
                    quote = tag[index]
                    index += 1
                    value_start = index
                    while index < len(tag) and tag[index] != quote:
                        index += 1
                    if index < len(tag):
                        span = (value_start, index)
                        index += 1
                else:
                    while index < len(tag) and tag[index] not in whitespace + b">":
                        index += 1
            attributes.setdefault(name, []).append(span)
        return attributes

    parser = ScriptContextParser()
    try:
        # Latin-1 is one codepoint per byte: getpos/line offsets remain exact
        # even with multi-byte UTF-8 text before a genuine script opening.
        parser.feed(data.decode("latin-1"))
        parser.close()
    except (ValueError, AssertionError):
        # Unknown/malformed document context cannot authorize APLUS exceptions.
        return exemptions
    if parser.unsupported_script_escape:
        # Conservative document-wide denial, not a partial HTML tokenizer.
        # Exact Favorite/Review URL exceptions collected above remain intact.
        return exemptions
    for tag_start, tag_end, actual_names in parser.script_tags:
        tag = data[tag_start:tag_end]
        if actual_names.count("dmtrack_b") != 1 or actual_names.count("plugins") != 1:
            continue
        attributes = attribute_spans(tag)
        tracks = attributes.get(b"dmtrack_b", [])
        plugins = attributes.get(b"plugins", [])
        if len(tracks) != 1 or len(plugins) != 1:
            continue
        track = tracks[0]
        plugin = plugins[0]
        if track is None or plugin is None or not tag[plugin[0]:plugin[1]].startswith(b"aplus_plugin_icbu/"):
            continue
        # This is one complete, context-bound tag. Evaluate lookaheads once
        # at its beginning; search would retry every suffix when sign is absent.
        if not PUBLIC_TRANSIENT_QUERY_ALLOWLIST[policy_id].match(tag):
            continue
        for query_match in SECRET_PATTERNS["sensitive_query"].finditer(tag[track[0]:track[1]]):
            key_match = re.match(rb"(?i)(?:[?&]|&amp;|%3f|%26)sign(?:=|%3d)", query_match.group())
            if key_match is None or has_nested_query_delimiter(query_match.group()[key_match.end():]):
                continue
            offset = tag_start + track[0]
            exemptions[(offset + query_match.start(), offset + query_match.end())] = policy_id
    return exemptions


def scan_secrets_report(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    classified_non_secret: list[dict[str, Any]] = []
    files_scanned = 0
    bytes_scanned = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SCAN_SUFFIXES:
            continue
        data = path.read_bytes()
        files_scanned += 1
        bytes_scanned += len(data)
        # MHTML/MIME, Markdown, JSON, JS and other text have no verified complete
        # HTML document context. Only a captured .html file enables APLUS.
        public_query_spans = public_transient_query_spans(data, html_context=path.suffix.lower() == ".html")
        for name, pattern in SECRET_PATTERNS.items():
            matches = list(pattern.finditer(data))
            if not matches:
                continue
            unclassified = 0
            allowlist_counts: dict[str, int] = {}
            for match in matches:
                allowlist_id = public_query_spans.get(match.span()) if name == "sensitive_query" else None
                if allowlist_id:
                    allowlist_counts[allowlist_id] = allowlist_counts.get(allowlist_id, 0) + 1
                else:
                    unclassified += 1
            if unclassified:
                findings.append(
                    {
                        "file": str(path.relative_to(root)),
                        "pattern": name,
                        "match_count": unclassified,
                    }
                )
            for allowlist_id, count in sorted(allowlist_counts.items()):
                classified_non_secret.append(
                    {
                        "file": str(path.relative_to(root)),
                        "pattern": name,
                        "classification": allowlist_id,
                        "match_count": count,
                        "reason": PUBLIC_QUERY_REASONS[allowlist_id],
                    }
                )
    scanner_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scanner": str(scanner_path),
        "scanner_sha256": sha256_file(scanner_path),
        "scope_root": str(root),
        "suffixes": sorted(TEXT_SCAN_SUFFIXES),
        "patterns": sorted(SECRET_PATTERNS),
        "public_transient_query_allowlist": sorted(PUBLIC_TRANSIENT_QUERY_ALLOWLIST),
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "finding_count": len(findings),
        "findings": findings,
        "classified_non_secret_count": sum(item["match_count"] for item in classified_non_secret),
        "classified_non_secret": classified_non_secret,
        "status": "PASS" if not findings else "FAIL",
    }


def verify_evidence_manifest(run_root: Path) -> dict[str, Any]:
    manifest_path = run_root / EVIDENCE_MANIFEST_PATH
    sidecar_path = run_root / EVIDENCE_MANIFEST_SHA_PATH
    failures: list[dict[str, Any]] = []
    if not manifest_path.is_file() or not sidecar_path.is_file():
        return {
            "status": "missing",
            "validated_files": 0,
            "failures": [{"error": "evidence_manifest_or_sidecar_missing"}],
            "entry_map": {},
        }
    try:
        expected_manifest_sha = sidecar_path.read_text(encoding="utf-8").split()[0].lower()
        actual_manifest_sha = sha256_file(manifest_path)
        if expected_manifest_sha != actual_manifest_sha:
            failures.append(
                {
                    "file": EVIDENCE_MANIFEST_PATH.as_posix(),
                    "error": "manifest_sidecar_hash_mismatch",
                    "expected": expected_manifest_sha,
                    "actual": actual_manifest_sha,
                }
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, IndexError, json.JSONDecodeError) as error:
        return {
            "status": "invalid",
            "validated_files": 0,
            "failures": [{"error": "evidence_manifest_invalid", "detail": repr(error)}],
            "entry_map": {},
        }

    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return {
            "status": "invalid",
            "validated_files": 0,
            "failures": [{"error": "evidence_manifest_files_not_array"}],
            "entry_map": {},
        }
    entry_map: dict[str, dict[str, Any]] = {}
    aggregate = hashlib.sha256()
    validated = 0
    for entry in entries:
        relative = str(entry.get("relative_path") or "")
        if not relative or relative in entry_map:
            failures.append({"file": relative or None, "error": "duplicate_or_empty_manifest_path"})
            continue
        entry_map[relative] = entry
        path = run_root / Path(relative)
        if not path.is_file():
            failures.append({"file": relative, "error": "manifest_file_missing"})
            continue
        actual_size = path.stat().st_size
        expected_size = int(entry.get("size_bytes", -1))
        if actual_size != expected_size:
            failures.append(
                {
                    "file": relative,
                    "error": "manifest_size_mismatch",
                    "expected": expected_size,
                    "actual": actual_size,
                }
            )
            continue
        actual_sha = sha256_file(path)
        expected_sha = str(entry.get("sha256") or "").lower()
        if actual_sha != expected_sha:
            failures.append(
                {
                    "file": relative,
                    "error": "manifest_sha256_mismatch",
                    "expected": expected_sha,
                    "actual": actual_sha,
                }
            )
            continue
        aggregate.update(f"{relative}\0{actual_size}\0{actual_sha}\n".encode("utf-8"))
        validated += 1
    actual_paths = {path.relative_to(run_root).as_posix() for path in iter_evidence_files(run_root)}
    declared_paths = set(entry_map)
    for relative in sorted(actual_paths - declared_paths):
        failures.append({"file": relative, "error": "evidence_file_not_listed_in_manifest"})
    for relative in sorted(declared_paths - actual_paths):
        failures.append({"file": relative, "error": "manifest_entry_not_present_in_evidence_scope"})
    expected_aggregate = str(manifest.get("aggregate_sha256") or "").lower()
    if validated == len(entries) and aggregate.hexdigest() != expected_aggregate:
        failures.append(
            {
                "error": "manifest_aggregate_hash_mismatch",
                "expected": expected_aggregate,
                "actual": aggregate.hexdigest(),
            }
        )
    return {
        "status": "valid" if not failures else "invalid",
        "validated_files": validated,
        "declared_files": len(entries),
        "manifest_sha256": sha256_file(manifest_path),
        "failures": failures,
        "entry_map": entry_map,
    }


def validate_asset_manifest_row(item: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    required = (
        "schema_version",
        "asset_id",
        "role",
        "source_origin",
        "source_path",
        "source_url_sha256",
        "download_status",
    )
    for key in required:
        if key not in item:
            failures.append(f"missing:{key}")
    if item.get("schema_version") != SCHEMA_VERSION:
        failures.append("schema_version")
    if item.get("role") not in {
        "gallery",
        "description",
        "product_media",
        "video",
        "document",
        "recommendation",
        "page_ui",
    }:
        failures.append("role")
    if not re.match(r"^https?://", str(item.get("source_origin") or "")):
        failures.append("source_origin")
    if not str(item.get("source_path") or "").startswith("/"):
        failures.append("source_path")
    if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("source_url_sha256") or "")):
        failures.append("source_url_sha256")
    if item.get("download_status") not in {"pending", "downloaded", "failed", "excluded_non_product"}:
        failures.append("download_status")
    if item.get("download_status") == "downloaded":
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("blob_sha256") or "")):
            failures.append("blob_sha256")
        if not isinstance(item.get("bytes"), int) or int(item.get("bytes", -1)) < 0:
            failures.append("bytes")
        if not isinstance(item.get("local_path"), str) or not item.get("local_path"):
            failures.append("local_path")
    return failures


def validate_product_snapshot(snapshot: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    required = (
        "schema_version",
        "run_id",
        "product_id",
        "historical_observation_only",
        "approved_fact",
        "catalog_observation",
        "public_page",
        "source_artifacts",
    )
    for key in required:
        if key not in snapshot:
            failures.append(f"missing:{key}")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        failures.append("schema_version")
    if not str(snapshot.get("run_id") or ""):
        failures.append("run_id")
    if not re.fullmatch(r"[0-9]+", str(snapshot.get("product_id") or "")):
        failures.append("product_id")
    if snapshot.get("historical_observation_only") is not True:
        failures.append("historical_observation_only")
    if snapshot.get("approved_fact") is not False:
        failures.append("approved_fact")
    if not isinstance(snapshot.get("catalog_observation"), dict):
        failures.append("catalog_observation")
    if not isinstance(snapshot.get("public_page"), dict):
        failures.append("public_page")
    if not isinstance(snapshot.get("source_artifacts"), dict):
        failures.append("source_artifacts")
    return failures


def verify_run(run_root: Path, expected_count: int) -> dict[str, Any]:
    freeze = json.loads((run_root / "01_商品索引" / "catalog_freeze.json").read_text(encoding="utf-8"))
    index = load_catalog_rows(run_root)
    missing: list[dict[str, str]] = []
    asset_failures = 0
    receipt_hash_failures: list[dict[str, Any]] = []
    asset_reference_failures: list[dict[str, Any]] = []
    schema_failures: list[dict[str, Any]] = []
    screenshot_format_failures: list[dict[str, Any]] = []
    mhtml_format_failures: list[dict[str, Any]] = []
    evidence = verify_evidence_manifest(run_root)
    evidence_entries = evidence.pop("entry_map")
    captured_count = 0
    checked_asset_blobs: set[str] = set()
    for row in index:
        product_id = str(row["product_id"])
        target = product_dir(run_root, product_id)
        for name in REQUIRED_PRODUCT_ARTIFACTS:
            if not (target / name).exists():
                missing.append({"product_id": product_id, "artifact": name})
        receipt_path = target / "capture_receipt.json"
        receipt: dict[str, Any] = {}
        if receipt_path.exists():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                receipt_hash_failures.append({"product_id": product_id, "error": "invalid_capture_receipt"})
        if receipt.get("status") == "captured":
            captured_count += 1
            if not receipt.get("dom_sha256") or not isinstance(receipt.get("dom_bytes"), int):
                receipt_hash_failures.append(
                    {"product_id": product_id, "artifact": "public_page_dom.json", "error": "capture_receipt_missing_dom_binding"}
                )
            for prefix, name in (
                ("response", "public_response.html"),
                ("rendered", "public_rendered.html"),
                ("screenshot", "public_fullpage.png"),
                ("dom", "public_page_dom.json"),
            ):
                declared_sha = receipt.get(f"{prefix}_sha256")
                path = target / name
                if declared_sha and path.is_file():
                    actual_sha = sha256_file(path)
                    if actual_sha != str(declared_sha).lower():
                        receipt_hash_failures.append(
                            {
                                "product_id": product_id,
                                "artifact": name,
                                "error": "capture_receipt_sha256_mismatch",
                                "expected": declared_sha,
                                "actual": actual_sha,
                            }
                        )
            screenshot_path = target / "public_fullpage.png"
            if screenshot_path.is_file():
                with screenshot_path.open("rb") as handle:
                    signature = handle.read(8)
                if signature != b"\x89PNG\r\n\x1a\n":
                    screenshot_format_failures.append(
                        {"product_id": product_id, "artifact": "public_fullpage.png", "error": "extension_content_mime_mismatch"}
                    )
        snapshot_path = target / "product_snapshot.json"
        if snapshot_path.exists():
            try:
                snapshot_failures = validate_product_snapshot(json.loads(snapshot_path.read_text(encoding="utf-8")))
                if snapshot_failures:
                    schema_failures.append(
                        {"product_id": product_id, "artifact": "product_snapshot.json", "errors": snapshot_failures}
                    )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                schema_failures.append(
                    {"product_id": product_id, "artifact": "product_snapshot.json", "errors": [repr(error)]}
                )
        mhtml_path = target / "public_page.mhtml"
        if mhtml_path.exists():
            with mhtml_path.open("rb") as handle:
                header = handle.read(4096).lower()
            if b"content-type: multipart/related" not in header or b"mime-version: 1.0" not in header:
                mhtml_format_failures.append(
                    {"product_id": product_id, "artifact": "public_page.mhtml", "error": "invalid_mhtml_header"}
                )
        manifest = target / "asset_manifest.jsonl"
        if manifest.exists():
            for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as error:
                    schema_failures.append(
                        {"product_id": product_id, "artifact": "asset_manifest.jsonl", "line": line_number, "errors": [repr(error)]}
                    )
                    continue
                row_failures = validate_asset_manifest_row(item)
                if row_failures:
                    schema_failures.append(
                        {"product_id": product_id, "artifact": "asset_manifest.jsonl", "line": line_number, "errors": row_failures}
                    )
                if item.get("download_status") == "failed":
                    asset_failures += 1
                if item.get("download_status") != "downloaded":
                    continue
                local_path = str(item.get("local_path") or "").replace("\\", "/")
                blob_sha = str(item.get("blob_sha256") or "").lower()
                if not local_path or not blob_sha:
                    asset_reference_failures.append(
                        {"product_id": product_id, "asset_id": item.get("asset_id"), "error": "downloaded_asset_missing_reference"}
                    )
                    continue
                if local_path in checked_asset_blobs:
                    continue
                checked_asset_blobs.add(local_path)
                path = run_root / Path(local_path)
                evidence_entry = evidence_entries.get(local_path)
                if not path.is_file():
                    asset_reference_failures.append({"file": local_path, "error": "asset_blob_missing"})
                elif path.stem.lower() != blob_sha:
                    asset_reference_failures.append(
                        {"file": local_path, "error": "asset_blob_filename_hash_mismatch", "expected": blob_sha}
                    )
                elif int(item.get("bytes", -1)) != path.stat().st_size:
                    asset_reference_failures.append(
                        {
                            "file": local_path,
                            "error": "asset_blob_size_mismatch",
                            "expected": item.get("bytes"),
                            "actual": path.stat().st_size,
                        }
                    )
                elif not evidence_entry or str(evidence_entry.get("sha256") or "").lower() != blob_sha:
                    asset_reference_failures.append(
                        {"file": local_path, "error": "asset_blob_not_covered_by_evidence_manifest", "expected": blob_sha}
                    )
    secret_report = scan_secrets_report(run_root)
    json_dump(run_root / "07_审计" / "secret_scan.json", secret_report)
    secrets = secret_report["findings"]
    checkpoint = build_resume_checkpoint(run_root)
    checkpoint_consistency_failures: list[dict[str, Any]] = []
    browser_checkpoint: dict[str, Any] = {}
    browser_checkpoint_path = run_root / BROWSER_CAPTURE_CHECKPOINT_PATH
    try:
        browser_checkpoint = json.loads(browser_checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        checkpoint_consistency_failures.append(
            {"artifact": BROWSER_CAPTURE_CHECKPOINT_PATH.as_posix(), "error": "invalid_or_missing", "detail": repr(error)}
        )

    ledger = summarize_request_ledger(run_root)
    catalog_product_ids = {str(row["product_id"]) for row in index}
    ledger_captured_product_ids = set(ledger["captured_product_ids"])
    expected_product_sequence_sha256 = sha256_bytes(
        "".join(f"{row['product_id']}\n" for row in index).encode("utf-8")
    )
    browser_expectations = {
        "status": "COMPLETE",
        "total_products": expected_count,
        "captured_count": expected_count,
        "remaining_count": 0,
        "last_attempted_ordinal": expected_count,
        "next_zero_based_index": None,
        "next_ordinal": None,
        "next_product_id": None,
        "catalog_product_id_sequence_sha256": expected_product_sequence_sha256,
        "external_writes": False,
    }
    for key, expected in browser_expectations.items():
        if browser_checkpoint.get(key) != expected:
            checkpoint_consistency_failures.append(
                {
                    "artifact": BROWSER_CAPTURE_CHECKPOINT_PATH.as_posix(),
                    "field": key,
                    "expected": expected,
                    "actual": browser_checkpoint.get(key),
                }
            )
    if checkpoint.get("status") != "COMPLETE":
        checkpoint_consistency_failures.append(
            {"artifact": "00_运行状态/resume_checkpoint.json", "field": "status", "expected": "COMPLETE", "actual": checkpoint.get("status")}
        )
    if ledger["invalid_line_numbers"]:
        checkpoint_consistency_failures.append(
            {"artifact": REQUEST_LEDGER_PATH.as_posix(), "error": "invalid_jsonl_lines", "lines": ledger["invalid_line_numbers"]}
        )
    if not set(ledger["methods"]).issubset({"GET", "HEAD"}):
        checkpoint_consistency_failures.append(
            {"artifact": REQUEST_LEDGER_PATH.as_posix(), "error": "non_read_method", "methods": ledger["methods"]}
        )
    if ledger["external_write_rows"] != 0:
        checkpoint_consistency_failures.append(
            {"artifact": REQUEST_LEDGER_PATH.as_posix(), "error": "external_write_observed", "count": ledger["external_write_rows"]}
        )
    if ledger_captured_product_ids != catalog_product_ids:
        checkpoint_consistency_failures.append(
            {
                "artifact": REQUEST_LEDGER_PATH.as_posix(),
                "error": "captured_catalog_coverage_mismatch",
                "missing_product_ids": sorted(catalog_product_ids - ledger_captured_product_ids),
                "extra_product_ids": sorted(ledger_captured_product_ids - catalog_product_ids),
            }
        )
    complete = (
        freeze.get("api_total") == expected_count
        and freeze.get("raw_rows") == expected_count
        and freeze.get("unique_product_ids") == expected_count
        and len(index) == expected_count
        and captured_count == expected_count
        and checkpoint.get("remaining_count") == 0
        and not missing
        and not asset_failures
        and not receipt_hash_failures
        and not asset_reference_failures
        and not schema_failures
        and not screenshot_format_failures
        and not mhtml_format_failures
        and evidence.get("status") == "valid"
        and not secrets
        and not checkpoint_consistency_failures
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": freeze.get("run_id"),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expected_count": expected_count,
        "api_total": freeze.get("api_total"),
        "unique_product_ids": freeze.get("unique_product_ids"),
        "catalog_index_rows": len(index),
        "captured_count": captured_count,
        "remaining_count": checkpoint.get("remaining_count"),
        "missing_artifacts": missing,
        "asset_failures": asset_failures,
        "capture_receipt_hash_failures": receipt_hash_failures,
        "asset_reference_failures": asset_reference_failures,
        "schema_failures": schema_failures,
        "screenshot_format_failures": screenshot_format_failures,
        "mhtml_format_failures": mhtml_format_failures,
        "checkpoint_consistency_failures": checkpoint_consistency_failures,
        "browser_capture_checkpoint": {
            key: browser_checkpoint.get(key)
            for key in (
                "updated_at",
                "status",
                "total_products",
                "captured_count",
                "remaining_count",
                "last_attempted_ordinal",
                "next_zero_based_index",
                "next_ordinal",
                "next_product_id",
                "catalog_product_id_sequence_sha256",
                "external_writes",
                "completion_basis",
                "prior_checkpoint_archived_as",
            )
        },
        "request_ledger": {
            key: ledger[key]
            for key in (
                "path",
                "row_count",
                "invalid_line_numbers",
                "methods",
                "http_statuses",
                "outcomes",
                "external_write_rows",
                "captured_product_id_count",
                "captured_product_id_set_sha256",
            )
        },
        "evidence_manifest": evidence,
        "secret_scan": {
            key: secret_report[key]
            for key in (
                "generated_at",
                "scanner",
                "scanner_sha256",
                "files_scanned",
                "bytes_scanned",
                "finding_count",
                "status",
            )
        },
        "secret_findings": secrets,
        "status": "COMPLETE" if complete else "PARTIAL",
    }
    json_dump(run_root / "07_审计" / "verification_report.json", report)
    return report


def iter_product_ids(run_root: Path) -> list[str]:
    path = run_root / "01_商品索引" / "catalog_index.jsonl"
    return [str(json.loads(line)["product_id"]) for line in path.read_text(encoding="utf-8").splitlines() if line]


def iter_captured_product_ids(run_root: Path) -> list[str]:
    return [
        product_id
        for product_id in iter_product_ids(run_root)
        if (product_dir(run_root, product_id) / "public_rendered.html").exists()
        and (product_dir(run_root, product_id) / "public_fullpage.png").exists()
    ]


def main() -> int:
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    parser = argparse.ArgumentParser(description="Read-only Alibaba product archive post-processor")
    parser.add_argument(
        "command",
        choices=(
            "extract",
            "extract-captured",
            "download",
            "download-all",
            "mhtml",
            "mhtml-captured",
            "process",
            "checkpoint",
            "case-files",
            "evidence-manifest",
            "verify",
            "audit-secrets",
        ),
    )
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--product-id")
    parser.add_argument("--expected-count", type=int, default=1)
    parser.add_argument("--max-asset-bytes", type=int, default=250 * 1024 * 1024)
    parser.add_argument("--asset-concurrency", type=int, default=8)
    parser.add_argument("--product-concurrency", type=int, default=4)
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    captured_commands = {"extract-captured", "download-all", "mhtml-captured"}
    product_ids = [args.product_id] if args.product_id else (
        iter_captured_product_ids(run_root) if args.command in captured_commands else iter_product_ids(run_root)
    )

    if args.command in {"extract", "extract-captured"}:
        payload = [extract_product(run_root, product_id) for product_id in product_ids]
    elif args.command == "download":
        payload = [download_product_assets(run_root, product_id, args.max_asset_bytes) for product_id in product_ids]
    elif args.command == "download-all":
        payload = download_all_assets(run_root, product_ids, args.max_asset_bytes, args.asset_concurrency)
    elif args.command == "mhtml":
        payload = [build_mhtml(run_root, product_id) for product_id in product_ids]
    elif args.command == "mhtml-captured":
        with ThreadPoolExecutor(max_workers=max(1, args.product_concurrency)) as pool:
            payload = list(pool.map(lambda product_id: build_mhtml(run_root, product_id), product_ids))
    elif args.command == "process":
        payload = []
        for product_id in product_ids:
            payload.append(
                {
                    "extract": extract_product(run_root, product_id),
                    "download": download_product_assets(run_root, product_id, args.max_asset_bytes),
                    "mhtml": build_mhtml(run_root, product_id),
                }
            )
    elif args.command == "checkpoint":
        payload = build_resume_checkpoint(run_root)
    elif args.command == "case-files":
        payload = build_case_files(run_root)
    elif args.command == "evidence-manifest":
        payload = build_evidence_manifest(run_root)
    elif args.command == "verify":
        payload = verify_run(run_root, args.expected_count)
    else:
        payload = scan_secrets_report(run_root)
        json_dump(run_root / "07_审计" / "secret_scan.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    exit_code = 0
    if args.command == "verify" and payload["status"] != "COMPLETE":
        exit_code = 2
    if args.command == "audit-secrets" and payload["finding_count"]:
        exit_code = 3
    jsonl_append(
        run_root / COMMAND_LOG_PATH,
        {
            "schema_version": SCHEMA_VERSION,
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cwd": str(Path.cwd()),
            "argv": [str(value) for value in sys.argv],
            "command": args.command,
            "exit_code": exit_code,
            "stdout_capture": "not_captured_by_cli",
            "stderr_capture": "not_captured_by_cli",
        },
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
