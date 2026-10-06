from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "alibaba_rag_preflight.py"
SPEC = importlib.util.spec_from_file_location("alibaba_rag_preflight", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RagPreflightTests(unittest.TestCase):
    @staticmethod
    def write_rag_manifest(root: Path, rows: list[dict[str, str]]) -> Path:
        included = root / "included_files.jsonl"
        included.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        manifest = root / "rag_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "created_at": "2026-08-03T00:00:00Z",
                    "model": "test",
                    "included_files_jsonl": str(included),
                    "vector_index": str(root / "faiss.index"),
                    "stats": {"included_files": len(rows)},
                }
            ),
            encoding="utf-8",
        )
        return manifest

    @staticmethod
    def write_policy(path: Path, rows: list[tuple[str, str]]) -> None:
        path.write_text(
            "".join(
                json.dumps({"product_id": product_id, "record_disposition": disposition})
                + "\n"
                for product_id, disposition in rows
            ),
            encoding="utf-8",
        )

    def test_plain_local_manifest_needs_no_alibaba_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.write_rag_manifest(
                root, [{"path": str(root / "local" / "notes.md")}]
            )
            report = MODULE.inspect_manifest(manifest)
        self.assertEqual(report["archive_policy_filter"]["archive_files"], 0)
        self.assertEqual(report["archive_policy_filter"]["rejected_files"], 0)
        self.assertTrue(report["ready_for_local_rag_reference"])

    def test_archive_root_without_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive"
            manifest = self.write_rag_manifest(
                root, [{"path": str(archive / "100000000001" / "snapshot.json")}]
            )
            with self.assertRaises(MODULE.DispositionPolicyError):
                MODULE.inspect_manifest(manifest, archive_root=archive)

    def test_rag_allows_approved_and_historical_but_rejects_waste_missing_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive"
            ids = ["100000000001", "100000000002", "100000000003", "100000000004", "100000000005"]
            rows = [
                {
                    "path": str(archive / product_id / "snapshot.json"),
                    "product_id": product_id,
                }
                for product_id in ids
            ]
            manifest = self.write_rag_manifest(root, rows)
            policy = root / "policy.jsonl"
            self.write_policy(
                policy,
                [
                    (ids[0], "APPROVED_INTERNAL_SOURCE"),
                    (ids[1], "HISTORICAL_REFERENCE_ONLY"),
                    (ids[2], "UNUSABLE_WASTE_RECORD"),
                    (ids[3], "SOMETHING_UNKNOWN"),
                ],
            )
            report = MODULE.inspect_manifest(
                manifest,
                disposition_manifest=policy,
                archive_root=archive,
            )
        policy_report = report["archive_policy_filter"]
        self.assertEqual(policy_report["archive_files"], 5)
        self.assertEqual(policy_report["rag_eligible_archive_files"], 2)
        self.assertEqual(policy_report["historical_reference_files"], 1)
        self.assertEqual(policy_report["rejected_products"], 3)
        self.assertEqual(policy_report["rejected_files"], 3)
        self.assertEqual(
            policy_report["rejection_reasons"],
            {
                "disposition_missing": 1,
                "record_disposition_unknown": 1,
                "record_disposition_unusable_waste_record": 1,
            },
        )
        self.assertFalse(report["ready_for_local_rag_reference"])

    def test_cli_returns_nonzero_when_archive_policy_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive"
            manifest = self.write_rag_manifest(
                root, [{"path": str(archive / "100000000001" / "snapshot.json")}]
            )
            output = root / "report.json"
            argv = [
                str(SCRIPT),
                "--manifest",
                str(manifest),
                "--output",
                str(output),
                "--archive-root",
                str(archive),
            ]
            with patch.object(sys, "argv", argv):
                code = MODULE.main()
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "FAIL_CLOSED")


if __name__ == "__main__":
    unittest.main()
