from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:crossdomain|umid|atmsecurity|xsrf|csrf|ctoken|_tb_token|access[_-]?token|"
    r"refresh[_-]?token|authorization|cookie|session)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|(?:crossdomainToken|umidToken|atmSecurityToken)\s*[\"'=:\s]+\S+|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:api[_-]?key|client[_-]?secret|password|access[_-]?token|refresh[_-]?token|session(?:id)?|cookie)\s*[=:\"']\s*[^\s,;&\"']{8,}|"
    r"[?&](?:token|api[_-]?key|access[_-]?token|refresh[_-]?token|session(?:id)?)=[^&#\s]{6,})",
    re.IGNORECASE,
)
FORBIDDEN_EXACT_KEYS = {
    "seller",
    "buyer",
    "seo",
    "tokens",
    "globaldata",
    "devdata",
    "nodemap",
    "cookie_header",
    "private_key",
    "client_secret",
    "api_key",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest_sha256(root: Path, files: Iterable[Path] | None = None) -> dict[str, Any]:
    root = root.resolve()
    selected = list(files) if files is not None else [path for path in root.rglob("*") if path.is_file()]
    rows: list[tuple[bytes, str]] = []
    total_bytes = 0
    for path in selected:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Tree member escapes root: {resolved}") from exc
        rows.append((relative.encode("utf-8"), f"{sha256_file(resolved)}  {relative}"))
        total_bytes += resolved.stat().st_size
    rows.sort(key=lambda row: row[0])
    manifest_text = "\n".join(row[1] for row in rows)
    return {
        "sha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "definition": "SHA-256 of UTF-8 newline-joined rows sorted by UTF-8 relative POSIX path: <file_sha256><two spaces><path>; no trailing newline",
    }


def assert_no_reparse_components(path: Path, label: str) -> None:
    """Reject an existing symlink/junction/mount-point component without following it."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if not current.exists():
            continue
        attributes = getattr(current.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attributes & reparse_flag:
            raise ValueError(f"{label} contains a reparse point, junction, mount point, or symlink: {current}")


def stable_json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
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


def write_json_atomic(path: Path, value: Any) -> None:
    write_text_atomic(path, stable_json_text(value))


@contextmanager
def exclusive_file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Concurrent or stale lock blocks review-log mutation: {lock_path}") from exc
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def write_csv_atomic(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            records.append(value)
    return records


def normalize_model(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalize_space(value).lower())
        if len(token) > 1
    }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def strip_html(value: Any) -> str:
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    parser.close()
    return normalize_space(html.unescape(" ".join(parser.parts)))


def parse_jsonish(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return fallback


def extract_numbers_and_units(value: Any) -> tuple[list[str], list[str]]:
    text = normalize_space(value)
    numbers = re.findall(r"(?<![A-Za-z])-?\d+(?:\.\d+)?", text)
    unit_pattern = re.compile(
        r"(?i)(?<![A-Za-z])(?:L/h|ml/cup|cups/h|gpm|MPa|kPa|psi|bar|kW|W|V|Hz|mm|cm|ml|L|°C|℃|inch(?:es)?|in\.)(?![A-Za-z])"
    )
    units: list[str] = []
    for match in unit_pattern.findall(text):
        canonical = match.replace("℃", "°C")
        if canonical not in units:
            units.append(canonical)
    return numbers, units


def iter_urls(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_urls(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_urls(child)
    elif isinstance(value, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", html.unescape(value)):
            yield match.rstrip(".,);]")


def url_basename(url: str) -> str:
    return Path(unquote(urlparse(url).path)).name.lower()


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Path escapes allowed root: {resolved}") from exc
    return resolved


def secret_findings(value: Any, location: str = "$") -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}"
            if str(key).casefold() in FORBIDDEN_EXACT_KEYS or SENSITIVE_KEY_PATTERN.search(str(key)):
                findings.append({"location": child_location, "reason": "sensitive_key"})
            findings.extend(secret_findings(child, child_location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(secret_findings(child, f"{location}[{index}]"))
    elif isinstance(value, str) and SENSITIVE_VALUE_PATTERN.search(value):
        findings.append({"location": location, "reason": "sensitive_value_pattern"})
    return findings


def secret_text_findings(text: str, location: str = "$text") -> list[dict[str, str]]:
    return [
        {"location": location, "reason": "sensitive_value_pattern"}
        for _ in SENSITIVE_VALUE_PATTERN.finditer(text)
    ]


def file_record(path: Path, label: str = "") -> dict[str, Any]:
    stat = path.stat()
    return {
        "label": label or path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(path),
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
