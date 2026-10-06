"""Repo-only regressions over complete original-producer, synthetic overlays.

Only record_disposition.build_overlay is invoked, with every input/output path
explicitly under a new temporary directory. No collector, CLI, workspace
discovery, business default path or network is used.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / ".agents/skills/alibaba-similar-draft/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import draft_core as core


PRODUCER_PATH = REPO_ROOT / "src/legacy-product-chain/scripts/record_disposition.py"
ISOLATED_CWD = Path("C:/Windows/Temp") if os.name == "nt" else Path(tempfile.gettempdir())
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CONSUMED = (
    "01_date_evidence/base_catalog_binding.json",
    "02_overlay/record_disposition.jsonl",
    "03_audit/build_receipt.json",
    "03_audit/verification_report.json",
)


class ProducerOverlayRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not PRODUCER_PATH.is_file():
            raise RuntimeError(f"original local archive producer is required: {PRODUCER_PATH}")
        spec = importlib.util.spec_from_file_location("draft_synthetic_overlay_producer", PRODUCER_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load the original local archive producer")
        cls.producer = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.producer
        spec.loader.exec_module(cls.producer)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="draft-full-producer-", dir=ISOLATED_CWD)
        self.root = Path(self.temp.name)
        self.serial = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, *, eligible: bool) -> tuple[Path, Path, str]:
        self.serial += 1
        workspace = self.root / f"synthetic-{self.serial}"
        catalog = workspace / "synthetic_archive/catalog/catalog_index.jsonl"
        catalog.parent.mkdir(parents=True)
        rows = [{"product_id": str(product_id), "ordinal": ordinal,
                 "run_id": "synthetic-only", "subject": "Synthetic product",
                 "gmt_modified": "2000-01-01" if eligible else "2000-01-02"}
                for ordinal, product_id in enumerate((101, 102), 1)]
        catalog.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        original_catalog_hash = core.sha256_file(catalog)
        overlay = workspace / "synthetic_archive__record_disposition_overlay__20000101_000000"
        with patch("socket.create_connection", side_effect=AssertionError("network prohibited")) as connection:
            receipt = self.producer.build_overlay(
                catalog_index=catalog, cutoff="2000-01-02", output_root=overlay,
                expected_count=2, generated_at="2026-10-06T03:00:00Z",
            )
            connection.assert_not_called()
        self.assertEqual(receipt["status"], "COMPLETE")
        self.assertFalse(receipt["external_write_enabled"])
        self.assertFalse(receipt["online_write_performed"])
        self.assertEqual(core.sha256_file(catalog), original_catalog_hash)
        self.assertEqual(core.read_json(overlay / "03_audit/verification_report.json")["status"], "COMPLETE")
        manifest = core.read_json(overlay / "03_audit/derived_manifest.json")
        actual_files = {path.relative_to(overlay).as_posix() for path in overlay.rglob("*") if path.is_file()}
        sealed_files = {entry["relative_path"] for entry in manifest["files"]}
        self.assertEqual(manifest["file_count"], 13)
        self.assertEqual(len(manifest["files"]), 13)
        self.assertEqual(sealed_files, actual_files - {
            "03_audit/derived_manifest.json", "03_audit/derived_manifest.json.sha256",
        })
        self.assertTrue(set(CONSUMED) <= sealed_files)
        empty = "01_date_evidence/unusable_ids.txt" if eligible else "01_date_evidence/eligible_ids.txt"
        entry = next(item for item in manifest["files"] if item["relative_path"] == empty)
        self.assertIs(type(entry["bytes"]), int)
        self.assertEqual(entry["bytes"], 0)
        self.assertEqual(entry["sha256"], EMPTY_SHA256)
        self.assertEqual((overlay / empty).read_bytes(), b"")
        return workspace, overlay, empty

    def reseal_manifest(self, overlay: Path, manifest: dict) -> None:
        """Give mutated metadata a valid outer seal; retain each tested defect."""
        manifest["file_count"] = len(manifest["files"])
        manifest["total_bytes"] = sum(item["bytes"] for item in manifest["files"]
                                      if type(item.get("bytes")) is int and item["bytes"] >= 0)
        aggregate = "".join(f"{item['sha256']}  {item['relative_path']}\n" for item in manifest["files"])
        manifest["aggregate_sha256"] = hashlib.sha256(aggregate.encode("utf-8")).hexdigest()
        path = overlay / "03_audit/derived_manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (overlay / "03_audit/derived_manifest.json.sha256").write_text(
            f"{core.sha256_file(path)}  derived_manifest.json\n", encoding="utf-8",
        )

    def test_full_producer_all_eligible_empty_unusable_file_passes(self) -> None:
        workspace, _, _ = self.build(eligible=True)
        result = core.verify_overlay(workspace)
        self.assertEqual((result["status"], result["eligible_count"], result["unusable_count"]), ("COMPLETE", 2, 0))
        self.assertFalse(result["external_write_enabled"])
        self.assertFalse(result["online_write_performed"])

    def test_full_producer_all_excluded_empty_eligible_file_passes(self) -> None:
        workspace, _, _ = self.build(eligible=False)
        result = core.verify_overlay(workspace)
        self.assertEqual((result["status"], result["eligible_count"], result["unusable_count"]), ("COMPLETE", 0, 2))
        self.assertFalse(result["external_write_enabled"])
        self.assertFalse(result["online_write_performed"])

    def test_malformed_empty_file_bytes_metadata_fails_after_resealing(self) -> None:
        for eligible in (True, False):
            workspace, overlay, empty = self.build(eligible=eligible)
            original = core.read_json(overlay / "03_audit/derived_manifest.json")
            for value in (False, True, "0", 0.0, None, -1):
                with self.subTest(eligible=eligible, value=repr(value)):
                    manifest = copy.deepcopy(original)
                    next(item for item in manifest["files"] if item["relative_path"] == empty)["bytes"] = value
                    self.reseal_manifest(overlay, manifest)
                    with self.assertRaisesRegex(core.GuardError, "byte count mismatch"):
                        core.verify_overlay(workspace)

    def test_missing_empty_file_bytes_metadata_fails_after_resealing(self) -> None:
        for eligible in (True, False):
            with self.subTest(eligible=eligible):
                workspace, overlay, empty = self.build(eligible=eligible)
                manifest = core.read_json(overlay / "03_audit/derived_manifest.json")
                next(item for item in manifest["files"] if item["relative_path"] == empty).pop("bytes")
                self.reseal_manifest(overlay, manifest)
                with self.assertRaisesRegex(core.GuardError, "byte count mismatch"):
                    core.verify_overlay(workspace)

    def test_empty_file_wrong_declared_length_fails_after_resealing(self) -> None:
        for eligible in (True, False):
            with self.subTest(eligible=eligible):
                workspace, overlay, empty = self.build(eligible=eligible)
                manifest = core.read_json(overlay / "03_audit/derived_manifest.json")
                next(item for item in manifest["files"] if item["relative_path"] == empty)["bytes"] = 1
                self.reseal_manifest(overlay, manifest)
                with self.assertRaisesRegex(core.GuardError, "byte count mismatch"):
                    core.verify_overlay(workspace)

    def test_empty_file_content_length_tampering_fails(self) -> None:
        for eligible in (True, False):
            with self.subTest(eligible=eligible):
                workspace, overlay, empty = self.build(eligible=eligible)
                (overlay / empty).write_bytes(b"x")
                with self.assertRaisesRegex(core.GuardError, "byte count mismatch"):
                    core.verify_overlay(workspace)

    def test_empty_file_wrong_sha_fails_after_resealing(self) -> None:
        for eligible in (True, False):
            with self.subTest(eligible=eligible):
                workspace, overlay, empty = self.build(eligible=eligible)
                manifest = core.read_json(overlay / "03_audit/derived_manifest.json")
                next(item for item in manifest["files"] if item["relative_path"] == empty)["sha256"] = "0" * 64
                self.reseal_manifest(overlay, manifest)
                with self.assertRaisesRegex(core.GuardError, "manifest hash mismatch"):
                    core.verify_overlay(workspace)

    def test_each_consumed_file_omitted_from_complete_manifest_is_rejected(self) -> None:
        for relative in CONSUMED:
            with self.subTest(relative=relative):
                workspace, overlay, _ = self.build(eligible=True)
                manifest = core.read_json(overlay / "03_audit/derived_manifest.json")
                manifest["files"] = [item for item in manifest["files"] if item["relative_path"] != relative]
                self.reseal_manifest(overlay, manifest)
                with self.assertRaisesRegex(core.GuardError, "manifest omits consumed evidence"):
                    core.verify_overlay(workspace)

    def test_each_consumed_file_missing_from_disk_is_rejected(self) -> None:
        for relative in CONSUMED:
            with self.subTest(relative=relative):
                workspace, overlay, _ = self.build(eligible=False)
                (overlay / relative).unlink()
                with self.assertRaisesRegex(core.GuardError, "overlay evidence is missing"):
                    core.verify_overlay(workspace)


if __name__ == "__main__":
    unittest.main(verbosity=2)
