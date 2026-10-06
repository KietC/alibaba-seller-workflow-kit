from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compile_approval import CompileBlocked, compile_approval  # noqa: E402
from draft_core import canonical_json, read_json, sha256_bytes, sha256_file  # noqa: E402
from dynamic_schema_gate import validate_dynamic_schema_capture  # noqa: E402
from dynamic_schema_test_support import build_official_dynamic_schema  # noqa: E402


TARGET = "测试型号-X7"
TEMPLATE = "998877665544"
RUN_ID = "run-审批-001"
APPROVED_FACT_FIELDS = (
    "product_title",
    "model",
    "body_mount",
    "hole_size",
    "height",
    "body_material",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def png_bytes(red: int, green: int, blue: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    raw = b"\x00" + bytes((red, green, blue))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class ApprovalFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dynamic_raw = root / "schema" / "approval-schema.xml"
        self.dynamic_official = root / "schema" / "approval-official-normalized.json"
        self.dynamic_schema = root / "schema" / "approval-current-schema.json"
        self.candidate = root / "候选审核包"
        self.assets = root / "产品素材"
        self.source = root / "事实来源" / "官网事实.txt"
        self.inventory = root / "证据" / "来源清单.json"
        self.decision = root / "人工审批决定.json"
        self.output = root / "已编译审批"
        self.candidate.mkdir(parents=True)
        self.assets.mkdir(parents=True)
        self.source.parent.mkdir(parents=True)
        self.source.write_text("这是审核后的产品事实来源。\n", encoding="utf-8")
        write_json(self.inventory, {"target_model": TARGET, "status": "LOCAL_ONLY"})
        self.write_dynamic_schema()
        self.asset_paths: list[Path] = []
        for index, rgb in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255), (127, 63, 191)), 1):
            path = self.assets / f"主图-{index}.png"
            path.write_bytes(png_bytes(*rgb))
            self.asset_paths.append(path)
        self._write_unsealed_documents()
        self.seal_package()
        self.write_partial_decision()

    def write_dynamic_schema(self, required_fields: set[str] | None = None) -> None:
        required_fields = required_fields or {"product_title", "model"}
        fields = []
        base_fields = (*APPROVED_FACT_FIELDS, "price", "minimum_order_quantity")
        for field in base_fields:
            fields.append(
                {
                    "logical_name": field,
                    "observed_label": field.replace("_", " ").title(),
                    "required": field in required_fields,
                }
            )
        for field in sorted(required_fields - set(base_fields)):
            fields.append(
                {
                    "logical_name": field,
                    "observed_label": field.replace("_", " ").title(),
                    "required": True,
                }
            )
        for path in (self.dynamic_raw, self.dynamic_official, self.dynamic_schema):
            path.unlink(missing_ok=True)
        self.dynamic_schema, self.dynamic_official, self.dynamic_raw = build_official_dynamic_schema(
            self.dynamic_raw.parent,
            fields=fields,
            run_id=RUN_ID,
            target_model=TARGET,
            template_product_id=TEMPLATE,
            category_id="fixture-category",
            product_type="Fixture Product",
            stem="approval",
        )

    def _write_unsealed_documents(self) -> None:
        write_json(
            self.candidate / "candidate_review.json",
            {
                "schema_version": "sellerkit.candidate_review.v1",
                "target_model": TARGET,
                "template_product_id": TEMPLATE,
                "status": "REVIEW_REQUIRED",
            },
        )
        fields = []
        values = {
            "product_title": "Testware X7 Commercial Faucet",
            "model": TARGET,
            "body_mount": "Deck Mount",
            "hole_size": "32-35 mm",
            "height": "1000 mm",
            "body_material": "SUS304",
        }
        for field in APPROVED_FACT_FIELDS:
            fields.append(
                {
                    "field": field,
                    "candidate_value": values[field],
                    "decision": "PENDING",
                    "source_disposition": "OFFICIAL_CURRENT_REVIEW_REQUIRED",
                    "source_path": str(self.source.resolve()),
                    "source_sha256": sha256_file(self.source),
                    "source_bytes": self.source.stat().st_size,
                    "write_allowed": False,
                }
            )
        for field in ("price", "minimum_order_quantity"):
            fields.append(
                {
                    "field": field,
                    "candidate_value": None,
                    "decision": "BLOCKED",
                    "source_disposition": "BLOCKED_PENDING_APPROVED_INTERNAL_SOURCE",
                    "source_path": None,
                    "source_sha256": None,
                    "source_bytes": None,
                    "write_allowed": False,
                }
            )
        write_json(
            self.candidate / "approved_source_manifest.template.json",
            {
                "schema_version": "sellerkit.approved_source_manifest_template.v1",
                "target_schema_version": "alibaba.approved-source-manifest.v1",
                "target_model": TARGET,
                "template_product_id": TEMPLATE,
                "review_status": "PENDING",
                "fields": fields,
                "asset_manifest_sha256": "0" * 64,
                "dynamic_schema_binding": validate_dynamic_schema_capture(
                    self.dynamic_schema,
                    expected_target_model=TARGET,
                    expected_template_product_id=TEMPLATE,
                    expected_run_id=RUN_ID,
                ),
            },
        )
        asset_rows = []
        for index, path in enumerate(self.asset_paths, 1):
            digest = sha256_file(path)
            asset_rows.append(
                {
                    "asset_id": f"candidate-{index}",
                    "candidate_order": index,
                    "category": "main_image",
                    "decision": "PENDING",
                    "default_decision": "PENDING",
                    "review_status": "PENDING",
                    "source_disposition": "OFFICIAL_CURRENT_REVIEW_REQUIRED",
                    "source_type": "OFFICIAL_CURRENT_IMAGE",
                    "target_model": TARGET,
                    "exact_model": True,
                    "exists": True,
                    "hash_match": True,
                    "local_path": str(path.resolve()),
                    "bytes": path.stat().st_size,
                    "sha256": digest,
                    "observed_sha256": digest,
                    "recorded_sha256": digest,
                }
            )
        write_json(
            self.candidate / "asset_manifest.candidates.json",
            {
                "schema_version": "sellerkit.asset_manifest_candidates.v1",
                "target_model": TARGET,
                "assets": asset_rows,
            },
        )
        write_json(
            self.candidate / "run_spec.json",
            {
                "schema_version": "sellerkit.similar_draft_run_spec.v1",
                "run_kind": "CREATE_SIMILAR_DRAFT",
                "run_id": RUN_ID,
                "target_model": TARGET,
                "template_product_id": TEMPLATE,
                "mode": "save_draft",
                "final_write_enabled": False,
            },
        )

    def seal_package(self) -> None:
        asset_path = self.candidate / "asset_manifest.candidates.json"
        template_path = self.candidate / "approved_source_manifest.template.json"
        template = read_json(template_path)
        template["asset_manifest_sha256"] = sha256_bytes(
            canonical_json(read_json(asset_path)).encode("utf-8")
        )
        write_json(template_path, template)
        rows = []
        for name in (
            "candidate_review.json",
            "approved_source_manifest.template.json",
            "asset_manifest.candidates.json",
            "run_spec.json",
        ):
            path = self.candidate / name
            rows.append(
                {
                    "role": name,
                    "path": str(path.resolve()),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        write_json(
            self.candidate / "evidence_manifest.json",
            {
                "schema_version": "sellerkit.similar_draft_evidence_manifest.v1",
                "target_model": TARGET,
                "template_product_id": TEMPLATE,
                "generated_files": rows,
                "inputs": [
                    {
                        "role": "target_source_inventory",
                        "path": str(self.inventory.resolve()),
                        "bytes": self.inventory.stat().st_size,
                        "sha256": sha256_file(self.inventory),
                    }
                ],
            },
        )

    def partial_decision_document(self) -> dict[str, Any]:
        values = {
            "product_title": "Testware X7 Commercial Faucet",
            "model": TARGET,
            "body_mount": "Deck Mount",
            "hole_size": "32-35 mm",
            "height": "1000 mm",
            "body_material": "SUS304",
        }
        return {
            "schema_version": "sellerkit.approval-decision.v1",
            "target_model": TARGET,
            "template_product_id": TEMPLATE,
            "run_id": RUN_ID,
            "approved_by": "审核人-甲",
            "user_instruction": "仅把明确批准的字段填入草稿，禁止发布。",
            "dynamic_schema_binding": {
                "path": str(self.dynamic_schema.resolve()),
                "bytes": self.dynamic_schema.stat().st_size,
                "sha256": sha256_file(self.dynamic_schema),
                "decision_id": "dynamic-schema-decision-1",
            },
            "profile": "PARTIAL_DRAFT_FILL_APPROVED_ONLY",
            "approved_fields": {
                field: {
                    "value": values[field],
                    "source_selector": {"kind": "CANDIDATE_FIELD_SOURCE", "candidate_field": field},
                    "decision_id": f"field-decision-{field}",
                }
                for field in APPROVED_FACT_FIELDS
            },
            "selected_asset_orders": [
                {
                    "candidate_order": index,
                    "upload_order": index,
                    "decision_id": f"asset-source-decision-{index}",
                    "identity_decision_id": f"asset-identity-decision-{index}",
                    "upload_decision_id": f"asset-upload-decision-{index}",
                }
                for index in range(1, 5)
            ],
            "blocked_fields": ["price", "minimum_order_quantity"],
        }

    def write_partial_decision(self, document: dict[str, Any] | None = None) -> None:
        write_json(self.decision, document or self.partial_decision_document())

    def add_commercial_decision(self) -> Path:
        path = self.root / "商业审批" / "价格与MOQ.json"
        write_json(
            path,
            {
                "schema_version": "sellerkit.approved-commercial-decision.v1",
                "decision_id": "commercial-price-moq-1",
                "target_model": TARGET,
                "status": "APPROVED",
                "approved_by": "商业审核人",
                "approved_at": "2026-08-05T12:00:00Z",
                "price": {
                    "currency": "USD",
                    "unit": "piece",
                    "trade_term": "FOB",
                    "pricing_method": "WEIGHTED_P25_P50_P75",
                    "candidate_product_ids": ["10001", "10002", "10003"],
                    "p25": 40,
                    "p50": 50,
                    "p75": 60,
                    "approved_value": 50,
                },
                "minimum_order_quantity": {"value": 10, "unit": "piece"},
                "historical_values_are_quote": False,
                "final_write_enabled": False,
            },
        )
        return path


class CompileApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="approval-编译测试-")
        self.fixture = ApprovalFixture(Path(self.temp.name) / "含中文路径")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_partial_success_is_hash_bound_and_never_publish_ready(self) -> None:
        receipt = compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["target_model"], TARGET)
        self.assertEqual(receipt["template_product_id"], TEMPLATE)
        self.assertFalse(receipt["publish_ready"])
        self.assertFalse(receipt["final_write_enabled"])
        source = read_json(self.fixture.output / "approved_source_manifest.json")
        assets = read_json(self.fixture.output / "approved_asset_manifest.json")
        self.assertEqual(source["target_model"], TARGET)
        self.assertEqual(source["bindings"]["template_product_id"], TEMPLATE)
        self.assertEqual(source["draft_field_policy"]["mode"], "PARTIAL_DRAFT_FILL_APPROVED_ONLY")
        self.assertEqual(assets["asset_count"], 4)
        self.assertTrue(all("含中文路径" in row["path"] for row in assets["assets"]))

    def test_full_approved_draft_requires_and_accepts_qualified_commercial_decision(self) -> None:
        commercial = self.fixture.add_commercial_decision()
        decision = self.fixture.partial_decision_document()
        decision["profile"] = "APPROVED_DRAFT"
        decision["blocked_fields"] = []
        selector = {
            "kind": "COMMERCIAL_DECISION",
            "path": str(commercial.resolve()),
            "bytes": commercial.stat().st_size,
            "sha256": sha256_file(commercial),
        }
        decision["approved_fields"]["price"] = {
            "value": 50,
            "source_selector": selector,
            "decision_id": "commercial-price-moq-1",
        }
        decision["approved_fields"]["minimum_order_quantity"] = {
            "value": 10,
            "source_selector": selector,
            "decision_id": "commercial-price-moq-1",
        }
        self.fixture.write_partial_decision(decision)
        receipt = compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)
        self.assertEqual(receipt["profile"], "APPROVED_DRAFT")
        self.assertFalse(receipt["publish_ready"])
        source = read_json(self.fixture.output / "approved_source_manifest.json")
        self.assertNotIn("draft_field_policy", source)
        self.assertTrue(any(row["source_role"] == "COMMERCIAL_DECISION" for row in source["sources"]))

    def test_silence_does_not_approve_an_undecided_field(self) -> None:
        decision = self.fixture.partial_decision_document()
        decision["blocked_fields"].remove("minimum_order_quantity")
        self.fixture.write_partial_decision(decision)
        with self.assertRaisesRegex(CompileBlocked, "undecided fields"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)
        self.assertFalse(self.fixture.output.exists())

    def test_missing_current_dynamic_schema_decision_is_rejected(self) -> None:
        decision = self.fixture.partial_decision_document()
        decision.pop("dynamic_schema_binding")
        self.fixture.write_partial_decision(decision)
        with self.assertRaisesRegex(CompileBlocked, "dynamic_schema_binding"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_required_field_absent_from_candidate_is_derived_from_current_schema_and_rejected(self) -> None:
        self.fixture.write_dynamic_schema({"product_title", "custom_compliance_code"})
        template_path = self.fixture.candidate / "approved_source_manifest.template.json"
        template = read_json(template_path)
        template["dynamic_schema_binding"] = validate_dynamic_schema_capture(
            self.fixture.dynamic_schema,
            expected_target_model=TARGET,
            expected_template_product_id=TEMPLATE,
            expected_run_id=RUN_ID,
        )
        write_json(template_path, template)
        self.fixture.seal_package()
        decision = self.fixture.partial_decision_document()
        decision["dynamic_schema_binding"].update(
            bytes=self.fixture.dynamic_schema.stat().st_size,
            sha256=sha256_file(self.fixture.dynamic_schema),
        )
        self.fixture.write_partial_decision(decision)
        with self.assertRaisesRegex(CompileBlocked, "required fields absent from candidate review"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_duplicate_selected_asset_order_is_rejected(self) -> None:
        decision = self.fixture.partial_decision_document()
        decision["selected_asset_orders"][1]["candidate_order"] = 1
        self.fixture.write_partial_decision(decision)
        with self.assertRaisesRegex(CompileBlocked, "duplicate candidate orders"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_asset_decision_ids_cannot_be_auto_generated(self) -> None:
        decision = self.fixture.partial_decision_document()
        del decision["selected_asset_orders"][0]["upload_decision_id"]
        self.fixture.write_partial_decision(decision)
        with self.assertRaisesRegex(CompileBlocked, "upload_decision_id.*required"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_duplicate_asset_content_is_rejected(self) -> None:
        shutil.copyfile(self.fixture.asset_paths[0], self.fixture.asset_paths[1])
        assets_path = self.fixture.candidate / "asset_manifest.candidates.json"
        assets = read_json(assets_path)
        digest = sha256_file(self.fixture.asset_paths[1])
        assets["assets"][1].update(
            bytes=self.fixture.asset_paths[1].stat().st_size,
            sha256=digest,
            observed_sha256=digest,
            recorded_sha256=digest,
        )
        write_json(assets_path, assets)
        self.fixture.seal_package()
        with self.assertRaisesRegex(CompileBlocked, "duplicate content"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_selected_reject_asset_is_rejected(self) -> None:
        assets_path = self.fixture.candidate / "asset_manifest.candidates.json"
        assets = read_json(assets_path)
        assets["assets"][0]["decision"] = "REJECT"
        write_json(assets_path, assets)
        self.fixture.seal_package()
        with self.assertRaisesRegex(CompileBlocked, "marked REJECT"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_asset_hash_drift_is_rejected(self) -> None:
        self.fixture.asset_paths[0].write_bytes(png_bytes(1, 2, 3))
        with self.assertRaisesRegex(CompileBlocked, "hash mismatch"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_candidate_package_hash_drift_is_rejected(self) -> None:
        run_spec_path = self.fixture.candidate / "run_spec.json"
        run_spec = read_json(run_spec_path)
        run_spec["new_unsealed_value"] = True
        write_json(run_spec_path, run_spec)
        with self.assertRaisesRegex(CompileBlocked, "evidence hash mismatch"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_unqualified_commercial_field_must_be_blocked(self) -> None:
        template_path = self.fixture.candidate / "approved_source_manifest.template.json"
        template = read_json(template_path)
        template["fields"].append(
            {
                "field": "inventory",
                "candidate_value": 100,
                "decision": "PENDING",
                "source_disposition": "BLOCKED_PENDING_APPROVED_INTERNAL_SOURCE",
                "source_path": str(self.fixture.source.resolve()),
                "source_sha256": sha256_file(self.fixture.source),
                "source_bytes": self.fixture.source.stat().st_size,
                "write_allowed": False,
            }
        )
        write_json(template_path, template)
        self.fixture.seal_package()
        decision = self.fixture.partial_decision_document()
        decision["approved_fields"]["inventory"] = {
            "value": 100,
            "source_selector": {"kind": "CANDIDATE_FIELD_SOURCE", "candidate_field": "inventory"},
            "decision_id": "inventory-not-commercially-approved",
        }
        self.fixture.write_partial_decision(decision)
        with self.assertRaisesRegex(CompileBlocked, "must be blocked: inventory"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_target_or_template_mismatch_is_rejected(self) -> None:
        decision = self.fixture.partial_decision_document()
        decision["template_product_id"] = "111222333"
        self.fixture.write_partial_decision(decision)
        with self.assertRaisesRegex(CompileBlocked, "template_product_id mismatch"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)

    def test_existing_output_is_never_overwritten(self) -> None:
        self.fixture.output.mkdir()
        sentinel = self.fixture.output / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(CompileBlocked, "refusing to overwrite"):
            compile_approval(self.fixture.candidate, self.fixture.decision, self.fixture.output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_atomic_rename_failure_leaves_no_output_or_staging(self) -> None:
        def fail_replace(_source: os.PathLike[str] | str, _destination: os.PathLike[str] | str) -> None:
            raise OSError("simulated atomic rename failure")

        with self.assertRaisesRegex(OSError, "simulated atomic rename failure"):
            compile_approval(
                self.fixture.candidate,
                self.fixture.decision,
                self.fixture.output,
                replace_func=fail_replace,
            )
        self.assertFalse(self.fixture.output.exists())
        self.assertEqual(list(self.fixture.output.parent.glob(f".{self.fixture.output.name}.staging-*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
