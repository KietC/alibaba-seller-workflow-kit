from __future__ import annotations

import csv
import hashlib
import html
import json
import mimetypes
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ROOT_URL = os.environ.get("SELLERKIT_SITE_ROOT_URL", "https://catalog.example.invalid/")
OUTPUT_ROOT = Path(os.environ.get("SELLERKIT_OUTPUT_ROOT", Path(__file__).resolve().parents[1])).resolve()
ALLOWED_HOSTS = {host.strip().lower() for host in os.environ.get("SELLERKIT_SITE_ALLOWED_HOSTS", "catalog.example.invalid").split(",") if host.strip()}

PAGE_DELAY_SECONDS = 0.8
ASSET_DELAY_SECONDS = 0.15
ASSET_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
CHECKPOINT_EVERY_ASSETS = 10
MAX_PAGES = 1200
MAX_ASSET_BYTES = 512 * 1024 * 1024
TIMEOUT_SECONDS = 35
ASSET_PROGRESS_INTERVAL_BYTES = 5 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 CodexSiteInventory/1.0"
)
PROGRESS_LOG = OUTPUT_ROOT / "reports" / "progress.log"
STATUS_JSON = OUTPUT_ROOT / "reports" / "status.json"

HTML_EXTS = {"", ".html", ".htm", ".php", ".asp", ".aspx"}
ASSET_EXTS = {
    ".css", ".js", ".mjs",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif",
    ".mp4", ".webm", ".mov", ".m4v", ".avi", ".mp3", ".wav", ".ogg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z",
}
SKIP_PAGE_PREFIXES = (
    "/cn", "/zh", "/zh-cn", "/zh_tw", "/tw", "/jp", "/ja", "/kr", "/ko",
    "/fr", "/de", "/es", "/ru", "/ar", "/pt", "/it", "/tr", "/vi", "/th",
)
SKIP_QUERY_KEYS = {"lang", "language", "translate", "googtrans"}


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    for path in [
        OUTPUT_ROOT / "reports",
        OUTPUT_ROOT / "pages_html_raw",
        OUTPUT_ROOT / "pages_text",
        OUTPUT_ROOT / "assets",
        OUTPUT_ROOT / "assets" / "css",
        OUTPUT_ROOT / "assets" / "js",
        OUTPUT_ROOT / "assets" / "images",
        OUTPUT_ROOT / "assets" / "fonts",
        OUTPUT_ROOT / "assets" / "media",
        OUTPUT_ROOT / "assets" / "documents",
        OUTPUT_ROOT / "assets" / "other",
    ]:
        path.mkdir(parents=True, exist_ok=True)


def reset_progress_log() -> None:
    ensure_dirs()
    if PROGRESS_LOG.exists() and PROGRESS_LOG.stat().st_size > 0:
        archive = PROGRESS_LOG.with_name(f"progress_{datetime.now().strftime('%Y%m%d_%H%M%S')}.previous.log")
        PROGRESS_LOG.replace(archive)
    PROGRESS_LOG.write_text("", encoding="utf-8")


def log_progress(message: str) -> None:
    line = f"[{now_stamp()}] {message}"
    print(line, flush=True)
    with PROGRESS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def write_status(payload: dict) -> None:
    data = {
        "updated_at": now_stamp(),
        "root_url": ROOT_URL,
        "output_root": str(OUTPUT_ROOT),
        **payload,
    }
    atomic_write_text(STATUS_JSON, json.dumps(data, ensure_ascii=False, indent=2))


def request(url: str, method: str = "GET") -> Request:
    return Request(
        url,
        method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close",
        },
    )


def fetch_bytes(url: str, method: str = "GET", max_bytes: int | None = None) -> tuple[int, str, dict, bytes]:
    try:
        with urlopen(request(url, method=method), timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            final_url = response.geturl()
            headers = dict(response.headers.items())
            if max_bytes is None:
                data = response.read()
            else:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"asset exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                data = b"".join(chunks)
            return status, final_url, headers, data
    except HTTPError as error:
        data = error.read() if error.fp else b""
        return error.code, error.geturl(), dict(error.headers.items()), data


def decode_body(data: bytes, headers: dict) -> str:
    content_type = headers.get("Content-Type", "")
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    encodings = []
    if match:
        encodings.append(match.group(1).strip("\"'"))
    encodings.extend(["utf-8", "gb18030", "latin-1"])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def normalize_url(raw: str, base: str = ROOT_URL, keep_query: bool = True) -> str | None:
    if not raw:
        return None
    raw = html.unescape(raw).strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:", "sms:", "weixin:")):
        return None
    joined = urljoin(base, raw)
    joined, _fragment = joined.split("#", 1) if "#" in joined else (joined, "")
    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"}:
        return None
    netloc = parsed.netloc.lower()
    path = quote(unquote(parsed.path or "/"), safe="/:@!$&'()*+,;=-._~%")
    query = parsed.query if keep_query else ""
    if query:
        pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() not in SKIP_QUERY_KEYS]
        query = urlencode(pairs, doseq=True)
    normalized = urlunparse(("https" if parsed.scheme == "http" and netloc in ALLOWED_HOSTS else parsed.scheme, netloc, path, "", query, ""))
    if normalized.endswith("?"):
        normalized = normalized[:-1]
    return normalized


def path_ext(url: str) -> str:
    return Path(unquote(urlparse(url).path)).suffix.lower()


def is_same_site(url: str) -> bool:
    return urlparse(url).netloc.lower() in ALLOWED_HOSTS


def is_asset_url(url: str) -> bool:
    return path_ext(url) in ASSET_EXTS


def should_crawl_page(url: str) -> bool:
    if not is_same_site(url):
        return False
    parsed = urlparse(url)
    path_lower = unquote(parsed.path).lower()
    if any(path_lower == prefix or path_lower.startswith(prefix + "/") for prefix in SKIP_PAGE_PREFIXES):
        return False
    if is_asset_url(url):
        return False
    ext = path_ext(url)
    if ext and ext not in HTML_EXTS:
        return False
    return True


def safe_name(value: str, limit: int = 90) -> str:
    value = unquote(value)
    value = re.sub(r"[^\w.\-]+", "_", value, flags=re.U).strip("._")
    if not value:
        value = "index"
    return value[:limit]


def page_file_stem(index: int, url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "index"
    base = safe_name(path.replace("/", "__"))
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{index:04d}_{base}_{digest}"


def asset_local_path(url: str, headers: dict | None = None) -> Path:
    ext = path_ext(url)
    if not ext and headers:
        guessed = mimetypes.guess_extension(headers.get("Content-Type", "").split(";")[0].strip())
        ext = guessed or ""
    category = asset_category(url, headers)
    parsed = urlparse(url)
    basename = safe_name(Path(unquote(parsed.path)).name or "asset")
    if ext and not basename.lower().endswith(ext):
        basename += ext
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    host = safe_name(parsed.netloc)
    return OUTPUT_ROOT / "assets" / category / f"{host}_{digest}_{basename}"


def asset_category(url: str, headers: dict | None = None) -> str:
    ext = path_ext(url)
    content_type = (headers or {}).get("Content-Type", "").lower()
    if ext == ".css" or "text/css" in content_type:
        return "css"
    if ext in {".js", ".mjs"} or "javascript" in content_type:
        return "js"
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif"} or content_type.startswith("image/"):
        return "images"
    if ext in {".woff", ".woff2", ".ttf", ".otf", ".eot"} or "font" in content_type:
        return "fonts"
    if ext in {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mp3", ".wav", ".ogg"} or content_type.startswith(("video/", "audio/")):
        return "media"
    if ext in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z"}:
        return "documents"
    return "other"


def split_srcset(value: str) -> Iterable[str]:
    for item in value.split(","):
        candidate = item.strip().split()
        if candidate:
            yield candidate[0]


CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?['\"]?([^'\"\);]+)", re.I)
EMBEDDED_PAGE_ROUTE_RE = re.compile(
    r"(?P<url>(?:(?:https?:)?//[A-Za-z0-9.-]+(?::\d+)?)?/"
    r"(?:Products|News&Blog)/[A-Za-z0-9][A-Za-z0-9._~!$&+=:@%/-]*)"
    r"(?=[?#\s\"'<>(),;\]}]|$)",
    re.I,
)


def extract_css_urls(css: str, base_url: str) -> set[str]:
    found = set()
    for pattern in [CSS_URL_RE, CSS_IMPORT_RE]:
        for match in pattern.finditer(css):
            raw = match.group(2) if pattern is CSS_URL_RE else match.group(1)
            normalized = normalize_url(raw, base_url)
            if normalized:
                found.add(normalized)
    return found


def extract_embedded_page_routes(document: str, base_url: str) -> set[str]:
    decoded = document
    for _ in range(3):
        expanded = html.unescape(decoded)
        expanded = re.sub(r"\\+(?:/|u002f|x2f)", "/", expanded, flags=re.I)
        expanded = re.sub(r"\\+(?:u0026|x26)", "&", expanded, flags=re.I)
        if expanded == decoded:
            break
        decoded = expanded

    found = set()
    for match in EMBEDDED_PAGE_ROUTE_RE.finditer(decoded):
        normalized = normalize_url(match.group("url"), base_url, keep_query=False)
        if normalized and should_crawl_page(normalized):
            found.add(normalized)
    return found


class PageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: set[str] = set()
        self.assets: set[str] = set()
        self.external_links: set[str] = set()
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.headings: dict[str, list[str]] = defaultdict(list)
        self.text_parts: list[str] = []
        self.lang = ""
        self._in_title = False
        self._skip_text_depth = 0
        self._current_heading: str | None = None
        self._style_depth = 0
        self.inline_css_assets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()

        if tag == "html":
            self.lang = attr.get("lang", "")
        if tag == "title":
            self._in_title = True
        if tag in {"script", "noscript"}:
            self._skip_text_depth += 1
        if tag == "style":
            self._skip_text_depth += 1
            self._style_depth += 1
        if tag in {"h1", "h2", "h3"}:
            self._current_heading = tag

        if tag == "meta":
            key = attr.get("name") or attr.get("property") or attr.get("http-equiv")
            content = attr.get("content", "")
            if key and content:
                self.meta[key.lower()] = content.strip()
                if key.lower() in {"og:image", "twitter:image", "twitter:image:src"}:
                    self.add_asset(content)

        if tag == "link":
            href = attr.get("href", "")
            rel = attr.get("rel", "").lower()
            as_attr = attr.get("as", "").lower()
            is_material_link = (
                "stylesheet" in rel
                or "icon" in rel
                or "manifest" in rel
                or "modulepreload" in rel
                or ("preload" in rel and as_attr in {"script", "style", "image", "font", "video", "audio", "fetch"})
            )
            normalized_href = normalize_url(href, self.base_url) if href else None
            if href and (is_material_link or (normalized_href and is_asset_url(normalized_href))):
                self.add_asset(href)

        for key in ["href", "src", "data-src", "data-original", "data-lazy", "poster", "data-background"]:
            raw = attr.get(key)
            if not raw:
                continue
            if key == "href" and tag == "a":
                self.add_link(raw)
            elif key == "href" and tag == "area":
                self.add_link(raw)
            elif key == "href":
                continue
            else:
                self.add_asset(raw)

        for key in ["srcset", "data-srcset"]:
            if attr.get(key):
                for raw in split_srcset(attr[key]):
                    self.add_asset(raw)

        style = attr.get("style", "")
        if style:
            self.inline_css_assets.update(extract_css_urls(style, self.base_url))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in {"script", "noscript", "style"} and self._skip_text_depth:
            self._skip_text_depth -= 1
        if tag == "style" and self._style_depth:
            self._style_depth -= 1
        if tag in {"h1", "h2", "h3"}:
            self._current_heading = None

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._style_depth:
            self.inline_css_assets.update(extract_css_urls(data, self.base_url))
            return
        if self._in_title:
            self.title_parts.append(text)
        if self._skip_text_depth:
            return
        if self._current_heading:
            self.headings[self._current_heading].append(text)
        self.text_parts.append(text)

    def add_link(self, raw: str) -> None:
        normalized = normalize_url(raw, self.base_url)
        if not normalized:
            return
        if is_same_site(normalized):
            if is_asset_url(normalized):
                self.assets.add(normalized)
            else:
                self.links.add(normalized)
        else:
            self.external_links.add(normalized)

    def add_asset(self, raw: str) -> None:
        normalized = normalize_url(raw, self.base_url)
        if normalized:
            self.assets.add(normalized)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts).strip()


@dataclass
class PageRecord:
    index: int
    url: str
    final_url: str
    status: int
    content_type: str
    title: str
    description: str
    keywords: str
    lang: str
    h1: str
    h2: str
    word_count: int
    internal_links: int
    external_links: int
    asset_refs: int
    html_file: str
    text_file: str
    bytes: int


@dataclass
class AssetRecord:
    url: str
    final_url: str = ""
    status: str = "pending"
    content_type: str = ""
    category: str = ""
    extension: str = ""
    size_bytes: int = 0
    local_file: str = ""
    referenced_by: set[str] = field(default_factory=set)
    note: str = ""


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def page_rows(page_records: list[PageRecord]) -> list[dict]:
    return [record.__dict__ for record in page_records]


def build_asset_rows(assets: dict[str, AssetRecord]) -> list[dict]:
    rows = []
    for record in sorted(assets.values(), key=lambda item: item.url):
        rows.append({
            "url": record.url,
            "final_url": record.final_url,
            "status": record.status,
            "category": record.category or asset_category(record.url),
            "extension": record.extension or path_ext(record.url),
            "content_type": record.content_type,
            "size_bytes": record.size_bytes,
            "local_file": record.local_file,
            "referenced_by_count": len(record.referenced_by),
            "referenced_by": " | ".join(sorted(record.referenced_by)[:20]),
            "note": record.note,
        })
    return rows


def write_error_checkpoint(errors: list[dict]) -> None:
    reports = OUTPUT_ROOT / "reports"
    write_csv(reports / "errors_partial.csv", errors, ["url", "stage", "error"])
    write_json(reports / "errors_partial.json", errors)


def write_page_checkpoint(page_records: list[PageRecord], external_links: list[dict], errors: list[dict], notes: dict) -> None:
    reports = OUTPUT_ROOT / "reports"
    rows = page_rows(page_records)
    write_csv(reports / "pages_partial.csv", rows, [
        "index", "url", "final_url", "status", "content_type", "title", "description",
        "keywords", "lang", "h1", "h2", "word_count", "internal_links",
        "external_links", "asset_refs", "html_file", "text_file", "bytes",
    ])
    write_json(reports / "pages_partial.json", rows)
    write_csv(reports / "external_links_partial.csv", external_links, ["source_page", "external_url"])
    write_json(reports / "external_links_partial.json", external_links)
    write_error_checkpoint(errors)
    write_status({
        "phase": "pages_complete",
        "pages_saved": len(page_records),
        "external_links": len(external_links),
        "errors": len(errors),
        "notes": notes,
        "report_ready": False,
    })


def write_asset_checkpoint(
    assets: dict[str, AssetRecord],
    errors: list[dict],
    processed: int,
    queue_remaining: int,
    last_url: str = "",
) -> None:
    reports = OUTPUT_ROOT / "reports"
    rows = build_asset_rows(assets)
    write_csv(reports / "assets_partial.csv", rows, [
        "url", "final_url", "status", "category", "extension", "content_type",
        "size_bytes", "local_file", "referenced_by_count", "referenced_by", "note",
    ])
    write_json(reports / "assets_partial.json", rows)
    write_error_checkpoint(errors)
    downloaded = sum(1 for row in rows if row["status"] in {"downloaded", "downloaded_cached"})
    failed = sum(
        1 for row in rows
        if row["status"] in {"error", "unexpected_error", "skipped_too_large"} or row["status"].startswith("http_")
    )
    skipped_or_listed = sum(1 for row in rows if row["status"].startswith("skipped") or row["status"].startswith("listed"))
    total_size = sum(row["size_bytes"] for row in rows if row["status"] in {"downloaded", "downloaded_cached"})
    write_status({
        "phase": "assets",
        "assets_processed": processed,
        "assets_total": len(assets),
        "assets_remaining": max(0, len(assets) - processed),
        "queue_remaining": queue_remaining,
        "downloaded_or_cached": downloaded,
        "failed": failed,
        "skipped_or_listed": skipped_or_listed,
        "downloaded_size_mb": round(total_size / (1024 * 1024), 2),
        "last_url": last_url,
        "errors": len(errors),
        "report_ready": False,
    })


def load_robot_parser(errors: list[dict]) -> tuple[robotparser.RobotFileParser, str]:
    robots_url = urljoin(ROOT_URL, "/robots.txt")
    robots_path = OUTPUT_ROOT / "reports" / "robots.txt"
    parser = robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        status, final_url, headers, data = fetch_bytes(robots_url)
        if status == 200:
            content = decode_body(data, headers)
            robots_path.write_text(content, encoding="utf-8")
            parser.parse(content.splitlines())
            return parser, f"robots.txt loaded from {final_url}"
        robots_path.write_text(f"robots.txt unavailable, status {status}\n", encoding="utf-8")
        parser.parse([])
        return parser, f"robots.txt unavailable, status {status}"
    except Exception as error:
        errors.append({"url": robots_url, "stage": "robots", "error": repr(error)})
        robots_path.write_text(f"robots.txt fetch error: {error!r}\n", encoding="utf-8")
        parser.parse([])
        return parser, f"robots.txt fetch error: {error!r}"


def load_sitemap_seed(errors: list[dict]) -> tuple[list[str], str]:
    sitemap_url = urljoin(ROOT_URL, "/sitemap.xml")
    seeds = []
    try:
        status, final_url, headers, data = fetch_bytes(sitemap_url)
        text = decode_body(data, headers)
        (OUTPUT_ROOT / "reports" / "sitemap.xml").write_text(text, encoding="utf-8")
        if status != 200:
            return [], f"sitemap.xml status {status}"
        try:
            root = ElementTree.fromstring(data)
            for loc in root.findall(".//{*}loc"):
                if loc.text:
                    normalized = normalize_url(loc.text.strip(), ROOT_URL)
                    if normalized and should_crawl_page(normalized):
                        seeds.append(normalized)
        except ElementTree.ParseError as error:
            errors.append({"url": sitemap_url, "stage": "sitemap_parse", "error": repr(error)})
        return seeds, f"sitemap.xml loaded from {final_url}, usable same-domain English seeds: {len(seeds)}"
    except Exception as error:
        errors.append({"url": sitemap_url, "stage": "sitemap", "error": repr(error)})
        return [], f"sitemap.xml fetch error: {error!r}"


def load_current_route_seeds() -> tuple[list[str], str]:
    seed_path = OUTPUT_ROOT / "reports" / "current_route_seeds_20000101_000000.json"
    if not seed_path.exists():
        return [], f"route seed file unavailable: {seed_path.name}"
    try:
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("routes", [])
        seeds: list[str] = []
        for row in rows:
            raw = row if isinstance(row, str) else row.get("route")
            if not isinstance(raw, str):
                continue
            normalized = normalize_url(raw, ROOT_URL, keep_query=False)
            if normalized and should_crawl_page(normalized):
                seeds.append(normalized)
        unique = list(dict.fromkeys(seeds))
        return unique, f"route seeds loaded from {seed_path.name}: {len(unique)} English URLs"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return [], f"route seed load error: {error!r}"


def english_enough(parser: PageParser, url: str) -> bool:
    path_lower = unquote(urlparse(url).path).lower()
    if any(path_lower == prefix or path_lower.startswith(prefix + "/") for prefix in SKIP_PAGE_PREFIXES):
        return False
    lang = (parser.lang or "").lower()
    if lang and not (lang.startswith("en") or lang in {"x-default"}):
        return False
    combined = " ".join([parser.title, parser.meta.get("description", ""), parser.text[:2500]])
    ascii_letters = len(re.findall(r"[A-Za-z]", combined))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", combined))
    if ascii_letters < 40 and cjk > ascii_letters:
        return False
    return True


def crawl_pages() -> tuple[list[PageRecord], dict[str, AssetRecord], list[dict], list[dict], dict]:
    ensure_dirs()
    errors: list[dict] = []
    external_links: list[dict] = []
    log_progress("Checking robots.txt and sitemap.")
    write_status({"phase": "pages", "pages_saved": 0, "assets_discovered": 0, "report_ready": False})
    robot, robots_note = load_robot_parser(errors)
    sitemap_seeds, sitemap_note = load_sitemap_seed(errors)
    audit_seeds, audit_seed_note = load_current_route_seeds()
    log_progress(f"Robots: {robots_note}")
    log_progress(f"Sitemap: {sitemap_note}")
    log_progress(f"Audit seeds: {audit_seed_note}")

    root = normalize_url(ROOT_URL, keep_query=False) or ROOT_URL
    queue = deque(dict.fromkeys([root] + sitemap_seeds + audit_seeds))
    queued = set(queue)
    visited: set[str] = set()
    page_records: list[PageRecord] = []
    assets: dict[str, AssetRecord] = {}

    while queue and len(page_records) < MAX_PAGES:
        url = queue.popleft()
        queued.discard(url)
        if url in visited:
            continue
        visited.add(url)
        if not should_crawl_page(url):
            continue
        if not robot.can_fetch(USER_AGENT, url):
            errors.append({"url": url, "stage": "page", "error": "blocked by robots.txt"})
            log_progress(f"PAGE skipped by robots.txt: {url}")
            continue

        try:
            log_progress(f"PAGE fetching candidate={len(visited)} saved={len(page_records)} queue={len(queue)} url={url}")
            status, final_url, headers, data = fetch_bytes(url)
            content_type = headers.get("Content-Type", "")
            if status >= 400:
                errors.append({"url": url, "stage": "page", "error": f"HTTP {status}"})
                log_progress(f"PAGE error HTTP {status}: {url}")
                time.sleep(PAGE_DELAY_SECONDS)
                continue
            if "html" not in content_type.lower() and path_ext(final_url) not in HTML_EXTS:
                asset = assets.setdefault(url, AssetRecord(url=url))
                asset.status = f"seen_as_non_html_{status}"
                asset.content_type = content_type
                log_progress(f"PAGE treated as asset/non-html: {final_url}")
                time.sleep(PAGE_DELAY_SECONDS)
                continue

            text = decode_body(data, headers)
            parser = PageParser(final_url)
            parser.feed(text)
            parser.links.update(extract_embedded_page_routes(text, final_url))
            if not english_enough(parser, final_url):
                log_progress(f"PAGE skipped non-English/language path: {final_url}")
                time.sleep(PAGE_DELAY_SECONDS)
                continue

            index = len(page_records) + 1
            stem = page_file_stem(index, final_url)
            html_file = OUTPUT_ROOT / "pages_html_raw" / f"{stem}.html"
            text_file = OUTPUT_ROOT / "pages_text" / f"{stem}.txt"
            html_file.write_text(text, encoding="utf-8", errors="replace")
            text_file.write_text(parser.text, encoding="utf-8", errors="replace")

            for asset_url in sorted(parser.assets | parser.inline_css_assets):
                record = assets.setdefault(asset_url, AssetRecord(url=asset_url))
                record.referenced_by.add(final_url)

            for external in sorted(parser.external_links):
                external_links.append({"source_page": final_url, "external_url": external})

            for link in sorted(parser.links):
                normalized = normalize_url(link, final_url)
                if not normalized or normalized in visited or normalized in queued:
                    continue
                if should_crawl_page(normalized):
                    queue.append(normalized)
                    queued.add(normalized)

            words = re.findall(r"[A-Za-z][A-Za-z0-9'&-]*", parser.text)
            page_records.append(PageRecord(
                index=index,
                url=url,
                final_url=final_url,
                status=status,
                content_type=content_type,
                title=parser.title,
                description=parser.meta.get("description", ""),
                keywords=parser.meta.get("keywords", ""),
                lang=parser.lang,
                h1=" | ".join(parser.headings.get("h1", [])[:6]),
                h2=" | ".join(parser.headings.get("h2", [])[:12]),
                word_count=len(words),
                internal_links=len(parser.links),
                external_links=len(parser.external_links),
                asset_refs=len(parser.assets | parser.inline_css_assets),
                html_file=str(html_file),
                text_file=str(text_file),
                bytes=len(data),
            ))
            log_progress(
                f"PAGE saved #{index}: assets_on_page={len(parser.assets | parser.inline_css_assets)} "
                f"new_queue={len(queue)} title={parser.title[:90]!r}"
            )
            write_status({
                "phase": "pages",
                "pages_saved": len(page_records),
                "page_candidates_seen": len(visited),
                "page_queue_remaining": len(queue),
                "assets_discovered": len(assets),
                "last_url": final_url,
                "errors": len(errors),
                "report_ready": False,
            })
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            errors.append({"url": url, "stage": "page", "error": repr(error)})
            log_progress(f"PAGE exception: {url} -> {error!r}")
        except Exception as error:
            errors.append({"url": url, "stage": "page_unexpected", "error": repr(error)})
            log_progress(f"PAGE unexpected exception: {url} -> {error!r}")
        time.sleep(PAGE_DELAY_SECONDS)

    notes = {
        "started_at": now_stamp(),
        "root_url": ROOT_URL,
        "robots": robots_note,
        "sitemap": sitemap_note,
        "audit_seeds": audit_seed_note,
        "visited_candidates": len(visited),
        "queued_remaining": len(queue),
        "max_pages": MAX_PAGES,
    }
    return page_records, assets, external_links, errors, notes


def download_assets(assets: dict[str, AssetRecord], errors: list[dict], robot: robotparser.RobotFileParser | None = None) -> None:
    queue = deque(sorted(assets.keys()))
    processed: set[str] = set()
    log_progress(f"ASSET phase started: discovered={len(assets)}")
    write_asset_checkpoint(assets, errors, 0, len(queue))

    def add_nested_css_assets(local_path: Path, final_url: str, headers: dict) -> None:
        if asset_category(final_url, headers) != "css":
            return
        css_text = decode_body(local_path.read_bytes(), headers)
        for nested in sorted(extract_css_urls(css_text, final_url)):
            if nested not in assets:
                nested_record = AssetRecord(url=nested)
                nested_record.referenced_by.add(final_url)
                assets[nested] = nested_record
                queue.append(nested)
                log_progress(f"ASSET discovered from CSS: {nested}")
            else:
                assets[nested].referenced_by.add(final_url)

    def checkpoint(last_url: str, force: bool = False) -> None:
        if force or len(processed) % CHECKPOINT_EVERY_ASSETS == 0 or not queue:
            write_asset_checkpoint(assets, errors, len(processed), len(queue), last_url)

    while queue:
        url = queue.popleft()
        if url in processed:
            continue
        processed.add(url)
        record = assets.setdefault(url, AssetRecord(url=url))
        record.extension = path_ext(url)

        if not is_same_site(url):
            record.status = "listed_external_not_downloaded"
            record.category = asset_category(url)
            record.note = "External dependency/resource; listed for redesign reference only."
            log_progress(f"ASSET listed external processed={len(processed)}/{len(assets)} url={url}")
            checkpoint(url)
            time.sleep(ASSET_DELAY_SECONDS)
            continue
        if robot is not None and not robot.can_fetch(USER_AGENT, url):
            record.status = "skipped_robots"
            record.category = asset_category(url)
            record.note = "Blocked by robots.txt"
            log_progress(f"ASSET skipped robots processed={len(processed)}/{len(assets)} url={url}")
            checkpoint(url, force=True)
            time.sleep(ASSET_DELAY_SECONDS)
            continue

        cached_path = asset_local_path(url)
        if cached_path.exists() and cached_path.stat().st_size > 0:
            record.final_url = url
            record.category = asset_category(url)
            record.extension = path_ext(url)
            record.local_file = str(cached_path)
            record.size_bytes = cached_path.stat().st_size
            record.status = "downloaded_cached"
            log_progress(
                f"ASSET cached category={record.category} size={record.size_bytes}B "
                f"processed={len(processed)}/{len(assets)} file={cached_path.name}"
            )
            add_nested_css_assets(cached_path, url, {})
            checkpoint(url)
            time.sleep(ASSET_DELAY_SECONDS)
            continue

        for attempt in range(1, ASSET_RETRY_ATTEMPTS + 1):
            try:
                log_progress(
                    f"ASSET fetching processed={len(processed)}/{len(assets)} queue={len(queue)} "
                    f"attempt={attempt}/{ASSET_RETRY_ATTEMPTS} url={url}"
                )
                with urlopen(request(url), timeout=TIMEOUT_SECONDS) as response:
                    status = getattr(response, "status", 200)
                    final_url = response.geturl()
                    headers = dict(response.headers.items())
                    record.final_url = final_url
                    record.content_type = headers.get("Content-Type", "")
                    record.category = asset_category(final_url, headers)
                    record.extension = path_ext(final_url) or record.extension
                    if status >= 400:
                        record.status = f"http_{status}"
                        errors.append({"url": url, "stage": "asset", "error": f"HTTP {status}"})
                        log_progress(f"ASSET error HTTP {status}: {url}")
                        break
                    content_length = int(headers.get("Content-Length") or 0)
                    if content_length > MAX_ASSET_BYTES:
                        raise ValueError(f"asset content-length exceeds {MAX_ASSET_BYTES} bytes")
                    if "html" in record.content_type.lower() and not is_asset_url(final_url):
                        record.status = "skipped_html_link"
                        record.note = "This URL resolved to HTML, so it was kept out of the material download set."
                        log_progress(f"ASSET skipped HTML link: {url}")
                        break

                    local_path = asset_local_path(final_url, headers)
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    record.local_file = str(local_path)
                    if local_path.exists() and local_path.stat().st_size > 0:
                        record.size_bytes = local_path.stat().st_size
                        record.status = "downloaded_cached"
                        log_progress(
                            f"ASSET cached category={record.category} size={record.size_bytes}B "
                            f"processed={len(processed)}/{len(assets)} file={local_path.name}"
                        )
                    else:
                        tmp_path = local_path.with_name(local_path.name + ".part")
                        total = 0
                        last_logged = 0
                        with tmp_path.open("wb") as handle:
                            while True:
                                chunk = response.read(1024 * 256)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > MAX_ASSET_BYTES:
                                    raise ValueError(f"asset exceeds {MAX_ASSET_BYTES} bytes")
                                handle.write(chunk)
                                if (
                                    total - last_logged >= ASSET_PROGRESS_INTERVAL_BYTES
                                    or (content_length and total == content_length)
                                ):
                                    last_logged = total
                                    if content_length:
                                        log_progress(
                                            f"ASSET downloading {total}/{content_length}B "
                                            f"processed={len(processed)}/{len(assets)} url={url}"
                                        )
                                    else:
                                        log_progress(
                                            f"ASSET downloading {total}B "
                                            f"processed={len(processed)}/{len(assets)} url={url}"
                                        )
                        tmp_path.replace(local_path)
                        record.size_bytes = total
                        record.status = "downloaded"
                log_progress(
                    f"ASSET saved category={record.category} size={record.size_bytes}B "
                    f"processed={len(processed)}/{len(assets)} file={Path(record.local_file).name}"
                )
                add_nested_css_assets(Path(record.local_file), record.final_url or url, headers)
                break
            except ValueError as error:
                record.status = "skipped_too_large"
                record.note = str(error)
                errors.append({"url": url, "stage": "asset", "error": repr(error)})
                log_progress(f"ASSET skipped too large: {url} -> {error!r}")
                break
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                if attempt < ASSET_RETRY_ATTEMPTS:
                    log_progress(
                        f"ASSET retry scheduled next_attempt={attempt + 1}/{ASSET_RETRY_ATTEMPTS} "
                        f"url={url} error={error!r}"
                    )
                    time.sleep(RETRY_DELAY_SECONDS * attempt)
                    continue
                record.status = "error"
                record.note = repr(error)
                errors.append({"url": url, "stage": "asset", "error": repr(error)})
                log_progress(f"ASSET exception: {url} -> {error!r}")
            except Exception as error:
                record.status = "unexpected_error"
                record.note = repr(error)
                errors.append({"url": url, "stage": "asset_unexpected", "error": repr(error)})
                log_progress(f"ASSET unexpected exception: {url} -> {error!r}")
                break
        checkpoint(url, force=record.status not in {"downloaded", "downloaded_cached"})
        time.sleep(ASSET_DELAY_SECONDS)

    write_asset_checkpoint(assets, errors, len(processed), 0)


def summarize_formats(assets: dict[str, AssetRecord]) -> list[dict]:
    summary: dict[tuple[str, str, str], dict] = {}
    for record in assets.values():
        key = (record.category or asset_category(record.url), record.extension or path_ext(record.url), record.content_type or "")
        item = summary.setdefault(key, {
            "category": key[0],
            "extension": key[1],
            "content_type": key[2],
            "count": 0,
            "downloaded": 0,
            "total_size_bytes": 0,
        })
        item["count"] += 1
        if record.status in {"downloaded", "downloaded_cached"}:
            item["downloaded"] += 1
            item["total_size_bytes"] += record.size_bytes
    return sorted(summary.values(), key=lambda row: (row["category"], row["extension"], row["content_type"]))


def write_reports(page_records: list[PageRecord], assets: dict[str, AssetRecord], external_links: list[dict], errors: list[dict], notes: dict) -> None:
    reports = OUTPUT_ROOT / "reports"

    pages_rows = page_rows(page_records)
    write_csv(reports / "pages.csv", pages_rows, [
        "index", "url", "final_url", "status", "content_type", "title", "description",
        "keywords", "lang", "h1", "h2", "word_count", "internal_links",
        "external_links", "asset_refs", "html_file", "text_file", "bytes",
    ])
    write_json(reports / "pages.json", pages_rows)

    asset_rows = build_asset_rows(assets)
    write_csv(reports / "assets.csv", asset_rows, [
        "url", "final_url", "status", "category", "extension", "content_type",
        "size_bytes", "local_file", "referenced_by_count", "referenced_by", "note",
    ])
    write_json(reports / "assets.json", asset_rows)

    write_csv(reports / "external_links.csv", external_links, ["source_page", "external_url"])
    write_json(reports / "external_links.json", external_links)

    write_csv(reports / "errors.csv", errors, ["url", "stage", "error"])
    write_json(reports / "errors.json", errors)

    format_rows = summarize_formats(assets)
    write_csv(reports / "asset_format_summary.csv", format_rows, [
        "category", "extension", "content_type", "count", "downloaded", "total_size_bytes",
    ])
    write_json(reports / "asset_format_summary.json", format_rows)

    downloaded_assets = [row for row in asset_rows if row["status"] in {"downloaded", "downloaded_cached"}]
    same_site_assets = [row for row in asset_rows if is_same_site(row["url"])]
    external_assets = [row for row in asset_rows if not is_same_site(row["url"])]
    total_size = sum(row["size_bytes"] for row in downloaded_assets)

    tree_lines = ["# Site tree", ""]
    for record in page_records:
        path = urlparse(record.final_url).path or "/"
        depth = max(0, len([part for part in path.split("/") if part]) - 1)
        indent = "  " * min(depth, 6)
        label = record.title or path
        tree_lines.append(f"{indent}- [{record.index:04d}] {label} - {record.final_url}")
    (reports / "site_tree.md").write_text("\n".join(tree_lines), encoding="utf-8")

    lines = [
        "# SellerKit English Site Inventory",
        "",
        f"- Target: {ROOT_URL}",
        f"- Output folder: {OUTPUT_ROOT}",
        f"- Generated at: {now_stamp()}",
        f"- Crawl mode: public same-domain English page inventory; no login, no bypass, no encrypted/private content.",
        f"- Robots note: {notes.get('robots', '')}",
        f"- Sitemap note: {notes.get('sitemap', '')}",
        f"- Page delay seconds: {PAGE_DELAY_SECONDS}",
        f"- Asset delay seconds: {ASSET_DELAY_SECONDS}",
        "",
        "## Totals",
        "",
        f"- English pages saved: {len(page_records)}",
        f"- Asset URLs discovered: {len(asset_rows)}",
        f"- Same-domain asset URLs: {len(same_site_assets)}",
        f"- External asset URLs listed: {len(external_assets)}",
        f"- Assets downloaded: {len(downloaded_assets)}",
        f"- Downloaded asset size: {total_size / (1024 * 1024):.2f} MB",
        f"- External links listed: {len(external_links)}",
        f"- Errors/warnings: {len(errors)}",
        "",
        "## Main Files",
        "",
        "- `reports/pages.csv` / `reports/pages.json`: page URL, title, meta, headings, text and raw HTML paths.",
        "- `reports/assets.csv` / `reports/assets.json`: asset URL, type, content type, extension, size, local path, referring page.",
        "- `reports/asset_format_summary.csv`: material format counts by category, extension, and MIME type.",
        "- `reports/external_links.csv`: outbound page links.",
        "- `reports/site_tree.md`: crawl tree ordered by discovery.",
        "- `pages_html_raw/`: saved raw HTML.",
        "- `pages_text/`: extracted visible text for redesign/content review.",
        "- `assets/`: downloaded same-domain images, CSS, JS, fonts, media, documents, and other files.",
        "",
        "## Asset Format Summary",
        "",
        "| Category | Extension | Content-Type | Count | Downloaded | Size MB |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for row in format_rows:
        lines.append(
            f"| {row['category']} | {row['extension'] or '(none)'} | {row['content_type'] or '(unknown)'} "
            f"| {row['count']} | {row['downloaded']} | {row['total_size_bytes'] / (1024 * 1024):.2f} |"
        )

    lines.extend([
        "",
        "## Notes",
        "",
        "- The public sitemap currently contains localhost URLs, so discovery used homepage/internal links instead.",
        "- External CDN and third-party resources are listed, not crawled as pages.",
        "- Non-English language paths such as `/cn` and translation switch links were skipped.",
    ])
    (reports / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ensure_dirs()
    reset_progress_log()
    write_status({"phase": "starting", "report_ready": False})
    log_progress("SellerKit inventory crawl started.")
    page_records, assets, external_links, errors, notes = crawl_pages()
    log_progress(f"Page phase complete: pages={len(page_records)} assets_discovered={len(assets)} errors={len(errors)}")
    write_page_checkpoint(page_records, external_links, errors, notes)
    robot, _ = load_robot_parser(errors)
    download_assets(assets, errors, robot)
    log_progress(f"Asset phase complete: assets_total={len(assets)} errors={len(errors)}")
    log_progress("Writing reports.")
    write_reports(page_records, assets, external_links, errors, notes)
    write_status({
        "phase": "complete",
        "pages_saved": len(page_records),
        "assets_total": len(assets),
        "errors": len(errors),
        "report_ready": True,
        "report": str(OUTPUT_ROOT / "reports" / "README.md"),
    })
    log_progress(f"Done. Report: {OUTPUT_ROOT / 'reports' / 'README.md'}")
    print(json.dumps({
        "output": str(OUTPUT_ROOT),
        "pages": len(page_records),
        "assets": len(assets),
        "errors": len(errors),
        "report": str(OUTPUT_ROOT / "reports" / "README.md"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
