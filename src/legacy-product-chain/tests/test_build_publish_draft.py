from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_publish_draft.py"
SPEC = importlib.util.spec_from_file_location("build_publish_draft", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class PublishDraftTests(unittest.TestCase):
    @staticmethod
    def master(source_id: str = "sheet"):
        return {
            "model": "MODEL-001",
            "category": {"category_id": "100", "verification_status": "verified"},
            "facts": [
                {
                    "field": "product_title",
                    "value": "MODEL-001",
                    "status": "verified",
                    "source_ids": [source_id],
                }
            ],
            "claims": [],
            "commercial": {},
            "sources": [{"source_id": "sheet", "path": "sheet.pdf", "kind": "internal"}],
        }

    @staticmethod
    def ready_assets():
        return {
            "candidates": [
                *[
                    {
                        "path": f"image-{index}.png",
                        "compliance": {"upload_ready_main": True},
                    }
                    for index in range(4)
                ],
                {
                    "path": "video.mp4",
                    "compliance": {"upload_ready_main_video": True},
                },
            ]
        }

    @staticmethod
    def manifest(disposition: str):
        return {
            "schema_version": "1.0.0",
            "sources": [
                {
                    "source_id": "sheet",
                    "record_disposition": disposition,
                }
            ],
        }

    def test_draft_fails_closed(self):
        master = {
            "model": "MODEL-001",
            "category": {"category_id": None, "verification_status": "needs_review"},
            "facts": [
                {"field": "product_title", "value": "MODEL-001", "status": "verified", "source_ids": ["sheet"]}
            ],
            "claims": [],
            "commercial": {
                "price": {"field": "price", "value": None, "status": "blocked", "source_ids": []}
            },
            "sources": [{"source_id": "sheet", "path": "sheet.pdf", "kind": "internal"}],
        }
        draft = MODULE.build_draft(
            master,
            {"candidates": []},
            self.manifest("APPROVED_INTERNAL_SOURCE"),
        )
        self.assertFalse(draft["external_write_enabled"])
        self.assertFalse(draft["approval"]["approved"])
        self.assertEqual(draft["source_gate"]["status"], "PASS")
        self.assertIn("category_id:not_verified", draft["blockers"])
        self.assertIn("main_images:0/4", draft["blockers"])
        self.assertTrue(any(item.startswith("blocked_fields:") for item in draft["blockers"]))

    def test_explicit_approved_internal_source_passes_source_gate(self):
        draft = MODULE.build_draft(
            self.master(),
            self.ready_assets(),
            self.manifest("APPROVED_INTERNAL_SOURCE"),
        )
        self.assertEqual(draft["source_gate"]["status"], "PASS")
        self.assertEqual(draft["source_gate"]["ready_field_count"], 1)
        self.assertEqual([item["status"] for item in draft["fields"]], ["ready"])
        self.assertEqual(len(draft["assets"]["main_image_candidates"]), 4)
        self.assertNotIn("source_gate:REJECTED", draft["blockers"])

    def test_records_manifest_shape_resolves_product_id(self):
        product_id = "100000000015"
        master = self.master(source_id=product_id)
        master["sources"][0]["source_id"] = product_id
        draft = MODULE.build_draft(
            master,
            self.ready_assets(),
            {
                "records": [
                    {
                        "product_id": product_id,
                        "disposition": "APPROVED_INTERNAL_SOURCE",
                    }
                ]
            },
        )
        self.assertEqual(draft["source_gate"]["status"], "PASS")
        self.assertEqual(draft["source_gate"]["referenced_source_ids"], [product_id])
        self.assertEqual(draft["source_gate"]["ready_field_count"], 1)

    def test_overlay_disposition_field_is_reported_and_rejected(self):
        product_id = "100000000015"
        master = self.master(source_id=product_id)
        master["sources"][0]["source_id"] = product_id
        draft = MODULE.build_draft(
            master,
            self.ready_assets(),
            {
                "records": [
                    {
                        "product_id": product_id,
                        "disposition": "HISTORICAL_REFERENCE_ONLY",
                    }
                ]
            },
        )
        self.assertEqual(draft["source_gate"]["status"], "REJECTED")
        self.assertEqual(
            draft["source_gate"]["checked_sources"][0]["record_disposition"],
            "HISTORICAL_REFERENCE_ONLY",
        )
        self.assertEqual(
            draft["source_gate"]["checked_sources"][0]["reason"],
            "disposition_rejected",
        )
        self.assertEqual(draft["source_gate"]["ready_field_count"], 0)

    def test_rejected_dispositions_block_entire_draft(self):
        for disposition in (
            "UNUSABLE_WASTE_RECORD",
            "BLOCKED_UNCLASSIFIED",
            "HISTORICAL_REFERENCE_ONLY",
        ):
            with self.subTest(disposition=disposition):
                draft = MODULE.build_draft(
                    self.master(),
                    self.ready_assets(),
                    self.manifest(disposition),
                )
                self.assertEqual(draft["source_gate"]["status"], "REJECTED")
                self.assertEqual(draft["source_gate"]["ready_field_count"], 0)
                self.assertFalse(any(item["status"] == "ready" for item in draft["fields"]))
                self.assertEqual(draft["assets"]["main_image_candidates"], [])
                self.assertEqual(draft["assets"]["main_video_candidates"], [])
                self.assertIn("source_gate:REJECTED", draft["blockers"])
                self.assertFalse(draft["external_write_enabled"])
                self.assertFalse(draft["approval"]["approved"])

    def test_missing_or_unknown_disposition_is_rejected(self):
        cases = (
            None,
            {"sources": []},
            self.manifest("UNKNOWN_SOURCE_STATUS"),
        )
        for manifest in cases:
            with self.subTest(manifest=manifest):
                draft = MODULE.build_draft(self.master(), self.ready_assets(), manifest)
                self.assertEqual(draft["source_gate"]["status"], "REJECTED")
                self.assertEqual(draft["source_gate"]["ready_field_count"], 0)
                self.assertFalse(any(item["status"] == "ready" for item in draft["fields"]))

    def test_unregistered_fact_source_is_rejected(self):
        draft = MODULE.build_draft(
            self.master(source_id="missing-source"),
            self.ready_assets(),
            self.manifest("APPROVED_INTERNAL_SOURCE"),
        )
        self.assertEqual(draft["source_gate"]["status"], "REJECTED")
        self.assertTrue(
            any(error["code"] == "unregistered_fact_source" for error in draft["source_gate"]["errors"])
        )
        self.assertEqual(draft["source_gate"]["ready_field_count"], 0)

    def test_cli_returns_nonzero_and_writes_rejected_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master_path = root / "master.json"
            assets_path = root / "assets.json"
            manifest_path = root / "dispositions.json"
            output_path = root / "draft.json"
            master_path.write_text(json.dumps(self.master()), encoding="utf-8")
            assets_path.write_text(json.dumps(self.ready_assets()), encoding="utf-8")
            manifest_path.write_text(
                json.dumps(self.manifest("UNUSABLE_WASTE_RECORD")),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--product-master",
                    str(master_path),
                    "--assets",
                    str(assets_path),
                    "--disposition-manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(output_path.is_file())
            draft = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(draft["source_gate"]["status"], "REJECTED")
            self.assertEqual(draft["source_gate"]["ready_field_count"], 0)

    def test_cli_content_detects_multiline_jsonl_and_rejects_waste_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master = self.master(source_id="approved-id")
            master["sources"] = [
                {"source_id": "approved-id", "path": "approved.pdf", "kind": "internal"},
                {"source_id": "waste-id", "path": "waste.json", "kind": "archive"},
            ]
            master_path = root / "master.json"
            assets_path = root / "assets.json"
            manifest_path = root / "record_disposition.data"
            output_path = root / "draft.json"
            master_path.write_text(json.dumps(master), encoding="utf-8")
            assets_path.write_text(json.dumps(self.ready_assets()), encoding="utf-8")
            manifest_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "product_id": "approved-id",
                                "disposition": "APPROVED_INTERNAL_SOURCE",
                            }
                        ),
                        "",
                        json.dumps(
                            {
                                "product_id": "waste-id",
                                "disposition": "UNUSABLE_WASTE_RECORD",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--product-master",
                    str(master_path),
                    "--assets",
                    str(assets_path),
                    "--disposition-manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            draft = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(draft["source_gate"]["status"], "REJECTED")
            self.assertEqual(draft["source_gate"]["ready_field_count"], 0)
            waste = next(
                item
                for item in draft["source_gate"]["checked_sources"]
                if item["source_id"] == "waste-id"
            )
            self.assertEqual(waste["record_disposition"], "UNUSABLE_WASTE_RECORD")
            self.assertEqual(waste["decision"], "REJECTED")
            self.assertFalse(any(item["status"] == "ready" for item in draft["fields"]))

    def test_cli_real_overlay_jsonl_path_allows_approved_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master = self.master(source_id="approved-a")
            master["sources"] = [
                {"source_id": "approved-a", "path": "a.pdf", "kind": "internal"},
                {"source_id": "approved-b", "path": "b.pdf", "kind": "internal"},
            ]
            master_path = root / "master.json"
            assets_path = root / "assets.json"
            overlay_dir = root / "02_overlay"
            overlay_dir.mkdir()
            manifest_path = overlay_dir / "record_disposition.jsonl"
            output_path = root / "draft.json"
            master_path.write_text(json.dumps(master), encoding="utf-8")
            assets_path.write_text(json.dumps(self.ready_assets()), encoding="utf-8")
            manifest_path.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "product_id": source_id,
                            "disposition": "APPROVED_INTERNAL_SOURCE",
                        }
                    )
                    for source_id in ("approved-a", "approved-b")
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--product-master",
                    str(master_path),
                    "--assets",
                    str(assets_path),
                    "--disposition-manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            draft = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(draft["source_gate"]["status"], "PASS")
            self.assertEqual(draft["source_gate"]["ready_field_count"], 1)
            self.assertEqual([item["status"] for item in draft["fields"]], ["ready"])
            self.assertFalse(draft["external_write_enabled"])
            self.assertFalse(draft["approval"]["approved"])

    def test_cli_invalid_jsonl_line_reports_line_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master = self.master(source_id="approved-id")
            master["sources"][0]["source_id"] = "approved-id"
            master_path = root / "master.json"
            assets_path = root / "assets.json"
            manifest_path = root / "record_disposition.jsonl"
            output_path = root / "draft.json"
            master_path.write_text(json.dumps(master), encoding="utf-8")
            assets_path.write_text(json.dumps(self.ready_assets()), encoding="utf-8")
            manifest_path.write_text(
                json.dumps(
                    {
                        "product_id": "approved-id",
                        "disposition": "APPROVED_INTERNAL_SOURCE",
                    }
                )
                + "\n\n{not-json",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--product-master",
                    str(master_path),
                    "--assets",
                    str(assets_path),
                    "--disposition-manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            draft = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(draft["source_gate"]["status"], "REJECTED")
            parse_error = next(
                error
                for error in draft["source_gate"]["errors"]
                if error["code"] == "invalid_disposition_jsonl_line"
            )
            self.assertTrue(parse_error["detail"].startswith("line 3:"))
            self.assertEqual(draft["source_gate"]["ready_field_count"], 0)
            self.assertFalse(any(item["status"] == "ready" for item in draft["fields"]))

    def test_schema_declares_builder_source_fields(self):
        schema_path = SCRIPT.parents[1] / "schemas" / "publish_draft.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIn("source_registry", schema["required"])
        self.assertIn("source_gate", schema["required"])
        self.assertIn("approval", schema["required"])
        self.assertIn("source_registry", schema["properties"])
        self.assertIn("source_gate", schema["properties"])


if __name__ == "__main__":
    unittest.main()
