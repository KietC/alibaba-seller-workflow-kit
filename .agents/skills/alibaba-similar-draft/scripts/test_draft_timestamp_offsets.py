"""Strict RFC3339 offset ranges through real commercial approval consumers."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import compile_approval
import compile_browser_action_plan as plan_compiler
import draft_core as core
import test_action_plan as action_fixtures
import test_compile_approval as approval_fixtures
import test_draft_contract_regressions as contract_fixtures


SCRIPTS = Path(__file__).resolve().parent
ISOLATED_CWD = Path("C:/Windows/Temp") if os.name == "nt" else Path(tempfile.gettempdir())
INVALID = tuple("2026-08-05T12:00:00" + offset for offset in (
    "+00:60", "+00:99", "+23:60", "+24:00", "-00:60", "-00:99", "-24:00",
))
VALID = (
    "2026-08-05T12:00:00Z", "2026-08-05t12:00:00z", "2026-08-05T12:00:00+00:00",
    "2026-08-05T12:00:00-00:00", "2026-08-05T12:00:00+23:59", "2026-08-05T12:00:00-23:59",
    "2026-08-05T12:00:00+09:30", "2026-08-05T12:00:00.123456-04:00",
)


class CommercialTimestampOffsetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="draft-offset-contract-", dir=ISOLATED_CWD)
        self.root = Path(self.temp.name)
        self.serial = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def next_root(self) -> Path:
        self.serial += 1
        return self.root / f"synthetic-{self.serial}"

    def approval(self, timestamp: str) -> approval_fixtures.ApprovalFixture:
        fixture = approval_fixtures.ApprovalFixture(self.next_root())
        path = fixture.add_commercial_decision()
        document = core.read_json(path)
        document["approved_at"] = timestamp
        approval_fixtures.write_json(path, document)
        decision = fixture.partial_decision_document()
        decision["blocked_fields"].remove("price")
        decision["approved_fields"]["price"] = {
            "value": 50, "decision_id": document["decision_id"],
            "source_selector": {"kind": "COMMERCIAL_DECISION", "path": str(path),
                                "bytes": path.stat().st_size, "sha256": core.sha256_file(path)},
        }
        fixture.write_partial_decision(decision)
        return fixture

    def preflight_fixture(self, timestamp: str) -> contract_fixtures.CommercialFieldRegressionTests:
        fixture = contract_fixtures.CommercialFieldRegressionTests()
        fixture.root = self.next_root()
        fixture.fixture = action_fixtures.ActionPlanCompilerTests()
        fixture.fixture.root = fixture.root
        fixture.root.mkdir()
        fixture.fixture.paths = fixture.fixture._build_fixture()
        fixture.source = core.read_json(fixture.fixture.paths["source"])
        path = fixture.add_field("price", qualified=True)
        document = core.read_json(path)
        document["approved_at"] = timestamp
        contract_fixtures.write_json(path, document)
        fixture.source["sources"][-1].update(bytes=path.stat().st_size, sha256=core.sha256_file(path))
        return fixture

    def test_normal_approval_rejects_out_of_range_offset_without_outputs(self) -> None:
        for timestamp in INVALID:
            with self.subTest(timestamp=timestamp):
                fixture = self.approval(timestamp)
                with self.assertRaisesRegex(compile_approval.CompileBlocked, "approved_at"):
                    compile_approval.compile_approval(fixture.candidate, fixture.decision, fixture.output)
                self.assertFalse(fixture.output.exists())
                self.assertFalse(list(fixture.output.parent.glob(f".{fixture.output.name}.staging-*")))

    def test_normal_approval_preserves_valid_utc_and_numeric_offsets(self) -> None:
        for timestamp in VALID:
            with self.subTest(timestamp=timestamp):
                fixture = self.approval(timestamp)
                receipt = compile_approval.compile_approval(fixture.candidate, fixture.decision, fixture.output)
                self.assertEqual(receipt["status"], "PASS")
                self.assertFalse(receipt["publish_ready"])
                self.assertFalse(receipt["final_write_enabled"])
                self.assertEqual({path.name for path in fixture.output.iterdir()}, {
                    "approved_source_manifest.json", "approved_asset_manifest.json", "compile_receipt.json",
                })

    def test_actual_preflight_and_public_plan_reject_out_of_range_offsets(self) -> None:
        for timestamp in INVALID:
            with self.subTest(timestamp=timestamp):
                fixture = self.preflight_fixture(timestamp)
                receipt = fixture.preflight()
                self.assertNotEqual(receipt["status"], "PASS")
                self.assertTrue(any("approved_at" in error and "canonical schema" in error for error in receipt["errors"]))
                with self.assertRaises(plan_compiler.PlanCompileError):
                    fixture.fixture.compile()

    def test_actual_preflight_and_public_plan_preserve_valid_offsets(self) -> None:
        for timestamp in VALID:
            with self.subTest(timestamp=timestamp):
                fixture = self.preflight_fixture(timestamp)
                self.assertEqual(fixture.preflight()["status"], "PASS")
                plan = fixture.fixture.compile()
                self.assertTrue(plan["invariants"]["final_write_permanently_disabled"])
                self.assertEqual(plan["invariants"]["submit_or_final_write_decision"], "HARD_DENY")

    def test_real_approval_cli_blocks_offset_overflow_and_accepts_valid_offset(self) -> None:
        environment = os.environ.copy()
        for key in ("SELLERKIT_ALIBABA_WORKSPACE", "SELLERKIT_ALIBABA_CONFIG"):
            environment.pop(key, None)
        environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        for timestamp in (INVALID[1], INVALID[0], INVALID[3], VALID[2], VALID[6]):
            with self.subTest(timestamp=timestamp):
                fixture = self.approval(timestamp)
                result = subprocess.run([
                    sys.executable, "-B", str(SCRIPTS / "compile_approval.py"),
                    "--candidate-dir", str(fixture.candidate), "--decision", str(fixture.decision),
                    "--output", str(fixture.output),
                ], cwd=ISOLATED_CWD, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
                if timestamp in INVALID:
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertIn("approved_at", json.loads(result.stderr)["error"])
                    self.assertFalse(fixture.output.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["status"], "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
