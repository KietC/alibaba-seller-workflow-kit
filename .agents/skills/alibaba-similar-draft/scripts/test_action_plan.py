from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from compile_browser_action_plan import (
    DEFAULT_ADAPTER,
    PlanCompileError,
    compile_action_plan,
    write_json_atomic_new,
)
from draft_core import approved_asset_sequence_sha256, canonical_json, sha256_bytes, sha256_file
from dynamic_schema_gate import validate_dynamic_schema_capture
from dynamic_schema_test_support import build_official_dynamic_schema


SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SCRIPT_DIR.parent / "references" / "schemas" / "browser-action-plan.schema.json"


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


class ActionPlanCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "迁移_测试_ß" / "素材"
        self.root.mkdir(parents=True)
        self.paths = self._build_fixture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_fixture(self) -> dict[str, Path]:
        timestamp = "2026-08-05T12:00:00Z"
        candidate_hash = "1" * 64
        schema_bundle_hash = "2" * 64
        inventory_hash = "3" * 64
        usage_policy = {
            "allow_rag": True,
            "fact_extraction": True,
            "publish_draft": True,
            "auto_listing": False,
            "external_disclosure": False,
            "final_write": False,
        }

        registry = {
            "schema_version": "alibaba.field-registry.v1",
            "registry_version": "synthetic-1",
            "registry_id": "synthetic-registry",
            "policy": {"final_write_enabled": False, "final_write_count_limit": 0},
            "fields": [
                {
                    "logical_name": "product_title",
                    "section": "BASIC_INFO",
                    "field_class": "DATA",
                    "clear_before_fill": True,
                },
                {
                    "logical_name": "category_properties",
                    "section": "CATEGORY_PROPERTIES",
                    "field_class": "CATEGORY_DYNAMIC",
                    "clear_before_fill": True,
                },
                {
                    "logical_name": "main_images",
                    "section": "MEDIA",
                    "field_class": "ASSET",
                    "clear_before_fill": True,
                },
                {
                    "logical_name": "trade_price",
                    "section": "TRADE",
                    "field_class": "DATA",
                    "clear_before_fill": True,
                },
                {
                    "logical_name": "minimum_order_quantity",
                    "section": "TRADE",
                    "field_class": "DATA",
                    "clear_before_fill": True,
                },
            ],
        }
        registry_path = self.root / "字段注册表.json"
        write_json(registry_path, registry)
        registry_hash = sha256_file(registry_path)

        endpoint_policy = {
            "schema_version": "alibaba.endpoint-policy.v1",
            "default_decision": "DENY",
            "final_write_enabled": False,
            "rules": [
                {
                    "id": "submit-hard-deny",
                    "classification": "FINAL_WRITE",
                    "methods": ["GET", "POST", "UNKNOWN"],
                    "evidence_state": "HARD_DENY",
                    "url_regex": "submit|publish",
                }
            ],
        }
        endpoint_policy_path = self.root / "端点策略.json"
        write_json(endpoint_policy_path, endpoint_policy)
        endpoint_policy_hash = sha256_file(endpoint_policy_path)

        fact_path = self.root / "产品事实_水龙头.json"
        fact_path.write_text('{"model":"MODEL-003","材料":"SUS304"}\n', encoding="utf-8")
        fact_fields = ["product_title", "model", "body_mount", "hole_size", "height", "body_material", "spray_valve"]

        image_bytes = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c6360000000020001e221bc330000000049454e44ae426082"
        )
        asset_paths: list[Path] = []
        for index in range(1, 5):
            path = self.root / f"主图_{index}.png"
            path.write_bytes(image_bytes + bytes([index]))
            asset_paths.append(path)

        def source_row(source_id: str, role: str, path: Path, decision_id: str, fields: list[str]) -> dict[str, Any]:
            return {
                "source_id": source_id,
                "source_role": role,
                "record_disposition": "APPROVED_INTERNAL_SOURCE",
                "path": str(path.resolve()),
                "media_type": "application/json" if path.suffix == ".json" else "image/png",
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "decision_id": decision_id,
                "approved_fields": fields,
                "usage_policy": usage_policy,
            }

        sources = [source_row("facts", "PRODUCT_FACT", fact_path, "facts-decision", fact_fields)]
        for index, path in enumerate(asset_paths, 1):
            sources.append(
                source_row(
                    f"asset-origin-{index}",
                    "APPROVED_ASSET_ORIGIN",
                    path,
                    f"asset-source-decision-{index}",
                    [f"main_image.asset_{index}"],
                )
            )

        fact_values = {
            "product_title": "SellerKit MODEL-003 Commercial Pre-Rinse Faucet",
            "model": "MODEL-003",
            "body_mount": "Single Hole Deck Mount",
            "hole_size": "Φ32~35 mm",
            "height": "1135 mm",
            "body_material": "SUS304",
            "spray_valve": "SUS304 Dual-function MODEL-022",
        }
        approved_values = {
            field: {
                "value": value,
                "value_sha256": sha256_bytes(canonical_json(value).encode("utf-8")),
                "source_id": "facts",
                "decision_id": "facts-decision",
                **(
                    {}
                    if field == "product_title"
                    else {
                        "write_target": {
                            "kind": "CATEGORY_PROPERTY",
                            "approved_property_name": field,
                        }
                    }
                ),
            }
            for field, value in fact_values.items()
        }
        dynamic_schema_path, _, _ = build_official_dynamic_schema(
            self.root,
            fields=[
                {
                    "logical_name": field,
                    "observed_label": field.replace("_", " ").title(),
                    "required": field in {"product_title", "model"},
                }
                for field in (*fact_fields, "price", "minimum_order_quantity")
            ],
            run_id="run-synthetic",
            target_model="MODEL-003",
            template_product_id="100000000014",
            category_id="100007076",
            product_type="NORMAL",
            stem="action-plan",
        )
        dynamic_schema_binding = validate_dynamic_schema_capture(
            dynamic_schema_path,
            expected_target_model="MODEL-003",
            expected_template_product_id="100000000014",
            expected_run_id="run-synthetic",
        )
        dynamic_schema_binding["decision_id"] = "dynamic-schema-action-plan-decision"
        source_manifest = {
            "schema_version": "alibaba.approved-source-manifest.v1",
            "manifest_id": "source-synthetic",
            "run_id": "run-synthetic",
            "target_model": "MODEL-003",
            "generated_at": timestamp,
            "approval": {
                "status": "APPROVED",
                "explicit": True,
                "scope": "SIMILAR_DRAFT_SOURCE_USE_ONLY",
                "origin": "HUMAN_REVIEWED_CANDIDATE_PACKAGE",
                "decision_id": "source-package-decision",
                "approved_by": "synthetic-reviewer",
                "approved_at": timestamp,
                "approval_package_sha256": candidate_hash,
            },
            "bindings": {
                "candidate_package_sha256": candidate_hash,
                "source_inventory_sha256": inventory_hash,
                "field_registry_sha256": registry_hash,
                "schema_bundle_sha256": schema_bundle_hash,
                "target_model_normalized": "MODEL-003",
                "template_product_id": "100000000014",
            },
            "dynamic_schema": dynamic_schema_binding,
            "source_count": len(sources),
            "rejected_source_count": 0,
            "sources": sources,
            "approved_field_values": approved_values,
            "draft_field_policy": {
                "mode": "PARTIAL_DRAFT_FILL_APPROVED_ONLY",
                "explicit_user_instruction_sha256": candidate_hash,
                "blocked_fields": ["price", "minimum_order_quantity", "category_properties.warranty"],
                "save_incomplete_draft_allowed": True,
                "publish_ready_claim_allowed": False,
                "final_write_enabled": False,
            },
            "write_policy": {
                "candidate_external_write_enabled": False,
                "draft_save_requires_separate_approval": True,
                "final_write_enabled": False,
                "raw_disposition_may_grant_approval": False,
            },
        }
        source_manifest_path = self.root / "获批来源清单.json"
        write_json(source_manifest_path, source_manifest)

        assets: list[dict[str, Any]] = []
        for index, path in enumerate(asset_paths, 1):
            assets.append(
                {
                    "asset_id": f"main-image-{index}",
                    "role": "MAIN_IMAGE",
                    "upload_order": index,
                    "path": str(path.resolve()),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "media_type": "image/png",
                    "record_disposition": "APPROVED_INTERNAL_SOURCE",
                    "source_id": f"asset-origin-{index}",
                    "source_decision_id": f"asset-source-decision-{index}",
                    "product_identity": {
                        "target_model": "MODEL-003",
                        "match_status": "MATCHED",
                        "match_method": "HASH_BOUND_DERIVATION",
                        "decision_id": f"identity-decision-{index}",
                    },
                    "dimensions": {"width": 1, "height": 1},
                    "compliance": {
                        "format_ok": True,
                        "size_ok": True,
                        "dimensions_ok": True,
                        "duration_ok": None,
                        "identity_ok": True,
                        "policy_ok": True,
                        "upload_ready": True,
                    },
                    "upload_approval": {
                        "approved": True,
                        "decision_id": f"upload-decision-{index}",
                        "approved_by": "synthetic-reviewer",
                        "approved_at": timestamp,
                    },
                    "derived_from_template": False,
                    "derived_from_historical_record": False,
                }
            )
        asset_manifest = {
            "schema_version": "alibaba.approved-asset-manifest.v1",
            "manifest_id": "asset-synthetic",
            "run_id": "run-synthetic",
            "target_model": "MODEL-003",
            "generated_at": timestamp,
            "approval": {
                "status": "APPROVED",
                "explicit": True,
                "scope": "SIMILAR_DRAFT_ASSET_USE_ONLY",
                "origin": "HUMAN_REVIEWED_CANDIDATE_PACKAGE",
                "decision_id": "asset-package-decision",
                "approved_by": "synthetic-reviewer",
                "approved_at": timestamp,
                "approval_package_sha256": candidate_hash,
            },
            "bindings": {
                "approved_source_manifest_sha256": sha256_file(source_manifest_path),
                "candidate_package_sha256": candidate_hash,
                "field_registry_sha256": registry_hash,
                "schema_bundle_sha256": schema_bundle_hash,
                "target_model_normalized": "MODEL-003",
            },
            "asset_count": len(assets),
            "main_image_count": 4,
            "main_video_count": 0,
            "asset_sequence_sha256": approved_asset_sequence_sha256(assets),
            "assets": assets,
            "rules": {
                "main_image_minimum_count": 4,
                "main_image_maximum_count": 6,
                "main_video_maximum_count": 1,
                "unique_asset_ids": True,
                "unique_upload_orders": True,
                "hash_recheck_before_upload": True,
                "template_media_may_be_inherited": False,
                "runtime_count_verification_required": True,
            },
            "write_policy": {
                "candidate_external_write_enabled": False,
                "upload_requires_current_probe_receipt": True,
                "draft_save_requires_separate_approval": True,
                "final_write_enabled": False,
            },
        }
        asset_manifest_path = self.root / "获批素材清单.json"
        write_json(asset_manifest_path, asset_manifest)

        run_spec = {
            "schema_version": "sellerkit.similar_draft_run_spec.v1",
            "generated_at": "2026-08-01T00:00:00Z",
            "status": "BLOCKED_REVIEW_REQUIRED",
            "run_kind": "CREATE_SIMILAR_DRAFT",
            "run_id": "run-synthetic",
            "target_model": "MODEL-003",
            "template_product_id": "100000000014",
            "mode": "save_draft",
            "approval_observed": False,
            "external_write_enabled": False,
            "direct_endpoint_write_enabled": False,
            "final_write_enabled": False,
            "inherited_content_must_be_cleared": True,
            "max_draft_save_count": 1,
            "template_scope": "FORM_STRUCTURE_ONLY",
            "required_preflight_status": "PASS",
            "required_probe_status": "PASS_ZERO_WRITE",
            "stop_state_before_final_submit": "DRAFT_SAVED_FINAL_REVIEW_READY",
            "endpoint_policy": {
                "path": str(endpoint_policy_path.resolve()),
                "sha256": endpoint_policy_hash,
                "policy_mode": "FAIL_CLOSED_AUDIT_ONLY",
            },
            "workspace_profile": {
                "profile_id": "synthetic-test-profile",
                "path": str((self.root / "synthetic-profile.json").resolve()),
                "sha256": "e" * 64,
            },
        }
        run_spec_path = self.root / "运行规格.json"
        write_json(run_spec_path, run_spec)

        write_approval = {
            "schema_version": "sellerkit.alibaba.draft-write-approval.v1",
            "approval_id": "write-approval-synthetic",
            "decision_id": "write-decision-synthetic",
            "status": "APPROVED",
            "explicit": True,
            "scope": "SAVE_ONE_DRAFT_VISIBLE_UI_ONLY",
            "approved_by": "synthetic-reviewer",
            "approved_at": "2026-08-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "target_model": "MODEL-003",
            "run_id": "run-synthetic",
            "template_product_id": "100000000014",
            "bindings": {
                "approved_source_manifest_sha256": sha256_file(source_manifest_path),
                "approved_asset_manifest_sha256": sha256_file(asset_manifest_path),
                "run_spec_sha256": sha256_file(run_spec_path),
                "field_registry_sha256": registry_hash,
                "endpoint_policy_sha256": endpoint_policy_hash,
                "adapter_sha256": sha256_file(DEFAULT_ADAPTER),
            },
            "write_policy": {
                "visible_ui_only": True,
                "maximum_draft_save_count": 1,
                "direct_endpoint_write_enabled": False,
                "final_write_enabled": False,
            },
        }
        write_approval_path = self.root / "write_approval.json"
        write_json(write_approval_path, write_approval)

        return {
            "source": source_manifest_path,
            "asset": asset_manifest_path,
            "run": run_spec_path,
            "write_approval": write_approval_path,
            "registry": registry_path,
            "policy": endpoint_policy_path,
            "adapter": DEFAULT_ADAPTER,
            "asset_1": asset_paths[0],
        }

    def compile(self) -> dict[str, Any]:
        return compile_action_plan(
            approved_source_manifest_path=self.paths["source"],
            approved_asset_manifest_path=self.paths["asset"],
            run_spec_path=self.paths["run"],
            write_approval_path=self.paths["write_approval"],
            field_registry_path=self.paths["registry"],
            endpoint_policy_path=self.paths["policy"],
            adapter_path=self.paths["adapter"],
        )

    def test_partial_plan_is_hash_bound_fail_closed_and_non_ascii_safe(self) -> None:
        plan = self.compile()
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], plan["schema_version"])
        self.assertTrue(set(schema["required"]) <= set(plan))

        self.assertEqual(plan["source_profile"], "PARTIAL_DRAFT_FILL_APPROVED_ONLY")
        self.assertEqual(plan["quality"]["terminal_quality"], "REVIEW_REQUIRED")
        self.assertFalse(plan["quality"]["publish_ready"])
        triplet = dict(plan["bindings"]["three_party"])
        observed_binding = triplet.pop("binding_sha256")
        self.assertEqual(observed_binding, sha256_bytes(canonical_json(triplet).encode("utf-8")))
        self.assertEqual(plan["phases"][0]["name"], "CLEAR_INHERITED_FIELDS")
        self.assertEqual(plan["phases"][1]["name"], "WRITE_APPROVED_FIELD_VALUES")

        source = json.loads(self.paths["source"].read_text(encoding="utf-8"))
        write_operations = plan["phases"][1]["operations"]
        planned_names = {
            operation["target"].get("approved_property_name", operation["target"]["logical_name"])
            for operation in write_operations
        }
        self.assertEqual(planned_names, set(source["approved_field_values"]))
        self.assertTrue(
            all(
                operation["target"]["kind"] == "CATEGORY_PROPERTY_RUNTIME_KEY"
                and operation["target"]["runtime_key"] is None
                for operation in write_operations
                if operation["target"].get("approved_property_name") == "model"
            )
        )
        self.assertTrue(set(plan["blocked_fields"]).isdisjoint(planned_names))

        uploads = plan["phases"][2]["operations"]
        self.assertEqual([row["target"]["upload_order"] for row in uploads], [1, 2, 3, 4])
        self.assertIn("迁移_测试_ß", uploads[0]["asset"]["path"])
        for row in uploads:
            asset_path = Path(row["asset"]["path"])
            self.assertEqual(asset_path.stat().st_size, row["asset"]["bytes"])
            self.assertEqual(sha256_file(asset_path), row["asset"]["sha256"])

        save = plan["phases"][4]["operations"][0]
        self.assertTrue(save["visible_ui_required"])
        self.assertFalse(save["direct_endpoint_call_allowed"])
        self.assertTrue(all(row["decision"] == "HARD_DENY" for row in plan["forbidden_actions"]))
        self.assertTrue(all(not row["visible_ui_click_allowed"] for row in plan["forbidden_actions"]))
        self.assertNotIn("SUBMIT", {operation["operation"] for phase in plan["phases"] for operation in phase["operations"]})
        self.assertFalse(plan["compiler"]["browser_or_network_executed"])

    def test_unapproved_value_is_rejected(self) -> None:
        source = json.loads(self.paths["source"].read_text(encoding="utf-8"))
        value = "must-not-enter-plan"
        source["approved_field_values"]["unapproved_claim"] = {
            "value": value,
            "value_sha256": sha256_bytes(canonical_json(value).encode("utf-8")),
            "source_id": "facts",
            "decision_id": "facts-decision",
        }
        write_json(self.paths["source"], source)
        asset = json.loads(self.paths["asset"].read_text(encoding="utf-8"))
        asset["bindings"]["approved_source_manifest_sha256"] = sha256_file(self.paths["source"])
        write_json(self.paths["asset"], asset)
        write_approval = json.loads(self.paths["write_approval"].read_text(encoding="utf-8"))
        write_approval["bindings"]["approved_source_manifest_sha256"] = sha256_file(self.paths["source"])
        write_approval["bindings"]["approved_asset_manifest_sha256"] = sha256_file(self.paths["asset"])
        write_json(self.paths["write_approval"], write_approval)
        with self.assertRaisesRegex(PlanCompileError, "outside approved source scope"):
            self.compile()

    def test_asset_hash_drift_is_rejected(self) -> None:
        with self.paths["asset_1"].open("ab") as stream:
            stream.write(b"drift")
        with self.assertRaisesRegex(PlanCompileError, "hash/size mismatch|hash/size drift"):
            self.compile()

    def test_final_submit_enablement_is_rejected(self) -> None:
        run_spec = json.loads(self.paths["run"].read_text(encoding="utf-8"))
        run_spec["final_write_enabled"] = True
        write_json(self.paths["run"], run_spec)
        with self.assertRaisesRegex(PlanCompileError, "final_write_enabled must be false"):
            self.compile()

    def test_unsafe_final_write_policy_is_rejected(self) -> None:
        policy = json.loads(self.paths["policy"].read_text(encoding="utf-8"))
        policy["rules"][0]["decision"] = "ALLOW"
        write_json(self.paths["policy"], policy)
        run_spec = json.loads(self.paths["run"].read_text(encoding="utf-8"))
        run_spec["endpoint_policy"]["sha256"] = sha256_file(self.paths["policy"])
        write_json(self.paths["run"], run_spec)
        with self.assertRaisesRegex(PlanCompileError, "FINAL_WRITE rule is unsafe"):
            self.compile()

    def test_atomic_writer_refuses_overwrite(self) -> None:
        plan = self.compile()
        output = self.root / "动作计划.json"
        first_hash = write_json_atomic_new(output, plan)
        self.assertEqual(first_hash, sha256_file(output))
        original = output.read_bytes()
        with self.assertRaisesRegex(PlanCompileError, "refusing to overwrite"):
            write_json_atomic_new(output, copy.deepcopy(plan))
        self.assertEqual(original, output.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
