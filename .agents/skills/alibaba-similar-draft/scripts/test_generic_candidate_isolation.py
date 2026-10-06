from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from draft_core import official_field_candidates  # noqa: E402


class GenericCandidateIsolationTests(unittest.TestCase):
    def _official(self, attributes: list[dict[str, str]]) -> dict[str, object]:
        return {
            "canonical_model": "ZX900",
            "category": "Laboratory Rinse Fixtures",
            "name": "ZX900 target evidence",
            "attributes": attributes,
            "source": {
                "source_path": "D:/evidence/ZX900/index.html",
                "source_sha256": "a" * 64,
                "source_disposition": "OFFICIAL_CURRENT_REVIEW_REQUIRED",
            },
        }

    def test_spray_candidate_is_derived_from_current_target_values(self) -> None:
        official = self._official(
            [
                {"name": "Spray Valve", "raw_value": "AISI316 Dual-mode"},
                {"name": "Spray Valve", "raw_value": "AISI316 Single-mode"},
                {
                    "name": "Dual-function Spray Valve Flow Mode",
                    "raw_value": "Model No.: ZX-SPRAY-D7 (Standard) Wide and focused rinse",
                },
                {
                    "name": "Single-function Spray Valve Flow Mode",
                    "raw_value": "Model No.: ZX-SPRAY-S2 (Optional) Focused rinse only",
                },
            ]
        )
        fields = {row["field"]: row for row in official_field_candidates(official, Path("fallback.jsonl"))}
        spray = fields["spray_valve"]
        self.assertEqual(spray["configuration_resolution"], "STANDARD_DUAL_OPTIONAL_SINGLE")
        self.assertEqual(spray["candidate_value"], "AISI316 Dual-mode (Standard), ZX-SPRAY-D7")
        self.assertIn("ZX-SPRAY-S2", spray["note"])
        self.assertNotIn("FOREIGN-SPRAY", json.dumps(spray, ensure_ascii=False))
        self.assertEqual(spray["source_path"], "D:/evidence/ZX900/index.html")

    def test_ambiguous_spray_semantics_fail_closed(self) -> None:
        official = self._official(
            [
                {"name": "Spray Valve", "raw_value": "AISI316 Dual-mode"},
                {"name": "Spray Valve", "raw_value": "AISI316 Single-mode"},
                {
                    "name": "Dual-function Spray Valve Flow Mode",
                    "raw_value": "Model No.: ZX-SPRAY-D7 Wide and focused rinse",
                },
                {
                    "name": "Single-function Spray Valve Flow Mode",
                    "raw_value": "Model No.: ZX-SPRAY-S2 (Optional) Focused rinse only",
                },
            ]
        )
        fields = {row["field"]: row for row in official_field_candidates(official, Path("fallback.jsonl"))}
        spray = fields["spray_valve"]
        self.assertEqual(spray["configuration_resolution"], "UNRESOLVED")
        self.assertIsNone(spray["candidate_value"])
        self.assertIn("intentionally empty", spray["note"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the workbook blocker unit test")
    def test_workbook_blockers_use_only_current_package_values(self) -> None:
        module_uri = (SCRIPT_DIR / "candidate_workbook_blockers.mjs").as_uri()
        fixture = {
            "review": {
                "target_model": "ZX900",
                "input_counts": {"visual_target_records": 9},
                "commercial_fields_status": "BLOCKED_ZX900_REVIEW",
                "candidates": [
                    {"model": "ZX-COMP-1", "price_eligible": True},
                    {"model": "ZX-COMP-2", "price_eligible": False},
                ],
            },
            "sourceTemplate": {
                "fields": [
                    {
                        "field": "spray_valve",
                        "candidate_value": "AISI316 Dual-mode (Standard), ZX-SPRAY-D7",
                        "configuration_resolution": "STANDARD_DUAL_OPTIONAL_SINGLE",
                    }
                ]
            },
            "assets": {
                "summary": {
                    "official_current_unique_count": 5,
                    "hash_bound_derivation_count": 1,
                    "source_chain_incomplete_count": 0,
                }
            },
        }
        code = (
            f'import {{ buildCandidateWorkbookBlockers as build }} from "{module_uri}";'
            "const value=JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(build(value)));"
        )
        completed = subprocess.run(
            [shutil.which("node") or "node", "--input-type=module", "-e", code, json.dumps(fixture)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        rendered = "\n".join(json.loads(completed.stdout))
        self.assertIn("ZX-SPRAY-D7", rendered)
        self.assertIn("ZX-COMP-1", rendered)
        self.assertIn("5 张官网", rendered)
        self.assertIn("仅 1 条", rendered)
        for foreign_value in ("MODEL-003", "MODEL-022", "MODEL-023", "ES100", "6000×9000"):
            self.assertNotIn(foreign_value, rendered)

    def test_generic_sources_contain_no_known_candidate_fallbacks(self) -> None:
        source_text = "\n".join(
            (SCRIPT_DIR / name).read_text(encoding="utf-8")
            for name in (
                "draft_core.py",
                "build_candidate_workbook.mjs",
                "candidate_workbook_blockers.mjs",
            )
        )
        for foreign_value in ("MODEL-003", "MODEL-022", "MODEL-023", "ES100", "6000×9000"):
            self.assertNotIn(foreign_value, source_text)


if __name__ == "__main__":
    unittest.main()
