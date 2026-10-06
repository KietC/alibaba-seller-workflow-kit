#!/usr/bin/env python3
"""Hermetic tests for runtime_config.py."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import runtime_config


SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_SOURCE = SCRIPT_DIR / "runtime_config.py"
VERIFY_SOURCE = SCRIPT_DIR.parent / "verify.ps1"
RUN_ACCEPTANCE_SOURCE = SCRIPT_DIR.parent / "run_local_acceptance.ps1"


def _snapshot(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file()
        )
    )


def _make_skill(root: Path) -> Path:
    skill = root / "skill-copy"
    for relative_path in runtime_config.REQUIRED_SKILL_FILES:
        destination = skill / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative_path.endswith(".json"):
            destination.write_text("{}\n", encoding="utf-8")
        else:
            destination.write_text("portable fixture\n", encoding="utf-8")
    return skill


def _make_workspace(root: Path, name: str) -> Path:
    workspace = root / name
    workspace.mkdir(parents=True)
    for relative_path, kind in runtime_config.WORKSPACE_CORE_MARKERS:
        marker = workspace / relative_path
        if kind == "directory":
            marker.mkdir(parents=True)
        else:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("marker\n", encoding="utf-8")
    return workspace


class PortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="alibaba-portability-")
        self.root = Path(self.temp.name) / f"迁移验证-{uuid.uuid4().hex[:8]}"
        self.root.mkdir()
        self.skill = _make_skill(self.root)
        self.workspace_a = _make_workspace(self.root, "工作区-甲")
        self.workspace_b = _make_workspace(self.root, "工作区-乙")
        self.workspace_c = _make_workspace(self.root, "工作区-丙")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _report(self, **kwargs: object) -> dict[str, object]:
        defaults: dict[str, object] = {
            "skill_root": self.skill,
            "cwd": self.root,
            "environ": {},
        }
        defaults.update(kwargs)
        return runtime_config.build_doctor_report(**defaults)

    def test_explicit_workspace_wins_over_environment_and_config(self) -> None:
        env = {
            runtime_config.ENV_WORKSPACE: str(self.workspace_b),
            runtime_config.ENV_CONFIG: json.dumps(
                {"workspace": str(self.workspace_c)}, ensure_ascii=False
            ),
        }
        report = self._report(
            explicit_workspace=self.workspace_a,
            environ=env,
            strict_workspace=True,
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["workspace"]["source"], "explicit_argument")
        self.assertEqual(Path(report["workspace"]["path"]), self.workspace_a.resolve())

    def test_environment_workspace_wins_over_config(self) -> None:
        env = {
            runtime_config.ENV_WORKSPACE: str(self.workspace_b),
            runtime_config.ENV_CONFIG: json.dumps(
                {"workspace": str(self.workspace_c)}, ensure_ascii=False
            ),
        }
        report = self._report(environ=env, strict_workspace=True)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["workspace"]["source"], "environment")
        self.assertEqual(Path(report["workspace"]["path"]), self.workspace_b.resolve())

    def test_inline_config_json_and_relative_workspace(self) -> None:
        env = {
            runtime_config.ENV_CONFIG: json.dumps(
                {"workspace": self.workspace_c.name}, ensure_ascii=False
            )
        }
        report = self._report(environ=env, strict_workspace=True)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["workspace"]["source"], "config_json")
        self.assertEqual(report["workspace"]["config_kind"], "inline_json")
        self.assertEqual(Path(report["workspace"]["path"]), self.workspace_c.resolve())

    def test_config_file_resolves_relative_to_its_own_directory(self) -> None:
        config_dir = self.root / "配置"
        config_dir.mkdir()
        local_workspace = _make_workspace(config_dir, "数据")
        config_path = config_dir / "runtime.json"
        config_path.write_text(
            json.dumps({"workspace_root": "数据"}, ensure_ascii=False),
            encoding="utf-8",
        )
        report = self._report(
            environ={runtime_config.ENV_CONFIG: str(config_path)},
            strict_workspace=True,
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["workspace"]["config_kind"], "json_file")
        self.assertEqual(Path(report["workspace"]["path"]), local_workspace.resolve())

    def test_cwd_ancestor_marker_discovery_handles_non_ascii(self) -> None:
        nested = self.workspace_a / "子目录" / "更深"
        nested.mkdir(parents=True)
        report = self._report(cwd=nested, strict_workspace=True)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["workspace"]["source"], "cwd_ancestor_markers")
        self.assertEqual(Path(report["workspace"]["path"]), self.workspace_a.resolve())

    def test_installed_skill_path_is_never_used_as_workspace(self) -> None:
        disguised = _make_workspace(self.root, "plugin-cache")
        skill_inside_workspace = _make_skill(disguised)
        unrelated_cwd = self.root / "unrelated"
        unrelated_cwd.mkdir()
        report = runtime_config.build_doctor_report(
            skill_root=skill_inside_workspace,
            cwd=unrelated_cwd,
            environ={},
        )
        self.assertEqual(report["status"], "PASS_WITHOUT_WORKSPACE")
        self.assertIsNone(report["workspace"]["path"])

    def test_core_only_pass_without_workspace_and_zero_write_controls(self) -> None:
        empty_cwd = self.root / "empty"
        empty_cwd.mkdir()
        before = _snapshot(self.root)
        report = self._report(cwd=empty_cwd)
        after = _snapshot(self.root)
        self.assertEqual(before, after)
        self.assertEqual(report["status"], "PASS_WITHOUT_WORKSPACE")
        self.assertTrue(report["ok"])
        self.assertEqual(report["controls"]["external_write_count"], 0)
        self.assertEqual(report["controls"]["network_request_count"], 0)
        self.assertFalse(report["controls"]["final_write_enabled"])

    def test_strict_workspace_is_fail_closed(self) -> None:
        empty_cwd = self.root / "empty-strict"
        empty_cwd.mkdir()
        report = self._report(cwd=empty_cwd, strict_workspace=True)
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["ok"])
        self.assertIn(
            "strict workspace mode requires a resolved workspace",
            report["errors"],
        )

    def test_configured_but_incomplete_workspace_fails(self) -> None:
        incomplete = self.root / "incomplete"
        incomplete.mkdir()
        report = self._report(explicit_workspace=incomplete)
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["ok"])

    def test_invalid_config_does_not_echo_secret_material(self) -> None:
        sensitive_value = "do-not-leak-" + uuid.uuid4().hex
        invalid = "{" + sensitive_value
        report = self._report(
            environ={runtime_config.ENV_CONFIG: invalid},
            strict_workspace=True,
        )
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["status"], "FAIL")
        self.assertNotIn(sensitive_value, rendered)

    def test_source_has_no_absolute_drive_or_embedded_credentials(self) -> None:
        source = RUNTIME_SOURCE.read_text(encoding="utf-8")
        # Cross-machine code must not contain a Windows drive literal.
        self.assertIsNone(re.search(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]", source))

        # Scan likely secret/token/password/cookie assignments while allowing
        # environment-variable names and explanatory documentation.
        credential_assignment = re.compile(
            r"(?i)(?:secret|token|password|cookie)\s*[:=]\s*[\"'][^\"']{8,}[\"']"
        )
        common_key_material = re.compile(
            r"(?i)(?:sk-[A-Za-z0-9_-]{16,}|bearer\s+[A-Za-z0-9._~-]{16,})"
        )
        self.assertIsNone(credential_assignment.search(source))
        self.assertIsNone(common_key_material.search(source))

    def test_cli_emits_parseable_ascii_json_and_expected_exit_codes(self) -> None:
        env = os.environ.copy()
        env.pop(runtime_config.ENV_WORKSPACE, None)
        env.pop(runtime_config.ENV_CONFIG, None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        empty_cwd = self.root / "cli-empty"
        empty_cwd.mkdir()

        common = [
            sys.executable,
            str(RUNTIME_SOURCE),
            "doctor",
            "--skill-root",
            str(self.skill),
            "--cwd",
            str(empty_cwd),
        ]
        core_only = subprocess.run(
            common,
            env=env,
            capture_output=True,
            check=False,
        )
        self.assertEqual(core_only.returncode, 0, core_only.stderr.decode(errors="replace"))
        payload = json.loads(core_only.stdout.decode("ascii"))
        self.assertEqual(payload["status"], "PASS_WITHOUT_WORKSPACE")

        strict = subprocess.run(
            [*common, "--strict-workspace"],
            env=env,
            capture_output=True,
            check=False,
        )
        self.assertEqual(strict.returncode, 2)
        payload = json.loads(strict.stdout.decode("ascii"))
        self.assertEqual(payload["status"], "FAIL")

    def test_verify_runner_bridges_explicit_workspace_into_test_environment(self) -> None:
        source = VERIFY_SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            "$env:SELLERKIT_ALIBABA_WORKSPACE = $resolvedWorkspace",
            source,
        )
        self.assertIn(
            "Remove-Item Env:SELLERKIT_ALIBABA_WORKSPACE -ErrorAction SilentlyContinue",
            source,
        )
        self.assertLess(
            source.index("$env:SELLERKIT_ALIBABA_WORKSPACE = $resolvedWorkspace"),
            source.index("-m unittest discover"),
        )

    @unittest.skipUnless(
        shutil.which("powershell.exe") or shutil.which("pwsh"),
        "PowerShell is required for the acceptance output-boundary test",
    )
    def test_acceptance_output_inside_skill_root_is_rejected_before_creation(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        assert powershell is not None
        forbidden_output = SCRIPT_DIR.parent / f"private-acceptance-{uuid.uuid4().hex}"
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(RUN_ACCEPTANCE_SOURCE),
                "-SkillRoot",
                str(SCRIPT_DIR.parent),
                "-Workspace",
                str(self.workspace_a),
                "-OutputDirectory",
                str(forbidden_output),
            ],
            capture_output=True,
            check=False,
        )
        rendered = (completed.stdout + completed.stderr).decode(errors="replace")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must be outside the Skill root", rendered)
        self.assertFalse(forbidden_output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
