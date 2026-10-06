from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import test_action_plan as action_fixture
import test_compile_approval as approval_fixture
from compile_approval import CompileBlocked, compile_approval
from compile_browser_action_plan import PlanCompileError, compile_action_plan
from draft_core import read_json, sha256_file
from localized_field_contract import (
    LocalizedFieldError,
    build_localized_target,
    validate_localized_target,
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def localized_capable_adapter(source: Path, output: Path) -> Path:
    adapter = read_json(source)
    adapter["capabilities"] = {
        "localized_fields": {
            "enabled": True,
            "locator_id": "field.localized_runtime",
            "resolution_source": "CURRENT_VISIBLE_LANGUAGE_LABEL_AND_LOCALE",
            "direction_validation_required": True,
            "max_length_rule_validation_required": True,
            "page_fingerprint_binding_required": True,
            "current_probe_required": True,
            "cached_language_index_allowed": False,
            "fixed_language_order_allowed": False,
            "coordinate_fallback_allowed": False,
        }
    }
    adapter["logical_locators"].append(
        {
            "locator_id": "field.localized_runtime",
            "kind": "LOCALIZED_FORM_FIELD",
            "resolve_using": ["CURRENT_VISIBLE_LANGUAGE_LABEL", "APPROVED_LOCALE", "CURRENT_ACCESSIBLE_ROLE"],
            "required_cardinality": "EXACTLY_ONE_VISIBLE_CONTROL",
            "persist_resolution": False,
        }
    )
    write_json(output, adapter)
    return output


class LocalizedFieldContractTests(unittest.TestCase):
    def target(self, locale: str = "en", label: str = "English", value: str = "Commercial faucet") -> dict:
        now = datetime.now(timezone.utc)
        return build_localized_target(
            logical_name="product_title",
            locale=locale,
            live_language_label=label,
            value=value,
            page_fingerprint_sha256="a" * 64,
            live_control_evidence_sha256="b" * 64,
            maximum=128,
            unit="UNICODE_CODE_POINTS",
            observed_count=len(value),
            captured_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            expires_at=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        )

    def test_ltr_and_rtl_targets_are_canonical(self) -> None:
        en = self.target()
        ar = self.target("ar", "العربية", "صنبور شطف مسبق تجاري")
        self.assertEqual(en["direction"], "LTR")
        self.assertEqual(ar["direction"], "RTL")
        validate_localized_target(en, "Commercial faucet", expected_page_fingerprint_sha256="a" * 64)
        validate_localized_target(ar, "صنبور شطف مسبق تجاري")

    def test_direction_hash_and_page_drift_fail_closed(self) -> None:
        for mutation, expected in (
            (lambda row: row.__setitem__("direction", "RTL"), "direction"),
            (lambda row: row.__setitem__("identity_sha256", "0" * 64), "identity_sha256"),
            (lambda row: row.__setitem__("max_length_rule_hash", "0" * 64), "max_length_rule_hash"),
        ):
            target = self.target()
            mutation(target)
            with self.assertRaisesRegex(LocalizedFieldError, expected):
                validate_localized_target(target, "Commercial faucet")
        with self.assertRaisesRegex(LocalizedFieldError, "page fingerprint"):
            validate_localized_target(self.target(), "Commercial faucet", expected_page_fingerprint_sha256="c" * 64)

    def test_expired_and_oversize_rules_fail_closed(self) -> None:
        target = self.target()
        target["max_length_rule"]["expires_at"] = "2020-01-01T00:00:00Z"
        from draft_core import canonical_json, sha256_bytes
        target["max_length_rule_hash"] = sha256_bytes(canonical_json(target["max_length_rule"]).encode("utf-8"))
        target["identity_sha256"] = "0" * 64
        with self.assertRaisesRegex(LocalizedFieldError, "expired"):
            validate_localized_target(target, "Commercial faucet")

    def test_platform_counter_without_bound_live_evidence_is_blocked(self) -> None:
        now = datetime.now(timezone.utc)
        with self.assertRaisesRegex(LocalizedFieldError, "live-counter evidence"):
            build_localized_target(
                logical_name="product_title", locale="zh-CN", live_language_label="简体中文",
                value="中" * 5000, page_fingerprint_sha256="a" * 64,
                live_control_evidence_sha256="b" * 64, maximum=128,
                unit="PLATFORM_COUNTER", observed_count=0,
                captured_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                expires_at=(now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            )

    def test_utf8_and_maximum_drift_are_blocked(self) -> None:
        now = datetime.now(timezone.utc)
        with self.assertRaisesRegex(LocalizedFieldError, "UTF8_BYTES observed_count"):
            build_localized_target(
                logical_name="product_title", locale="zh-CN", live_language_label="简体中文",
                value="中文", page_fingerprint_sha256="a" * 64,
                live_control_evidence_sha256="b" * 64, maximum=20,
                unit="UTF8_BYTES", observed_count=2,
                captured_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                expires_at=(now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            )
        with self.assertRaisesRegex(LocalizedFieldError, "exceeds"):
            build_localized_target(
                logical_name="product_title", locale="zh-CN", live_language_label="简体中文",
                value="中文", page_fingerprint_sha256="a" * 64,
                live_control_evidence_sha256="b" * 64, maximum=1,
                unit="UNICODE_CODE_POINTS", observed_count=2,
                captured_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                expires_at=(now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            )

    def _inject_localized(self, case: action_fixture.ActionPlanCompilerTests, *, capable: bool) -> dict:
        value = "صنبور شطف مسبق تجاري"
        target = self.target("ar", "العربية", value)
        source = read_json(case.paths["source"])
        key = "localized.product_title.ar"
        source["sources"][0]["approved_fields"].append(key)
        source["approved_field_values"][key] = {
            "value": value,
            "value_sha256": target["expected_sha256"],
            "source_id": "facts",
            "decision_id": "facts-decision",
            "write_target": target,
        }
        write_json(case.paths["source"], source)
        assets = read_json(case.paths["asset"])
        assets["bindings"]["approved_source_manifest_sha256"] = sha256_file(case.paths["source"])
        write_json(case.paths["asset"], assets)
        if capable:
            adapter = localized_capable_adapter(case.paths["adapter"], case.root / "localized-adapter.json")
            case.paths["adapter"] = adapter
        approval = read_json(case.paths["write_approval"])
        approval["bindings"]["approved_source_manifest_sha256"] = sha256_file(case.paths["source"])
        approval["bindings"]["approved_asset_manifest_sha256"] = sha256_file(case.paths["asset"])
        approval["bindings"]["adapter_sha256"] = sha256_file(case.paths["adapter"])
        write_json(case.paths["write_approval"], approval)
        return target

    def test_default_adapter_explicitly_rejects_localized_fields(self) -> None:
        case = action_fixture.ActionPlanCompilerTests("test_partial_plan_is_hash_bound_fail_closed_and_non_ascii_safe")
        case.setUp()
        try:
            self._inject_localized(case, capable=False)
            with self.assertRaisesRegex(PlanCompileError, "lacks explicit LOCALIZED_FIELD support"):
                case.compile()
        finally:
            case.tearDown()

    def test_capable_adapter_compiles_per_locale_write_clear_and_readback(self) -> None:
        case = action_fixture.ActionPlanCompilerTests("test_partial_plan_is_hash_bound_fail_closed_and_non_ascii_safe")
        case.setUp()
        try:
            target = self._inject_localized(case, capable=True)
            plan = case.compile()
            writes = [row for row in plan["phases"][1]["operations"] if row["target"].get("kind") == "LOCALIZED_FIELD"]
            clears = [row for row in plan["phases"][0]["operations"] if row["target"].get("kind") == "LOCALIZED_FIELD"]
            verifies = [row for row in plan["phases"][3]["operations"] if row["target"].get("kind") == "LOCALIZED_FIELD"]
            self.assertEqual((len(writes), len(clears), len(verifies)), (1, 1, 1))
            self.assertEqual(writes[0]["target"], target)
            self.assertEqual(writes[0]["readback"]["identity_sha256"], target["identity_sha256"])
            self.assertEqual(writes[0]["length_validation"]["rule_hash"], target["max_length_rule_hash"])
            self.assertEqual(verifies[0]["localized_identity_sha256"], target["identity_sha256"])
            self.assertTrue(all(row["decision"] == "HARD_DENY" for row in plan["forbidden_actions"]))
        finally:
            case.tearDown()

    def test_approval_compiler_emits_per_locale_manifest_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = approval_fixture.ApprovalFixture(Path(temp) / "多语言审批")
            source = Path(temp) / "多语言标题.txt"
            source.write_text("English and Arabic approved titles\n", encoding="utf-8")
            decision = fixture.partial_decision_document()
            localized = []
            for locale, label, value in (("en", "English", "Commercial faucet"), ("ar", "العربية", "صنبور شطف مسبق تجاري")):
                localized.append(
                    {
                        "value": value,
                        "source_selector": {
                            "kind": "EXPLICIT_SOURCE_FILE",
                            "path": str(source.resolve()),
                            "bytes": source.stat().st_size,
                            "sha256": sha256_file(source),
                            "source_role": "PRODUCT_FACT",
                        },
                        "decision_id": f"localized-{locale}-decision",
                        "write_target": self.target(locale, label, value),
                    }
                )
            decision["approved_localized_fields"] = localized
            fixture.write_partial_decision(decision)
            receipt = compile_approval(fixture.candidate, fixture.decision, fixture.output)
            manifest = read_json(fixture.output / "approved_source_manifest.json")
            self.assertEqual(receipt["approved_localized_field_count"], 2)
            self.assertEqual(set(k for k in manifest["approved_field_values"] if k.startswith("localized.")), {"localized.product_title.en", "localized.product_title.ar"})

    def test_duplicate_locale_approval_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = approval_fixture.ApprovalFixture(Path(temp) / "重复语言")
            source = Path(temp) / "title.txt"
            source.write_text("approved\n", encoding="utf-8")
            target = self.target()
            row = {
                "value": "Commercial faucet",
                "source_selector": {"kind": "EXPLICIT_SOURCE_FILE", "path": str(source.resolve()), "bytes": source.stat().st_size, "sha256": sha256_file(source), "source_role": "PRODUCT_FACT"},
                "decision_id": "localized-en-decision",
                "write_target": target,
            }
            decision = fixture.partial_decision_document()
            decision["approved_localized_fields"] = [row, {**copy.deepcopy(row), "decision_id": "localized-en-decision-2"}]
            fixture.write_partial_decision(decision)
            with self.assertRaisesRegex(CompileBlocked, "duplicate localized"):
                compile_approval(fixture.candidate, fixture.decision, fixture.output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
