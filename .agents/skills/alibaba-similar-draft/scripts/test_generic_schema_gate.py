from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from draft_core import GuardError, build_generic_title_candidate  # noqa: E402
from dynamic_schema_gate import (  # noqa: E402
    field_set_sha256,
    receipt_sha256,
    required_field_set_sha256,
    validate_dynamic_schema_capture,
)
from dynamic_schema_test_support import build_official_dynamic_schema  # noqa: E402
from normalize_dynamic_publish_schema import normalize_capture  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def schema_capture(root: Path, *, captured_at: datetime | None = None, stem: str = "soap") -> Path:
    path, _, _ = build_official_dynamic_schema(
        root,
        fields=[
            {
                "logical_name": "product_title",
                "observed_label": "Product Name",
                "required": True,
                "max_length": 128,
                "max_length_unit": "PLATFORM_COUNTER",
            },
            {"logical_name": "capacity", "observed_label": "Capacity", "required": True},
            {"logical_name": "body_mount", "observed_label": "Mounting Type", "required": False},
        ],
        run_id="run-soap-1",
        target_model="SD100",
        template_product_id="123456789",
        category_id="soap-dispenser-leaf",
        product_type="NORMAL",
        captured_at=captured_at,
        stem=stem,
    )
    return path


class GenericTitleTests(unittest.TestCase):
    def test_non_faucet_category_never_receives_faucet_fallback(self) -> None:
        official = {
            "category": "Soap Dispensers",
            "attributes": [
                {"name": "Material", "raw_value": "SUS304", "position": 1, "is_cover": True},
                {"name": "Installation Type", "raw_value": "Wall Mounted", "position": 2, "is_cover": True},
                {"name": "Capacity", "raw_value": "1000 ml", "position": 3, "is_cover": True},
            ],
        }
        result = build_generic_title_candidate(official)
        self.assertIn("Soap Dispensers", result["candidate_value"])
        self.assertIn("SUS304", result["candidate_value"])
        self.assertNotIn("faucet", result["candidate_value"].casefold())
        self.assertEqual(
            result["derivation"],
            "TARGET_CATEGORY_PLUS_OFFICIAL_FACTS_PLUS_EXPLICIT_APPROVED_CLAIMS",
        )

    def test_missing_category_fails_instead_of_guessing_product_family(self) -> None:
        with self.assertRaisesRegex(GuardError, "no target category"):
            build_generic_title_candidate({"name": "Unknown Product", "attributes": []})

    def test_claim_requires_explicit_approved_evidence_binding(self) -> None:
        with self.assertRaisesRegex(GuardError, "claim marked for use"):
            build_generic_title_candidate(
                {
                    "category": "Soap Dispensers",
                    "attributes": [],
                    "title_claim_decisions": [{"use_in_title": True, "status": "PENDING", "text": "Fast refill"}],
                }
            )

    def test_explicit_approved_claim_can_extend_its_own_category_title(self) -> None:
        result = build_generic_title_candidate(
            {
                "category": "Drinking Taps",
                "attributes": [{"name": "Material", "raw_value": "SUS304", "position": 1}],
                "title_claim_decisions": [
                    {
                        "use_in_title": True,
                        "status": "APPROVED",
                        "text": "Touchless Sensor",
                        "decision_id": "claim-decision-1",
                        "source_sha256": "c" * 64,
                    }
                ],
            }
        )
        self.assertIn("Drinking Taps", result["candidate_value"])
        self.assertIn("Touchless Sensor", result["candidate_value"])
        self.assertNotIn("Pre-Rinse", result["candidate_value"])


class DynamicSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dynamic-schema-gate-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def validate(self, path: Path) -> dict:
        return validate_dynamic_schema_capture(
            path,
            expected_target_model="SD100",
            expected_template_product_id="123456789",
            expected_run_id="run-soap-1",
        )

    def test_required_fields_come_only_from_current_capture(self) -> None:
        result = self.validate(schema_capture(self.root))
        self.assertEqual(result["required_field_ids"], ["capacity", "product_title"])
        self.assertNotIn("height", result["required_field_ids"])
        self.assertNotIn("body_material", result["required_field_ids"])

    def test_tampered_required_set_hash_fails_closed(self) -> None:
        path = schema_capture(self.root)
        document = json.loads(path.read_text(encoding="utf-8"))
        field = next(row for row in document["fields"] if row["logical_name"] == "body_mount")
        field["required"] = True
        document["field_set_sha256"] = field_set_sha256(document)
        document["required_field_set_sha256"] = required_field_set_sha256(document)
        document["normalizer"]["receipt_sha256"] = receipt_sha256(document)
        document["capture_id"] = f"dynamic-schema-{document['normalizer']['receipt_sha256'][:20]}"
        write_json(path, document)
        with self.assertRaisesRegex(GuardError, "not derived"):
            self.validate(path)

    def test_expired_capture_fails_closed(self) -> None:
        with self.assertRaisesRegex(GuardError, "expired"):
            schema_capture(
                self.root,
                captured_at=datetime.now(timezone.utc) - timedelta(minutes=16),
                stem="expired",
            )

    def test_future_capture_fails_closed(self) -> None:
        with self.assertRaisesRegex(GuardError, "future"):
            schema_capture(
                self.root,
                captured_at=datetime.now(timezone.utc) + timedelta(minutes=6),
                stem="future",
            )

    def test_raw_payload_hash_drift_fails_closed(self) -> None:
        path = schema_capture(self.root)
        raw_path = self.root / "soap-schema.xml"
        raw_path.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(GuardError, "raw XML payload byte-size mismatch|raw XML payload hash mismatch"):
            self.validate(path)

    def test_normalizer_emits_a_verifiable_current_capture_and_refuses_overwrite(self) -> None:
        output, official, raw_path = build_official_dynamic_schema(
            self.root,
            fields=[{"logical_name": "product_title", "required": True}],
            run_id="run-soap-1",
            target_model="SD100",
            template_product_id="123456789",
            category_id="soap-dispenser-leaf",
            product_type="NORMAL",
            stem="normalizer",
        )
        receipt = {"status": "PASS_CURRENT_SCHEMA_BOUND", "capture": self.validate(output)}
        self.assertEqual(receipt["status"], "PASS_CURRENT_SCHEMA_BOUND")
        self.assertEqual(receipt["capture"]["required_field_ids"], ["product_title"])
        with self.assertRaisesRegex(GuardError, "refusing to overwrite"):
            normalize_capture(
                raw_payload=raw_path,
                official_schema=official,
                output=output,
                run_id="run-soap-1",
                target_model="SD100",
                template_product_id="123456789",
                category_id="soap-dispenser-leaf",
                product_type="NORMAL",
                source_kind="OFFICIAL_SCHEMA_GET",
                source_endpoint="https://eco.taobao.com/router/rest",
            )

    def test_evil_endpoint_and_operation_mismatch_are_rejected(self) -> None:
        _, official, raw = build_official_dynamic_schema(
            self.root,
            fields=[{"logical_name": "product_title", "required": True}],
            run_id="run-soap-1", target_model="SD100", template_product_id="123456789",
            category_id="soap-dispenser-leaf", product_type="NORMAL", stem="source",
        )
        for endpoint, operation, message in (
            ("https://evil.invalid/router/rest", "alibaba.icbu.product.schema.get", "official TOP"),
            ("https://eco.taobao.com/router/rest", "schema.get", "source operation"),
        ):
            with self.subTest(endpoint=endpoint, operation=operation), self.assertRaisesRegex(GuardError, message):
                normalize_capture(
                    raw_payload=raw, official_schema=official, output=self.root / f"bad-{len(endpoint)}-{len(operation)}.json",
                    run_id="run-soap-1", target_model="SD100", template_product_id="123456789",
                    category_id="soap-dispenser-leaf", product_type="NORMAL", source_kind="OFFICIAL_SCHEMA_GET",
                    source_endpoint=endpoint, source_operation=operation,
                )

    def test_non_xml_raw_and_arbitrary_observations_are_rejected(self) -> None:
        _, official, raw = build_official_dynamic_schema(
            self.root,
            fields=[{"logical_name": "product_title", "required": True}],
            run_id="run-soap-1", target_model="SD100", template_product_id="123456789",
            category_id="soap-dispenser-leaf", product_type="NORMAL", stem="inputs",
        )
        non_xml = self.root / "not-xml.json"
        write_json(non_xml, {"fields": [{"logical_name": "forged"}]})
        with self.assertRaisesRegex(GuardError, "same raw XML path"):
            normalize_capture(
                raw_payload=non_xml, official_schema=official, output=self.root / "nonxml-output.json",
                run_id="run-soap-1", target_model="SD100", template_product_id="123456789",
                category_id="soap-dispenser-leaf", product_type="NORMAL", source_kind="OFFICIAL_SCHEMA_GET",
                source_endpoint="https://eco.taobao.com/router/rest",
            )
        observations = self.root / "forged-observations.json"
        write_json(observations, {"fields": [{"logical_name": "forged", "required": True}]})
        with self.assertRaisesRegex(GuardError, "arbitrary field observations"):
            normalize_capture(
                raw_payload=raw, official_schema=official, field_observations=observations,
                output=self.root / "observations-output.json", run_id="run-soap-1", target_model="SD100",
                template_product_id="123456789", category_id="soap-dispenser-leaf", product_type="NORMAL",
                source_kind="OFFICIAL_SCHEMA_GET", source_endpoint="https://eco.taobao.com/router/rest",
            )

    def test_visible_ui_source_is_explicitly_local_only_and_blocked_live(self) -> None:
        raw = self.root / "visible.json"
        write_json(raw, {"fields": []})
        with self.assertRaisesRegex(GuardError, "LOCAL_CONTRACT_ONLY/BLOCKED_LIVE"):
            normalize_capture(
                raw_payload=raw, official_schema=None, output=self.root / "visible-output.json",
                run_id="run-soap-1", target_model="SD100", template_product_id="123456789",
                category_id="soap-dispenser-leaf", product_type="NORMAL", source_kind="VISIBLE_UI_ZERO_WRITE",
                source_endpoint="https://post.alibaba.com/product/publish.htm",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
