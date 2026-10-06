from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse


ROOT_URL = os.environ.get("SELLERKIT_SITE_ROOT_URL", "https://catalog.example.invalid/")
HOSTS = {host.strip().lower() for host in os.environ.get("SELLERKIT_SITE_ALLOWED_HOSTS", "catalog.example.invalid").split(",") if host.strip()}
ROOT = Path(os.environ.get("SELLERKIT_OUTPUT_ROOT", Path(__file__).resolve().parents[1])).resolve()
if os.name == "nt" and str(ROOT).startswith("\\\\") and not str(ROOT).startswith("\\\\?\\"):
    ROOT = Path("\\\\?\\UNC\\" + str(ROOT)[2:])
REPORTS = ROOT / "reports"
MIRROR = ROOT / "local_mirror"
MIRROR_ASSETS = MIRROR / "__rewritten_assets"
SUCCESS_ASSET_STATUSES = {
    "downloaded",
    "downloaded_cached",
    "downloaded_hash_verified",
    "downloaded_hash_changed",
    "downloaded_hash_new",
}


ATTR_RE = re.compile(
    r"(?P<name>\b(?:href|src|action|poster|data-src|data-original|data-lazy|data-background|data-url)\b)"
    r"(?P<eq>\s*=\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.I | re.S,
)
SRCSET_ATTR_RE = re.compile(
    r"(?P<name>\b(?:srcset|data-srcset)\b)(?P<eq>\s*=\s*)(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.I | re.S,
)
CSS_URL_RE = re.compile(r"url\(\s*(?P<quote>['\"]?)(?P<value>.*?)(?P=quote)\s*\)", re.I | re.S)
CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?['\"]?(?P<value>[^'\"\);]+)", re.I)
PRESERVED_MIRROR_DIRS = ("__andy_avatar", "andy-avatar-demo", "andy-avatar-video-demo")
ROUTE_FALLBACKS = {
    "/News&Blog": "/News&Blog/All-Blogs",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalize_url(raw: str, base: str = ROOT_URL) -> str | None:
    if not raw:
        return None
    raw = html.unescape(raw).strip()
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:", "sms:")):
        return None
    joined = urljoin(base, raw)
    joined = joined.split("#", 1)[0]
    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"}:
        return None
    netloc = parsed.netloc.lower()
    path = quote(unquote(parsed.path or "/"), safe="/:@!$&'()*+,;=-._~%")
    return urlunparse(("https" if parsed.scheme == "http" and netloc in HOSTS else parsed.scheme, netloc, path, "", parsed.query, ""))


def safe_segment(segment: str) -> str:
    segment = unquote(segment).strip()
    segment = re.sub(r"[<>:\"\\|?*\x00-\x1f]+", "_", segment)
    segment = segment.strip(" .")
    return segment or "index"


def page_output_path(url: str) -> Path:
    parsed = urlparse(url)
    parts = [safe_segment(part) for part in parsed.path.split("/") if part]
    if not parts:
        base = MIRROR
    else:
        base = MIRROR.joinpath(*parts)
    if parsed.query:
        digest = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
        return base / f"query_{digest}.html"
    return base / "index.html"


def local_url_from_path(path: Path) -> str:
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        root_text = str(ROOT)
        if os.name != "nt" or not root_text.startswith("\\\\?\\UNC\\"):
            raise
        display_root = Path("\\\\" + root_text[len("\\\\?\\UNC\\"):])
        rel = path.relative_to(display_root).as_posix()
    return "/" + "/".join(quote(part) for part in rel.split("/"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")


def map_from_reports() -> tuple[dict[str, str], dict[str, dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    pages = read_csv(REPORTS / "pages.csv")
    assets = read_csv(REPORTS / "assets.csv")

    page_map: dict[str, str] = {}
    for row in pages:
        out = page_output_path(row["final_url"] or row["url"])
        local = local_url_from_path(out)
        for key in ("url", "final_url"):
            if row.get(key):
                page_map[row[key]] = local
                page_map[row[key].rstrip("/")] = local

    asset_map: dict[str, dict[str, str]] = {}
    for row in assets:
        status = row["status"]
        if status not in SUCCESS_ASSET_STATUSES or not row["local_file"]:
            continue
        local_file = Path(row["local_file"])
        if not local_file.exists():
            continue
        if row["category"] == "css":
            rewritten = MIRROR_ASSETS / "css" / local_file.name
            local = local_url_from_path(rewritten)
        else:
            local = local_url_from_path(local_file)
        for key in ("url", "final_url"):
            if row.get(key):
                asset_map[row[key]] = {**row, "local_url": local}
                asset_map[row[key].rstrip("/")] = {**row, "local_url": local}
    return page_map, asset_map, pages, assets


def rewrite_one_url(value: str, base_url: str, page_map: dict[str, str], asset_map: dict[str, dict[str, str]], stats: dict[str, int]) -> str:
    normalized = normalize_url(value, base_url)
    if not normalized:
        return value
    normalized_no_slash = normalized.rstrip("/")
    if normalized in page_map:
        stats["page_links_rewritten"] += 1
        return page_map[normalized]
    if normalized_no_slash in page_map:
        stats["page_links_rewritten"] += 1
        return page_map[normalized_no_slash]
    if normalized in asset_map:
        stats["asset_links_rewritten"] += 1
        return asset_map[normalized]["local_url"]
    if normalized_no_slash in asset_map:
        stats["asset_links_rewritten"] += 1
        return asset_map[normalized_no_slash]["local_url"]
    parsed = urlparse(normalized)
    if parsed.netloc.lower() in HOSTS:
        stats["same_domain_unmapped"] += 1
    else:
        stats["external_kept"] += 1
    return value


def rewrite_srcset(value: str, base_url: str, page_map: dict[str, str], asset_map: dict[str, dict[str, str]], stats: dict[str, int]) -> str:
    rewritten_items = []
    for item in value.split(","):
        parts = item.strip().split()
        if not parts:
            continue
        parts[0] = rewrite_one_url(parts[0], base_url, page_map, asset_map, stats)
        rewritten_items.append(" ".join(parts))
    return ", ".join(rewritten_items)


def rewrite_css_urls(text: str, base_url: str, page_map: dict[str, str], asset_map: dict[str, dict[str, str]], stats: dict[str, int]) -> str:
    def repl_url(match: re.Match[str]) -> str:
        value = match.group("value").strip()
        if not value or value.startswith(("data:", "#")):
            return match.group(0)
        rewritten = rewrite_one_url(value, base_url, page_map, asset_map, stats)
        return f"url('{rewritten}')"

    def repl_import(match: re.Match[str]) -> str:
        value = match.group("value").strip()
        rewritten = rewrite_one_url(value, base_url, page_map, asset_map, stats)
        return match.group(0).replace(value, rewritten)

    return CSS_IMPORT_RE.sub(repl_import, CSS_URL_RE.sub(repl_url, text))


def rewrite_html(text: str, base_url: str, page_map: dict[str, str], asset_map: dict[str, dict[str, str]], stats: dict[str, int]) -> str:
    def repl_srcset(match: re.Match[str]) -> str:
        value = rewrite_srcset(match.group("value"), base_url, page_map, asset_map, stats)
        return f"{match.group('name')}{match.group('eq')}{match.group('quote')}{value}{match.group('quote')}"

    def repl_attr(match: re.Match[str]) -> str:
        value = rewrite_one_url(match.group("value"), base_url, page_map, asset_map, stats)
        return f"{match.group('name')}{match.group('eq')}{match.group('quote')}{value}{match.group('quote')}"

    text = SRCSET_ATTR_RE.sub(repl_srcset, text)
    text = ATTR_RE.sub(repl_attr, text)
    return rewrite_css_urls(text, base_url, page_map, asset_map, stats)


def build() -> dict:
    page_map, asset_map, pages, assets = map_from_reports()

    missing_pages = [row["html_file"] for row in pages if not row.get("html_file") or not Path(row["html_file"]).exists()]
    expected_local_assets = [
        row
        for row in assets
        if row.get("status") in SUCCESS_ASSET_STATUSES
    ]
    missing_local_assets = [
        row.get("local_file") or row.get("url") or "<unknown>"
        for row in expected_local_assets
        if not row.get("local_file") or not Path(row["local_file"]).exists()
    ]
    if not pages or missing_pages or missing_local_assets:
        raise RuntimeError(
            "Mirror preflight failed before cleanup: "
            f"pages={len(pages)}, missing_pages={len(missing_pages)}, "
            f"missing_local_assets={len(missing_local_assets)}. "
            "The existing local_mirror was left untouched."
        )

    resolved_root = ROOT.resolve()
    resolved_mirror = MIRROR.resolve()
    if resolved_mirror.parent != resolved_root or resolved_mirror.name != "local_mirror":
        raise RuntimeError(f"Refusing to rebuild unexpected mirror path: {resolved_mirror}")

    with tempfile.TemporaryDirectory(prefix="sellerkit-mirror-preserve-", dir=ROOT) as temp_dir:
        preserve_root = Path(temp_dir)
        for name in PRESERVED_MIRROR_DIRS:
            source = MIRROR / name
            if source.is_dir():
                shutil.copytree(source, preserve_root / name)

        if MIRROR.exists():
            shutil.rmtree(MIRROR)
        MIRROR.mkdir(parents=True, exist_ok=True)

        for name in PRESERVED_MIRROR_DIRS:
            preserved = preserve_root / name
            if preserved.is_dir():
                shutil.copytree(preserved, MIRROR / name)

    stats = {
        "pages_written": 0,
        "css_written": 0,
        "page_links_rewritten": 0,
        "asset_links_rewritten": 0,
        "same_domain_unmapped": 0,
        "external_kept": 0,
    }

    for row in assets:
        if row["status"] in SUCCESS_ASSET_STATUSES and row["category"] == "css" and row["local_file"]:
            source = Path(row["local_file"])
            if source.exists():
                css_text = read_text(source)
                base = row["final_url"] or row["url"]
                rewritten = rewrite_css_urls(css_text, base, page_map, asset_map, stats)
                write_text(MIRROR_ASSETS / "css" / source.name, rewritten)
                stats["css_written"] += 1

    for row in pages:
        source = Path(row["html_file"])
        if not source.exists():
            continue
        base = row["final_url"] or row["url"]
        html = rewrite_html(read_text(source), base, page_map, asset_map, stats)
        out = page_output_path(base)
        write_text(out, html)
        stats["pages_written"] += 1

    fallback_routes_applied: list[dict[str, str]] = []
    for target_route, source_route in ROUTE_FALLBACKS.items():
        target = page_output_path(urljoin(ROOT_URL, target_route))
        source = page_output_path(urljoin(ROOT_URL, source_route))
        if source.exists() and (not target.exists() or target.stat().st_size == 0):
            write_text(target, read_text(source))
            fallback_routes_applied.append({"target": target_route, "source": source_route})

    nonlocal_assets = [
        {k: row[k] for k in ["status", "category", "extension", "url", "note"] if k in row}
        for row in assets
        if row["status"] not in SUCCESS_ASSET_STATUSES
    ]
    report = {
        "output": str(MIRROR),
        "entry": str(MIRROR / "index.html"),
        "pages_expected": len(pages),
        "pages_written": stats["pages_written"],
        "css_written": stats["css_written"],
        "assets_expected": len(assets),
        "downloaded_or_cached_assets": sum(1 for row in assets if row["status"] in SUCCESS_ASSET_STATUSES),
        "nonlocal_assets": len(nonlocal_assets),
        "fallback_routes_applied": fallback_routes_applied,
        "stats": stats,
        "nonlocal_asset_examples": nonlocal_assets[:30],
    }
    write_text(MIRROR / "__mirror_report.json", json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rebuild the SellerKit local mirror after validating every archived page and local asset."
    )
    parser.parse_args()
    print(json.dumps(build(), ensure_ascii=False, indent=2))
