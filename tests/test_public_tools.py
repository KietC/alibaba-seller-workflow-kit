"""Source-only release checks use fictional fixtures and no network.
纯源码发布检查使用虚构素材，不联网。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from check_public_release import check


class PublicReleaseTests(unittest.TestCase):
    def make_root(self, directory):
        root = Path(directory)
        for name in ("README.md", "docs/GETTING_STARTED.md", "docs/PLATFORMS_AND_WORKFLOWS.md", "docs/TROUBLESHOOTING.md"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("## English\n\nExample.\n\n## 中文\n\n示例。\n", encoding="utf-8")
        (root / "LICENSE").write_text("Synthetic license fixture.\n", encoding="utf-8")
        (root / "requirements.txt").write_text("", encoding="utf-8")
        return root

    def test_source_only_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            result = check(self.make_root(directory))
            self.assertEqual(result["status"], "PASS_PUBLIC_SOURCE_CHECK")

    def test_runtime_file_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / "cookies.json").write_text("{}", encoding="utf-8")
            self.assertTrue(any(f["rule"] == "private_runtime_file" for f in check(root)["findings"]))

    def test_matched_value_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            sentinel = "gh" + "p_" + "X" * 36
            (root / "fixture.py").write_text(repr(sentinel), encoding="utf-8")
            result = check(root)
            self.assertTrue(any(f["rule"] == "github_token" for f in result["findings"]))
            self.assertNotIn(sentinel, str(result["findings"]))

    def test_language_order_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / "README.md").write_text("## 中文\n\n## English\n", encoding="utf-8")
            self.assertTrue(any(f["rule"] == "english_then_chinese_sections_required" for f in check(root)["findings"]))


if __name__ == "__main__":
    unittest.main()
