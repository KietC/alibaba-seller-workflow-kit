"""Check a source-only public copy, without printing matched sensitive values.
检查纯源码公开副本，不输出命中的敏感值。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import stat
from pathlib import Path

import source_bundle

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}
FORBIDDEN_DIRS = source_bundle.FORBIDDEN_DIRS | {"wheelhouse", "private-workspace"}
REQUIRED = ("README.md", "LICENSE", "requirements.txt", "docs/GETTING_STARTED.md",
            "docs/PLATFORMS_AND_WORKFLOWS.md", "docs/TROUBLESHOOTING.md")


def check(root: Path) -> dict:
    root = root.resolve(strict=True)
    findings, records = [], []
    for required in REQUIRED:
        if not (root / required).is_file():
            findings.append({"path": required, "rule": "required_file_missing"})
    stack, names = [root], set()
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(root).as_posix()
            mode = path.lstat()
            # Reject links before exclusions: a link must never bypass inspection.
            # 先拒绝链接再排除目录，防止链接绕过检查。
            if stat.S_ISLNK(mode.st_mode) or getattr(mode, "st_file_attributes", 0) & 0x400:
                findings.append({"path": rel, "rule": "link_or_reparse_point"})
                continue
            if path.is_dir():
                if path.name in SKIP_DIRS:
                    continue
                if path.name.lower() in FORBIDDEN_DIRS:
                    findings.append({"path": rel, "rule": "private_runtime_directory"})
                else:
                    stack.append(path)
                continue
            if rel.casefold() in names:
                findings.append({"path": rel, "rule": "case_collision"})
            names.add(rel.casefold())
            try:
                if not source_bundle.allowed_file(Path(rel)):
                    findings.append({"path": rel, "rule": "unapproved_file_type"})
                    continue
            except ValueError:
                findings.append({"path": rel, "rule": "private_runtime_file"})
                continue
            raw = path.read_bytes()
            records.append({"path": rel, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
            try:
                body = raw.decode("utf-8-sig")
                if "\x00" in body:
                    findings.append({"path": rel, "rule": "nul_byte"})
                errors, emails = source_bundle.scan_text(path, rel)
                findings.extend(errors + emails)
                if path.suffix == ".py":
                    ast.parse(body, filename=rel)
                if path.suffix == ".json":
                    json.loads(body)
                if path.suffix == ".md":
                    fences = re.findall(r"(?m)^\s*(```|~~~)", body)
                    if len(fences) % 2:
                        findings.append({"path": rel, "rule": "unbalanced_markdown_fence"})
            except (UnicodeError, SyntaxError, ValueError):
                findings.append({"path": rel, "rule": "text_or_syntax_error"})
    for rel in ("README.md", "docs/GETTING_STARTED.md", "docs/PLATFORMS_AND_WORKFLOWS.md", "docs/TROUBLESHOOTING.md"):
        path = root / rel
        if path.is_file():
            body = path.read_text(encoding="utf-8-sig")
            if "## English" not in body or "## 中文" not in body or body.index("## English") > body.index("## 中文"):
                findings.append({"path": rel, "rule": "english_then_chinese_sections_required"})
    # Generic pattern checks are not proof of universal privacy; use manual review too.
    # 通用规则不等于绝对隐私保证，仍需人工复核。
    return {"status": "PASS_PUBLIC_SOURCE_CHECK" if not findings else "BLOCKED_PUBLIC_SOURCE_CHECK",
            "files_checked": len(records), "bytes_checked": sum(r["bytes"] for r in records),
            "findings": findings, "files": sorted(records, key=lambda r: r["path"]),
            "business_actions": 0, "final_write_enabled": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    result = check(args.root)
    if args.receipt:
        output = args.receipt.resolve()
        try:
            output.relative_to(args.root.resolve())
        except ValueError:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            parser.error("receipts must be outside the public source tree")
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, ensure_ascii=False, indent=2))
    return 0 if not result["findings"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
