#!/usr/bin/env python3
"""Portable, read-only runtime discovery and doctor for this skill.

The module deliberately separates the installed skill location from the
business-data workspace.  A workspace is accepted only from an explicit
argument, a dedicated environment variable, a small JSON configuration, or
markers found while walking upward from the caller's current directory.

The doctor never opens a browser, performs a network request, or writes a
file.  Its JSON output is ASCII-safe even when the Windows console is not in
UTF-8 mode.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import locale
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "sellerkit.alibaba.portability_doctor.v1"
ENV_WORKSPACE = "SELLERKIT_ALIBABA_WORKSPACE"
ENV_CONFIG = "SELLERKIT_ALIBABA_CONFIG"
MINIMUM_PYTHON = (3, 11)
MAXIMUM_PYTHON = (3, 13)

# These paths belong to the distributable skill itself, not to a data
# workspace.  Keeping the list here makes a damaged or partial copy fail
# before any browser work can begin.
REQUIRED_SKILL_FILES: tuple[str, ...] = (
    "SKILL.md",
    "agents/openai.yaml",
    "requirements.lock.txt",
    "verify.ps1",
    "run_local_acceptance.ps1",
    "scripts/resolve_offline_python.ps1",
    "references/workflow.md",
    "references/next-product-sop.v1.md",
    "references/next-product-sop.v2.md",
    "references/next-product-checklist.template.json",
    "references/browser-capture-attestation-requirements.v1.md",
    "references/offline-iab-harness.md",
    "references/endpoint-policy.v1.json",
    "references/publish-field-registry.v1.json",
    "references/schemas/approved-asset-manifest.schema.json",
    "references/schemas/approved-commercial-decision.schema.json",
    "references/schemas/approved-source-manifest.schema.json",
    "references/schemas/approval-compile-receipt.schema.json",
    "references/schemas/approval-decision.schema.json",
    "references/schemas/browser-action-plan.schema.json",
    "references/schemas/browser-capture-material.schema.json",
    "references/schemas/browser-observation-capture.schema.json",
    "references/schemas/checkpoint.schema.json",
    "references/schemas/checkpoint-transition-evidence.schema.json",
    "references/schemas/draft-save-attempt-claim-receipt.schema.json",
    "references/schemas/draft-save-activation-receipt.schema.json",
    "references/schemas/draft-save-activation-request.schema.json",
    "references/schemas/draft-save-body-semantics.schema.json",
    "references/schemas/draft-save-result-evidence.schema.json",
    "references/schemas/draft-write-approval.schema.json",
    "references/schemas/dynamic-publish-schema.schema.json",
    "references/schemas/field-registry.schema.json",
    "references/schemas/fill-execution.schema.json",
    "references/schemas/fill-verification-receipt.schema.json",
    "references/schemas/inventory-delta-verification-receipt.schema.json",
    "references/schemas/localized-field-target.schema.json",
    "references/schemas/localized-counter-expectation.schema.json",
    "references/schemas/localized-counter-verification-receipt.schema.json",
    "references/schemas/next-product-checklist.schema.json",
    "references/schemas/next-product-checklist-verification-receipt.schema.json",
    "references/schemas/official-schema-current-values.schema.json",
    "references/schemas/official-schema-values-verification-receipt.schema.json",
    "references/schemas/official-schema-xml-normalized.schema.json",
    "references/schemas/post-refresh-verification.schema.json",
    "references/schemas/pre-save-readback.schema.json",
    "references/schemas/pre-save-verification-receipt.schema.json",
    "references/schemas/probe-receipt.schema.json",
    "references/schemas/probe-read-activation-receipt.schema.json",
    "references/schemas/probe-read-activation-request.schema.json",
    "references/schemas/probe-verification-receipt.schema.json",
    "references/schemas/product-inventory-snapshot.schema.json",
    "references/schemas/run-receipt.schema.json",
    "references/schemas/run-spec.schema.json",
    "references/schemas/similar-draft.schema.json",
    "references/schemas/workspace-profile.schema.json",
    "references/schemas/visible-ui-draft-save-event.schema.json",
    "references/adapters/iab-v1.json",
    "references/adapters/offline-iab-harness-v1.json",
    "profiles/example-profile.v1.json",
    "scripts/compile_approval.py",
    "scripts/compile_browser_action_plan.py",
    "scripts/build_candidate_workbook.mjs",
    "scripts/candidate_workbook_cli.mjs",
    "scripts/candidate_workbook_blockers.mjs",
    "scripts/capture_derivation.py",
    "scripts/build_local_contract_snapshot.py",
    "scripts/draft_core.py",
    "scripts/draft_guard.py",
    "scripts/dynamic_schema_gate.py",
    "scripts/localized_field_contract.py",
    "scripts/validate_localized_counter_capture.py",
    "scripts/normalize_dynamic_publish_schema.py",
    "scripts/parse_official_schema_xml.py",
    "scripts/state_store.py",
    "scripts/runtime_config.py",
    "scripts/validate_draft_save_activation.py",
    "scripts/validate_next_product_checklist.py",
    "scripts/validate_official_schema_values.py",
    "scripts/validate_post_save_inventory.py",
    "scripts/validate_pre_save_readback.py",
    "scripts/test_candidate_workbook_cli.mjs",
    "scripts/test_candidate_workbook_cli.py",
    "tests/offline-iab-harness/package.json",
    "tests/offline-iab-harness/run.ps1",
    "tests/offline-iab-harness/fixture-server.mjs",
    "tests/offline-iab-harness/publish-replica.html",
    "tests/offline-iab-harness/runtime-adapter.mjs",
    "tests/offline-iab-harness/request-guard.mjs",
    "tests/offline-iab-harness/harness.spec.mjs",
    "tests/offline-iab-harness/fixtures/locales.json",
)

# This public edition distributes source, not a private Windows wheelhouse.
# Dependencies come from root requirements.txt; no live gate is relaxed.
# 公开版只分发源码，不携带私有 Windows wheelhouse。
# 依赖由根目录 requirements.txt 安装；任何在线门禁均不放宽。

JSON_SKILL_FILES: tuple[str, ...] = tuple(
    path for path in REQUIRED_SKILL_FILES if path.endswith(".json")
)

TEXT_SKILL_FILES: tuple[str, ...] = tuple(
    path
    for path in REQUIRED_SKILL_FILES
    if Path(path).suffix.lower() in {".json", ".md", ".mjs", ".ps1", ".py", ".txt", ".yaml", ".yml"}
)

# These are stable corpus markers already consumed by the guarded workflow.
# They intentionally do not include the installed skill path: a plugin cache
# is code, not a source of business data.
WORKSPACE_CORE_MARKERS: tuple[tuple[str, str], ...] = (
    ("sellerkit-alibaba-review-ready-v1/work/official_current.jsonl", "file"),
    ("sellerkit-alibaba-review-ready-v1/work/alibaba_historical_sanitized.jsonl", "file"),
)


class RuntimeConfigError(ValueError):
    """A configuration source was present but could not be used safely."""


@dataclass(frozen=True)
class WorkspaceResolution:
    path: Path | None
    source: str
    configured: bool
    config_kind: str | None = None


def _clean_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalise_path(value: object, *, base: Path) -> Path:
    text = _clean_value(value)
    if text is None:
        raise RuntimeConfigError("workspace path is empty")
    expanded = os.path.expandvars(os.path.expanduser(text))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve(strict=False)


def _load_config(raw: str, *, cwd: Path) -> tuple[dict[str, Any], Path, str]:
    """Load an inline JSON object or a UTF-8 JSON file without echoing input."""

    stripped = raw.strip()
    if not stripped:
        raise RuntimeConfigError(f"{ENV_CONFIG} is empty")

    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise RuntimeConfigError(
                f"{ENV_CONFIG} contains invalid JSON at character {exc.pos}"
            ) from None
        base = cwd
        kind = "inline_json"
    else:
        config_path = _normalise_path(stripped, base=cwd)
        if not config_path.is_file():
            raise RuntimeConfigError(f"{ENV_CONFIG} JSON file does not exist")
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            raise RuntimeConfigError(f"{ENV_CONFIG} JSON file is not UTF-8") from None
        except json.JSONDecodeError as exc:
            raise RuntimeConfigError(
                f"{ENV_CONFIG} file contains invalid JSON at character {exc.pos}"
            ) from None
        base = config_path.parent
        kind = "json_file"

    if not isinstance(payload, dict):
        raise RuntimeConfigError(f"{ENV_CONFIG} must contain a JSON object")
    return payload, base, kind


def _workspace_from_config(
    raw: str, *, cwd: Path
) -> tuple[Path | None, str]:
    payload, base, kind = _load_config(raw, cwd=cwd)
    value = payload.get("workspace")
    if value is None:
        value = payload.get("workspace_root")
    if value is None:
        return None, kind
    if not isinstance(value, str):
        raise RuntimeConfigError("configured workspace must be a string path")
    return _normalise_path(value, base=base), kind


def _marker_exists(root: Path, relative_path: str, kind: str) -> bool:
    candidate = root / relative_path
    if kind == "directory":
        return candidate.is_dir()
    if kind == "file":
        return candidate.is_file()
    raise AssertionError(f"unsupported marker kind: {kind}")


def has_workspace_markers(root: Path) -> bool:
    return all(
        _marker_exists(root, relative_path, kind)
        for relative_path, kind in WORKSPACE_CORE_MARKERS
    )


def discover_workspace_from_cwd(cwd: Path) -> Path | None:
    """Walk caller-owned cwd ancestors only; never inspect the skill path."""

    current = cwd.resolve(strict=False)
    for candidate in (current, *current.parents):
        if has_workspace_markers(candidate):
            return candidate
    return None


def resolve_workspace(
    explicit_workspace: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> WorkspaceResolution:
    """Resolve a workspace using deterministic, documented precedence.

    Precedence: explicit argument, ``SELLERKIT_ALIBABA_WORKSPACE``, workspace
    in ``SELLERKIT_ALIBABA_CONFIG``, then markers in cwd ancestors.
    """

    env = os.environ if environ is None else environ
    caller_cwd = Path.cwd() if cwd is None else Path(cwd)
    caller_cwd = caller_cwd.resolve(strict=False)

    explicit = _clean_value(explicit_workspace)
    if explicit is not None:
        return WorkspaceResolution(
            _normalise_path(explicit, base=caller_cwd),
            "explicit_argument",
            True,
        )

    env_workspace = _clean_value(env.get(ENV_WORKSPACE))
    if env_workspace is not None:
        return WorkspaceResolution(
            _normalise_path(env_workspace, base=caller_cwd),
            "environment",
            True,
        )

    config_raw = _clean_value(env.get(ENV_CONFIG))
    if config_raw is not None:
        configured_workspace, config_kind = _workspace_from_config(
            config_raw, cwd=caller_cwd
        )
        if configured_workspace is not None:
            return WorkspaceResolution(
                configured_workspace,
                "config_json",
                True,
                config_kind,
            )

    discovered = discover_workspace_from_cwd(caller_cwd)
    if discovered is not None:
        return WorkspaceResolution(discovered, "cwd_ancestor_markers", False)
    return WorkspaceResolution(None, "none", False)


def _check_item(
    check_id: str,
    ok: bool,
    *,
    required: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "ok": bool(ok),
        "required": bool(required),
        "detail": detail,
    }


def _skill_checks(skill_root: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for relative_path in REQUIRED_SKILL_FILES:
        candidate = skill_root / relative_path
        checks.append(
            _check_item(
                f"skill_file:{relative_path}",
                candidate.is_file(),
                required=True,
                detail="present" if candidate.is_file() else "missing",
            )
        )

    utf8_ok = True
    invalid_utf8: list[str] = []
    for relative_path in TEXT_SKILL_FILES:
        candidate = skill_root / relative_path
        if not candidate.is_file():
            continue
        try:
            candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            utf8_ok = False
            invalid_utf8.append(relative_path)
    checks.append(
        _check_item(
            "skill_text_utf8",
            utf8_ok,
            required=True,
            detail="all required text files decode as UTF-8"
            if utf8_ok
            else "invalid UTF-8: " + ", ".join(invalid_utf8),
        )
    )

    valid_json = True
    invalid_json: list[str] = []
    for relative_path in JSON_SKILL_FILES:
        candidate = skill_root / relative_path
        if not candidate.is_file():
            continue
        try:
            json.loads(candidate.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            valid_json = False
            invalid_json.append(relative_path)
    checks.append(
        _check_item(
            "skill_json_parse",
            valid_json,
            required=True,
            detail="all required JSON files parse"
            if valid_json
            else "invalid JSON: " + ", ".join(invalid_json),
        )
    )
    return checks


def _workspace_checks(workspace: Path | None) -> list[dict[str, Any]]:
    if workspace is None:
        return []
    checks = [
        _check_item(
            "workspace_directory",
            workspace.is_dir(),
            required=True,
            detail="present" if workspace.is_dir() else "missing",
        )
    ]
    for relative_path, kind in WORKSPACE_CORE_MARKERS:
        present = _marker_exists(workspace, relative_path, kind)
        checks.append(
            _check_item(
                f"workspace_marker:{relative_path}",
                present,
                required=True,
                detail=f"{kind} present" if present else f"missing {kind}",
            )
        )
    return checks


def _runtime_checks() -> list[dict[str, Any]]:
    version_ok = MINIMUM_PYTHON <= sys.version_info[:2] <= MAXIMUM_PYTHON
    platform_ok = os.name == "nt" and sys.maxsize > 2**32
    filesystem_encoding = sys.getfilesystemencoding() or "unknown"
    fs_utf8 = filesystem_encoding.lower().replace("-", "") == "utf8"
    return [
        _check_item(
            "python_version",
            version_ok,
            required=True,
            detail=(
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                f" (supported {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}-"
                f"{MAXIMUM_PYTHON[0]}.{MAXIMUM_PYTHON[1]})"
            ),
        ),
        _check_item(
            "windows_x64_runtime",
            platform_ok,
            required=True,
            detail=f"os.name={os.name}; pointer_bits={64 if sys.maxsize > 2**32 else 32}",
        ),
        _check_item(
            "filesystem_encoding_utf8",
            fs_utf8,
            required=True,
            detail=filesystem_encoding,
        ),
    ]


def _is_utf8_encoding(value: str | None) -> bool:
    if value is None:
        return False
    return value.lower().replace("-", "").replace("_", "") in {
        "utf8",
        "utf8sig",
    }


def build_doctor_report(
    *,
    explicit_workspace: str | Path | None = None,
    strict_workspace: bool = False,
    environ: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    skill_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic report.  This function performs reads only."""

    env = os.environ if environ is None else environ
    caller_cwd = Path.cwd() if cwd is None else Path(cwd)
    installed_skill = (
        Path(__file__).resolve().parent.parent
        if skill_root is None
        else Path(skill_root).resolve(strict=False)
    )

    config_error: str | None = None
    try:
        resolution = resolve_workspace(
            explicit_workspace,
            environ=env,
            cwd=caller_cwd,
        )
    except RuntimeConfigError as exc:
        config_error = str(exc)
        resolution = WorkspaceResolution(None, "configuration_error", True)

    runtime_checks = _runtime_checks()
    skill_checks = _skill_checks(installed_skill)
    workspace_checks = _workspace_checks(resolution.path)

    required_checks = [
        *runtime_checks,
        *skill_checks,
        *workspace_checks,
    ]
    required_ok = all(
        check["ok"] for check in required_checks if check["required"]
    )
    workspace_missing = resolution.path is None
    if config_error is not None:
        status = "FAIL"
    elif not required_ok:
        status = "FAIL"
    elif workspace_missing and strict_workspace:
        status = "FAIL"
    elif workspace_missing:
        status = "PASS_WITHOUT_WORKSPACE"
    else:
        status = "PASS"

    stdout_encoding = sys.stdout.encoding
    preferred_encoding = locale.getpreferredencoding(False)
    utf8_warnings: list[str] = []
    if not _is_utf8_encoding(stdout_encoding):
        utf8_warnings.append(
            "stdout is not UTF-8; JSON remains ASCII-safe, set PYTHONUTF8=1 for text workflows"
        )
    if not _is_utf8_encoding(preferred_encoding):
        utf8_warnings.append(
            "locale preferred encoding is not UTF-8; set PYTHONUTF8=1 for text workflows"
        )

    errors: list[str] = []
    if config_error is not None:
        errors.append(config_error)
    if workspace_missing and strict_workspace:
        errors.append("strict workspace mode requires a resolved workspace")
    errors.extend(
        check["id"]
        for check in required_checks
        if check["required"] and not check["ok"]
    )

    return {
        "schema": SCHEMA,
        "status": status,
        "ok": status in {"PASS", "PASS_WITHOUT_WORKSPACE"},
        "mode": "read_only_doctor",
        "controls": {
            "external_write_count": 0,
            "network_request_count": 0,
            "browser_action_count": 0,
            "final_write_enabled": False,
        },
        "workspace": {
            "path": str(resolution.path) if resolution.path is not None else None,
            "source": resolution.source,
            "configured": resolution.configured,
            "config_kind": resolution.config_kind,
            "strict": bool(strict_workspace),
            "checks": workspace_checks,
        },
        "runtime": {
            "implementation": sys.implementation.name,
            "python": {
                "major": sys.version_info.major,
                "minor": sys.version_info.minor,
                "micro": sys.version_info.micro,
            },
            "checks": runtime_checks,
            "encoding": {
                "filesystem": sys.getfilesystemencoding(),
                "stdout": stdout_encoding,
                "preferred": preferred_encoding,
                "utf8_mode": bool(sys.flags.utf8_mode),
            },
        },
        "skill": {
            "root": str(installed_skill),
            "checks": skill_checks,
        },
        "optional_dependencies": {
            "jsonschema": importlib.util.find_spec("jsonschema") is not None,
        },
        "warnings": utf8_warnings,
        "errors": errors,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only portability doctor for alibaba-similar-draft"
    )
    parser.add_argument("command", nargs="?", default="doctor", choices=("doctor",))
    parser.add_argument("--workspace", help="explicit business-data workspace")
    parser.add_argument(
        "--strict-workspace",
        action="store_true",
        help="fail unless a workspace and every core marker are present",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        help="override caller cwd for deterministic discovery/testing",
    )
    parser.add_argument(
        "--skill-root",
        type=Path,
        help="override skill root for package verification/testing",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print the JSON report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = build_doctor_report(
        explicit_workspace=args.workspace,
        strict_workspace=args.strict_workspace,
        cwd=args.cwd,
        skill_root=args.skill_root,
    )
    # ensure_ascii=True keeps output parseable on legacy Windows code pages.
    print(
        json.dumps(
            report,
            ensure_ascii=True,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
