from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


class CandidateWorkbookCliNodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the pure Node workbook CLI tests")
    def test_pure_node_suite(self) -> None:
        completed = subprocess.run(
            [
                shutil.which("node") or "node",
                "--test",
                str(SCRIPT_DIR / "test_candidate_workbook_cli.mjs"),
            ],
            cwd=SCRIPT_DIR.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
