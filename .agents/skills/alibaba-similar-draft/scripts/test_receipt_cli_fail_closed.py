from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import validate_localized_counter_capture as localized  # noqa: E402
import validate_next_product_checklist as checklist  # noqa: E402
import validate_official_schema_values as official  # noqa: E402
import validate_post_save_inventory as post_save  # noqa: E402
import validate_pre_save_readback as pre_save  # noqa: E402


Main = Callable[[list[str] | None], int]


class ReceiptCliFailClosedTests(unittest.TestCase):
    def cases(self, root: Path, bad: Path) -> list[tuple[str, Main, list[str], str, int]]:
        return [
            (
                "official", official.main,
                [
                    "--normalized-schema", str(bad), "--current-values", str(bad),
                    "--capture", str(bad), "--expected-run-id", "run-1",
                    "--expected-target-model", "MODEL100",
                    "--expected-template-product-id", "100000000001",
                    "--expected-browser-session-sha256", "0" * 64,
                    "--receipt", str(root / "official-receipt.json"),
                ],
                "BLOCKED_VALUE_VALIDATION", 3,
            ),
            (
                "localized", localized.main,
                [
                    "--capture", str(bad), "--expectation", str(bad),
                    "--receipt", str(root / "localized-receipt.json"),
                ],
                "BLOCKED_COUNTER_CAPTURE", 3,
            ),
            (
                "pre-save", pre_save.main,
                [
                    "--action-plan", str(bad), "--run-spec", str(bad),
                    "--probe-source", str(bad), "--probe-verification", str(bad),
                    "--pre-save-readback", str(bad),
                    "--receipt", str(root / "pre-save-receipt.json"),
                ],
                "BLOCKED_PRE_SAVE", 2,
            ),
            (
                "post-save", post_save.main,
                [
                    "--action-plan", str(bad), "--run-spec", str(bad),
                    "--probe-verification", str(bad), "--pre-save-verification", str(bad),
                    "--activation-receipt", str(bad), "--before-inventory", str(bad),
                    "--after-inventory", str(bad), "--save-result", str(bad),
                    "--post-refresh", str(bad), "--adapter", str(bad),
                    "--receipt", str(root / "post-save-receipt.json"),
                ],
                "BLOCKED_INVENTORY_DELTA", 2,
            ),
            (
                "checklist", checklist.main,
                [
                    "--checklist", str(bad), "--stage", "pre_save",
                    "--receipt", str(root / "checklist-receipt.json"),
                ],
                "BLOCKED", 2,
            ),
        ]

    @staticmethod
    def invoke(main: Main, argv: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def test_missing_inputs_emit_new_structured_blocked_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.json"
            for name, main, argv, expected_status, expected_code in self.cases(root, missing):
                with self.subTest(name=name):
                    self.assertEqual(self.invoke(main, argv), expected_code)
                    receipt_path = Path(argv[-1])
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    self.assertEqual(receipt["status"], expected_status)
                    self.assertTrue(receipt.get("errors"), receipt)

    def test_malformed_json_emits_new_structured_blocked_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / "malformed.json"
            malformed.write_text('{"broken":', encoding="utf-8")
            for name, main, argv, expected_status, expected_code in self.cases(root, malformed):
                with self.subTest(name=name):
                    self.assertEqual(self.invoke(main, argv), expected_code)
                    receipt_path = Path(argv[-1])
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    self.assertEqual(receipt["status"], expected_status)
                    self.assertTrue(receipt.get("errors"), receipt)

    def test_existing_receipts_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.json"
            sentinel = b'{"sentinel":"preserve-exact-bytes"}\n'
            for name, main, argv, _expected_status, expected_code in self.cases(root, missing):
                with self.subTest(name=name):
                    receipt_path = Path(argv[-1])
                    receipt_path.write_bytes(sentinel)
                    self.assertEqual(self.invoke(main, argv), expected_code)
                    self.assertEqual(receipt_path.read_bytes(), sentinel)


if __name__ == "__main__":
    unittest.main()
