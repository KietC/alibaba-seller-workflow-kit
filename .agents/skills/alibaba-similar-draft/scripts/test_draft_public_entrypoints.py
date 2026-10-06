"""Pure synthetic CLI, canonical-schema, alias-target and zero-count regressions."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import compile_approval
import compile_browser_action_plan as plan_compiler
import draft_core as core
import local_schema_validation
import test_compile_approval as approval_fixtures
import test_draft_contract_regressions as contract_fixtures


SCRIPTS = Path(__file__).resolve().parent
ISOLATED_CWD = Path("C:/Windows/Temp") if os.name == "nt" else Path(tempfile.gettempdir())


class ApprovalCliRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="draft-real-cli-", dir=ISOLATED_CWD)
        self.fixture = approval_fixtures.ApprovalFixture(Path(self.temp.name) / "合成审批")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self, *, decision: Path | None = None) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        for name in ("SELLERKIT_ALIBABA_WORKSPACE", "SELLERKIT_ALIBABA_CONFIG"):
            environment.pop(name, None)
        environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "compile_approval.py"),
             "--candidate-dir", str(self.fixture.candidate),
             "--decision", str(decision or self.fixture.decision), "--output", str(self.fixture.output)],
            cwd=ISOLATED_CWD, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )

    def add_price(self, *, value: object = 50.0, one: bool = False, malformed_ids: bool = False) -> None:
        path = self.fixture.add_commercial_decision()
        document = core.read_json(path)
        if one:
            document["price"].update(p25=1, p50=1, p75=1, approved_value=1)
        if malformed_ids:
            document["price"]["candidate_product_ids"] = "123"
        approval_fixtures.write_json(path, document)
        decision = self.fixture.partial_decision_document()
        decision["blocked_fields"].remove("price")
        decision["approved_fields"]["price"] = {
            "value": value, "decision_id": document["decision_id"],
            "source_selector": {"kind": "COMMERCIAL_DECISION", "path": str(path),
                                "bytes": path.stat().st_size, "sha256": core.sha256_file(path)},
        }
        self.fixture.write_partial_decision(decision)

    def assert_success(self, completed: subprocess.CompletedProcess) -> None:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertEqual(receipt["status"], "PASS")
        self.assertFalse(receipt["publish_ready"])
        self.assertFalse(receipt["final_write_enabled"])
        self.assertEqual({path.name for path in self.fixture.output.iterdir()},
                         {"approved_source_manifest.json", "approved_asset_manifest.json", "compile_receipt.json"})
        self.assertEqual(core.read_json(self.fixture.output / "compile_receipt.json"), receipt)
        for name in ("approved_source_manifest.json", "approved_asset_manifest.json"):
            self.assertEqual(core.sha256_file(self.fixture.output / name), receipt["outputs"][name]["sha256"])
        self.assertFalse(list(self.fixture.output.parent.glob(f".{self.fixture.output.name}.staging-*")))

    def assert_blocked(self, completed: subprocess.CompletedProcess, text: str) -> None:
        self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
        receipt = json.loads(completed.stderr)
        self.assertEqual(receipt["status"], "BLOCKED")
        self.assertIn(text, receipt["error"])
        self.assertNotIn("unexpected keyword argument", receipt["error"])
        self.assertFalse(self.fixture.output.exists())
        self.assertFalse(list(self.fixture.output.parent.glob(f".{self.fixture.output.name}.staging-*")))

    def test_real_cli_valid_partial_emits_all_three_files(self) -> None:
        self.assert_success(self.invoke())

    def test_real_cli_qualified_single_price_emits_all_three_files(self) -> None:
        self.add_price()
        self.assert_success(self.invoke())

    def test_real_cli_missing_decision_blocks_without_output(self) -> None:
        self.assert_blocked(self.invoke(decision=Path(self.temp.name) / "absent.json"), "human decision JSON is missing")

    def test_real_cli_boolean_price_blocks_without_output(self) -> None:
        self.add_price(value=True, one=True)
        self.assert_blocked(self.invoke(), "commercial value mismatch")

    def test_real_cli_malformed_commercial_schema_blocks_without_output(self) -> None:
        self.add_price(malformed_ids=True)
        self.assert_blocked(self.invoke(), "candidate_product_ids")

    def test_real_cli_existing_output_remains_unchanged(self) -> None:
        self.fixture.output.mkdir()
        sentinel = self.fixture.output / "sentinel.txt"
        sentinel.write_text("existing synthetic output\n", encoding="utf-8")
        before = core.sha256_file(sentinel)
        completed = self.invoke()
        self.assertEqual(completed.returncode, 3)
        self.assertIn("refusing to overwrite", json.loads(completed.stderr)["error"])
        self.assertEqual(core.sha256_file(sentinel), before)
        self.assertEqual([path.name for path in self.fixture.output.iterdir()], ["sentinel.txt"])

    def test_real_cli_duplicate_price_aliases_block_without_output(self) -> None:
        self.add_price()
        template_path = self.fixture.candidate / "approved_source_manifest.template.json"
        template = core.read_json(template_path)
        row = copy.deepcopy(next(row for row in template["fields"] if row["field"] == "price"))
        row["field"] = "trade_price"
        template["fields"].append(row)
        approval_fixtures.write_json(template_path, template)
        self.fixture.seal_package()
        decision = core.read_json(self.fixture.decision)
        decision["approved_fields"]["trade_price"] = copy.deepcopy(decision["approved_fields"]["price"])
        self.fixture.write_partial_decision(decision)
        with self.assertRaisesRegex(compile_approval.CompileBlocked, "duplicate approved registry targets"):
            compile_approval.compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)
        self.assert_blocked(self.invoke(), "duplicate approved registry targets")


class CommercialSchemaAndAliasRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = contract_fixtures.CommercialFieldRegressionTests()
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def rebind_document(self, path: Path, document: dict) -> None:
        contract_fixtures.write_json(path, document)
        self.fixture.source["sources"][-1].update(bytes=path.stat().st_size, sha256=core.sha256_file(path))

    def assert_schema_blocked(self) -> None:
        receipt = self.fixture.preflight()
        self.assertNotEqual(receipt["status"], "PASS", receipt)
        self.assertTrue(any("canonical schema" in error for error in receipt["errors"]), receipt)
        with self.assertRaises(plan_compiler.PlanCompileError):
            self.fixture.fixture.compile()

    def test_string_ids_never_qualify_as_three_comparables_in_public_plan(self) -> None:
        for field in ("price", "trade_price", "minimum_order_quantity"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.fixture.source)
                path = self.fixture.add_field(field, qualified=True)
                document = core.read_json(path)
                document["price"]["candidate_product_ids"] = "123"
                self.rebind_document(path, document)
                self.assert_schema_blocked()
                self.fixture.source = original

    def test_invalid_id_array_shapes_fail_canonical_preflight_and_public_plan(self) -> None:
        path = self.fixture.add_field("price", qualified=True)
        original = core.read_json(path)
        for ids in (123, ["1", "1", "2"], ["1", 2, 3], ["1", "2"], ["1", "2", "bad"]):
            with self.subTest(ids=ids):
                document = copy.deepcopy(original)
                document["price"]["candidate_product_ids"] = ids
                self.rebind_document(path, document)
                self.assert_schema_blocked()

    def test_other_malformed_canonical_commercial_shapes_fail_public_plan(self) -> None:
        path = self.fixture.add_field("price", qualified=True)
        original = core.read_json(path)
        for key, bad in (("approved_at", "not-a-date"), ("approved_by", 1), ("decision_id", ""),
                         ("price", []), ("unexpected_property", "extra")):
            with self.subTest(key=key):
                document = copy.deepcopy(original)
                document[key] = bad
                self.rebind_document(path, document)
                self.assert_schema_blocked()

    def test_missing_jsonschema_dependency_fails_closed_in_real_isolated_process(self) -> None:
        path = self.fixture.add_field("price", qualified=True)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        for name in ("SELLERKIT_ALIBABA_WORKSPACE", "SELLERKIT_ALIBABA_CONFIG"):
            environment.pop(name, None)
        code = ("import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                "import draft_core; print(json.dumps(draft_core._validate_commercial_decision(Path(sys.argv[2]),sys.argv[3])))")
        completed = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code, str(SCRIPTS), str(path),
                                    self.fixture.source["target_model"]], cwd=ISOLATED_CWD,
                                   env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(any("runtime unavailable" in error for error in json.loads(completed.stdout)))

    def test_single_price_and_alias_plans_match_actual_fill_consumer(self) -> None:
        for field in ("price", "trade_price"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.fixture.source)
                self.fixture.add_field(field, qualified=True)
                self.assertEqual(self.fixture.preflight()["status"], "PASS")
                plan = self.fixture.fixture.compile()
                errors = []
                expected = core._expected_fill_contract(plan, errors)
                self.assertEqual(errors, [])
                targets = [json.loads(key) for key in expected[0]
                           if json.loads(key).get("logical_name") == "trade_price"]
                self.assertEqual(len(targets), 1)
                self.fixture.source = original

    def assert_duplicate_blocked(self, *, conflict: bool = False) -> None:
        self.fixture.add_field("price", qualified=True)
        path = self.fixture.add_field("trade_price", qualified=True)
        if conflict:
            self.fixture.replace_reference_number("trade_price", path, 51)
            self.fixture.replace_value("trade_price", 51)
        receipt = self.fixture.preflight()
        self.assertTrue(any("duplicate approved registry targets" in error for error in receipt["errors"]), receipt)
        with self.assertRaisesRegex(plan_compiler.PlanCompileError, "duplicate approved registry targets"):
            self.fixture.fixture.compile()
        registry = core.read_json(compile_approval.FIELD_REGISTRY)
        fields = {row["logical_name"]: row for row in registry["fields"]}
        with self.assertRaisesRegex(plan_compiler.PlanCompileError, "duplicate approved registry targets"):
            plan_compiler._approved_value_operations(self.fixture.source, fields, set())

    def test_equal_dual_price_alias_approval_blocks_before_public_plan(self) -> None:
        self.assert_duplicate_blocked()

    def test_conflicting_dual_price_alias_approval_blocks_before_public_plan(self) -> None:
        self.assert_duplicate_blocked(conflict=True)


class OfflineSchemaRegistryRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="draft-offline-schema-", dir=ISOLATED_CWD)
        self.root = Path(self.temp.name)
        self.schema = self.root / "main.schema.json"
        contract_fixtures.write_json(self.root / "common.schema.json", {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://synthetic.invalid/schemas/common.schema.json", "$defs": {"id": {"type": "integer"}},
        })
        contract_fixtures.write_json(self.schema, {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://synthetic.invalid/schemas/main.schema.json", "type": "object",
            "properties": {"id": {"$ref": "common.schema.json#/$defs/id"}},
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_relative_common_schema_resolves_locally_without_network(self) -> None:
        with patch("socket.create_connection", side_effect=AssertionError("network prohibited")) as connection:
            self.assertEqual(local_schema_validation.local_schema_errors({"id": 1}, self.schema), [])
            connection.assert_not_called()

    def test_relative_common_schema_rejects_wrong_type_without_network(self) -> None:
        with patch("socket.create_connection", side_effect=AssertionError("network prohibited")) as connection:
            errors = local_schema_validation.local_schema_errors({"id": "wrong"}, self.schema)
            self.assertTrue(any("not of type 'integer'" in error for error in errors), errors)
            connection.assert_not_called()

    def test_unknown_remote_reference_is_blocked_without_retrieval(self) -> None:
        schema = core.read_json(self.schema)
        schema["properties"]["id"]["$ref"] = "https://never-fetch.invalid/remote.schema.json"
        contract_fixtures.write_json(self.schema, schema)
        with patch("socket.create_connection", side_effect=AssertionError("network prohibited")) as connection:
            errors = local_schema_validation.local_schema_errors({"id": 1}, self.schema)
            self.assertTrue(any("failed closed" in error for error in errors), errors)
            connection.assert_not_called()


class OverlayZeroCountRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = contract_fixtures.OverlaySealRegressionTests()
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def bind_uniform_catalog(self, *, eligible: bool) -> None:
        rows = [json.loads(line) for line in self.fixture.disposition.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            row.update(record_disposition=core.HISTORICAL_DISPOSITION if eligible else "UNUSABLE_WASTE_RECORD",
                       allow_rag=eligible)
        self.fixture.disposition.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        path = self.fixture.overlay / self.fixture.CONSUMED[2]
        receipt = core.read_json(path)
        receipt.update(eligible_count=len(rows) if eligible else 0, unusable_count=0 if eligible else len(rows))
        contract_fixtures.write_json(path, receipt)
        self.fixture.seal()

    def test_all_eligible_catalog_preserves_zero_unusable_count(self) -> None:
        self.bind_uniform_catalog(eligible=True)
        result = core.verify_overlay(self.fixture.workspace)
        self.assertEqual((result["status"], result["eligible_count"], result["unusable_count"]), ("COMPLETE", 2, 0))

    def test_all_excluded_catalog_preserves_zero_eligible_count(self) -> None:
        self.bind_uniform_catalog(eligible=False)
        result = core.verify_overlay(self.fixture.workspace)
        self.assertEqual((result["status"], result["eligible_count"], result["unusable_count"]), ("COMPLETE", 0, 2))

    def test_missing_or_changed_zero_counts_still_fail_after_resealing(self) -> None:
        self.bind_uniform_catalog(eligible=True)
        path = self.fixture.overlay / self.fixture.CONSUMED[2]
        original = core.read_json(path)
        for key, value in (("unusable_count", None), ("unusable_count", 1), ("eligible_count", 0),
                           ("unusable_count", False), ("unusable_count", "0")):
            with self.subTest(key=key, value=value):
                receipt = copy.deepcopy(original)
                if value is None:
                    receipt.pop(key)
                else:
                    receipt[key] = value
                contract_fixtures.write_json(path, receipt)
                self.fixture.seal()
                with self.assertRaisesRegex(core.GuardError, "eligibility totals changed"):
                    core.verify_overlay(self.fixture.workspace)


if __name__ == "__main__":
    unittest.main(verbosity=2)
