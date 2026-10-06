"""Synthetic-only regressions: per-field commercial approval and overlay seals."""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import compile_approval
import compile_browser_action_plan as plan_compiler
import draft_core as core
import test_action_plan as action_fixtures
import test_compile_approval as approval_fixtures


def write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


class CommercialFieldRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="draft-commercial-contract-")
        self.root = Path(self.temp.name)
        self.fixture = action_fixtures.ActionPlanCompilerTests()
        self.fixture.root = self.root / "合成动作计划"
        self.fixture.root.mkdir()
        self.fixture.paths = self.fixture._build_fixture()
        self.source = core.read_json(self.fixture.paths["source"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_field(self, field: str, *, qualified: bool = False) -> Path | None:
        value = 50 if field in ("price", "trade_price") else 10
        fact_source = self.source["sources"][0]
        source_id, decision_id = "facts", "facts-decision"
        path = None
        if qualified:
            path = self.fixture.root / f"commercial-{field}.json"
            document = {
                "schema_version": "sellerkit.approved-commercial-decision.v1",
                "decision_id": f"commercial-{field}",
                "target_model": self.source["target_model"],
                "status": "APPROVED", "approved_by": "synthetic-reviewer",
                "approved_at": "2026-08-05T12:00:00Z",
                "price": {
                    "currency": "USD", "unit": "piece", "trade_term": "FOB",
                    "pricing_method": "WEIGHTED_P25_P50_P75",
                    "candidate_product_ids": ["101", "102", "103"],
                    "p25": 40, "p50": 50, "p75": 60, "approved_value": 50,
                },
                "minimum_order_quantity": {"value": 10, "unit": "piece"},
                "historical_values_are_quote": False, "final_write_enabled": False,
            }
            write_json(path, document)
            source_id, decision_id = f"commercial-{field}", document["decision_id"]
            descriptor = copy.deepcopy(fact_source)
            descriptor.update(
                source_id=source_id, source_role="COMMERCIAL_DECISION", path=str(path),
                bytes=path.stat().st_size, sha256=core.sha256_file(path),
                decision_id=decision_id, approved_fields=[field],
            )
            self.source["sources"].append(descriptor)
            self.source["source_count"] = len(self.source["sources"])
        else:
            fact_source["approved_fields"].append(field)
        blocked_field = "price" if field == "trade_price" else field
        if blocked_field in self.source["draft_field_policy"]["blocked_fields"]:
            self.source["draft_field_policy"]["blocked_fields"].remove(blocked_field)
        self.source["approved_field_values"][field] = {
            "value": value, "value_sha256": core.sha256_bytes(core.canonical_json(value).encode()),
            "source_id": source_id, "decision_id": decision_id,
        }
        return path

    def persist(self) -> None:
        paths = self.fixture.paths
        write_json(paths["source"], self.source)
        assets = core.read_json(paths["asset"])
        assets["bindings"]["approved_source_manifest_sha256"] = core.sha256_file(paths["source"])
        write_json(paths["asset"], assets)
        approval = core.read_json(paths["write_approval"])
        approval["bindings"].update(
            approved_source_manifest_sha256=core.sha256_file(paths["source"]),
            approved_asset_manifest_sha256=core.sha256_file(paths["asset"]),
        )
        write_json(paths["write_approval"], approval)

    def preflight(self) -> dict:
        self.persist()
        return core.validate_approved_manifest(
            self.source, core.read_json(self.fixture.paths["asset"]),
            manifest_path=self.fixture.paths["source"], asset_manifest_path=self.fixture.paths["asset"],
        )

    def assert_blocked(self, field: str) -> None:
        result = self.preflight()
        self.assertNotEqual(result["status"], "PASS", result)
        self.assertTrue(any(field in error and "COMMERCIAL_DECISION" in error for error in result["errors"]), result)
        with self.assertRaises(action_fixtures.PlanCompileError):
            self.fixture.compile()

    def test_price_only_fact_approval_blocks_preflight_and_public_compiler(self) -> None:
        self.add_field("price")
        self.assert_blocked("price")

    def test_moq_only_fact_approval_blocks_preflight_and_public_compiler(self) -> None:
        self.add_field("minimum_order_quantity")
        self.assert_blocked("minimum_order_quantity")

    def test_both_fact_approvals_remain_blocked(self) -> None:
        self.add_field("price")
        self.add_field("minimum_order_quantity")
        self.assert_blocked("price")

    def test_qualified_moq_source_does_not_launder_fact_price(self) -> None:
        self.add_field("price")
        self.add_field("minimum_order_quantity", qualified=True)
        self.assert_blocked("price")

    def test_qualified_price_source_does_not_launder_fact_moq(self) -> None:
        self.add_field("price", qualified=True)
        self.add_field("minimum_order_quantity")
        self.assert_blocked("minimum_order_quantity")

    def assert_qualified_passes(self, field: str) -> None:
        self.add_field(field, qualified=True)
        result = self.preflight()
        self.assertEqual(result["status"], "PASS", result)
        plan = self.fixture.compile()
        logical_name = "trade_price" if field == "price" else field
        operations = [row for phase in plan["phases"] for row in phase["operations"]
                      if row.get("operation") == "WRITE_APPROVED_FIELD_VALUE"
                      and row.get("target", {}).get("logical_name") == logical_name]
        self.assertEqual(len(operations), 1)
        self.assertTrue(plan["invariants"]["final_write_permanently_disabled"])
        self.assertEqual(plan["invariants"]["submit_or_final_write_decision"], "HARD_DENY")

    def test_qualified_price_only_partial_approval_remains_supported(self) -> None:
        self.assert_qualified_passes("price")

    def test_qualified_moq_only_partial_approval_remains_supported(self) -> None:
        self.assert_qualified_passes("minimum_order_quantity")

    def test_single_commercial_field_revalidates_qualification(self) -> None:
        for field in ("price", "minimum_order_quantity"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.source)
                path = self.add_field(field, qualified=True)
                document = core.read_json(path)
                document["price"]["candidate_product_ids"] = ["101", "102"]
                write_json(path, document)
                self.source["sources"][-1].update(bytes=path.stat().st_size, sha256=core.sha256_file(path))
                result = self.preflight()
                self.assertNotEqual(result["status"], "PASS")
                self.assertTrue(any("three distinct" in error for error in result["errors"]), result)
                with self.assertRaises(action_fixtures.PlanCompileError):
                    self.fixture.compile()
                self.source = original

    def test_fact_only_partial_approval_still_passes(self) -> None:
        self.assertEqual(self.preflight()["status"], "PASS")

    def test_approved_value_must_match_the_qualified_commercial_file(self) -> None:
        for field in ("price", "minimum_order_quantity"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.source)
                self.add_field(field, qualified=True)
                self.source["approved_field_values"][field].update(
                    value=999, value_sha256=core.sha256_bytes(core.canonical_json(999).encode()),
                )
                result = self.preflight()
                self.assertTrue(any(f"commercial value mismatch for {field}" in error for error in result["errors"]), result)
                with self.assertRaises(action_fixtures.PlanCompileError):
                    self.fixture.compile()
                self.source = original

    def test_decision_id_must_match_the_qualified_commercial_file(self) -> None:
        for field in ("price", "minimum_order_quantity"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.source)
                self.add_field(field, qualified=True)
                self.source["sources"][-1]["decision_id"] = "not-the-commercial-decision"
                self.source["approved_field_values"][field]["decision_id"] = "not-the-commercial-decision"
                result = self.preflight()
                self.assertTrue(any(f"commercial decision_id mismatch for {field}" in error for error in result["errors"]), result)
                with self.assertRaises(action_fixtures.PlanCompileError):
                    self.fixture.compile()
                self.source = original

    def test_normal_compiler_supports_qualified_single_field_approval(self) -> None:
        for field, value in (("price", 50), ("minimum_order_quantity", 10)):
            with self.subTest(field=field):
                fixture = approval_fixtures.ApprovalFixture(self.root / f"compiler-{field}")
                commercial = fixture.add_commercial_decision()
                decision = fixture.partial_decision_document()
                decision["blocked_fields"].remove(field)
                decision["approved_fields"][field] = {
                    "value": value, "decision_id": "commercial-price-moq-1",
                    "source_selector": {
                        "kind": "COMMERCIAL_DECISION", "path": str(commercial),
                        "bytes": commercial.stat().st_size, "sha256": core.sha256_file(commercial),
                    },
                }
                fixture.write_partial_decision(decision)
                result = compile_approval.compile_approval(fixture.candidate, fixture.decision, fixture.output)
                self.assertEqual(result["status"], "PASS")
                self.assertFalse(result["publish_ready"])


    def replace_value(self, field: str, value: object) -> None:
        self.source["approved_field_values"][field].update(
            value=value, value_sha256=core.sha256_bytes(core.canonical_json(value).encode()),
        )

    def replace_reference_number(self, field: str, path: Path, value: int | float) -> None:
        document = core.read_json(path)
        if field in ("price", "trade_price"):
            document["price"].update(p25=value, p50=value, p75=value, approved_value=value)
        else:
            document["minimum_order_quantity"]["value"] = value
        write_json(path, document)
        self.source["sources"][-1].update(bytes=path.stat().st_size, sha256=core.sha256_file(path))

    def assert_value_blocked(self, field: str) -> None:
        result = self.preflight()
        self.assertNotEqual(result["status"], "PASS", result)
        self.assertIn(f"commercial value mismatch for {field}", result["errors"])
        with self.assertRaises(action_fixtures.PlanCompileError):
            self.fixture.compile()

    def assert_current_value_passes(self, field: str, value: object) -> None:
        self.replace_value(field, value)
        self.assertEqual(self.preflight()["status"], "PASS")
        plan = self.fixture.compile()
        logical = "trade_price" if field in ("price", "trade_price") else field
        writes = [row for phase in plan["phases"] for row in phase["operations"]
                  if row.get("operation") == "WRITE_APPROVED_FIELD_VALUE"
                  and row.get("target", {}).get("logical_name") == logical]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["value"], value)
        self.assertEqual(plan["invariants"]["submit_or_final_write_decision"], "HARD_DENY")

    def normal_commercial_case(self, field: str, value: object, *, number: int | float | None = None,
                               fact: bool = False) -> dict:
        case_id = len(list(self.root.glob("normal-strict-*")))
        fixture = approval_fixtures.ApprovalFixture(self.root / f"normal-strict-{case_id}")
        canonical = "price" if field == "trade_price" else field
        if field != canonical:
            template_path = fixture.candidate / "approved_source_manifest.template.json"
            template = core.read_json(template_path)
            for row in template["fields"]:
                if row["field"] == canonical:
                    row["field"] = field
                    if fact:
                        row.update(source_disposition="OFFICIAL_CURRENT_REVIEW_REQUIRED", decision="PENDING",
                                   source_path=str(fixture.source), source_sha256=core.sha256_file(fixture.source),
                                   source_bytes=fixture.source.stat().st_size)
            write_json(template_path, template)
            fixture.seal_package()
        path = fixture.add_commercial_decision()
        if number is not None:
            document = core.read_json(path)
            if canonical == "price":
                document["price"].update(p25=number, p50=number, p75=number, approved_value=number)
            else:
                document["minimum_order_quantity"]["value"] = number
            write_json(path, document)
        decision = fixture.partial_decision_document()
        decision["blocked_fields"].remove(canonical)
        decision["approved_fields"][field] = {
            "value": value, "decision_id": "commercial-price-moq-1",
            "source_selector": ({"kind": "CANDIDATE_FIELD_SOURCE", "candidate_field": field} if fact else {
                "kind": "COMMERCIAL_DECISION", "path": str(path), "bytes": path.stat().st_size,
                "sha256": core.sha256_file(path),
            }),
        }
        fixture.write_partial_decision(decision)
        return compile_approval.compile_approval(fixture.candidate, fixture.decision, fixture.output)

    def test_trade_price_fact_alias_blocks_preflight_and_public_plan(self) -> None:
        self.add_field("trade_price")
        self.assert_blocked("trade_price")

    def test_qualified_trade_price_alias_passes_public_plan_and_normal_compiler(self) -> None:
        self.add_field("trade_price", qualified=True)
        self.assert_current_value_passes("trade_price", 50.0)
        self.assertEqual(self.normal_commercial_case("trade_price", 50.0)["status"], "PASS")

    def test_trade_price_fact_alias_blocks_normal_compiler_even_if_candidate_looks_factual(self) -> None:
        with self.assertRaisesRegex(compile_approval.CompileBlocked, "qualified commercial decision"):
            self.normal_commercial_case("trade_price", 50, fact=True)

    def test_alias_and_registry_commercial_coverage_are_shared(self) -> None:
        self.assertIs(plan_compiler.FIELD_REGISTRY_ALIASES, core.FIELD_REGISTRY_ALIASES)
        registry = core.read_json(compile_approval.FIELD_REGISTRY)
        expected = {row["logical_name"] for row in registry["fields"]
                    if row.get("source_policy") == "APPROVED_COMMERCIAL_DECISION"}
        expected |= {key for key, target in plan_compiler.FIELD_REGISTRY_ALIASES.items() if target in expected}
        self.assertEqual(core.commercial_registry_fields(registry), expected)
        self.assertTrue(expected <= compile_approval.COMMERCIAL_FIELDS)
        self.assertIsNone(core.canonical_commercial_field("moq"))
        self.assertEqual(core.canonical_commercial_field("minimum_order_quantity"), "minimum_order_quantity")

    def test_unsupported_registry_commercial_fact_fields_remain_blocked(self) -> None:
        for field in ("sale_unit", "inventory", "lead_time"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.source)
                self.add_field(field)
                result = self.preflight()
                self.assertTrue(any(field in error and "qualified decision schema" in error
                                    for error in result["errors"]), result)
                with self.assertRaises(action_fixtures.PlanCompileError):
                    self.fixture.compile()
                self.source = original

    def test_conflicting_price_object_context_blocks_preflight_and_public_plan(self) -> None:
        self.add_field("price", qualified=True)
        for key, conflicting in (("currency", "CNY"), ("unit", "set"), ("trade_term", "EXW"),
                                 ("alternate_value", 999)):
            with self.subTest(key=key):
                value = {"approved_value": 50, "currency": "USD", "unit": "piece", "trade_term": "FOB"}
                value[key] = conflicting
                self.replace_value("price", value)
                self.assert_value_blocked("price")

    def test_boolean_scalar_commercial_values_block_preflight_and_public_plan(self) -> None:
        for field in ("price", "trade_price", "minimum_order_quantity"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.source)
                path = self.add_field(field, qualified=True)
                self.replace_reference_number(field, path, 1)
                self.replace_value(field, True)
                self.assert_value_blocked(field)
                self.source = original

    def test_boolean_nested_commercial_values_block_preflight_and_public_plan(self) -> None:
        for field, value in (("price", {"approved_value": True}),
                             ("minimum_order_quantity", {"value": True, "unit": "piece"})):
            with self.subTest(field=field):
                original = copy.deepcopy(self.source)
                path = self.add_field(field, qualified=True)
                self.replace_reference_number(field, path, 1)
                self.replace_value(field, value)
                self.assert_value_blocked(field)
                self.source = original

    def test_finite_float_scalars_and_zero_price_remain_compatible(self) -> None:
        for field, value in (("price", 50.0), ("price", 0), ("minimum_order_quantity", 10.0)):
            with self.subTest(field=field, value=value):
                original = copy.deepcopy(self.source)
                path = self.add_field(field, qualified=True)
                if value == 0:
                    self.replace_reference_number(field, path, value)
                self.assert_current_value_passes(field, value)
                self.source = original

    def test_consistent_compact_and_full_price_objects_remain_compatible(self) -> None:
        path = self.add_field("price", qualified=True)
        values = [
            {"approved_value": 50.0},
            {"approved_value": 50.0, "currency": "USD", "unit": "piece", "trade_term": "FOB"},
            core.read_json(path)["price"],
        ]
        for value in values:
            with self.subTest(value=value):
                self.assert_current_value_passes("price", value)

    def test_consistent_full_moq_object_remains_compatible(self) -> None:
        self.add_field("minimum_order_quantity", qualified=True)
        self.assert_current_value_passes("minimum_order_quantity", {"value": 10.0, "unit": "piece"})

    def test_moq_object_conflicting_unit_remains_blocked(self) -> None:
        self.add_field("minimum_order_quantity", qualified=True)
        self.replace_value("minimum_order_quantity", {"value": 10, "unit": "set"})
        self.assert_value_blocked("minimum_order_quantity")

    def test_nonnumeric_and_nonfinite_commercial_values_fail_closed(self) -> None:
        for field in ("price", "minimum_order_quantity"):
            original = copy.deepcopy(self.source)
            self.add_field(field, qualified=True)
            for value in ("50", [50], float("nan"), float("inf"), float("-inf"), False):
                with self.subTest(field=field, value=value):
                    self.replace_value(field, value)
                    self.assert_value_blocked(field)
            self.source = original

    def test_normal_compiler_rejects_boolean_scalar_and_nested_values(self) -> None:
        for field, value in (("price", True), ("minimum_order_quantity", True),
                             ("price", {"approved_value": True}),
                             ("minimum_order_quantity", {"value": True, "unit": "piece"})):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(compile_approval.CompileBlocked, "commercial value mismatch"):
                    self.normal_commercial_case(field, value, number=1)

    def test_normal_compiler_rejects_each_conflicting_price_context(self) -> None:
        for key, conflicting in (("currency", "CNY"), ("unit", "set"), ("trade_term", "EXW")):
            with self.subTest(key=key):
                value = {"approved_value": 50, "currency": "USD", "unit": "piece", "trade_term": "FOB"}
                value[key] = conflicting
                with self.assertRaisesRegex(compile_approval.CompileBlocked, "commercial value mismatch"):
                    self.normal_commercial_case("price", value)

    def test_normal_compiler_preserves_scalar_and_consistent_object_compatibility(self) -> None:
        for field, value in (
            ("price", 50.0), ("minimum_order_quantity", 10.0),
            ("price", {"approved_value": 50.0}),
            ("price", {"approved_value": 50.0, "currency": "USD", "unit": "piece", "trade_term": "FOB"}),
            ("minimum_order_quantity", {"value": 10.0, "unit": "piece"}),
        ):
            with self.subTest(field=field, value=value):
                result = self.normal_commercial_case(field, value)
                self.assertEqual(result["status"], "PASS")
                self.assertFalse(result["publish_ready"])


class OverlaySealRegressionTests(unittest.TestCase):
    CONSUMED = (
        "01_date_evidence/base_catalog_binding.json", "02_overlay/record_disposition.jsonl",
        "03_audit/build_receipt.json", "03_audit/verification_report.json",
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="draft-overlay-contract-")
        self.workspace = Path(self.temp.name) / "合成工作区"
        base = self.workspace / "synthetic_archive"
        base.mkdir(parents=True)
        self.catalog = base / "catalog_index.jsonl"
        self.catalog.write_text('{"product_id":"123"}\n{"product_id":"456"}\n', encoding="utf-8")
        self.overlay = self.workspace / "synthetic_archive__record_disposition_overlay__20000101_000000"
        for name in ("01_date_evidence", "02_overlay", "03_audit"):
            (self.overlay / name).mkdir(parents=True)
        rows = [
            {"product_id": "123", "record_disposition": core.HISTORICAL_DISPOSITION, "allow_rag": True},
            {"product_id": "456", "record_disposition": "UNUSABLE_WASTE_RECORD", "allow_rag": False},
        ]
        rows[0]["synthetic_revision"] = "A"
        self.disposition = self.overlay / self.CONSUMED[1]
        self.disposition.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        write_json(self.overlay / self.CONSUMED[0], {
            "expected_count": 2, "cutoff": core.DEFAULT_CUTOFF.isoformat(),
            "catalog_index_bytes": self.catalog.stat().st_size, "catalog_index_sha256": core.sha256_file(self.catalog),
            "synthetic_revision": "A",
        })
        write_json(self.overlay / self.CONSUMED[2], {
            "status": "COMPLETE", "coverage_complete": True, "eligible_count": 1, "unusable_count": 1,
            "record_count": 2, "external_write_enabled": False, "online_write_performed": False,
            "original_catalog_unchanged": True, "overlay_root": str(self.overlay), "synthetic_revision": "A",
        })
        write_json(self.overlay / self.CONSUMED[3], {
            "status": "COMPLETE", "failures": [], "checks": {"synthetic_check": True}, "synthetic_revision": "A",
        })
        (self.overlay / "placeholder.txt").write_text("unrelated synthetic evidence\n", encoding="utf-8")
        self.manifest = self.overlay / "03_audit/derived_manifest.json"
        self.sidecar = self.manifest.with_suffix(".json.sha256")
        self.seal()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seal(self, relative_paths: tuple | list | None = None) -> None:
        paths = self.CONSUMED if relative_paths is None else relative_paths
        files = [{"relative_path": relative, "bytes": (self.overlay / relative).stat().st_size,
                  "sha256": core.sha256_file(self.overlay / relative)} for relative in paths]
        write_json(self.manifest, {"status": "COMPLETE", "file_count": len(files), "files": files})
        self.sidecar.write_text(core.sha256_file(self.manifest) + "  derived_manifest.json\n", encoding="utf-8")

    def test_complete_consumed_file_seal_passes(self) -> None:
        self.assertEqual(core.verify_overlay(self.workspace)["status"], "COMPLETE")

    def test_unrelated_file_seal_is_rejected(self) -> None:
        self.seal(["placeholder.txt"])
        with self.assertRaisesRegex(core.GuardError, "omits consumed evidence"):
            core.verify_overlay(self.workspace)

    def test_each_consumed_file_must_be_declared(self) -> None:
        for missing in self.CONSUMED:
            with self.subTest(missing=missing):
                self.seal([path for path in self.CONSUMED if path != missing])
                with self.assertRaisesRegex(core.GuardError, "omits consumed evidence"):
                    core.verify_overlay(self.workspace)

    def test_each_consumed_file_has_sha256_checked_even_without_size_change(self) -> None:
        for relative in self.CONSUMED:
            with self.subTest(relative=relative):
                path = self.overlay / relative
                before = path.read_bytes()
                manifest_hash = core.sha256_file(self.manifest)
                changed = before.replace(b'"synthetic_revision": "A"', b'"synthetic_revision": "B"')
                self.assertNotEqual(changed, before)
                self.assertEqual(len(changed), len(before))
                path.write_bytes(changed)
                with self.assertRaisesRegex(core.GuardError, "manifest hash mismatch"):
                    core.verify_overlay(self.workspace)
                self.assertEqual(core.sha256_file(self.manifest), manifest_hash)
                path.write_bytes(before)

    def test_each_consumed_file_has_size_checked(self) -> None:
        for relative in self.CONSUMED:
            with self.subTest(relative=relative):
                path = self.overlay / relative
                before = path.read_bytes()
                path.write_bytes(before + b"\n")
                with self.assertRaisesRegex(core.GuardError, "manifest byte count mismatch"):
                    core.verify_overlay(self.workspace)
                path.write_bytes(before)

    def test_external_catalog_remains_bound_by_sealed_binding(self) -> None:
        self.catalog.write_text('{"product_id":"999"}\n{"product_id":"456"}\n', encoding="utf-8")
        with self.assertRaisesRegex(core.GuardError, "catalog hash"):
            core.verify_overlay(self.workspace)

    def test_complete_seal_survives_non_ascii_relocation(self) -> None:
        relocated = Path(self.temp.name) / "迁移_ß_工作区"
        shutil.copytree(self.workspace, relocated)
        result = core.verify_overlay(relocated)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertTrue(result["relocation_rebound"])

    def test_manifest_sidecar_mutation_remains_rejected(self) -> None:
        self.sidecar.write_text("0" * 64 + "  derived_manifest.json\n", encoding="utf-8")
        with self.assertRaisesRegex(core.GuardError, "sidecar hash mismatch"):
            core.verify_overlay(self.workspace)


if __name__ == "__main__":
    unittest.main(verbosity=2)
