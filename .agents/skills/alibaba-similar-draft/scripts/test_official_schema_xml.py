from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from jsonschema import Draft202012Validator


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from parse_official_schema_xml import ParseError, main, parse_schema_xml  # noqa: E402


OFFICIAL_EXAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<itemSchema>
  <field id="productTitle" name="Product name" type="input">
    <rules>
      <rule name="requiredRule" value="true"/>
      <rule name="maxLengthRule" value="128" exProperty="include" unit="byte"/>
      <rule name="regexRule" value="[^a-z]+" exProperty="not include"/>
      <rule name="valueTypeRule" value="text"/>
    </rules>
  </field>
  <field id="item_status" name="Status" type="singleCheck">
    <rules><rule name="requiredRule" value="true"/></rules>
    <options>
      <option displayName="On sale" value="0"/>
      <option displayName="Scheduled" value="1"/>
    </options>
    <default-value>0</default-value>
  </field>
  <field id="start_time" name="Start time" type="input">
    <rules>
      <rule name="valueTypeRule" value="time"/>
      <rule name="disableRule" value="true">
        <depend-group operator="and">
          <depend-express fieldId="item_status" value="1" symbol="!="/>
        </depend-group>
      </rule>
    </rules>
  </field>
  <field id="fob" name="FOB Price" type="complex">
    <fields>
      <field id="range_min" type="input">
        <rules><rule name="requiredRule" value="true"/></rules>
      </field>
      <field id="unit_type" type="singleCheck">
        <options><option displayName="USD" value="1"/></options>
      </field>
    </fields>
  </field>
</itemSchema>
"""


class OfficialSchemaXmlTests(unittest.TestCase):
    def parse(self, root: Path, xml: str = OFFICIAL_EXAMPLE):
        xml_path = root / "schema.xml"
        output = root / "normalized.json"
        xml_path.write_text(xml, encoding="utf-8")
        document = parse_schema_xml(
            xml_path=xml_path,
            output=output,
            capture_id="capture-1",
            run_id="run-1",
            target_model="MODEL100",
            template_product_id="100000000001",
            category_id="100007076",
            product_type="NORMAL",
            captured_at="2026-08-08T12:00:00Z",
            valid_minutes=15,
        )
        return document, output

    def test_official_field_rule_option_dependency_and_complex_examples_parse(self):
        with tempfile.TemporaryDirectory() as temporary:
            document, output = self.parse(Path(temporary))
            self.assertEqual(document["status"], "PARSED_MACHINE_RULES_COMPLETE")
            self.assertEqual(document["manual_review_rules"], [])
            self.assertEqual(document["unsupported_constructs"], [])
            self.assertFalse(document["final_write_enabled"])
            title = document["fields"][0]
            self.assertTrue(title["required"])
            max_rule = next(rule for rule in title["rules"] if rule["name"] == "maxLengthRule")
            self.assertEqual(max_rule["value"], "128")
            self.assertEqual(max_rule["attributes"]["unit"], "byte")
            dependency = document["fields"][2]["rules"][1]["depend_groups"][0]
            self.assertEqual(dependency["expressions"][0]["field_id"], "item_status")
            self.assertEqual(document["fields"][3]["children"][0]["path"], "fob.range_min")
            schema = json.loads(
                (SKILL_ROOT / "references" / "schemas" / "official-schema-xml-normalized.schema.json").read_text(encoding="utf-8")
            )
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(json.loads(output.read_text(encoding="utf-8")))

    def test_tip_and_unknown_business_rules_require_review_but_are_not_lost(self):
        xml = OFFICIAL_EXAMPLE.replace(
            '<rule name="valueTypeRule" value="text"/>',
            '<rule name="valueTypeRule" value="text"/><rule name="tipRule" value="Review this"/><rule name="383278799_1" value="Business check"/>',
        )
        with tempfile.TemporaryDirectory() as temporary:
            document, _ = self.parse(Path(temporary), xml)
            self.assertEqual(document["status"], "PARSED_REVIEW_REQUIRED")
            self.assertEqual({entry["rule_name"] for entry in document["manual_review_rules"]}, {"tipRule", "383278799_1"})

    def test_unknown_type_or_dependency_symbol_blocks(self):
        xml = OFFICIAL_EXAMPLE.replace('type="input">\n    <rules>', 'type="mystery">\n    <rules>', 1)
        xml = xml.replace('symbol="!="', 'symbol="approximately"')
        with tempfile.TemporaryDirectory() as temporary:
            document, _ = self.parse(Path(temporary), xml)
            self.assertEqual(document["status"], "BLOCKED_UNSUPPORTED_SCHEMA")
            self.assertTrue(document["unsupported_constructs"])

    def test_doctype_or_entity_is_rejected_before_xml_parse(self):
        xml = '<!DOCTYPE itemSchema [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><itemSchema><field id="x" type="input"/></itemSchema>'
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ParseError, "DTD/entity"):
                self.parse(Path(temporary), xml)

    def test_utf16_and_utf32_doctype_or_entity_are_rejected(self):
        xml = '<?xml version="1.0"?><!DOCTYPE itemSchema [<!ENTITY fid "title">]><itemSchema><field id="&fid;" type="input"/></itemSchema>'
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                xml_path = root / "schema.xml"
                xml_path.write_bytes(xml.encode(encoding))
                with self.assertRaisesRegex(ParseError, "DTD/entity"):
                    parse_schema_xml(
                        xml_path=xml_path, output=root / "normalized.json",
                        capture_id="capture-encoded", run_id="run-encoded",
                        target_model="MODEL100", template_product_id="100000000001",
                        category_id="100007076", product_type="NORMAL",
                    )

    def test_output_is_never_overwritten_and_validity_is_at_most_15_minutes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.parse(root)
            with self.assertRaisesRegex(ParseError, "overwrite"):
                self.parse(root)
            xml_path = root / "other.xml"
            xml_path.write_text(OFFICIAL_EXAMPLE, encoding="utf-8")
            with self.assertRaisesRegex(ParseError, "between 1 and 15"):
                parse_schema_xml(
                    xml_path=xml_path,
                    output=root / "other.json",
                    capture_id="capture-2",
                    run_id="run-2",
                    target_model="MODEL100",
                    template_product_id="100000000001",
                    category_id="100007076",
                    product_type="NORMAL",
                    valid_minutes=16,
                )

    def test_missing_capture_time_is_replay_review_only_and_not_fresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            xml_path = root / "schema.xml"
            output = root / "normalized.json"
            xml_path.write_text(OFFICIAL_EXAMPLE, encoding="utf-8")
            document = parse_schema_xml(
                xml_path=xml_path, output=output,
                capture_id="capture-replay", run_id="run-replay",
                target_model="MODEL100", template_product_id="100000000001",
                category_id="100007076", product_type="NORMAL",
            )
            self.assertEqual(document["status"], "REPLAY_REVIEW_ONLY")
            self.assertIsNone(document["captured_at"])
            self.assertIsNone(document["expires_at"])
            self.assertFalse(document["freshness_eligible"])
            self.assertEqual(document["raw_xml"]["bytes"], xml_path.stat().st_size)
            self.assertEqual(len(document["raw_xml"]["sha256"]), 64)

    def test_cli_review_and_replay_are_nonzero_but_machine_complete_is_zero(self):
        def invoke(root: Path, *, xml: str, captured_at: str | None) -> int:
            xml_path = root / "schema.xml"
            output = root / "normalized.json"
            xml_path.write_text(xml, encoding="utf-8")
            argv = [
                "--xml-path", str(xml_path), "--output", str(output),
                "--capture-id", "capture-cli", "--run-id", "run-cli",
                "--target-model", "MODEL100", "--template-product-id", "100000000001",
                "--category-id", "100007076", "--product-type", "NORMAL",
            ]
            if captured_at is not None:
                argv.extend(["--captured-at", captured_at])
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return main(argv)

        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(invoke(Path(temporary), xml=OFFICIAL_EXAMPLE, captured_at=None), 4)
        review_xml = OFFICIAL_EXAMPLE.replace(
            '<rule name="valueTypeRule" value="text"/>',
            '<rule name="valueTypeRule" value="text"/><rule name="tipRule" value="Review"/>',
        )
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(invoke(Path(temporary), xml=review_xml, captured_at="2026-08-08T12:00:00Z"), 4)
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(invoke(Path(temporary), xml=OFFICIAL_EXAMPLE, captured_at="2026-08-08T12:00:00Z"), 0)


if __name__ == "__main__":
    unittest.main()
