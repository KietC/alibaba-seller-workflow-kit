from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


EXCLUDED_DIRECTORY_NAMES = {"__pycache__", ".git", ".pytest_cache", ".runtime", "out", "out2"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".har", ".mhtml", ".crdownload"}
FORBIDDEN_FILE_TOKENS = {"cookie", "storage-state", "browser-profile", "authorization-token", "session-token"}
ALLOWED_TOP_LEVEL_DIRECTORIES = {"agents", "profiles", "references", "scripts", "tests", "wheelhouse"}
ALLOWED_ROOT_FILES = {"SKILL.md", "requirements.lock.txt", "run_local_acceptance.ps1", "verify.ps1"}
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _stat_is_reparse_point(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0) or 0)
        & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _inspect_source_entry(path: Path, root: Path) -> tuple[Path, int]:
    """Resolve one source entry without following links or Windows reparse points."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"cannot inspect package source entry: {path}") from exc
    is_junction = getattr(path, "is_junction", None)
    try:
        junction_detected = bool(is_junction()) if callable(is_junction) else False
    except OSError as exc:
        raise ValueError(f"cannot inspect package source entry junction state: {path}") from exc
    if _stat_is_reparse_point(metadata) or junction_detected:
        raise ValueError(
            f"package source entry is a symlink, junction, or reparse point: {path}"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"cannot resolve package source entry: {path}") from exc
    if not is_within(resolved, root):
        raise ValueError(f"package source entry resolves outside the Skill root: {path}")
    return resolved, metadata.st_mode


def collect_files(skill_root: Path) -> list[Path]:
    skill_root = Path(skill_root)
    try:
        root_metadata = skill_root.lstat()
    except OSError as exc:
        raise ValueError(f"cannot inspect Skill root: {skill_root}") from exc
    root_is_junction = getattr(skill_root, "is_junction", None)
    try:
        root_junction_detected = (
            bool(root_is_junction()) if callable(root_is_junction) else False
        )
    except OSError as exc:
        raise ValueError(f"cannot inspect Skill root junction state: {skill_root}") from exc
    if _stat_is_reparse_point(root_metadata) or root_junction_detected:
        raise ValueError("Skill root must not be a symlink, junction, or reparse point")
    root = skill_root.resolve(strict=True)
    if not (root / "SKILL.md").is_file():
        raise ValueError("skill root does not contain SKILL.md")
    files: list[Path] = []
    directories = [root]
    while directories:
        directory = directories.pop()
        for path in directory.iterdir():
            resolved, mode = _inspect_source_entry(path, root)
            relative = resolved.relative_to(root)
            at_top_level = len(relative.parts) == 1
            lowered = relative.as_posix().lower()
            if at_top_level and any(token in lowered for token in FORBIDDEN_FILE_TOKENS):
                raise ValueError(f"forbidden secret-bearing filename in package: {relative}")
            if stat.S_ISDIR(mode):
                if path.name in EXCLUDED_DIRECTORY_NAMES:
                    continue
                if at_top_level and path.name not in ALLOWED_TOP_LEVEL_DIRECTORIES:
                    raise ValueError(
                        f"unapproved top-level directory in package source: {path.name}"
                    )
                directories.append(resolved)
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f"unsupported filesystem entry in package source: {relative}")
            if at_top_level and path.name not in ALLOWED_ROOT_FILES:
                raise ValueError(f"unapproved root file in package source: {path.name}")
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            if any(token in lowered for token in FORBIDDEN_FILE_TOKENS):
                raise ValueError(f"forbidden secret-bearing filename in package: {relative}")
            files.append(resolved)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())


def build_snapshot(*, skill_root: Path, output_zip: Path, receipt_path: Path) -> dict[str, Any]:
    source_root = Path(skill_root)
    root = source_root.resolve(strict=True)
    output = output_zip.resolve()
    receipt = receipt_path.resolve()
    if is_within(output, root) or is_within(receipt, root):
        raise ValueError("ZIP and packaging receipt must be outside the Skill root")
    if output == receipt:
        raise ValueError("ZIP and packaging receipt must be different paths")
    if output.exists() or receipt.exists():
        raise FileExistsError("refusing to overwrite an existing ZIP or receipt")
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    files = collect_files(source_root)
    entries = []
    for path in files:
        payload = path.read_bytes()
        entries.append({
            "path": PurePosixPath("alibaba-similar-draft", *path.relative_to(root).parts).as_posix(),
            "bytes": len(payload), "sha256": sha256_bytes(payload),
        })
    tree_sha256 = sha256_bytes(canonical_json(entries))
    package_manifest = {
        "schema_version": "sellerkit.alibaba.local-contract-snapshot-manifest.v1",
        "trust_status": "PACKAGE_INTEGRITY_ONLY",
        "quality_acceptance_status": "NOT_ASSERTED_BY_PACKAGER",
        "live_execution_status": "BLOCKED_LIVE_ATTESTATION",
        "implementation_ready": False, "final_write_enabled": False,
        "entry_count": len(entries), "tree_sha256": tree_sha256, "entries": entries,
    }
    manifest_payload = json.dumps(package_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path, entry in zip(files, entries, strict=True):
                info = zipfile.ZipInfo(entry["path"], FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED; info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            info = zipfile.ZipInfo("package_manifest.json", FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED; info.external_attr = 0o100644 << 16
            archive.writestr(info, manifest_payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        with zipfile.ZipFile(temporary, "r") as archive:
            names = archive.namelist()
            expected_names = [row["path"] for row in entries] + ["package_manifest.json"]
            if names != expected_names or len(names) != len(set(names)):
                raise ValueError("ZIP entry list is incomplete, duplicated, or non-canonical")
            for entry in entries:
                if sha256_bytes(archive.read(entry["path"])) != entry["sha256"]:
                    raise ValueError(f"ZIP entry hash mismatch: {entry['path']}")
            if archive.read("package_manifest.json") != manifest_payload:
                raise ValueError("embedded package manifest mismatch")
        os.replace(temporary, output)
    except Exception:
        try: temporary.unlink()
        except OSError: pass
        raise
    result = {
        "schema_version": "sellerkit.alibaba.local-contract-snapshot-receipt.v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "PASS_PACKAGE_INTEGRITY",
        "trust_status": "PACKAGE_INTEGRITY_ONLY",
        "quality_acceptance_status": "NOT_ASSERTED_BY_PACKAGER",
        "live_execution_status": "BLOCKED_LIVE_ATTESTATION",
        "zip_path": str(output), "zip_bytes": output.stat().st_size, "zip_sha256": sha256_file(output),
        "source_skill_root": str(root), "source_file_count": len(entries), "source_tree_sha256": tree_sha256,
        "embedded_manifest_sha256": sha256_bytes(manifest_payload),
        "browser_action_count": 0, "network_request_count": 0, "external_write_count": 0,
        "implementation_ready": False, "final_write_enabled": False,
    }
    temporary_receipt = receipt.with_name(f".{receipt.name}.{os.getpid()}.tmp")
    try:
        temporary_receipt.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_receipt, receipt)
    except Exception:
        try: temporary_receipt.unlink()
        except OSError: pass
        raise
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic Alibaba Skill package-integrity snapshot; this does not assert test or live readiness")
    parser.add_argument("--skill-root", type=Path, required=True)
    parser.add_argument("--output-zip", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_snapshot(skill_root=args.skill_root, output_zip=args.output_zip, receipt_path=args.receipt)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status":"BLOCKED_PACKAGE","error":str(exc)}, ensure_ascii=False))
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
