from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import warnings
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Iterable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .config import EXPECTED, Paths, get_settings
from .utils import (
    assert_no_reparse_components,
    ensure_within,
    exclusive_file_lock,
    load_json,
    load_jsonl,
    sha256_file,
    stable_json_text,
    write_json_atomic,
    write_text_atomic,
)


SCHEMA_VERSION = "sellerkit.website_image_materialization.v1"
RECEIPT_SCHEMA_VERSION = "sellerkit.website_image_materialization_receipt.v1"
CHECKPOINT_SCHEMA_VERSION = "sellerkit.website_image_materialization_checkpoint.v1"
SUPPORTED_ROLES = frozenset({"main_image", "gallery"})
from .config import configured_website_hosts
ALLOWED_HOSTS = configured_website_hosts()
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"})
MAX_PER_HOST_WORKERS = 8
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MIN_IMAGE_BYTES = 512
MIN_IMAGE_EDGE = 64
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
PLACEHOLDER_NAME_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])(?:no[ _-]?image|placeholder|default[ _-]?image|blank|spacer|loading)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
CACHE_PREFIX_PATTERN = re.compile(r"^[a-z0-9.-]+_[0-9a-f]{12}_(.+)$", re.IGNORECASE)


class ImageMaterializationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImageMetadata:
    sha256: str
    size_bytes: int
    content_type: str
    image_format: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True)
class CacheFile:
    cache_kind: str
    path: Path


@dataclass(frozen=True)
class CacheResolution:
    key: str
    unique_sha256: str
    chosen: CacheFile | None
    metadata: ImageMetadata | None
    valid_files: tuple[tuple[CacheFile, ImageMetadata], ...]
    invalid_files: tuple[tuple[CacheFile, str, str], ...]
    distinct_sha256: tuple[str, ...]


@dataclass(frozen=True)
class Acquisition:
    status: str
    verification_status: str
    acquisition_method: str
    local_file: str = ""
    sha256: str = ""
    size_bytes: int | None = None
    content_type: str = ""
    response_content_type: str = ""
    image_format: str = ""
    width: int | None = None
    height: int | None = None
    final_url: str = ""
    cache_sources: tuple[dict[str, Any], ...] = ()
    resumed_from_checkpoint: bool = False
    failure_code: str = ""
    failure_detail: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _clean_detail(value: Any, limit: int = 800) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _ascii_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.encode("idna").decode("ascii"),
            quote(unquote(parsed.path), safe="/%:@!$&'()*+,;=-._~"),
            quote(unquote(parsed.query), safe="=&%:+,;@/?-._~"),
            "",
        )
    )


def _validate_public_url(url: str) -> str:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https":
        raise ImageMaterializationError("UNSAFE_URL_SCHEME", f"Only HTTPS image URLs are allowed: {url}")
    if hostname not in ALLOWED_HOSTS:
        raise ImageMaterializationError("UNAPPROVED_IMAGE_HOST", f"Image host is outside the official public site: {url}")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ImageMaterializationError("UNSAFE_URL_AUTHORITY", f"Credentials or a nonstandard port are forbidden: {url}")
    if parsed.fragment:
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return url


def _canonical_suffix(value: str) -> str:
    suffix = value.casefold().lstrip(".")
    return "jpg" if suffix in {"jpg", "jpeg"} else suffix


def _normalized_basename(value: str) -> str:
    basename = Path(unquote(value)).name
    matched = CACHE_PREFIX_PATTERN.match(basename)
    if matched:
        basename = matched.group(1)
    suffix = _canonical_suffix(Path(basename).suffix)
    stem = unicodedata.normalize("NFKC", Path(basename).stem).casefold()
    normalized_stem = "".join(character for character in stem if character.isalnum())
    return f"{normalized_stem}.{suffix}" if normalized_stem and suffix else normalized_stem


def _source_basename(url: str) -> str:
    return Path(unquote(urlsplit(url).path)).name


def _sniff_mime(path: Path) -> str:
    with path.open("rb") as stream:
        head = stream.read(64)
    stripped = head.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise ImageMaterializationError("HTML_RESPONSE_REJECTED", "Payload is HTML, not an image")
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 16 and head[4:8] == b"ftyp" and head[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    raise ImageMaterializationError("UNKNOWN_IMAGE_SIGNATURE", "Payload has no supported raster image signature")


def _validate_image_file(
    path: Path,
    *,
    source_basename: str,
    declared_content_type: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
    expected_sha256: str = "",
) -> ImageMetadata:
    size_bytes = path.stat().st_size
    if size_bytes == 0:
        raise ImageMaterializationError("EMPTY_IMAGE_REJECTED", "Image file is empty")
    if size_bytes < MIN_IMAGE_BYTES:
        raise ImageMaterializationError("TINY_IMAGE_REJECTED", f"Image is only {size_bytes} bytes")
    if size_bytes > max_bytes:
        raise ImageMaterializationError("IMAGE_SIZE_LIMIT_EXCEEDED", f"Image is {size_bytes} bytes; limit is {max_bytes}")
    if PLACEHOLDER_NAME_PATTERN.search(unicodedata.normalize("NFKC", source_basename)):
        raise ImageMaterializationError("PLACEHOLDER_NAME_REJECTED", f"Placeholder-like image name: {source_basename}")

    declared = declared_content_type.partition(";")[0].strip().casefold()
    if declared and declared not in {"application/octet-stream", "binary/octet-stream"} and not declared.startswith("image/"):
        raise ImageMaterializationError("NON_IMAGE_MIME_REJECTED", f"Response Content-Type is {declared}")
    if declared == "image/svg+xml":
        raise ImageMaterializationError("VECTOR_IMAGE_REJECTED", "SVG is not accepted as a product raster image")

    signature_mime = _sniff_mime(path)
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImageMaterializationError("PILLOW_REQUIRED", "Pillow is required for full image decode validation") from exc

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                width, height = int(image.width), int(image.height)
                image_format = str(image.format or "").upper()
                pillow_mime = str(Image.MIME.get(image_format) or "").casefold()
    except ImageMaterializationError:
        raise
    except Exception as exc:
        raise ImageMaterializationError("IMAGE_DECODE_REJECTED", _clean_detail(exc)) from exc

    if width < MIN_IMAGE_EDGE or height < MIN_IMAGE_EDGE:
        raise ImageMaterializationError(
            "PLACEHOLDER_DIMENSIONS_REJECTED",
            f"Image dimensions {width}x{height} are below the {MIN_IMAGE_EDGE}px minimum edge",
        )
    if not image_format or not pillow_mime:
        raise ImageMaterializationError("UNSUPPORTED_IMAGE_FORMAT", f"Pillow reported unsupported format {image_format!r}")
    if pillow_mime != signature_mime:
        raise ImageMaterializationError(
            "IMAGE_SIGNATURE_FORMAT_MISMATCH",
            f"Signature is {signature_mime}, decoder reports {pillow_mime}",
        )
    if declared.startswith("image/") and declared not in {signature_mime, "image/jpg", "image/pjpeg"}:
        raise ImageMaterializationError(
            "IMAGE_MIME_SIGNATURE_MISMATCH",
            f"Response Content-Type is {declared}, signature is {signature_mime}",
        )

    digest = sha256_file(path)
    if expected_sha256 and digest != expected_sha256:
        raise ImageMaterializationError(
            "IMAGE_HASH_MISMATCH",
            f"Expected SHA-256 {expected_sha256}, observed {digest}",
        )
    extension = {
        "JPEG": "jpg",
        "PNG": "png",
        "GIF": "gif",
        "BMP": "bmp",
        "TIFF": "tiff",
        "WEBP": "webp",
        "AVIF": "avif",
    }.get(image_format)
    if not extension:
        raise ImageMaterializationError("UNSUPPORTED_IMAGE_FORMAT", f"Unsupported decoded format {image_format}")
    return ImageMetadata(
        sha256=digest,
        size_bytes=size_bytes,
        content_type=signature_mime,
        image_format=image_format,
        extension=extension,
        width=width,
        height=height,
    )


def _cache_roots(paths: Paths) -> tuple[tuple[str, Path], ...]:
    stable_root = paths.website_candidate_root.parent.parent / "assets"
    candidate_root = paths.website_candidate_root / "assets"
    return (("stable", stable_root), ("candidate", candidate_root))


def _build_cache_index(paths: Paths) -> tuple[dict[str, list[CacheFile]], dict[str, Any]]:
    index: dict[str, list[CacheFile]] = defaultdict(list)
    per_cache: dict[str, dict[str, Any]] = {}
    for cache_kind, root in _cache_roots(paths):
        assert_no_reparse_components(root, f"website_{cache_kind}_image_cache")
        scanned = 0
        indexed = 0
        if root.is_dir():
            for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
                if not path.is_file() or path.suffix.casefold() not in IMAGE_SUFFIXES:
                    continue
                scanned += 1
                safe_path = ensure_within(path, root)
                key = _normalized_basename(path.name)
                if not key:
                    continue
                indexed += 1
                index[key].append(CacheFile(cache_kind=cache_kind, path=safe_path))
        per_cache[cache_kind] = {
            "root": str(root),
            "root_exists": root.is_dir(),
            "image_files_scanned": scanned,
            "image_files_indexed": indexed,
        }
    for files in index.values():
        files.sort(key=lambda item: (item.cache_kind, str(item.path).casefold()))
    return dict(index), per_cache


def _resolve_cache_keys(
    index: dict[str, list[CacheFile]],
    required_keys: Collection[str],
    *,
    max_bytes: int,
) -> tuple[dict[str, CacheResolution], dict[str, Any]]:
    resolutions: dict[str, CacheResolution] = {}
    validation_cache: dict[Path, ImageMetadata | ImageMaterializationError] = {}
    invalid_details: list[dict[str, str]] = []
    examined = 0
    valid = 0
    for key in sorted(required_keys):
        valid_files: list[tuple[CacheFile, ImageMetadata]] = []
        invalid_files: list[tuple[CacheFile, str, str]] = []
        for cache_file in index.get(key, []):
            examined += 1
            cached = validation_cache.get(cache_file.path)
            if cached is None:
                try:
                    cached = _validate_image_file(
                        cache_file.path,
                        source_basename=cache_file.path.name,
                        max_bytes=max_bytes,
                    )
                except ImageMaterializationError as exc:
                    cached = exc
                validation_cache[cache_file.path] = cached
            if isinstance(cached, ImageMetadata):
                valid += 1
                valid_files.append((cache_file, cached))
            else:
                invalid_files.append((cache_file, cached.code, _clean_detail(cached)))
                invalid_details.append(
                    {"cache_kind": cache_file.cache_kind, "path": str(cache_file.path), "code": cached.code}
                )
        distinct_hashes = tuple(sorted({metadata.sha256 for _, metadata in valid_files}))
        chosen: CacheFile | None = None
        metadata: ImageMetadata | None = None
        unique_hash = distinct_hashes[0] if len(distinct_hashes) == 1 else ""
        if unique_hash:
            choices = [(cache_file, item) for cache_file, item in valid_files if item.sha256 == unique_hash]
            choices.sort(
                key=lambda item: (
                    0 if item[0].cache_kind == "candidate" else 1,
                    str(item[0].path).casefold(),
                )
            )
            chosen, metadata = choices[0]
        resolutions[key] = CacheResolution(
            key=key,
            unique_sha256=unique_hash,
            chosen=chosen,
            metadata=metadata,
            valid_files=tuple(valid_files),
            invalid_files=tuple(invalid_files),
            distinct_sha256=distinct_hashes,
        )
    stats = {
        "required_basename_keys": len(required_keys),
        "matched_basename_keys": sum(bool(index.get(key)) for key in required_keys),
        "candidate_files_examined": examined,
        "valid_candidate_files": valid,
        "invalid_candidate_files": len(invalid_details),
        "invalid_candidate_details": invalid_details,
        "unique_hash_recoverable_keys": sum(bool(item.unique_sha256) for item in resolutions.values()),
        "ambiguous_hash_keys": sum(len(item.distinct_sha256) > 1 for item in resolutions.values()),
        "missing_or_invalid_keys": sum(not item.distinct_sha256 for item in resolutions.values()),
    }
    return resolutions, stats


def _cas_target(cas_root: Path, metadata: ImageMetadata) -> Path:
    return cas_root / metadata.sha256[:2] / f"{metadata.sha256}.{metadata.extension}"


def _commit_file_to_cas(
    source: Path,
    cas_root: Path,
    metadata: ImageMetadata,
    *,
    cas_lock: threading.Lock,
    remove_source: bool,
    max_bytes: int,
) -> Path:
    target = _cas_target(cas_root, metadata)
    target.parent.mkdir(parents=True, exist_ok=True)
    with cas_lock:
        if target.is_file():
            _validate_image_file(
                target,
                source_basename=target.name,
                max_bytes=max_bytes,
                expected_sha256=metadata.sha256,
            )
            if remove_source:
                source.unlink(missing_ok=True)
            return target

        if remove_source:
            staging = source
        else:
            fd, temporary = tempfile.mkstemp(prefix=".cache-copy-", suffix=".partial", dir=target.parent)
            os.close(fd)
            staging = Path(temporary)
            try:
                with source.open("rb") as input_stream, staging.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            except Exception:
                staging.unlink(missing_ok=True)
                raise
        try:
            _validate_image_file(
                staging,
                source_basename=source.name,
                max_bytes=max_bytes,
                expected_sha256=metadata.sha256,
            )
            if target.exists():
                raise ImageMaterializationError("CAS_PATH_COLLISION", f"Unexpected existing CAS path: {target}")
            os.replace(staging, target)
        finally:
            if staging != target:
                staging.unlink(missing_ok=True)
        return target


class _OfficialOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, _ascii_url(newurl))


def _retry_delay(headers: Any, attempt: int) -> float:
    retry_after = str(headers.get("Retry-After") or "").strip() if headers is not None else ""
    if retry_after.isdigit():
        return min(float(retry_after), 30.0)
    return min(2.0 ** (attempt - 1), 8.0)


def _download_to_staging(
    url: str,
    staging_root: Path,
    *,
    timeout_seconds: float,
    max_bytes: int,
    attempts: int,
) -> tuple[Path, ImageMetadata, str, str]:
    safe_url = _validate_public_url(url)
    opener = urllib.request.build_opener(_OfficialOnlyRedirectHandler())
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        temporary: Path | None = None
        try:
            request = urllib.request.Request(
                _ascii_url(safe_url),
                method="GET",
                headers={
                    "User-Agent": "SellerKitLocalEvidenceMaterializer/1.0",
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.8,*/*;q=0.1",
                    "Cache-Control": "no-cache",
                },
            )
            with opener.open(request, timeout=timeout_seconds) as response:
                final_url = _validate_public_url(str(response.geturl()))
                response_content_type = str(response.headers.get("Content-Type") or "")
                content_length = str(response.headers.get("Content-Length") or "").strip()
                if content_length.isdigit() and int(content_length) > max_bytes:
                    raise ImageMaterializationError(
                        "IMAGE_SIZE_LIMIT_EXCEEDED",
                        f"Content-Length is {content_length}; limit is {max_bytes}",
                    )
                fd, temporary_text = tempfile.mkstemp(prefix=".download-", suffix=".partial", dir=staging_root)
                temporary = Path(temporary_text)
                total = 0
                with os.fdopen(fd, "wb") as stream:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ImageMaterializationError(
                                "IMAGE_SIZE_LIMIT_EXCEEDED",
                                f"Download exceeded the {max_bytes} byte limit",
                            )
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                metadata = _validate_image_file(
                    temporary,
                    source_basename=_source_basename(safe_url),
                    declared_content_type=response_content_type,
                    max_bytes=max_bytes,
                )
                completed = temporary
                temporary = None
                return completed, metadata, final_url, response_content_type.partition(";")[0].strip().casefold()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt >= attempts:
                break
            time.sleep(_retry_delay(exc.headers, attempt))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(_retry_delay(None, attempt))
        except ImageMaterializationError:
            raise
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)
    if isinstance(last_error, urllib.error.HTTPError):
        raise ImageMaterializationError("HTTP_DOWNLOAD_FAILED", f"HTTP {last_error.code} for {safe_url}") from last_error
    raise ImageMaterializationError("NETWORK_DOWNLOAD_FAILED", _clean_detail(last_error or "download failed")) from last_error


def _cache_sources(resolution: CacheResolution) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "cache_kind": cache_file.cache_kind,
            "path": str(cache_file.path),
            "sha256": metadata.sha256,
        }
        for cache_file, metadata in sorted(
            resolution.valid_files,
            key=lambda item: (item[0].cache_kind, str(item[0].path).casefold()),
        )
    )


def _acquire_url(
    url: str,
    resolution: CacheResolution | None,
    cas_root: Path,
    staging_root: Path,
    *,
    allow_network: bool,
    timeout_seconds: float,
    max_bytes: int,
    attempts: int,
    cas_lock: threading.Lock,
) -> Acquisition:
    try:
        if resolution and resolution.unique_sha256 and resolution.chosen and resolution.metadata:
            target = _commit_file_to_cas(
                resolution.chosen.path,
                cas_root,
                resolution.metadata,
                cas_lock=cas_lock,
                remove_source=False,
                max_bytes=max_bytes,
            )
            kinds = sorted({cache_file.cache_kind for cache_file, _ in resolution.valid_files})
            return Acquisition(
                status="MATERIALIZED",
                verification_status="MATERIALIZED_VALIDATED_CACHE_RECOVERY",
                acquisition_method="CACHE_BASENAME_UNIQUE_SHA_" + "_AND_".join(item.upper() for item in kinds),
                local_file=str(target),
                sha256=resolution.metadata.sha256,
                size_bytes=resolution.metadata.size_bytes,
                content_type=resolution.metadata.content_type,
                image_format=resolution.metadata.image_format,
                width=resolution.metadata.width,
                height=resolution.metadata.height,
                final_url=url,
                cache_sources=_cache_sources(resolution),
            )
        if not allow_network:
            if resolution and len(resolution.distinct_sha256) > 1:
                raise ImageMaterializationError(
                    "CACHE_AMBIGUOUS_NETWORK_DISABLED",
                    f"Normalized basename has {len(resolution.distinct_sha256)} distinct valid hashes",
                )
            raise ImageMaterializationError("CACHE_MISS_NETWORK_DISABLED", "No unique valid cache image and network is disabled")
        temporary, metadata, final_url, response_content_type = _download_to_staging(
            url,
            staging_root,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            attempts=attempts,
        )
        target = _commit_file_to_cas(
            temporary,
            cas_root,
            metadata,
            cas_lock=cas_lock,
            remove_source=True,
            max_bytes=max_bytes,
        )
        return Acquisition(
            status="MATERIALIZED",
            verification_status="MATERIALIZED_VALIDATED_PUBLIC_DOWNLOAD",
            acquisition_method="PUBLIC_HTTPS_DOWNLOAD",
            local_file=str(target),
            sha256=metadata.sha256,
            size_bytes=metadata.size_bytes,
            content_type=metadata.content_type,
            response_content_type=response_content_type,
            image_format=metadata.image_format,
            width=metadata.width,
            height=metadata.height,
            final_url=final_url,
        )
    except ImageMaterializationError as exc:
        return Acquisition(
            status="FAILED",
            verification_status="UNMATERIALIZED_EXPLICIT_FAILURE",
            acquisition_method="NONE",
            final_url=url,
            failure_code=exc.code,
            failure_detail=_clean_detail(exc),
        )
    except Exception as exc:
        return Acquisition(
            status="FAILED",
            verification_status="UNMATERIALIZED_EXPLICIT_FAILURE",
            acquisition_method="NONE",
            final_url=url,
            failure_code="UNEXPECTED_MATERIALIZATION_ERROR",
            failure_detail=_clean_detail(exc),
        )


def _load_products(
    paths: Paths,
    products: Iterable[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str, str]:
    official_path = paths.work_root / "official_current.jsonl"
    if products is None:
        if not official_path.is_file():
            raise FileNotFoundError(f"Official-current product records are missing: {official_path}")
        loaded = load_jsonl(official_path)
        digest = sha256_file(official_path)
        source_path = str(official_path)
    else:
        loaded = [dict(item) for item in products]
        serialized = "".join(stable_json_text(item) for item in sorted(loaded, key=lambda row: str(row.get("canonical_model") or "")))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        source_path = "<provided official_current records>"
    return sorted(loaded, key=lambda item: str(item.get("canonical_model") or "")), digest, source_path


def _image_references(
    products: list[dict[str, Any]],
    roles: frozenset[str],
    *,
    strict_current_r3: bool,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    release_ids: set[str] = set()
    for product in products:
        model = str(product.get("canonical_model") or "").strip()
        if not model:
            raise ValueError("Official-current product record has no canonical_model")
        if model in seen_models:
            raise ValueError(f"Duplicate official-current canonical model: {model}")
        seen_models.add(model)
        source = product.get("source") or {}
        source_type = str(source.get("source_type") or "")
        release_id = str(source.get("release_id") or "")
        release_ids.add(release_id)
        if source_type != "official_current":
            raise ValueError(f"{model} is not an official_current record")
        if strict_current_r3 and not release_id.endswith("-r3"):
            raise ValueError(f"{model} is outside the approved current release: {release_id}")
        role_counts: Counter[str] = Counter()
        for asset in product.get("assets") or []:
            role = str(asset.get("role") or "")
            if role not in roles:
                continue
            url = _validate_public_url(str(asset.get("source_url") or "").strip())
            role_counts[role] += 1
            asset_order = int(asset.get("order") or role_counts[role])
            identity = f"{model}\0{role}\0{asset_order}\0{url}"
            references.append(
                {
                    "reference_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                    "canonical_model": model,
                    "raw_model": str(product.get("raw_model") or ""),
                    "product_id": product.get("product_id"),
                    "product_name": str(product.get("name") or ""),
                    "role": role,
                    "role_index": role_counts[role],
                    "asset_order": asset_order,
                    "source_url": url,
                    "source_basename": _source_basename(url),
                    "normalized_basename": _normalized_basename(_source_basename(url)),
                    "source_page_url": str(source.get("source_url") or ""),
                    "source_page_sha256": str(source.get("source_sha256") or ""),
                    "release_id": release_id,
                }
            )
        if "main_image" in roles and role_counts["main_image"] != 1:
            raise ValueError(f"{model} has {role_counts['main_image']} main-image references; expected exactly one")
    references.sort(
        key=lambda row: (
            row["canonical_model"],
            0 if row["role"] == "main_image" else 1,
            row["role_index"],
            row["source_url"],
        )
    )
    if strict_current_r3:
        if len(products) != EXPECTED["website_products"]:
            raise ValueError(f"Expected {EXPECTED['website_products']} approved current products, observed {len(products)}")
        expected_by_role = {
            "main_image": EXPECTED["website_main_image_references"],
            "gallery": EXPECTED["website_gallery_references"],
        }
        for role in roles:
            observed = sum(reference["role"] == role for reference in references)
            if observed != expected_by_role[role]:
                raise ValueError(f"Expected {expected_by_role[role]} {role} references, observed {observed}")
        if len(release_ids) != 1:
            raise ValueError(f"Official-current records contain multiple release IDs: {sorted(release_ids)}")
    return references


def _record_from_acquisition(reference: dict[str, Any], acquisition: Acquisition, observed_at_utc: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        **reference,
        "status": acquisition.status,
        "verification_status": acquisition.verification_status,
        "acquisition_method": acquisition.acquisition_method,
        "observed_at_utc": observed_at_utc,
        "final_url": acquisition.final_url,
        "local_file": acquisition.local_file,
        "local_file_exists": bool(acquisition.local_file and Path(acquisition.local_file).is_file()),
        "sha256": acquisition.sha256,
        "size_bytes": acquisition.size_bytes,
        "content_type": acquisition.content_type,
        "response_content_type": acquisition.response_content_type,
        "image_format": acquisition.image_format,
        "width": acquisition.width,
        "height": acquisition.height,
        "cache_sources": list(acquisition.cache_sources),
        "resumed_from_checkpoint": acquisition.resumed_from_checkpoint,
        "failure_code": acquisition.failure_code,
        "failure_detail": acquisition.failure_detail,
    }


def _acquisition_from_record(
    record: dict[str, Any],
    cas_root: Path,
    *,
    max_bytes: int,
) -> Acquisition | None:
    if record.get("status") != "MATERIALIZED":
        return None
    local_text = str(record.get("local_file") or "")
    digest = str(record.get("sha256") or "")
    if not local_text or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    try:
        local_path = ensure_within(Path(local_text), cas_root)
        metadata = _validate_image_file(
            local_path,
            source_basename=local_path.name,
            max_bytes=max_bytes,
            expected_sha256=digest,
        )
    except (OSError, ValueError, ImageMaterializationError):
        return None
    cache_sources = tuple(record.get("cache_sources") or ())
    acquisition_method = str(record.get("acquisition_method") or "")
    if acquisition_method == "RESUMED_CONTENT_ADDRESSED_IMAGE":
        if cache_sources:
            kinds = sorted({str(item.get("cache_kind") or "") for item in cache_sources if item.get("cache_kind")})
            acquisition_method = "CACHE_BASENAME_UNIQUE_SHA_" + "_AND_".join(item.upper() for item in kinds)
        else:
            acquisition_method = "PUBLIC_HTTPS_DOWNLOAD"
    return Acquisition(
        status="MATERIALIZED",
        verification_status="MATERIALIZED_VALIDATED_RESUME",
        acquisition_method=acquisition_method or "PUBLIC_HTTPS_DOWNLOAD",
        local_file=str(local_path),
        sha256=metadata.sha256,
        size_bytes=metadata.size_bytes,
        content_type=metadata.content_type,
        response_content_type=str(record.get("response_content_type") or ""),
        image_format=metadata.image_format,
        width=metadata.width,
        height=metadata.height,
        final_url=str(record.get("final_url") or record.get("source_url") or ""),
        cache_sources=cache_sources,
        resumed_from_checkpoint=True,
    )


def _load_checkpoint(
    checkpoint_path: Path,
    official_digest: str,
    cas_root: Path,
    *,
    max_bytes: int,
) -> dict[str, Acquisition]:
    if not checkpoint_path.is_file():
        return {}
    try:
        checkpoint = load_json(checkpoint_path)
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("official_current_sha256") != official_digest
    ):
        return {}
    resumed: dict[str, Acquisition] = {}
    for record in checkpoint.get("records") or []:
        reference_id = str(record.get("reference_id") or "")
        acquisition = _acquisition_from_record(record, cas_root, max_bytes=max_bytes)
        if reference_id and acquisition:
            resumed[reference_id] = acquisition
    return resumed


def _write_checkpoint(
    checkpoint_path: Path,
    official_digest: str,
    records: Iterable[dict[str, Any]],
    updated_at_utc: str,
) -> None:
    selected = sorted(
        records,
        key=lambda row: (
            str(row.get("canonical_model") or ""),
            str(row.get("role") or ""),
            int(row.get("role_index") or 0),
            str(row.get("source_url") or ""),
        ),
    )
    write_json_atomic(
        checkpoint_path,
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "official_current_sha256": official_digest,
            "updated_at_utc": updated_at_utc,
            "records": selected,
        },
    )


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    write_text_atomic(path, "".join(stable_json_text(record) for record in records))


def materialize_website_images(
    paths: Paths,
    *,
    products: Iterable[dict[str, Any]] | None = None,
    roles: Collection[str] = ("main_image", "gallery"),
    workers: int = MAX_PER_HOST_WORKERS,
    allow_network: bool = True,
    timeout_seconds: float = 30.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    attempts: int = 3,
    strict_current_r3: bool = True,
) -> dict[str, Any]:
    """Materialize official-current product images into a validated SHA-256 CAS.

    The function never modifies either source cache. It first accepts a cache
    basename only when all valid candidates for that normalized basename have
    one content hash, then uses public HTTPS GETs for unresolved references.
    JSONL outputs and the receipt are atomically replaced; immutable image blobs
    are added to the content-addressed store without overwriting existing blobs.
    """

    selected_roles = frozenset(str(role) for role in roles)
    if not selected_roles or not selected_roles.issubset(SUPPORTED_ROLES):
        raise ValueError(f"roles must be a non-empty subset of {sorted(SUPPORTED_ROLES)}")
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_bytes < MIN_IMAGE_BYTES:
        raise ValueError(f"max_bytes must be at least {MIN_IMAGE_BYTES}")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    effective_workers = min(int(workers), MAX_PER_HOST_WORKERS)

    run_started = _utc_now()
    loaded_products, official_digest, official_source_path = _load_products(paths, products)
    references = _image_references(loaded_products, selected_roles, strict_current_r3=strict_current_r3)
    reference_by_id = {reference["reference_id"]: reference for reference in references}

    cas_root = paths.project_root / "artifacts" / "original" / "website_images" / "sha256"
    staging_root = cas_root / ".staging"
    checkpoint_path = paths.work_root / "website_image_materialization_checkpoint.json"
    main_manifest = paths.work_root / "website_main_images.jsonl"
    gallery_manifest = paths.work_root / "website_gallery_images.jsonl"
    receipt_path = paths.evidence_root / "website_image_materialization_receipt.json"
    lock_path = paths.work_root / ".website_image_materialization.lock"
    cas_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    assert_no_reparse_components(cas_root, "website_image_cas")

    with exclusive_file_lock(lock_path):
        resumed_by_reference = _load_checkpoint(
            checkpoint_path,
            official_digest,
            cas_root,
            max_bytes=max_bytes,
        )
        resumed_by_url: dict[str, Acquisition] = {}
        for reference_id, acquisition in resumed_by_reference.items():
            reference = reference_by_id.get(reference_id)
            if reference:
                resumed_by_url.setdefault(reference["source_url"], acquisition)

        cache_index, per_cache_stats = _build_cache_index(paths)
        required_keys = {reference["normalized_basename"] for reference in references if reference["normalized_basename"]}
        cache_resolutions, cache_resolution_stats = _resolve_cache_keys(
            cache_index,
            required_keys,
            max_bytes=max_bytes,
        )

        urls = sorted({reference["source_url"] for reference in references})
        acquisitions_by_url: dict[str, Acquisition] = dict(resumed_by_url)
        pending_urls = [url for url in urls if url not in acquisitions_by_url]
        cas_lock = threading.Lock()
        completed_records: dict[str, dict[str, Any]] = {}
        for reference in references:
            acquisition = acquisitions_by_url.get(reference["source_url"])
            if acquisition:
                completed_records[reference["reference_id"]] = _record_from_acquisition(
                    reference, acquisition, run_started
                )

        with ThreadPoolExecutor(max_workers=effective_workers, thread_name_prefix="website-image") as executor:
            future_to_url = {
                executor.submit(
                    _acquire_url,
                    url,
                    cache_resolutions.get(_normalized_basename(_source_basename(url))),
                    cas_root,
                    staging_root,
                    allow_network=allow_network,
                    timeout_seconds=timeout_seconds,
                    max_bytes=max_bytes,
                    attempts=attempts,
                    cas_lock=cas_lock,
                ): url
                for url in pending_urls
            }
            completions_since_checkpoint = 0
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                acquisition = future.result()
                acquisitions_by_url[url] = acquisition
                for reference in references:
                    if reference["source_url"] != url:
                        continue
                    completed_records[reference["reference_id"]] = _record_from_acquisition(
                        reference, acquisition, run_started
                    )
                completions_since_checkpoint += 1
                if completions_since_checkpoint >= effective_workers:
                    _write_checkpoint(
                        checkpoint_path,
                        official_digest,
                        completed_records.values(),
                        _utc_now(),
                    )
                    completions_since_checkpoint = 0

        records = [
            completed_records[reference["reference_id"]]
            for reference in references
            if reference["reference_id"] in completed_records
        ]
        if len(records) != len(references):
            missing = sorted(set(reference_by_id) - set(completed_records))
            raise RuntimeError(f"Internal image-materialization result loss: {len(missing)} references missing")
        _write_checkpoint(checkpoint_path, official_digest, records, _utc_now())

        output_paths: dict[str, Path] = {}
        if "main_image" in selected_roles:
            main_records = [record for record in records if record["role"] == "main_image"]
            _write_jsonl(main_manifest, main_records)
            output_paths["website_main_images"] = main_manifest
        if "gallery" in selected_roles:
            gallery_records = [record for record in records if record["role"] == "gallery"]
            _write_jsonl(gallery_manifest, gallery_records)
            output_paths["website_gallery_images"] = gallery_manifest

        materialized_count = sum(record["status"] == "MATERIALIZED" for record in records)
        failure_counts = Counter(
            str(record.get("failure_code") or "UNKNOWN_FAILURE")
            for record in records
            if record["status"] != "MATERIALIZED"
        )
        role_counts: dict[str, dict[str, int]] = {}
        for role in sorted(selected_roles):
            role_records = [record for record in records if record["role"] == role]
            role_counts[role] = {
                "references": len(role_records),
                "materialized": sum(record["status"] == "MATERIALIZED" for record in role_records),
                "failed": sum(record["status"] != "MATERIALIZED" for record in role_records),
                "unique_content_sha256": len(
                    {record["sha256"] for record in role_records if record["status"] == "MATERIALIZED"}
                ),
            }
        method_counts = Counter(record["acquisition_method"] for record in records)
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": (
                "PASS_ALL_REQUESTED_IMAGES_MATERIALIZED"
                if materialized_count == len(records)
                else "PARTIAL_REQUIRES_RETRY"
            ),
            "started_at_utc": run_started,
            "completed_at_utc": _utc_now(),
            "official_current": {
                "path": official_source_path,
                "sha256": official_digest,
                "product_count": len(loaded_products),
                "release_ids": sorted({str((product.get("source") or {}).get("release_id") or "") for product in loaded_products}),
            },
            "configuration": {
                "roles": sorted(selected_roles),
                "requested_workers": int(workers),
                "effective_workers": effective_workers,
                "per_host_worker_limit": MAX_PER_HOST_WORKERS,
                "allow_network": bool(allow_network),
                "timeout_seconds": timeout_seconds,
                "max_bytes": max_bytes,
                "attempts": attempts,
                "strict_current_r3": strict_current_r3,
            },
            "counts": {
                "references": len(records),
                "unique_source_urls": len(urls),
                "materialized": materialized_count,
                "failed": len(records) - materialized_count,
                "resumed_references": sum(bool(record.get("resumed_from_checkpoint")) for record in records),
                "by_role": role_counts,
                "by_acquisition_method": dict(sorted(method_counts.items())),
                "by_failure_code": dict(sorted(failure_counts.items())),
            },
            "cache_scan": {
                "per_cache": per_cache_stats,
                "indexed_basename_keys": len(cache_index),
                **cache_resolution_stats,
            },
            "content_addressed_store": {
                "root": str(cas_root),
                "unique_materialized_sha256": len(
                    {record["sha256"] for record in records if record["status"] == "MATERIALIZED"}
                ),
                "definition": "artifacts/original/website_images/sha256/<first-two-hex>/<sha256>.<decoded-format-extension>",
                "source_cache_files_modified": 0,
                "remote_write_count": 0,
            },
            "outputs": {
                label: {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                for label, path in sorted(output_paths.items())
            },
            "checkpoint": {
                "path": str(checkpoint_path),
                "sha256": sha256_file(checkpoint_path),
            },
        }
        write_json_atomic(receipt_path, receipt)
        receipt["receipt_path"] = str(receipt_path)
        receipt["receipt_sha256"] = sha256_file(receipt_path)
        return receipt


def build_website_main_image_view(paths: Paths) -> dict[str, Any]:
    """Create a human-browsable verified per-model view without duplicating CAS content.

    The authoritative evidence remains ``website_main_images.jsonl`` plus the
    SHA-256 CAS.  This derived view gives every website model its own filename;
    NTFS hard links are preferred and a byte-for-byte copy is used only when a
    hard link cannot be created.
    """

    manifest_path = paths.website_main_images_path
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Website main-image manifest is missing: {manifest_path}")
    records = load_jsonl(manifest_path)
    if len(records) != EXPECTED["website_main_image_references"]:
        raise ValueError(f"Unexpected website main-image record count, observed {len(records)}")

    view_root = paths.output_root / "website_images" / "main_by_model"
    view_root.mkdir(parents=True, exist_ok=True)
    index_path = view_root / "index.jsonl"
    receipt_path = paths.evidence_root / "website_main_image_view_receipt.json"
    expected_names: set[str] = {"index.jsonl"}
    view_rows: list[dict[str, Any]] = []
    methods: Counter[str] = Counter()
    seen_models: set[str] = set()

    for row in sorted(records, key=lambda item: (str(item.get("canonical_model") or ""), str(item.get("product_id") or ""))):
        model = str(row.get("canonical_model") or "")
        declared_sha256 = str(row.get("sha256") or "").lower()
        if not model or model in seen_models:
            raise ValueError(f"Main-image view requires one unique canonical model; got {model!r}")
        seen_models.add(model)
        if row.get("status") != "MATERIALIZED" or not re.fullmatch(r"[0-9a-f]{64}", declared_sha256):
            raise ValueError(f"Main image for {model} is not materialized with a valid SHA-256")
        source_path = ensure_within(Path(str(row.get("local_file") or "")), paths.website_image_blob_root)
        if not source_path.is_file() or sha256_file(source_path) != declared_sha256:
            raise ValueError(f"Main-image CAS verification failed for {model}: {source_path}")
        suffix = source_path.suffix.lower() or ".img"
        filename = f"{model}{suffix}"
        expected_names.add(filename)
        target_path = view_root / filename
        if target_path.is_file() and sha256_file(target_path) == declared_sha256:
            method = "REUSED_VERIFIED_VIEW_FILE"
        else:
            fd, temporary_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".partial", dir=view_root)
            os.close(fd)
            temporary_path = Path(temporary_name)
            temporary_path.unlink()
            try:
                try:
                    os.link(source_path, temporary_path)
                    method = "NTFS_HARDLINK"
                except OSError:
                    shutil.copy2(source_path, temporary_path)
                    method = "VERIFIED_COPY"
                if sha256_file(temporary_path) != declared_sha256:
                    raise ValueError(f"Derived main-image view hash mismatch for {model}")
                os.replace(temporary_path, target_path)
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
        methods[method] += 1
        view_rows.append(
            {
                "schema_version": "sellerkit.website_main_image_view.v1",
                "canonical_model": model,
                "raw_model": str(row.get("raw_model") or ""),
                "product_id": row.get("product_id"),
                "product_name": str(row.get("product_name") or ""),
                "source_page_url": str(row.get("source_page_url") or ""),
                "source_url": str(row.get("source_url") or ""),
                "cas_file": str(source_path),
                "view_file": str(target_path.resolve()),
                "sha256": declared_sha256,
                "size_bytes": target_path.stat().st_size,
                "width": row.get("width"),
                "height": row.get("height"),
                "view_method": method,
            }
        )

    unexpected = sorted(path.name for path in view_root.iterdir() if path.is_file() and path.name not in expected_names)
    if unexpected:
        raise ValueError(f"Unexpected stale files in main-image view: {unexpected[:10]}")
    _write_jsonl(index_path, view_rows)
    receipt = {
        "schema_version": "sellerkit.website_main_image_view_receipt.v1",
        "status": "PASS_MODEL_MAIN_IMAGE_VIEW",
        "generated_at_utc": _utc_now(),
        "source_manifest": {
            "path": str(manifest_path.resolve()),
            "rows": len(records),
            "sha256": sha256_file(manifest_path),
        },
        "view": {
            "root": str(view_root.resolve()),
            "model_file_count": len(view_rows),
            "unique_content_sha256": len({row["sha256"] for row in view_rows}),
            "methods": dict(sorted(methods.items())),
            "index_path": str(index_path.resolve()),
            "index_sha256": sha256_file(index_path),
        },
        "external_write_enabled": False,
    }
    write_json_atomic(receipt_path, receipt)
    receipt["receipt_path"] = str(receipt_path.resolve())
    receipt["receipt_sha256"] = sha256_file(receipt_path)
    return receipt


__all__ = [
    "DEFAULT_MAX_BYTES",
    "MAX_PER_HOST_WORKERS",
    "SCHEMA_VERSION",
    "build_website_main_image_view",
    "materialize_website_images",
]
