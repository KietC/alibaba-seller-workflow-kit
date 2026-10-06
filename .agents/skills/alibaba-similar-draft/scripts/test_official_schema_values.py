from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from parse_official_schema_xml import parse_schema_xml
from capture_derivation import (
    capture_body_material,
    derive_current_field_rows,
    observation_material,
)
from validate_official_schema_values import main, object_sha256, sha256_file, verify_values


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
SESSION = "a" * 64
XML = """<?xml version="1.0" encoding="UTF-8"?>
<itemSchema>
  <field id="title" name="Title" type="input"><rules>
    <rule name="requiredRule" value="true"/>
    <rule name="minLengthRule" value="2" unit="character"/>
    <rule name="maxLengthRule" value="8" unit="byte"/>
    <rule name="regexRule" value="^[A-Z]" exProperty="include"/>
    <rule name="regexRule" value="[0-9]" exProperty="not include"/>
    <rule name="valueTypeRule" value="text"/>
  </rules></field>
  <field id="status" name="Status" type="singleCheck"><rules><rule name="requiredRule" value="true"/></rules>
    <options><option displayName="A" value="A"/><option displayName="B" value="B"/></options>
  </field>
  <field id="start" name="Start" type="input"><rules>
    <rule name="disableRule" value="true"><depend-group operator="and"><depend-express fieldId="status" symbol="!=" value="B"/></depend-group></rule>
    <rule name="valueTypeRule" value="time"/>
  </rules></field>
  <field id="qty" name="Quantity" type="input"><rules>
    <rule name="valueTypeRule" value="integer"/><rule name="minValueRule" value="1"/><rule name="maxValueRule" value="10"/>
  </rules></field>
  <field id="tags" name="Tags" type="multiInput"><rules>
    <rule name="minInputNumRule" value="1"/><rule name="maxInputNumRule" value="2"/>
  </rules></field>
  <field id="color" name="Color" type="multiCheck">
    <options><option displayName="Red" value="red"/><option displayName="Blue" value="blue"/></options>
  </field>
</itemSchema>
"""


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def flatten(fields: list[dict]) -> list[dict]:
    result = []
    for field in fields:
        result.append(field)
        result.extend(flatten(field["children"]))
    return result


class ValueFixture:
    def __init__(self, root: Path, xml: str = XML) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.xml_path = root / "schema.xml"
        self.normalized_path = root / "normalized.json"
        self.capture_path = root / "browser-observations.json"
        self.values_path = root / "values.json"
        self.xml_path.write_text(xml, encoding="utf-8")
        self.normalized = parse_schema_xml(
            xml_path=self.xml_path, output=self.normalized_path, capture_id="capture-1", run_id="run-1",
            target_model="MODEL100", template_product_id="100000000001", category_id="100007076",
            product_type="NORMAL", captured_at="2026-08-08T12:00:00Z", valid_minutes=15,
        )
        self.values = {"title": "ABCD", "status": "A", "start": None, "qty": "5", "tags": ["one", "two"], "color": ["red"]}
        self.disabled = {"start": True}
        self.targeted = {field_id: field_id != "start" for field_id in self.values}
        self.capture_source_kind = "SYNTHETIC_TEST"
        self._write_values()

    def _write_values(self) -> None:
        fields = flatten(self.normalized["fields"])
        observations = []
        for field in fields:
            value = copy.deepcopy(self.values.get(field["id"]))
            observation = {
                "observation_type": "OFFICIAL_FIELD_VALUE",
                "observation_id": f"obs-{field['id']}", "path": field["path"], "id": field["id"],
                "value": value, "observed_disabled": self.disabled.get(field["id"], False),
                "write_targeted": self.targeted.get(field["id"], False),
            }
            observation["observation_sha256"] = object_sha256(observation_material(observation))
            observations.append(observation)
        capture = {
            "schema_version": "sellerkit.alibaba-browser-observation-capture.v1",
            "capture_id": "browser-capture-1", "run_id": "run-1", "target_model": "MODEL100",
            "template_product_id": "100000000001", "captured_at": "2026-08-08T12:05:00Z",
            "browser_session_sha256": SESSION, "page_fingerprint_sha256": "f" * 64,
            "source": {
                "kind": self.capture_source_kind, "trust_basis": "UNAUTHENTICATED_LOCAL_CAPTURE",
                "read_only": True, "login_active": True, "captcha_present": False,
                "credential_values_recorded": False, "raw_secret_values_recorded": False,
            },
            "observations": observations,
            "observation_ids_sha256": object_sha256([row["observation_id"] for row in observations]),
            "write_authority": "NONE_READ_ONLY_EVIDENCE", "final_write_enabled": False,
        }
        capture["capture_body_sha256"] = object_sha256(capture_body_material(capture))
        write_json(self.capture_path, capture)
        rows, derivation_errors = derive_current_field_rows(capture)
        if derivation_errors:
            raise AssertionError(derivation_errors)
        ledger = {
            "schema_version": "sellerkit.alibaba-official-schema-current-values.v2", "ledger_id": "ledger-1",
            "run_id": "run-1", "target_model": "MODEL100", "template_product_id": "100000000001",
            "captured_at": "2026-08-08T12:05:00Z", "browser_session_sha256": SESSION,
            "bindings": {
                "normalized_schema_sha256": sha256_file(self.normalized_path),
                "raw_schema_sha256": self.normalized["raw_xml"]["sha256"], "schema_capture_id": "capture-1",
                "schema_field_paths_sha256": self.normalized["field_paths_sha256"],
                "schema_rule_graph_sha256": self.normalized["rule_graph_sha256"],
                "observation_capture_id": capture["capture_id"],
                "observation_capture_sha256": sha256_file(self.capture_path),
                "observation_capture_body_sha256": capture["capture_body_sha256"],
                "observation_ids_sha256": capture["observation_ids_sha256"],
            },
            "source": {
                "kind": "CAPTURE_DERIVED_VISIBLE_FORM_READBACK", "capture_source_kind": self.capture_source_kind,
                "trust_status": "LOCAL_CONTRACT", "read_only": True, "login_active": True,
                "captcha_present": False, "credential_values_recorded": False,
            },
            "fields": rows, "field_paths_sha256": object_sha256([row["path"] for row in rows]),
            "write_authority": "NONE_READ_ONLY_EVIDENCE", "final_write_enabled": False,
        }
        body = copy.deepcopy(ledger)
        ledger["ledger_body_sha256"] = object_sha256(body)
        write_json(self.values_path, ledger)

    def set_value(self, field_id: str, value, *, disabled: bool | None = None, write_targeted: bool | None = None) -> None:
        self.values[field_id] = value
        if disabled is not None:
            self.disabled[field_id] = disabled
        if write_targeted is not None:
            self.targeted[field_id] = write_targeted
        self._write_values()

    def verify(self, **overrides) -> dict:
        arguments = {
            "normalized_schema_path": self.normalized_path, "current_values_path": self.values_path,
            "capture_path": self.capture_path,
            "expected_run_id": "run-1", "expected_target_model": "MODEL100",
            "expected_template_product_id": "100000000001", "expected_browser_session_sha256": SESSION,
            "now": datetime(2026, 8, 8, 12, 6, tzinfo=timezone.utc),
        }
        arguments.update(overrides)
        return verify_values(**arguments)


class OfficialSchemaValueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ValueFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_blocked(self, needle: str) -> None:
        receipt = self.fixture.verify()
        self.assertIn(receipt["status"], {"BLOCKED_VALUE_VALIDATION", "BLOCKED_UNSUPPORTED_SCHEMA"}, receipt)
        haystack = receipt["errors"] + receipt["unsupported_rules"]
        self.assertTrue(any(needle in row for row in haystack), receipt)

    def test_all_supported_rules_pass_and_receipts_validate(self) -> None:
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "PASS_MACHINE_RULES", receipt)
        self.assertEqual(receipt["trust_status"], "LOCAL_CONTRACT", receipt)
        self.assertFalse(receipt["live_attestation_verified"], receipt)
        self.assertEqual(receipt["field_count"], 6)
        for name in (
            "browser-observation-capture.schema.json",
            "official-schema-current-values.schema.json",
            "official-schema-values-verification-receipt.schema.json",
        ):
            schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
        receipt_schema = json.loads((SCHEMA_DIR / "official-schema-values-verification-receipt.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(receipt_schema).validate(receipt)

    def test_capture_reserves_hash_bound_localized_counter_observation(self) -> None:
        capture = json.loads(self.fixture.capture_path.read_text(encoding="utf-8"))
        localized = {
            "observation_type": "LOCALIZED_COUNTER", "observation_id": "counter-ar-SA-title",
            "locale": "ar-SA", "language_label": "العربية", "direction": "RTL",
            "value": "منتج", "raw_counter_text": "4 / 128", "observed_count": 4,
            "maximum": 128, "unit": "CHARACTER", "render_sha256": "c" * 64,
            "control_sha256": "d" * 64,
            "page_fingerprint_sha256": capture["page_fingerprint_sha256"],
            "browser_session_sha256": capture["browser_session_sha256"],
        }
        localized["observation_sha256"] = object_sha256(observation_material(localized))
        capture["observations"].append(localized)
        capture["observation_ids_sha256"] = object_sha256(
            [row["observation_id"] for row in capture["observations"]]
        )
        capture["capture_body_sha256"] = object_sha256(capture_body_material(capture))
        write_json(self.fixture.capture_path, capture)

        ledger = json.loads(self.fixture.values_path.read_text(encoding="utf-8"))
        ledger["bindings"].update(
            observation_capture_sha256=sha256_file(self.fixture.capture_path),
            observation_capture_body_sha256=capture["capture_body_sha256"],
            observation_ids_sha256=capture["observation_ids_sha256"],
        )
        body = copy.deepcopy(ledger)
        body.pop("ledger_body_sha256", None)
        ledger["ledger_body_sha256"] = object_sha256(body)
        write_json(self.fixture.values_path, ledger)
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "PASS_MACHINE_RULES", receipt)
        self.assertEqual(receipt["field_count"], 6, receipt)

    def test_localized_counter_must_bind_capture_page_and_session(self) -> None:
        capture = json.loads(self.fixture.capture_path.read_text(encoding="utf-8"))
        localized = {
            "observation_type": "LOCALIZED_COUNTER", "observation_id": "counter-en-US-title",
            "locale": "en-US", "language_label": "English", "direction": "LTR",
            "value": "Title", "raw_counter_text": "5 / 128", "observed_count": 5,
            "maximum": 128, "unit": "CHARACTER", "render_sha256": "c" * 64,
            "control_sha256": "d" * 64, "page_fingerprint_sha256": "e" * 64,
            "browser_session_sha256": capture["browser_session_sha256"],
        }
        localized["observation_sha256"] = object_sha256(observation_material(localized))
        capture["observations"].append(localized)
        capture["observation_ids_sha256"] = object_sha256(
            [row["observation_id"] for row in capture["observations"]]
        )
        capture["capture_body_sha256"] = object_sha256(capture_body_material(capture))
        write_json(self.fixture.capture_path, capture)
        self.assert_blocked("localized counter page fingerprint mismatch")

    def test_required_and_byte_character_length_are_enforced(self) -> None:
        self.fixture.set_value("title", "")
        self.assert_blocked("required value is empty")
        self.fixture.set_value("title", "A你好啊")
        self.assert_blocked("exceeds 8 byte")
        self.fixture.set_value("title", "A")
        self.assert_blocked("shorter than 2 character")

    def test_value_type_and_numeric_range_are_enforced(self) -> None:
        self.fixture.set_value("qty", "five")
        self.assert_blocked("does not match type integer")
        self.fixture.set_value("qty", "11")
        self.assert_blocked("numeric maximum")
        self.fixture.set_value("qty", "0")
        self.assert_blocked("numeric minimum")

    def test_options_and_input_counts_are_enforced(self) -> None:
        self.fixture.set_value("color", ["green"])
        self.assert_blocked("not an allowed option")
        self.fixture.set_value("tags", ["one", "two", "three"])
        self.assert_blocked("input count exceeds 2")

    def test_empty_collection_does_not_bypass_minimum_input_count(self) -> None:
        self.fixture.set_value("tags", [])
        self.assert_blocked("input count is below 1")

    def test_regex_include_and_not_include_are_enforced(self) -> None:
        self.fixture.set_value("title", "abcd")
        self.assert_blocked("does not include required regex")
        self.fixture.set_value("title", "ABC1")
        self.assert_blocked("includes forbidden regex")

    def test_disable_dependency_and_write_target_are_enforced(self) -> None:
        self.fixture.set_value("start", None, disabled=False, write_targeted=False)
        self.assert_blocked("observed disabled state")
        self.fixture.set_value("start", None, disabled=True, write_targeted=True)
        self.assert_blocked("disabled/read-only field is write-targeted")
        self.fixture.set_value("status", "B")
        self.fixture.set_value("start", "12:30", disabled=False, write_targeted=True)
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "PASS_MACHINE_RULES", receipt)

    def test_schema_run_target_template_session_and_raw_hash_are_bound(self) -> None:
        self.assertEqual(self.fixture.verify(expected_run_id="wrong")["status"], "BLOCKED_VALUE_VALIDATION")
        self.assertEqual(self.fixture.verify(expected_browser_session_sha256="b" * 64)["status"], "BLOCKED_VALUE_VALIDATION")
        self.fixture.xml_path.write_text(XML + "\n", encoding="utf-8")
        self.assert_blocked("raw schema XML file bytes/hash drifted")

    def test_manual_and_unknown_business_rules_are_review_required(self) -> None:
        for rule in ('<rule name="tipRule" value="Review"/>', '<rule name="383278799_1" value="Business check"/>'):
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as temporary:
                xml = XML.replace('<rule name="valueTypeRule" value="text"/>', f'<rule name="valueTypeRule" value="text"/>{rule}')
                fixture = ValueFixture(Path(temporary), xml)
                receipt = fixture.verify()
                self.assertEqual(receipt["status"], "REVIEW_REQUIRED", receipt)
                self.assertTrue(receipt["manual_review_rules"])

    def test_unsupported_machine_rule_and_invalid_regex_block(self) -> None:
        xml = XML.replace('<rule name="valueTypeRule" value="text"/>', '<rule name="valueTypeRule" value="text"/><rule name="maxTargetSizeRule" value="10"/>')
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ValueFixture(Path(temporary), xml)
            receipt = fixture.verify()
            self.assertEqual(receipt["status"], "BLOCKED_UNSUPPORTED_SCHEMA", receipt)
        xml = XML.replace('value="^[A-Z]"', 'value="["')
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ValueFixture(Path(temporary), xml)
            receipt = fixture.verify()
            self.assertEqual(receipt["status"], "BLOCKED_UNSUPPORTED_SCHEMA", receipt)

    def test_expired_schema_and_rehashed_ledger_tampering_block(self) -> None:
        receipt = self.fixture.verify(now=datetime(2026, 8, 8, 12, 20, tzinfo=timezone.utc))
        self.assertEqual(receipt["status"], "BLOCKED_VALUE_VALIDATION", receipt)
        ledger = json.loads(self.fixture.values_path.read_text(encoding="utf-8"))
        ledger["fields"][0].update(
            value="TAMPERED", value_sha256=object_sha256("TAMPERED"), value_present=True,
            observation_sha256="b" * 64,
        )
        body = copy.deepcopy(ledger)
        body.pop("ledger_body_sha256", None)
        ledger["ledger_body_sha256"] = object_sha256(body)
        write_json(self.fixture.values_path, ledger)
        self.assert_blocked("not capture-derived")

    def test_rehashed_capture_evidence_cannot_be_substituted_in_ledger(self) -> None:
        ledger = json.loads(self.fixture.values_path.read_text(encoding="utf-8"))
        row = ledger["fields"][0]
        row["observation_id"] = "forged-observation"
        row["observation_sha256"] = object_sha256({"forged": True})
        body = copy.deepcopy(ledger)
        body.pop("ledger_body_sha256", None)
        ledger["ledger_body_sha256"] = object_sha256(body)
        write_json(self.fixture.values_path, ledger)
        self.assert_blocked("not capture-derived")

    def test_capture_observation_and_body_hashes_are_recomputed(self) -> None:
        capture = json.loads(self.fixture.capture_path.read_text(encoding="utf-8"))
        capture["observations"][0]["value"] = "FORGED"
        # Rehashing the outer body cannot hide a stale per-observation hash.
        capture["capture_body_sha256"] = object_sha256(capture_body_material(capture))
        write_json(self.fixture.capture_path, capture)
        self.assert_blocked("browser observation hash mismatch")

    def test_codex_self_report_remains_local_and_cli_does_not_return_success(self) -> None:
        self.fixture.capture_source_kind = "CODEX_IN_APP_BROWSER_TOOL_RESULT"
        self.fixture._write_values()
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "PASS_MACHINE_RULES", receipt)
        self.assertEqual(receipt["trust_status"], "LOCAL_CONTRACT", receipt)
        self.assertFalse(receipt["live_attestation_verified"], receipt)
        output = self.fixture.root / "cli-receipt.json"
        with patch("validate_official_schema_values.verify_values", return_value=receipt), patch("builtins.print"):
            exit_code = main([
                "--normalized-schema", str(self.fixture.normalized_path),
                "--current-values", str(self.fixture.values_path),
                "--capture", str(self.fixture.capture_path),
                "--expected-run-id", "run-1", "--expected-target-model", "MODEL100",
                "--expected-template-product-id", "100000000001",
                "--expected-browser-session-sha256", SESSION, "--receipt", str(output),
            ])
        self.assertEqual(exit_code, 4)
        self.assertTrue(output.is_file())

    def test_caller_cannot_promote_local_capture_to_live_attested(self) -> None:
        ledger = json.loads(self.fixture.values_path.read_text(encoding="utf-8"))
        ledger["source"]["trust_status"] = "LIVE_ATTESTED"
        body = copy.deepcopy(ledger)
        body.pop("ledger_body_sha256", None)
        ledger["ledger_body_sha256"] = object_sha256(body)
        write_json(self.fixture.values_path, ledger)
        receipt = self.fixture.verify()
        self.assertEqual(receipt["trust_status"], "LOCAL_CONTRACT", receipt)
        self.assertFalse(receipt["live_attestation_verified"], receipt)
        self.assertEqual(receipt["status"], "BLOCKED_VALUE_VALIDATION", receipt)
        self.assertTrue(any("trust status" in row for row in receipt["errors"]), receipt)

    def test_live_attested_branch_is_unreachable_without_external_verifier_receipt(self) -> None:
        # Even replacing the local trust classifier cannot fabricate the
        # required verified-attestation receipt or live_attestation_verified.
        with patch("validate_official_schema_values.capture_trust_status", return_value="LIVE_ATTESTED"):
            receipt = self.fixture.verify()
        self.assertEqual(receipt["trust_status"], "LIVE_ATTESTED", receipt)
        self.assertFalse(receipt["live_attestation_verified"], receipt)
        self.assertEqual(receipt["status"], "BLOCKED_VALUE_VALIDATION", receipt)
        self.assertTrue(any("verification receipt schema" in row for row in receipt["errors"]), receipt)

    def test_future_schema_and_value_capture_are_blocked(self) -> None:
        normalized = json.loads(self.fixture.normalized_path.read_text(encoding="utf-8"))
        normalized["captured_at"] = "2099-01-01T00:00:00Z"
        normalized["expires_at"] = "2099-01-01T00:15:00Z"
        write_json(self.fixture.normalized_path, normalized)
        ledger = json.loads(self.fixture.values_path.read_text(encoding="utf-8"))
        ledger["captured_at"] = "2099-01-01T00:05:00Z"
        ledger["bindings"]["normalized_schema_sha256"] = sha256_file(self.fixture.normalized_path)
        body = copy.deepcopy(ledger)
        body.pop("ledger_body_sha256", None)
        ledger["ledger_body_sha256"] = object_sha256(body)
        write_json(self.fixture.values_path, ledger)
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "BLOCKED_VALUE_VALIDATION", receipt)
        self.assertTrue(any("future" in row for row in receipt["errors"]), receipt)

    def test_rehashed_normalized_rule_tamper_still_disagrees_with_raw_xml(self) -> None:
        normalized = json.loads(self.fixture.normalized_path.read_text(encoding="utf-8"))
        title = normalized["fields"][0]
        next(rule for rule in title["rules"] if rule["name"] == "requiredRule")["value"] = "false"

        def rehash(field: dict) -> None:
            for rule in field["rules"]:
                material = copy.deepcopy(rule)
                material.pop("rule_sha256", None)
                rule["rule_sha256"] = object_sha256(material)
            for child in field["children"]:
                rehash(child)
            field["rule_sha256"] = object_sha256({
                "path": field["path"], "type": field["type"], "rules": field["rules"],
                "options": field["options"],
                "children_rule_sha256": [child["rule_sha256"] for child in field["children"]],
            })

        for field in normalized["fields"]:
            rehash(field)
        normalized["rule_graph_sha256"] = object_sha256([
            {"path": field["path"], "rule_sha256": field["rule_sha256"]}
            for field in flatten(normalized["fields"])
        ])
        write_json(self.fixture.normalized_path, normalized)

        ledger = json.loads(self.fixture.values_path.read_text(encoding="utf-8"))
        row = next(item for item in ledger["fields"] if item["id"] == "title")
        row.update(value="", value_sha256=object_sha256(""), value_present=False)
        ledger["bindings"]["normalized_schema_sha256"] = sha256_file(self.fixture.normalized_path)
        ledger["bindings"]["schema_rule_graph_sha256"] = normalized["rule_graph_sha256"]
        body = copy.deepcopy(ledger)
        body.pop("ledger_body_sha256", None)
        ledger["ledger_body_sha256"] = object_sha256(body)
        write_json(self.fixture.values_path, ledger)
        receipt = self.fixture.verify()
        self.assertEqual(receipt["status"], "BLOCKED_VALUE_VALIDATION", receipt)
        self.assertTrue(any("does not match bound raw XML" in row for row in receipt["errors"]), receipt)


if __name__ == "__main__":
    unittest.main()
