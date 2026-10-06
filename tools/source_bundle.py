# Export only an explicit source allowlist; never include login state or business records.
# 仅导出明确允许的源码；禁止导出登录状态和业务记录。
"""Local source extraction/review bundle; no GitHub/network operations.

Uses the existing Skill snapshot helper for filesystem validation and hashing.
The allowlist plan and machine provenance remain OUTSIDE the exported repo.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

EXCLUDED_DIRS = {".git", ".runtime", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv"}
ALLOWED_EXT = {".py", ".mjs", ".js", ".ps1", ".md", ".json", ".html", ".css", ".svg", ".yaml", ".yml"}
ALLOWED_TEXT_NAMES = {"requirements.lock.txt", "requirements-test.txt", "requirements.txt", "SHA256SUMS.txt"}
FORBIDDEN_EXT = {".har", ".mhar", ".mhtml", ".mht", ".sqlite", ".db", ".jsonl", ".log", ".csv", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".pem", ".key", ".safetensors", ".gguf", ".whl"}
FORBIDDEN_NAMES = {".env", "cookies.json", "cookie.json", "storage-state.json", "credentials.json", "secrets.json", "approved_source_manifest.json", "approved_asset_manifest.json", "approved_commercial_decision.json", "checkpoint.json", "run_spec.json", "verification_receipt.json"}
FORBIDDEN_DIRS = {"cases", "training_runs", "outputs", "output", "work", "artifacts", "evidence", "screenshots", "browser_profile", "browser-profile", "sessions"}
CONTROL_NAMES = {"SOURCE_MANIFEST.json", "SHA256SUMS.txt"}
SECRET_PATTERNS = {
    "github_token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{45,})"),
    "provider_key": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "literal_bearer": re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{24,}"),
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def load_helper(path: Path):
    spec = importlib.util.spec_from_file_location("existing_snapshot_helper", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load existing snapshot helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError("unsafe relative path")
    return path


def allowed_file(relative: Path) -> bool:
    if relative.name.lower() in FORBIDDEN_NAMES or relative.suffix.lower() in FORBIDDEN_EXT:
        raise ValueError(f"forbidden payload filename: {relative.as_posix()}")
    if any(part.lower() in FORBIDDEN_DIRS for part in relative.parts[:-1]):
        raise ValueError(f"forbidden payload directory: {relative.as_posix()}")
    return relative.suffix.lower() in ALLOWED_EXT or relative.name in ALLOWED_TEXT_NAMES or relative.name in {".gitignore", ".gitattributes", "LICENSE"}


def walk_source(root: Path, helper):
    stack = [root]
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if path.name in EXCLUDED_DIRS:
                continue
            resolved, mode = helper._inspect_source_entry(path, root)
            if stat.S_ISDIR(mode):
                stack.append(resolved)
            elif stat.S_ISREG(mode):
                yield resolved
            else:
                raise ValueError("non-regular source entry")


def stage(plan_path: Path, repo: Path, audit: Path):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    helper = load_helper(Path(plan["snapshot_helper"]))
    repo = repo.resolve(strict=True)
    audit = audit.resolve(strict=True)
    if contained(audit, repo) or contained(plan_path.resolve(), repo):
        raise ValueError("machine plan/provenance must remain outside repo")
    index = []
    destinations = set()
    for component in plan["components"]:
        root = Path(component["source"]).resolve(strict=True)
        if helper._stat_is_reparse_point(Path(component["source"]).lstat()):
            raise ValueError("source root is a link/reparse point")
        prefix = validate_relative(component["destination"])
        selected = set()
        for name in component.get("files", []):
            rel = validate_relative(name)
            path = root.joinpath(*rel.parts)
            resolved, mode = helper._inspect_source_entry(path, root)
            if not stat.S_ISREG(mode):
                raise ValueError("explicit file is not regular")
            selected.add(resolved)
        for name in component.get("trees", []):
            rel = validate_relative(name)
            path = root.joinpath(*rel.parts)
            resolved, mode = helper._inspect_source_entry(path, root)
            if not stat.S_ISDIR(mode):
                raise ValueError("explicit tree is not a directory")
            selected.update(walk_source(resolved, helper))
        extensions = set(component.get("extensions", []))
        for path in sorted(selected):
            rel = path.relative_to(root)
            if extensions and path.suffix.lower() not in extensions and path.name not in ALLOWED_TEXT_NAMES:
                continue
            if not allowed_file(rel):
                raise ValueError(f"unapproved source type: {rel.as_posix()}")
            target_rel = (prefix / PurePosixPath(rel.as_posix())).as_posix()
            folded = target_rel.casefold()
            if folded in destinations:
                raise ValueError("duplicate/case-colliding destination")
            destinations.add(folded)
            target = repo.joinpath(*PurePosixPath(target_rel).parts)
            if not contained(target.resolve(), repo):
                raise ValueError("destination escapes repo")
            if target.exists():
                raise FileExistsError(f"stage never overwrites: {target_rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = helper.sha256_file(path)
            shutil.copyfile(path, target)
            if helper.sha256_file(path) != digest or helper.sha256_file(target) != digest:
                raise ValueError("source drift or copy mismatch")
            index.append({"component": component["name"], "source": str(path), "path": target_rel, "bytes": target.stat().st_size, "sha256": digest})
    output = audit / "source-provenance.local.json"
    with output.open("x", encoding="utf-8") as stream:
        json.dump({"status": "SOURCE_COPY_ONLY", "network_actions": 0, "files": index}, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": "STAGED", "copied_files": len(index), "bytes": sum(row["bytes"] for row in index)}, ensure_ascii=False))


def scan_text(path: Path, relative: str):
    if path.suffix.lower() == ".whl":
        return [], []
    body = path.read_text(encoding="utf-8-sig")
    errors = []
    review_path = Path(__file__).resolve().parents[1] / "tools/reviewed-synthetic-markers.json"
    reviews = json.loads(review_path.read_text(encoding="utf-8")).get("entries", []) if review_path.exists() else []
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    for rule, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(body):
            line = body.count("\n", 0, match.start()) + 1
            if any(review.get("path") == relative and review.get("rule") == rule
                   and review.get("line") == line and review.get("file_sha256") == file_hash
                   and "/tests/" in relative and review.get("reason") for review in reviews):
                continue
            errors.append({"path": relative, "rule": rule, "line": line})
    email_review = []
    for match in EMAIL_RE.finditer(body):
        domain = match.group(1).lower()
        if domain in {"example.com", "example.org", "example.net", "test.com", "test.org"} or domain.endswith((".invalid", ".example", ".test")):
            continue
        email_review.append({"path": relative, "rule": "email_requires_review", "line": body.count("\n", 0, match.start()) + 1})
    return errors, email_review


def payload_records(repo: Path, helper):
    records, errors, email_review = [], [], []
    for path in walk_source(repo, helper):
        rel = path.relative_to(repo).as_posix()
        if rel in CONTROL_NAMES:
            continue
        if not allowed_file(Path(rel)):
            raise ValueError(f"unapproved repo payload: {rel}")
        findings, emails = scan_text(path, rel)
        errors.extend(findings)
        email_review.extend(emails)
        records.append({"path": rel, "bytes": path.stat().st_size, "sha256": helper.sha256_file(path)})
    return sorted(records, key=lambda row: row["path"]), errors, email_review


def helper_from_repo(repo: Path):
    return load_helper(repo / ".agents/skills/alibaba-similar-draft/scripts/build_local_contract_snapshot.py")


def finalize(repo: Path, output_zip: Path, receipt: Path):
    repo = repo.resolve(strict=True)
    helper = helper_from_repo(repo)
    if contained(output_zip.resolve(), repo) or contained(receipt.resolve(), repo):
        raise ValueError("ZIP and local receipt must be outside repo")
    for path in (repo / "SOURCE_MANIFEST.json", repo / "SHA256SUMS.txt", output_zip, receipt):
        if path.exists():
            raise FileExistsError("refusing to replace existing finalized artifacts")
    records, errors, email_review = payload_records(repo, helper)
    if errors or email_review:
        print(json.dumps({"status": "BLOCKED_SECRET_REVIEW", "findings": errors, "email_review": email_review}, ensure_ascii=False))
        raise ValueError("payload needs sensitive-pattern review; matched values were not printed")
    manifest = {"schema_version": "sellerkit.alibaba.source-review-bundle.v1", "status": "PUBLIC_SOURCE_SNAPSHOT", "alibaba_actions": 0, "final_write_enabled": False, "live_execution_status": "BLOCKED_LIVE_ATTESTATION", "known_issues": "docs/TROUBLESHOOTING.md", "synthetic_marker_review": "tools/reviewed-synthetic-markers.json", "files": records, "file_count": len(records), "bytes": sum(row["bytes"] for row in records)}
    manifest_path = repo / "SOURCE_MANIFEST.json"
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    manifest_hash = helper.sha256_file(manifest_path)
    sums = {row["path"]: row["sha256"] for row in records}
    sums["SOURCE_MANIFEST.json"] = manifest_hash
    with (repo / "SHA256SUMS.txt").open("x", encoding="utf-8", newline="\n") as stream:
        stream.writelines(f"{digest}  {rel}\n" for rel, digest in sorted(sums.items()))
    with zipfile.ZipFile(output_zip, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel in sorted([row["path"] for row in records] + sorted(CONTROL_NAMES)):
            info = zipfile.ZipInfo(rel, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, repo.joinpath(*PurePosixPath(rel).parts).read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    result = verify(repo, manifest_path, output_zip)
    result.update(zip_path=str(output_zip), zip_bytes=output_zip.stat().st_size, zip_sha256=helper.sha256_file(output_zip), source_manifest_sha256=manifest_hash)
    with receipt.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))


def verify(repo: Path, manifest_path: Path, archive_path: Path | None = None):
    repo = repo.resolve(strict=True)
    helper = helper_from_repo(repo)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual, errors, email_review = payload_records(repo, helper)
    if actual != manifest["files"] or errors or email_review:
        raise ValueError("payload drift, extra files, or sensitive-pattern findings")
    expected_sums = {row["path"]: row["sha256"] for row in actual}
    expected_sums["SOURCE_MANIFEST.json"] = helper.sha256_file(manifest_path)
    expected_text = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(expected_sums.items()))
    if (repo / "SHA256SUMS.txt").read_text(encoding="utf-8") != expected_text:
        raise ValueError("checksum file drift")
    if archive_path:
        with zipfile.ZipFile(archive_path) as archive:
            expected = sorted([row["path"] for row in actual] + sorted(CONTROL_NAMES))
            if sorted(archive.namelist()) != expected or archive.testzip() is not None:
                raise ValueError("ZIP entries or CRC mismatch")
            for rel in expected:
                if helper.sha256_bytes(archive.read(rel)) != helper.sha256_file(repo.joinpath(*PurePosixPath(rel).parts)):
                    raise ValueError("ZIP content hash mismatch")
    return {"status": "PASS_SOURCE_BUNDLE_INTEGRITY", "uploaded": False, "file_count": len(actual) + 2, "payload_bytes": sum(row["bytes"] for row in actual), "secret_pattern_findings": len(errors), "unreviewed_email_findings": len(email_review), "live_execution_status": "BLOCKED_LIVE_ATTESTATION"}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--plan", type=Path, required=True)
    stage_parser.add_argument("--repo-root", type=Path, required=True)
    stage_parser.add_argument("--audit-dir", type=Path, required=True)
    final = sub.add_parser("finalize")
    final.add_argument("--repo-root", type=Path, required=True)
    final.add_argument("--zip", type=Path, required=True)
    final.add_argument("--receipt", type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--repo-root", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--zip", type=Path)
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.plan, args.repo_root, args.audit_dir)
    elif args.command == "finalize":
        finalize(args.repo_root, args.zip, args.receipt)
    else:
        print(json.dumps(verify(args.repo_root, args.manifest, args.zip), ensure_ascii=False))


if __name__ == "__main__":
    main()
