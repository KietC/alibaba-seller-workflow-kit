from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from compile_browser_action_plan import (
    PlanCompileError,
    _approved_value_operations,
    compile_action_plan,
)
from draft_core import canonical_json, sha256_bytes, sha256_file
import test_action_plan as action_plan_fixture
import test_compile_approval as approval_fixture
from compile_approval import compile_approval


class PlanWriteApprovalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the canonical synthetic manifests without inheriting/rerunning
        # ActionPlanCompilerTests as test cases.
        self.fixture = action_plan_fixture.ActionPlanCompilerTests(
            methodName="test_atomic_writer_refuses_overwrite"
        )
        self.fixture.setUp()
        self.paths = self.fixture.paths

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def compile(self, *, write_approval_path: Path | None = None) -> dict[str, Any]:
        return compile_action_plan(
            approved_source_manifest_path=self.paths["source"],
            approved_asset_manifest_path=self.paths["asset"],
            run_spec_path=self.paths["run"],
            write_approval_path=write_approval_path or self.paths["write_approval"],
            field_registry_path=self.paths["registry"],
            endpoint_policy_path=self.paths["policy"],
            adapter_path=self.paths["adapter"],
        )

    def load_approval(self) -> dict[str, Any]:
        return json.loads(self.paths["write_approval"].read_text(encoding="utf-8"))

    def save_approval(self, approval: dict[str, Any]) -> None:
        action_plan_fixture.write_json(self.paths["write_approval"], approval)

    def test_valid_independent_approval_is_bound_into_save_operation(self) -> None:
        plan = self.compile()
        approval = self.load_approval()
        binding = plan["bindings"]["write_approval"]
        save = plan["phases"][4]["operations"][0]

        self.assertEqual(save["operation"], "SAVE_DRAFT")
        self.assertEqual(binding["sha256"], sha256_file(self.paths["write_approval"]))
        self.assertEqual(binding["approval_id"], approval["approval_id"])
        self.assertEqual(binding["run_id"], plan["target"]["run_id"])
        self.assertEqual(binding["target_model"], plan["target"]["model"])
        self.assertEqual(binding["template_product_id"], plan["target"]["template_product_id"])
        self.assertEqual(save["approval"]["write_approval_sha256"], binding["sha256"])
        self.assertEqual(save["approval"]["expires_at"], approval["expires_at"])
        self.assertTrue(plan["runtime_requirements"]["write_approval_must_be_unexpired_at_execution"])
        self.assertTrue(plan["invariants"]["save_draft_requires_valid_write_approval"])

    def test_missing_write_approval_cannot_compile_a_save(self) -> None:
        missing = self.paths["write_approval"].with_name("missing-write-approval.json")
        with self.assertRaises(FileNotFoundError):
            self.compile(write_approval_path=missing)

    def test_expired_write_approval_is_rejected(self) -> None:
        approval = self.load_approval()
        approval["approved_at"] = "2025-01-01T00:00:00Z"
        approval["expires_at"] = "2025-01-02T00:00:00Z"
        self.save_approval(approval)
        with self.assertRaisesRegex(PlanCompileError, "write approval is expired"):
            self.compile()

    def test_future_approval_is_rejected(self) -> None:
        approval = self.load_approval()
        approval["approved_at"] = "2098-01-01T00:00:00Z"
        approval["expires_at"] = "2099-01-01T00:00:00Z"
        self.save_approval(approval)
        with self.assertRaisesRegex(PlanCompileError, "approved_at is in the future"):
            self.compile()

    def test_target_run_and_template_bindings_are_each_enforced(self) -> None:
        cases = (
            ("target_model", "OTHER", "target model mismatch"),
            ("run_id", "other-run", "run id mismatch"),
            ("template_product_id", "999999", "template product id mismatch"),
        )
        original = self.load_approval()
        for key, value, expected in cases:
            with self.subTest(key=key):
                approval = json.loads(json.dumps(original))
                approval[key] = value
                self.save_approval(approval)
                with self.assertRaisesRegex(PlanCompileError, expected):
                    self.compile()

    def test_each_input_hash_is_covered_by_write_approval(self) -> None:
        approval = self.load_approval()
        for key in sorted(approval["bindings"]):
            with self.subTest(binding=key):
                changed = json.loads(json.dumps(approval))
                changed["bindings"][key] = "0" * 64
                self.save_approval(changed)
                with self.assertRaisesRegex(PlanCompileError, f"binding mismatch: {key}"):
                    self.compile()

    def test_write_policy_cannot_enable_direct_or_final_write(self) -> None:
        for key in ("direct_endpoint_write_enabled", "final_write_enabled"):
            with self.subTest(key=key):
                approval = self.load_approval()
                approval["write_policy"][key] = True
                self.save_approval(approval)
                with self.assertRaisesRegex(PlanCompileError, "draft write approval schema validation failed"):
                    self.compile()


class FieldTargetResolutionTests(unittest.TestCase):
    @staticmethod
    def source_manifest(field: str, value: Any, *, write_target: dict[str, Any] | None = None) -> dict[str, Any]:
        decision = {
            "value": value,
            "value_sha256": sha256_bytes(canonical_json(value).encode("utf-8")),
            "source_id": "facts",
            "decision_id": "decision-1",
        }
        if write_target is not None:
            decision["write_target"] = write_target
        return {
            "sources": [
                {
                    "source_id": "facts",
                    "decision_id": "decision-1",
                    "approved_fields": [field],
                }
            ],
            "approved_field_values": {field: decision},
        }

    def test_approved_price_maps_only_to_trade_price_registry_field(self) -> None:
        source = self.source_manifest("price", {"currency": "USD", "value": 88})
        registry = {"trade_price": {"logical_name": "trade_price", "section": "TRADE"}}
        operations = _approved_value_operations(source, registry, set())
        self.assertEqual(len(operations), 1)
        self.assertEqual(
            operations[0]["target"],
            {
                "kind": "FIELD_REGISTRY",
                "logical_name": "trade_price",
                "section": "TRADE",
                "runtime_key": None,
            },
        )
        self.assertEqual(operations[0]["locator"]["adapter_ref"], "field.registry_runtime")

    def test_unknown_ordinary_field_does_not_fall_back_to_category_property(self) -> None:
        source = self.source_manifest("mystery_claim", "unsafe fallback")
        with self.assertRaisesRegex(PlanCompileError, "not explicitly approved as a category property"):
            _approved_value_operations(source, {}, set())

    def test_explicit_category_property_is_allowed_without_runtime_key(self) -> None:
        source = self.source_manifest(
            "spray_valve",
            "SUS304 dual-function",
            write_target={
                "kind": "CATEGORY_PROPERTY",
                "approved_property_name": "Spray Valve",
            },
        )
        operations = _approved_value_operations(source, {}, set())
        self.assertEqual(operations[0]["target"]["kind"], "CATEGORY_PROPERTY_RUNTIME_KEY")
        self.assertEqual(operations[0]["target"]["approved_property_name"], "Spray Valve")
        self.assertIsNone(operations[0]["target"]["runtime_key"])

    def test_price_cannot_be_redirected_to_category_property(self) -> None:
        source = self.source_manifest(
            "price",
            88,
            write_target={"kind": "CATEGORY_PROPERTY", "approved_property_name": "Price"},
        )
        registry = {"trade_price": {"logical_name": "trade_price", "section": "TRADE"}}
        with self.assertRaisesRegex(PlanCompileError, "cannot be redirected"):
            _approved_value_operations(source, registry, set())


class ApprovalTargetPropagationTests(unittest.TestCase):
    def test_explicit_category_target_survives_approval_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = approval_fixture.ApprovalFixture(Path(temporary) / "approval-target")
            decision = fixture.partial_decision_document()
            decision["approved_fields"]["body_material"]["write_target"] = {
                "kind": "CATEGORY_PROPERTY",
                "approved_property_name": "Material",
            }
            fixture.write_partial_decision(decision)
            compile_approval(fixture.candidate, fixture.decision, fixture.output)
            manifest = json.loads(
                (fixture.output / "approved_source_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["approved_field_values"]["body_material"]["write_target"],
                {"kind": "CATEGORY_PROPERTY", "approved_property_name": "Material"},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
