from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "record_disposition.py"
SPEC = importlib.util.spec_from_file_location("record_disposition", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


FIXED_TIME = "2026-08-03T10:00:00.123456Z"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_catalog(root: Path, rows: list[dict[str, object]]) -> Path:
    catalog = root / "base_archive" / "01_catalog" / "catalog_index.jsonl"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    return catalog


def catalog_rows() -> list[dict[str, object]]:
    return [
        {
            "schema_version": "1.0.0",
            "run_id": "run-1",
            "ordinal": 1,
            "product_id": "1001",
            "subject": "Historical",
            "gmt_modified": "2000-01-01",
        },
        {
            "schema_version": "1.0.0",
            "run_id": "run-1",
            "ordinal": 2,
            "product_id": "1002",
            "subject": "Cutoff",
            "gmt_modified": "2000-01-02",
        },
    ]


class RecordDispositionTests(unittest.TestCase):
    def test_control_matrix_and_date_semantics(self) -> None:
        self.assertEqual(
            MODULE.disposition_for("2000-01-01", "2000-01-02"),
            MODULE.HISTORICAL,
        )
        self.assertEqual(
            MODULE.disposition_for("2000-01-02", "2000-01-02"),
            MODULE.UNUSABLE,
        )
        self.assertEqual(
            MODULE.controls_for(MODULE.HISTORICAL),
            {
                "allow_rag": True,
                "fact_extraction": True,
                "asset_reuse": True,
                "publish_draft": False,
                "auto_listing": False,
            },
        )
        self.assertTrue(all(value is False for value in MODULE.controls_for(MODULE.UNUSABLE).values()))
        with self.assertRaises(MODULE.DispositionError):
            MODULE.controls_for("UNKNOWN")

    def test_build_creates_versioned_sibling_with_full_verified_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = make_catalog(root, catalog_rows())
            before = sha256(catalog)
            output = root / "overlay-v1"
            result = MODULE.build_overlay(
                catalog,
                output_root=output,
                expected_count=2,
                generated_at=FIXED_TIME,
            )

            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(result["eligible_count"], 1)
            self.assertEqual(result["unusable_count"], 1)
            self.assertEqual(before, sha256(catalog))
            self.assertEqual(output.parent, catalog.parent.parent.parent)
            for directory in MODULE.REQUIRED_DIRECTORIES:
                self.assertTrue((output / directory).is_dir())

            records = MODULE.load_jsonl(output / MODULE.OVERLAY_RELATIVE_PATH)
            self.assertEqual([row["product_id"] for row in records], ["1001", "1002"])
            historical, unusable = records
            self.assertEqual(historical["record_disposition"], historical["disposition"])
            self.assertEqual(unusable["record_disposition"], unusable["disposition"])
            mismatched = dict(unusable)
            mismatched["record_disposition"] = MODULE.HISTORICAL
            self.assertIn("canonical_disposition_mismatch", MODULE.validate_record(mismatched))
            self.assertEqual(historical["disposition"], MODULE.HISTORICAL)
            self.assertTrue(historical["allow_rag"])
            self.assertTrue(historical["fact_extraction"])
            self.assertTrue(historical["asset_reuse"])
            self.assertFalse(historical["publish_draft"])
            self.assertFalse(historical["auto_listing"])
            self.assertEqual(unusable["disposition"], MODULE.UNUSABLE)
            self.assertTrue(
                all(
                    unusable[key] is False
                    for key in ("allow_rag", "fact_extraction", "asset_reuse", "publish_draft", "auto_listing")
                )
            )
            self.assertEqual(
                historical["date_evidence"]["semantics"],
                "last_modified_proxy_not_creation_date",
            )
            self.assertEqual(
                (output / MODULE.ELIGIBLE_IDS_RELATIVE_PATH).read_text(encoding="utf-8"),
                "1001\n",
            )
            self.assertEqual(
                (output / MODULE.UNUSABLE_IDS_RELATIVE_PATH).read_text(encoding="utf-8"),
                "1002\n",
            )
            report = (output / MODULE.REPORT_RELATIVE_PATH).read_text(encoding="utf-8")
            self.assertIn("不是商品创建日期", report)
            self.assertIn("APPROVED_INTERNAL_SOURCE", report)

            sidecar_hash = (output / MODULE.MANIFEST_SIDECAR_RELATIVE_PATH).read_text(
                encoding="ascii"
            ).split()[0]
            self.assertEqual(sidecar_hash, sha256(output / MODULE.MANIFEST_RELATIVE_PATH))
            recorded_verification = MODULE.load_json(output / MODULE.VERIFICATION_REPORT_RELATIVE_PATH)
            self.assertEqual(recorded_verification["status"], "COMPLETE")
            self.assertTrue(all(recorded_verification["checks"].values()))
            secret_scan = MODULE.load_json(output / MODULE.SECRET_SCAN_REPORT_RELATIVE_PATH)
            self.assertEqual(secret_scan["status"], "PASS")
            self.assertEqual(secret_scan["finding_count"], 0)
            self.assertEqual(
                set(secret_scan["patterns"]),
                {"bearer", "cookie", "authorization", "ctoken", "_tb_token_", "_csrf_token_"},
            )
            commands = MODULE.load_jsonl(output / MODULE.COMMANDS_RELATIVE_PATH)
            self.assertEqual(
                [row["operation"] for row in commands],
                ["materialize_overlay", "verify_core", "secret_scan"],
            )
            manifest = MODULE.load_json(output / MODULE.MANIFEST_RELATIVE_PATH)
            manifest_paths = {row["relative_path"] for row in manifest["files"]}
            self.assertTrue(
                {
                    MODULE.VERIFICATION_REPORT_RELATIVE_PATH.as_posix(),
                    MODULE.SECRET_SCAN_REPORT_RELATIVE_PATH.as_posix(),
                    MODULE.COMMANDS_RELATIVE_PATH.as_posix(),
                }.issubset(manifest_paths)
            )
            verification = MODULE.verify_overlay(output, catalog, expected_count=2)
            self.assertEqual(verification["status"], "COMPLETE", verification["errors"])
            self.assertEqual(verification["record_count"], 2)

    def test_missing_date_fails_before_creating_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = catalog_rows()
            rows[1].pop("gmt_modified")
            catalog = make_catalog(root, rows)
            output = root / "overlay-v1"
            with self.assertRaises(MODULE.DispositionError):
                MODULE.build_overlay(
                    catalog,
                    output_root=output,
                    expected_count=2,
                    generated_at=FIXED_TIME,
                )
            self.assertFalse(output.exists())

    def test_overlay_must_be_sibling_not_inside_base_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = make_catalog(root, catalog_rows())
            output = catalog.parent.parent / "forbidden-child"
            with self.assertRaises(MODULE.DispositionError):
                MODULE.build_overlay(
                    catalog,
                    output_root=output,
                    expected_count=2,
                    generated_at=FIXED_TIME,
                )
            self.assertFalse(output.exists())

    def test_verify_detects_overlay_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = make_catalog(root, catalog_rows())
            output = root / "overlay-v1"
            MODULE.build_overlay(
                catalog,
                output_root=output,
                expected_count=2,
                generated_at=FIXED_TIME,
            )
            records = MODULE.load_jsonl(output / MODULE.OVERLAY_RELATIVE_PATH)
            records[0]["publish_draft"] = True
            (output / MODULE.OVERLAY_RELATIVE_PATH).write_bytes(MODULE.jsonl_bytes(records))
            verification = MODULE.verify_overlay(output, catalog, expected_count=2)
            self.assertEqual(verification["status"], "FAIL")
            self.assertTrue(any("publish_draft" in error or "record_hash" in error for error in verification["errors"]))

    def test_check_is_verified_and_unknown_product_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = make_catalog(root, catalog_rows())
            output = root / "overlay-v1"
            MODULE.build_overlay(
                catalog,
                output_root=output,
                expected_count=2,
                generated_at=FIXED_TIME,
            )
            known, known_code = MODULE.check_product(output, "1001", catalog, expected_count=2)
            self.assertEqual(known_code, 0)
            self.assertEqual(known["verification_status"], "COMPLETE")
            self.assertTrue(known["allow_rag"])
            self.assertFalse(known["publish_draft"])

            unknown, unknown_code = MODULE.check_product(output, "9999", catalog, expected_count=2)
            self.assertEqual(unknown_code, 4)
            self.assertEqual(unknown["status"], "UNKNOWN_FAIL_CLOSED")
            self.assertTrue(
                all(
                    unknown[key] is False
                    for key in ("allow_rag", "fact_extraction", "asset_reuse", "publish_draft", "auto_listing")
                )
            )

    def test_gate_enforces_usage_and_exit_codes_after_full_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = make_catalog(root, catalog_rows())
            output = root / "overlay-v1"
            MODULE.build_overlay(
                catalog,
                output_root=output,
                expected_count=2,
                generated_at=FIXED_TIME,
            )
            allowed, allowed_code = MODULE.gate_usage(
                output, "1001", "rag", catalog, expected_count=2
            )
            self.assertEqual(allowed_code, 0)
            self.assertEqual(allowed["status"], "ALLOWED")
            self.assertTrue(allowed["allowed"])

            denied_historical, denied_historical_code = MODULE.gate_usage(
                output, "1001", "auto_listing", catalog, expected_count=2
            )
            self.assertEqual(denied_historical_code, 3)
            self.assertEqual(denied_historical["status"], "DENIED")

            denied_unusable, denied_unusable_code = MODULE.gate_usage(
                output, "1002", "rag", catalog, expected_count=2
            )
            self.assertEqual(denied_unusable_code, 3)
            self.assertFalse(denied_unusable["allowed"])

            unknown, unknown_code = MODULE.gate_usage(
                output, "9999", "rag", catalog, expected_count=2
            )
            self.assertEqual(unknown_code, 4)
            self.assertEqual(unknown["status"], "UNKNOWN_FAIL_CLOSED")

    def test_secret_scan_detects_required_patterns_and_build_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan_root = root / "scan"
            scan_root.mkdir()
            (scan_root / "sample.txt").write_text(
                "Authorization: Bearer abcdefghijklmnop\n"
                "Cookie: session=abcdefghijklmnop\n"
                "https://x.test/?ctoken=abcdefghijkl&_tb_token_=abcdefghijkl&_csrf_token_=abcdefghijkl\n",
                encoding="utf-8",
            )
            report = MODULE.scan_overlay_secrets(scan_root, FIXED_TIME)
            self.assertEqual(report["status"], "FAIL")
            self.assertTrue(
                {"bearer", "authorization", "cookie", "ctoken", "_tb_token_", "_csrf_token_"}.issubset(
                    {finding["pattern"] for finding in report["findings"]}
                )
            )

            rows = catalog_rows()
            rows[0]["subject"] = "Authorization: Bearer abcdefghijklmnop"
            catalog = make_catalog(root / "build_case", rows)
            output = root / "build_case" / "overlay-v1"
            with self.assertRaises(MODULE.DispositionError):
                MODULE.build_overlay(
                    catalog,
                    output_root=output,
                    expected_count=2,
                    generated_at=FIXED_TIME,
                )
            failed_report = MODULE.load_json(output / MODULE.SECRET_SCAN_REPORT_RELATIVE_PATH)
            self.assertEqual(failed_report["status"], "FAIL")
            self.assertGreater(failed_report["finding_count"], 0)

    def test_base_hash_drift_invalidates_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = make_catalog(root, catalog_rows())
            output = root / "overlay-v1"
            MODULE.build_overlay(
                catalog,
                output_root=output,
                expected_count=2,
                generated_at=FIXED_TIME,
            )
            catalog.write_text(catalog.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            verification = MODULE.verify_overlay(output, catalog, expected_count=2)
            self.assertEqual(verification["status"], "FAIL")
            self.assertTrue(any(error.startswith("base_catalog:") for error in verification["errors"]))

    def test_schema_locks_semantics_and_publish_controls(self) -> None:
        schema = json.loads(MODULE.schema_source_path().read_text(encoding="utf-8"))
        required = set(schema["required"])
        self.assertTrue(
            {
                "allow_rag",
                "fact_extraction",
                "asset_reuse",
                "publish_draft",
                "auto_listing",
                "record_disposition",
            }.issubset(required)
        )
        self.assertEqual(
            schema["properties"]["date_evidence"]["properties"]["semantics"]["const"],
            "last_modified_proxy_not_creation_date",
        )
        self.assertFalse(schema["properties"]["external_write_enabled"]["const"])
        self.assertFalse(schema["properties"]["publish_draft"]["const"])
        self.assertFalse(schema["properties"]["auto_listing"]["const"])


if __name__ == "__main__":
    unittest.main()
