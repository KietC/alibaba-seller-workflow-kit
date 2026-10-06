"""Synthetic tests for export boundaries, independent of business data."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("source_bundle", ROOT / "tools/source_bundle.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BundleBoundaryTests(unittest.TestCase):
    def test_path_escape_and_ads_denied(self):
        for path in ("../bad.py", "/bad.py", "C:/bad.py", "dir\\bad.py", "a:stream.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                module.validate_relative(path)

    def test_runtime_data_denied(self):
        for path in ("cookies.json", "capture.mhtml", "records.jsonl", "private.sqlite", "outputs/run.py", "cases/test.py", "training_runs/test.py", "wheelhouse/attrs.whl"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                module.allowed_file(Path(path))

    def test_source_and_synthetic_fixtures_allowed(self):
        for path in ("scripts/main.py", "references/approved-source-manifest.schema.json", "tests/fixture.html", "tests/fixture.css", "requirements.lock.txt", "LICENSE"):
            self.assertTrue(module.allowed_file(Path(path)))

    def test_secret_value_detected_without_returning_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            sentinel = "gh" + "p_" + "A" * 36
            path.write_text(sentinel, encoding="utf-8")
            errors, _ = module.scan_text(path, "source.py")
            self.assertEqual(errors[0]["rule"], "github_token")
            self.assertNotIn(sentinel, str(errors))

    def test_email_requires_review_but_synthetic_address_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_text("audit@example.invalid", encoding="utf-8")
            self.assertEqual(module.scan_text(path, "source.py"), ([], []))
            path.write_text("audit@" + "nonexample.tld", encoding="utf-8")
            errors, review = module.scan_text(path, "source.py")
            self.assertEqual(errors, [])
            self.assertEqual(review[0]["rule"], "email_requires_review")


if __name__ == "__main__":
    unittest.main(verbosity=2)
