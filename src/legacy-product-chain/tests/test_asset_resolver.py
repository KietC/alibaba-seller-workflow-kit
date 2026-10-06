from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "alibaba_asset_resolver.py"
SPEC = importlib.util.spec_from_file_location("alibaba_asset_resolver", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class AssetResolverTests(unittest.TestCase):
    @staticmethod
    def write_ready_png(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big")
            + b"IHDR"
            + (800).to_bytes(4, "big")
            + (800).to_bytes(4, "big")
        )

    @staticmethod
    def write_policy(path: Path, product_id: str, disposition: str, *, legacy: bool = False) -> None:
        key = "disposition" if legacy else "record_disposition"
        path.write_text(
            json.dumps({"records": [{"product_id": product_id, key: disposition}]}),
            encoding="utf-8",
        )

    def test_exact_model_boundary(self):
        pattern = MODULE.exact_model_pattern("MODEL-001")
        self.assertIsNotNone(pattern.search("MODEL-001主图.jpg"))
        self.assertIsNotNone(pattern.search("folder/MODEL-001_Example-Owner/video.mp4"))
        self.assertIsNone(pattern.search("MODEL-0010主图.jpg"))
        self.assertIsNone(pattern.search("XMODEL-001主图.jpg"))

    def test_role_classification(self):
        self.assertEqual(MODULE.classify_role(Path("产品主图/MODEL-001.jpg")), "main_image")
        self.assertEqual(MODULE.classify_role(Path("产品单张/MODEL-001.pdf")), "product_sheet")
        self.assertEqual(MODULE.classify_role(Path("产品视频/MODEL-001.mp4")), "product_video")

    def test_plain_local_source_remains_compatible_without_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "local_marketing"
            self.write_ready_png(root / "MODEL-001.png")
            report = MODULE.resolve_assets("MODEL-001", root)
        self.assertEqual(report["summary"]["upload_ready_main_images"], 1)
        self.assertEqual(report["summary"]["rejected_files"], 0)
        self.assertFalse(report["candidates"][0]["source_policy"]["archive_source"])

    def test_archive_source_without_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "archive" / "100000000001"
            self.write_ready_png(root / "MODEL-001.png")
            with self.assertRaises(MODULE.DispositionPolicyError):
                MODULE.resolve_assets(
                    "MODEL-001", root, archive_source=True, product_id="100000000001"
                )

    def test_archive_dispositions_enforce_publish_and_rag_boundaries(self):
        cases = [
            ("APPROVED_INTERNAL_SOURCE", True, True, 0, 0),
            ("HISTORICAL_REFERENCE_ONLY", False, True, 0, 1),
            ("UNUSABLE_WASTE_RECORD", False, False, 1, 1),
            ("BLOCKED_UNCLASSIFIED", False, False, 1, 1),
            ("SOMETHING_UNKNOWN", False, False, 1, 1),
        ]
        for disposition, upload, rag, hard_rejected, publish_rejected in cases:
            with self.subTest(disposition=disposition), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "archive" / "100000000001"
                self.write_ready_png(root / "MODEL-001.png")
                policy = Path(temp) / "policy.json"
                self.write_policy(policy, "100000000001", disposition)
                report = MODULE.resolve_assets(
                    "MODEL-001",
                    root,
                    disposition_manifest=policy,
                    archive_source=True,
                    product_id="100000000001",
                )
                candidate = report["candidates"][0]
                self.assertEqual(candidate["source_policy"]["upload_allowed"], upload)
                self.assertEqual(candidate["source_policy"]["rag_allowed"], rag)
                self.assertEqual(report["summary"]["rejected_files"], hard_rejected)
                self.assertEqual(
                    report["summary"]["publish_rejected_files"], publish_rejected
                )
                self.assertEqual(
                    report["summary"]["upload_ready_main_images"], 1 if upload else 0
                )

    def test_legacy_disposition_alias_and_missing_product_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "archive" / "100000000001"
            self.write_ready_png(root / "MODEL-001.png")
            policy = Path(temp) / "policy.json"
            self.write_policy(policy, "9999999999999", "APPROVED_INTERNAL_SOURCE", legacy=True)
            report = MODULE.resolve_assets(
                "MODEL-001",
                root,
                disposition_manifest=policy,
                archive_source=True,
                product_id="100000000001",
            )
        self.assertEqual(report["summary"]["rejected_files"], 1)
        self.assertEqual(report["summary"]["rejection_reasons"], {"disposition_missing": 1})

    def test_cli_returns_nonzero_and_writes_failure_report_without_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "archive" / "100000000001"
            output = Path(temp) / "report.json"
            self.write_ready_png(root / "MODEL-001.png")
            argv = [
                str(SCRIPT),
                "--model",
                "MODEL-001",
                "--root",
                str(root),
                "--output",
                str(output),
                "--archive-source",
                "--product-id",
                "100000000001",
            ]
            with patch.object(sys, "argv", argv):
                code = MODULE.main()
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "FAIL_CLOSED")


if __name__ == "__main__":
    unittest.main()
