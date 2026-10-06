from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from capture_derivation import capture_body_material, object_sha256, observation_material
from validate_localized_counter_capture import field_identity, verify_counter_capture


SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = SCRIPT_DIR.parent / "references" / "schemas"
SESSION = "a" * 64
PAGE = "b" * 64
NOW = datetime(2026, 8, 8, 12, 5, tzinfo=timezone.utc)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


class CounterFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.capture_path = root / "capture.json"
        self.expectation_path = root / "expectation.json"
        fields = [
            {"locale":"en","language_label":"English","direction":"LTR","value":"Easy pull faucet","maximum":128,"unit":"UNICODE_CODE_POINTS"},
            {"locale":"zh-CN","language_label":"简体中文","direction":"LTR","value":"省力预冲洗龙头","maximum":128,"unit":"UTF8_BYTES"},
            {"locale":"ar","language_label":"العربية","direction":"RTL","value":"صنبور شطف","maximum":128,"unit":"PLATFORM_COUNTER"},
        ]
        for row in fields:
            row["value_sha256"] = object_sha256(row["value"])
        unit_map = {"UNICODE_CODE_POINTS":"CHARACTER","UTF8_BYTES":"UTF8_BYTE","PLATFORM_COUNTER":"PLATFORM_REPORTED"}
        observations = []
        for index, row in enumerate(fields, 1):
            if row["unit"] == "UNICODE_CODE_POINTS": count = len(row["value"])
            elif row["unit"] == "UTF8_BYTES": count = len(row["value"].encode("utf-8"))
            else: count = 42
            observation = {
                "observation_type":"LOCALIZED_COUNTER","observation_id":f"counter-{index}",
                "locale":row["locale"],"language_label":row["language_label"],"direction":row["direction"],
                "value":row["value"],"raw_counter_text":f"{count} / {row['maximum']}",
                "observed_count":count,"maximum":row["maximum"],"unit":unit_map[row["unit"]],
                "render_sha256":f"{index:x}" * 64,"control_sha256":f"{index + 3:x}" * 64,
                "page_fingerprint_sha256":PAGE,"browser_session_sha256":SESSION,
            }
            observation["observation_sha256"] = object_sha256(observation_material(observation))
            observations.append(observation)
            row.update(
                observed_count=observation["observed_count"],
                raw_counter_text=observation["raw_counter_text"],
                render_sha256=observation["render_sha256"],
                control_sha256=observation["control_sha256"],
            )
        self.expectation = {
            "schema_version":"sellerkit.alibaba-localized-counter-expectation.v1",
            "expectation_id":"counter-expectation-1","run_id":"run-1","target_model":"MODEL100",
            "template_product_id":"100000000001","browser_session_sha256":SESSION,
            "page_fingerprint_sha256":PAGE,"fields":fields,
            "field_identities_sha256":object_sha256([field_identity(row) for row in fields]),
            "exact_locale_set_required":True,"write_authority":"NONE_READ_ONLY_EVIDENCE","final_write_enabled":False,
        }
        self.capture = {
            "schema_version":"sellerkit.alibaba-browser-observation-capture.v1","capture_id":"capture-1",
            "run_id":"run-1","target_model":"MODEL100","template_product_id":"100000000001",
            "captured_at":"2026-08-08T12:00:00Z","browser_session_sha256":SESSION,"page_fingerprint_sha256":PAGE,
            "source":{"kind":"SYNTHETIC_TEST","trust_basis":"UNAUTHENTICATED_LOCAL_CAPTURE","read_only":True,
                      "login_active":True,"captcha_present":False,"credential_values_recorded":False,"raw_secret_values_recorded":False},
            "observations":observations,"observation_ids_sha256":object_sha256([row["observation_id"] for row in observations]),
            "write_authority":"NONE_READ_ONLY_EVIDENCE","final_write_enabled":False,
        }
        self.rehash_capture(); self.write()

    def rehash_capture(self) -> None:
        for row in self.capture["observations"]:
            row["observation_sha256"] = object_sha256(observation_material(row))
        self.capture["observation_ids_sha256"] = object_sha256([row["observation_id"] for row in self.capture["observations"]])
        self.capture["capture_body_sha256"] = object_sha256(capture_body_material(self.capture))

    def write(self) -> None:
        write_json(self.capture_path, self.capture); write_json(self.expectation_path, self.expectation)

    def verify(self, now: datetime = NOW) -> dict:
        self.write()
        return verify_counter_capture(capture_path=self.capture_path, expectation_path=self.expectation_path, now=now)


class LocalizedCounterCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = CounterFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_blocked(self, needle: str) -> None:
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "BLOCKED_COUNTER_CAPTURE", receipt)
        self.assertTrue(any(needle in error for error in receipt["errors"]), receipt)

    def test_valid_three_unit_capture_is_local_contract_only(self) -> None:
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "PASS_LOCAL_CONTRACT", receipt)
        self.assertEqual(receipt["trust_status"], "LOCAL_CONTRACT")
        self.assertFalse(receipt["final_write_enabled"])
        self.assertEqual(receipt["locale_count"], 3)
        for name in ("browser-observation-capture.schema.json", "localized-counter-expectation.schema.json", "localized-counter-verification-receipt.schema.json"):
            Draft202012Validator.check_schema(json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8")))

    def test_forged_value_with_rehashed_capture_still_disagrees_with_expectation(self) -> None:
        self.fixture.capture["observations"][0]["value"] = "forged"
        self.fixture.rehash_capture()
        self.assert_blocked("value mismatch")

    def test_counter_artifacts_and_raw_readback_are_exactly_bound(self) -> None:
        mutations = (
            ("observed_count", 41, "observed_count mismatch"),
            ("raw_counter_text", "41 / 128", "raw_counter_text mismatch"),
            ("render_sha256", "e" * 64, "render_sha256 mismatch"),
            ("control_sha256", "f" * 64, "control_sha256 mismatch"),
        )
        for key, value, needle in mutations:
            with self.subTest(key=key):
                self.fixture = CounterFixture(Path(self.temporary.name))
                self.fixture.capture["observations"][2][key] = value
                self.fixture.rehash_capture()
                self.assert_blocked(needle)

    def test_missing_or_duplicate_locale_blocks(self) -> None:
        self.fixture.capture["observations"].pop()
        self.fixture.rehash_capture()
        self.assert_blocked("locale set")
        self.fixture = CounterFixture(Path(self.temporary.name))
        duplicate = copy.deepcopy(self.fixture.capture["observations"][0]); duplicate["observation_id"] = "duplicate-en"
        self.fixture.capture["observations"].append(duplicate); self.fixture.rehash_capture()
        self.assert_blocked("duplicate observed locale")

    def test_semantic_duplicate_locale_case_conflicts_block(self) -> None:
        duplicate = copy.deepcopy(self.fixture.capture["observations"][0])
        duplicate.update(observation_id="duplicate-EN", locale="EN", render_sha256="e" * 64, control_sha256="f" * 64)
        self.fixture.capture["observations"].append(duplicate); self.fixture.rehash_capture()
        self.assert_blocked("semantic duplicate observed locale")

        self.fixture = CounterFixture(Path(self.temporary.name))
        duplicate_expected = copy.deepcopy(self.fixture.expectation["fields"][0])
        duplicate_expected.update(locale="EN", render_sha256="e" * 64, control_sha256="f" * 64)
        self.fixture.expectation["fields"].append(duplicate_expected)
        self.fixture.expectation["field_identities_sha256"] = object_sha256(
            [field_identity(row) for row in self.fixture.expectation["fields"]]
        )
        self.assert_blocked("semantic duplicate expected locale")

    def test_wrong_session_and_page_binding_block(self) -> None:
        self.fixture.capture["browser_session_sha256"] = "c" * 64
        self.fixture.rehash_capture()
        self.assert_blocked("browser_session_sha256")

    def test_future_and_stale_capture_block(self) -> None:
        self.fixture.capture["captured_at"] = "2099-01-01T00:00:00Z"; self.fixture.rehash_capture()
        self.assert_blocked("future")
        self.fixture = CounterFixture(Path(self.temporary.name)); self.fixture.capture["captured_at"] = "2026-08-08T11:00:00Z"; self.fixture.rehash_capture()
        self.assert_blocked("older than 15 minutes")

    def test_raw_counter_overflow_and_count_mismatch_block(self) -> None:
        row = self.fixture.capture["observations"][0]
        row["raw_counter_text"] = "counter unavailable"; self.fixture.rehash_capture()
        self.assert_blocked("raw counter text")
        self.fixture = CounterFixture(Path(self.temporary.name)); row = self.fixture.capture["observations"][0]
        row.update(observed_count=129, raw_counter_text="129 / 128"); self.fixture.rehash_capture()
        self.assert_blocked("exceeds maximum")

    def test_platform_counter_rejects_stale_or_ambiguous_count_text(self) -> None:
        row = self.fixture.capture["observations"][2]
        row["raw_counter_text"] = "41 / 128; current 42 / 128"
        self.fixture.expectation["fields"][2]["raw_counter_text"] = row["raw_counter_text"]
        self.fixture.expectation["field_identities_sha256"] = object_sha256(
            [field_identity(field) for field in self.fixture.expectation["fields"]]
        )
        self.fixture.rehash_capture()
        self.assert_blocked("does not uniquely encode current count and maximum")

    def test_render_or_control_evidence_reuse_blocks(self) -> None:
        self.fixture.capture["observations"][1]["control_sha256"] = self.fixture.capture["observations"][0]["control_sha256"]
        self.fixture.rehash_capture(); self.assert_blocked("control artifact reused")

    def test_codex_source_self_report_never_upgrades_trust(self) -> None:
        self.fixture.capture["source"]["kind"] = "CODEX_IN_APP_BROWSER_TOOL_RESULT"; self.fixture.rehash_capture()
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "PASS_LOCAL_CONTRACT", receipt)
        self.assertEqual(receipt["trust_status"], "LOCAL_CONTRACT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
