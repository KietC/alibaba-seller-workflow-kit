from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


SKILL_DIR = Path(__file__).resolve().parent.parent
REFERENCE_DIR = SKILL_DIR / "references"
SCHEMA_DIR = REFERENCE_DIR / "schemas"
REGISTRY_PATH = REFERENCE_DIR / "publish-field-registry.v1.json"
SCHEMA_PATHS = sorted(SCHEMA_DIR.glob("*.schema.json"))
SCRIPT_DIR = SKILL_DIR / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from draft_core import (  # noqa: E402
    GuardError,
    approved_asset_sequence_sha256,
    canonical_json,
    sha256_bytes,
    sha256_file,
    validate_approved_manifest,
    verify_fill_receipt,
    verify_probe,
)
from state_store import abort_checkpoint_no_write, checkpoint_hash, initialize_checkpoint, transition_checkpoint  # noqa: E402
from dynamic_schema_gate import validate_dynamic_schema_capture  # noqa: E402
from dynamic_schema_test_support import build_official_dynamic_schema  # noqa: E402


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ContractSchemaTests(unittest.TestCase):
    def test_all_schemas_are_valid_draft_2020_12(self) -> None:
        expected = {
            "approval-decision.schema.json",
            "approval-compile-receipt.schema.json",
            "approved-asset-manifest.schema.json",
            "approved-commercial-decision.schema.json",
            "approved-source-manifest.schema.json",
            "browser-action-plan.schema.json",
            "browser-capture-material.schema.json",
            "browser-observation-capture.schema.json",
            "checkpoint.schema.json",
            "checkpoint-transition-evidence.schema.json",
            "draft-write-approval.schema.json",
            "draft-save-attempt-claim-receipt.schema.json",
            "draft-save-activation-receipt.schema.json",
            "draft-save-activation-request.schema.json",
            "draft-save-body-semantics.schema.json",
            "draft-save-result-evidence.schema.json",
            "dynamic-publish-schema.schema.json",
            "field-registry.schema.json",
            "fill-execution.schema.json",
            "fill-verification-receipt.schema.json",
            "inventory-delta-verification-receipt.schema.json",
            "localized-field-target.schema.json",
            "localized-counter-expectation.schema.json",
            "localized-counter-verification-receipt.schema.json",
            "next-product-checklist.schema.json",
            "next-product-checklist-verification-receipt.schema.json",
            "official-schema-xml-normalized.schema.json",
            "official-schema-current-values.schema.json",
            "official-schema-values-verification-receipt.schema.json",
            "post-refresh-verification.schema.json",
            "pre-save-readback.schema.json",
            "pre-save-verification-receipt.schema.json",
            "probe-receipt.schema.json",
            "probe-read-activation-receipt.schema.json",
            "probe-read-activation-request.schema.json",
            "probe-verification-receipt.schema.json",
            "product-inventory-snapshot.schema.json",
            "run-receipt.schema.json",
            "run-spec.schema.json",
            "similar-draft.schema.json",
            "workspace-profile.schema.json",
            "visible-ui-draft-save-event.schema.json",
        }
        self.assertEqual({path.name for path in SCHEMA_PATHS}, expected)
        ids: list[str] = []
        for path in SCHEMA_PATHS:
            schema = read_json(path)
            Draft202012Validator.check_schema(schema)
            ids.append(str(schema.get("$id") or ""))
        self.assertTrue(all(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_workspace_profile_matches_schema(self) -> None:
        profile = read_json(SKILL_DIR / "profiles" / "example-profile.v1.json")
        schema = read_json(SCHEMA_DIR / "workspace-profile.schema.json")
        Draft202012Validator(schema).validate(profile)

    def test_zero_write_probe_verifier_emitter_matches_schema(self) -> None:
        policy = read_json(REFERENCE_DIR / "endpoint-policy.v1.json")
        probe = {
            "current_url": (
                "https://post.alibaba.com/product/publish.htm"
                "?pubType=similarPost&itemId=1234567890&behavior=copyNew"
            ),
            "target_model": "PORTABLE100",
            "template_product_id": "1234567890",
            "captcha_detected": False,
            "network_guard_installed_before_navigation": True,
            "cookie_or_storage_read": False,
            "page_fingerprint": "f" * 64,
            "online_count_before": 10,
            "online_count_after": 10,
            "draft_count_before": 2,
            "draft_count_after": 2,
            "requests": [],
        }
        receipt = verify_probe(probe, policy)
        self.assertEqual(receipt["status"], "BLOCKED_PROBE")
        self.assertTrue(any("strict probe schema_version" in error for error in receipt["errors"]))
        schema = read_json(SCHEMA_DIR / "probe-verification-receipt.schema.json")
        Draft202012Validator(schema).validate(receipt)

    def test_fill_verifier_emitter_matches_schema(self) -> None:
        receipt = verify_fill_receipt(
            {
                "target_model": "PORTABLE100",
                "template_product_id": "1234567890",
                "field_readbacks": {"model": "PORTABLE100", "material": "SUS304"},
                "forbidden_template_values": ["1234567890", "OLDMODEL", "$10-99"],
                "inherited_content_cleared": True,
                "template_video_present": False,
                "final_submit_triggered": False,
                "draft_save_count": 1,
            }
        )
        self.assertEqual(receipt["status"], "FAIL")
        self.assertTrue(any("post-refresh" in error for error in receipt["errors"]), receipt)
        schema = read_json(SCHEMA_DIR / "fill-verification-receipt.schema.json")
        Draft202012Validator(schema).validate(receipt)

    def test_checkpoint_emitter_matches_schema_and_requires_save_attempt_state(self) -> None:
        schema = read_json(SCHEMA_DIR / "checkpoint.schema.json")
        validator = Draft202012Validator(schema)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            checkpoint = initialize_checkpoint(
                run_dir,
                "PORTABLE100",
                "1234567890",
                {"input": "0" * 64},
            )
            validator.validate(checkpoint)
            checkpoint_path = run_dir / "checkpoint.json"
            self.assertEqual(checkpoint["save_attempt_count"], 0)
            self.assertIn("SAVE_ATTEMPTED", schema["$defs"]["state"]["enum"])
            aborted = abort_checkpoint_no_write(
                checkpoint_path,
                checkpoint["revision"],
                checkpoint_hash(checkpoint),
                "contract test stopped before all external writes",
            )
            validator.validate(aborted)
            self.assertEqual(aborted["state"], "ABORTED_NO_WRITE")
            with self.assertRaises(GuardError):
                transition_checkpoint(
                    checkpoint_path,
                    checkpoint["revision"],
                    checkpoint_hash(checkpoint),
                    "DRAFT_SAVED",
                    Path(temporary) / "missing-evidence.json",
                    draft_id="portable-draft-1",
                )

    def test_registry_matches_its_schema(self) -> None:
        registry = read_json(REGISTRY_PATH)
        schema = read_json(SCHEMA_DIR / "field-registry.schema.json")
        Draft202012Validator(schema).validate(registry)

    def test_registry_has_no_publish_state_or_concrete_dynamic_id(self) -> None:
        registry = read_json(REGISTRY_PATH)
        serialized = json.dumps(registry, ensure_ascii=False)
        self.assertNotIn("PUBLISHED", serialized)
        self.assertIsNone(re.search(r'(?<![A-Za-z0-9])p-\d+', serialized))
        self.assertFalse(registry["policy"]["final_write_enabled"])
        self.assertEqual(registry["policy"]["final_write_count_limit"], 0)

    def test_registry_names_are_unique_and_dangerous_actions_are_disabled(self) -> None:
        registry = read_json(REGISTRY_PATH)
        fields = [row["logical_name"] for row in registry["fields"]]
        actions = [row["logical_name"] for row in registry["actions"]]
        self.assertEqual(len(fields), len(set(fields)))
        self.assertEqual(len(actions), len(set(actions)))
        self.assertEqual(len(fields), 25)
        self.assertEqual(len(actions), 4)
        for action in registry["actions"]:
            self.assertFalse(action["default_enabled"])
            self.assertFalse(action["direct_endpoint_call_allowed"])
        final = next(row for row in registry["actions"] if row["classification"] == "FINAL_WRITE")
        self.assertFalse(final["can_enable_after_explicit_action_approval"])

    def test_canonical_approval_manifests_match_preflight_consumer(self) -> None:
        usage_policy = {
            "allow_rag": True,
            "fact_extraction": True,
            "publish_draft": True,
            "auto_listing": False,
            "external_disclosure": False,
            "final_write": False,
        }
        candidate_hash = "1" * 64
        registry_hash = sha256_file(REGISTRY_PATH)
        schema_bundle_hash = "2" * 64
        inventory_hash = "3" * 64
        timestamp = "2026-08-04T12:00:00Z"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dynamic_schema_path, _, _ = build_official_dynamic_schema(
                root,
                fields=[
                    {
                        "logical_name": field,
                        "observed_label": field.replace("_", " ").title(),
                        "required": field in {"product_title", "model"},
                    }
                    for field in (
                        "product_title",
                        "model",
                        "body_mount",
                        "hole_size",
                        "height",
                        "body_material",
                        "price",
                        "minimum_order_quantity",
                    )
                ],
                run_id="run-fixture",
                target_model="MODEL-003",
                template_product_id="100000000014",
                category_id="100007076",
                product_type="NORMAL",
                stem="contracts",
            )
            dynamic_schema_binding = validate_dynamic_schema_capture(
                dynamic_schema_path,
                expected_target_model="MODEL-003",
                expected_template_product_id="100000000014",
                expected_run_id="run-fixture",
            )
            dynamic_schema_binding["decision_id"] = "dynamic-schema-contract-decision"
            fact_path = root / "facts.json"
            fact_path.write_text('{"model":"MODEL-003"}\n', encoding="utf-8")
            commercial_path = root / "commercial.json"
            commercial = {
                "schema_version": "sellerkit.approved-commercial-decision.v1",
                "decision_id": "commercial-decision-1",
                "target_model": "MODEL-003",
                "status": "APPROVED",
                "approved_by": "fixture-reviewer",
                "approved_at": timestamp,
                "price": {
                    "currency": "USD",
                    "unit": "unit",
                    "trade_term": "FOB",
                    "pricing_method": "WEIGHTED_P25_P50_P75",
                    "candidate_product_ids": ["1001", "1002", "1003"],
                    "p25": 90,
                    "p50": 95,
                    "p75": 100,
                    "approved_value": 95,
                },
                "minimum_order_quantity": {"value": 50, "unit": "unit"},
                "historical_values_are_quote": False,
                "final_write_enabled": False,
            }
            commercial_path.write_text(json.dumps(commercial, sort_keys=True), encoding="utf-8")

            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000d49444154789c6360000000020001e221bc330000000049454e44ae426082"
            )
            asset_paths: list[Path] = []
            for index in range(1, 5):
                path = root / f"asset-{index}.png"
                path.write_bytes(png + bytes([index]))
                asset_paths.append(path)

            def source_row(source_id: str, role: str, path: Path, decision_id: str, fields: list[str]) -> dict:
                return {
                    "source_id": source_id,
                    "source_role": role,
                    "record_disposition": "APPROVED_INTERNAL_SOURCE",
                    "path": str(path),
                    "media_type": "application/json" if path.suffix == ".json" else "image/png",
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "decision_id": decision_id,
                    "approved_fields": fields,
                    "usage_policy": usage_policy,
                }

            sources = [
                source_row(
                    "facts",
                    "PRODUCT_FACT",
                    fact_path,
                    "facts-decision",
                    ["product_title", "model", "body_mount", "hole_size", "height", "body_material"],
                ),
                source_row(
                    "commercial",
                    "COMMERCIAL_DECISION",
                    commercial_path,
                    "commercial-decision-1",
                    ["price", "minimum_order_quantity"],
                ),
            ]
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

            source_manifest = {
                "schema_version": "alibaba.approved-source-manifest.v1",
                "manifest_id": "source-manifest-fixture",
                "run_id": "run-fixture",
                "target_model": "MODEL-003",
                "generated_at": timestamp,
                "approval": {
                    "status": "APPROVED",
                    "explicit": True,
                    "scope": "SIMILAR_DRAFT_SOURCE_USE_ONLY",
                    "origin": "HUMAN_REVIEWED_CANDIDATE_PACKAGE",
                    "decision_id": "package-source-decision",
                    "approved_by": "fixture-reviewer",
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
                "write_policy": {
                    "candidate_external_write_enabled": False,
                    "draft_save_requires_separate_approval": True,
                    "final_write_enabled": False,
                    "raw_disposition_may_grant_approval": False,
                },
            }
            source_manifest_path = root / "approved_source_manifest.json"
            source_manifest_path.write_text(json.dumps(source_manifest, sort_keys=True), encoding="utf-8")

            assets = []
            for index, path in enumerate(asset_paths, 1):
                assets.append(
                    {
                        "asset_id": f"main-image-{index}",
                        "role": "MAIN_IMAGE",
                        "upload_order": index,
                        "path": str(path),
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
                            "approved_by": "fixture-reviewer",
                            "approved_at": timestamp,
                        },
                        "derived_from_template": False,
                        "derived_from_historical_record": False,
                    }
                )

            asset_manifest = {
                "schema_version": "alibaba.approved-asset-manifest.v1",
                "manifest_id": "asset-manifest-fixture",
                "run_id": "run-fixture",
                "target_model": "MODEL-003",
                "generated_at": timestamp,
                "approval": {
                    "status": "APPROVED",
                    "explicit": True,
                    "scope": "SIMILAR_DRAFT_ASSET_USE_ONLY",
                    "origin": "HUMAN_REVIEWED_CANDIDATE_PACKAGE",
                    "decision_id": "package-asset-decision",
                    "approved_by": "fixture-reviewer",
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
            asset_manifest_path = root / "approved_asset_manifest.json"
            asset_manifest_path.write_text(json.dumps(asset_manifest, sort_keys=True), encoding="utf-8")

            Draft202012Validator(read_json(SCHEMA_DIR / "approved-commercial-decision.schema.json")).validate(commercial)
            Draft202012Validator(read_json(SCHEMA_DIR / "approved-source-manifest.schema.json")).validate(source_manifest)
            Draft202012Validator(read_json(SCHEMA_DIR / "approved-asset-manifest.schema.json")).validate(asset_manifest)
            receipt = validate_approved_manifest(
                source_manifest,
                asset_manifest,
                manifest_path=source_manifest_path,
                asset_manifest_path=asset_manifest_path,
            )
            self.assertEqual(receipt["status"], "PASS", receipt["errors"])
            self.assertEqual(receipt["approved_asset_count"], 4)
            self.assertGreaterEqual(receipt["approved_field_count"], 8)

            partial_source_manifest = json.loads(json.dumps(source_manifest))
            partial_source_manifest["manifest_id"] = "source-manifest-partial-fixture"
            partial_source_manifest["sources"] = [
                row for row in partial_source_manifest["sources"] if row["source_role"] != "COMMERCIAL_DECISION"
            ]
            partial_source_manifest["source_count"] = len(partial_source_manifest["sources"])
            partial_source_manifest["draft_field_policy"] = {
                "mode": "PARTIAL_DRAFT_FILL_APPROVED_ONLY",
                "explicit_user_instruction_sha256": candidate_hash,
                "blocked_fields": ["price", "minimum_order_quantity", "inventory"],
                "save_incomplete_draft_allowed": True,
                "publish_ready_claim_allowed": False,
                "final_write_enabled": False,
            }
            fact_values = {
                "product_title": "SellerKit MODEL-003 Commercial Pre-Rinse Faucet",
                "model": "MODEL-003",
                "body_mount": "Single Hole Deck Mount",
                "hole_size": "Φ32~35 mm",
                "height": "1135 mm",
                "body_material": "SUS304",
            }
            partial_source_manifest["approved_field_values"] = {
                field: {
                    "value": value,
                    "value_sha256": sha256_bytes(canonical_json(value).encode("utf-8")),
                    "source_id": "facts",
                    "decision_id": "facts-decision",
                }
                for field, value in fact_values.items()
            }
            source_manifest_path.write_text(json.dumps(partial_source_manifest, sort_keys=True), encoding="utf-8")
            partial_asset_manifest = json.loads(json.dumps(asset_manifest))
            partial_asset_manifest["manifest_id"] = "asset-manifest-partial-fixture"
            partial_asset_manifest["bindings"]["approved_source_manifest_sha256"] = sha256_file(source_manifest_path)
            Draft202012Validator(read_json(SCHEMA_DIR / "approved-source-manifest.schema.json")).validate(
                partial_source_manifest
            )
            Draft202012Validator(read_json(SCHEMA_DIR / "approved-asset-manifest.schema.json")).validate(
                partial_asset_manifest
            )
            partial_receipt = validate_approved_manifest(
                partial_source_manifest,
                partial_asset_manifest,
                manifest_path=source_manifest_path,
            )
            self.assertEqual(partial_receipt["status"], "PASS", partial_receipt["errors"])
            self.assertEqual(partial_receipt["draft_profile"], "PARTIAL_DRAFT_FILL_APPROVED_ONLY")

    def test_legacy_simplified_manifest_versions_are_rejected(self) -> None:
        receipt = validate_approved_manifest(
            {"schema_version": "sellerkit.approved_source_manifest.v1", "target_model": "MODEL-003"},
            {"schema_version": "sellerkit.approved_asset_manifest.v1", "assets": []},
        )
        self.assertEqual(receipt["status"], "BLOCKED_PREFLIGHT")
        self.assertIn("unsupported canonical source manifest schema", receipt["errors"])
        self.assertIn("unsupported canonical asset manifest schema", receipt["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
