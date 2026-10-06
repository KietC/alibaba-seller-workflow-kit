from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import draft_guard  # noqa: E402
from draft_core import (  # noqa: E402
    EMPTY_READBACK_SHA256,
    canonical_json,
    sha256_bytes,
    sha256_file,
)


def object_sha256(value: object) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def field_target(logical_name: str, section: str) -> dict[str, object]:
    return {
        "kind": "FIELD_REGISTRY",
        "logical_name": logical_name,
        "section": section,
        "runtime_key": None,
    }


def category_target(property_name: str) -> dict[str, object]:
    return {
        "kind": "CATEGORY_PROPERTY_RUNTIME_KEY",
        "logical_name": "category_properties",
        "section": "CATEGORY_PROPERTIES",
        "approved_property_name": property_name,
        "runtime_key": None,
    }


def localized_target(logical_name: str, value: str) -> dict[str, object]:
    value_sha256 = object_sha256(value)
    rule: dict[str, object] = {
        "source": "CURRENT_VISIBLE_UI",
        "captured_at": "2026-08-07T12:04:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "locale": "en",
        "live_language_label_sha256": object_sha256("English"),
        "page_fingerprint_sha256": "b" * 64,
        "live_control_evidence_sha256": "e" * 64,
        "maximum": 128,
        "unit": "PLATFORM_COUNTER",
        "observed_count": len(value),
        "candidate_value_sha256": value_sha256,
        "current_session_only": True,
        "require_post_write_counter_readback": True,
        "final_write_enabled": False,
    }
    target: dict[str, object] = {
        "kind": "LOCALIZED_FIELD",
        "logical_name": logical_name,
        "locale": "en",
        "live_language_label": "English",
        "direction": "LTR",
        "expected_sha256": value_sha256,
        "max_length_rule_hash": object_sha256(rule),
        "max_length_rule": rule,
        "identity_sha256": "",
    }
    identity = {
        "logical_name": target["logical_name"],
        "locale": target["locale"],
        "live_language_label": target["live_language_label"],
        "direction": target["direction"],
        "expected_sha256": target["expected_sha256"],
        "max_length_rule_hash": target["max_length_rule_hash"],
    }
    target["identity_sha256"] = object_sha256(identity)
    return target


def locator(adapter_ref: str) -> dict[str, object]:
    return {
        "adapter_ref": adapter_ref,
        "resolve_from_current_probe": True,
        "reuse_prior_resolution": False,
    }


def build_run_spec() -> dict[str, object]:
    return {
        "schema_version": "sellerkit.similar_draft_run_spec.v1",
        "run_kind": "CREATE_SIMILAR_DRAFT",
        "run_id": "MODEL-016",
        "generated_at": "2026-08-08T12:00:00Z",
        "status": "BLOCKED_REVIEW_REQUIRED",
        "target_model": "MODEL-003",
        "template_product_id": "100000000014",
        "mode": "save_draft",
        "approval_observed": False,
        "external_write_enabled": False,
        "final_write_enabled": False,
        "direct_endpoint_write_enabled": False,
        "max_draft_save_count": 1,
        "template_scope": "FORM_STRUCTURE_ONLY",
        "inherited_content_must_be_cleared": True,
        "required_preflight_status": "PASS",
        "required_probe_status": "PASS_ZERO_WRITE",
        "stop_state_before_final_submit": "DRAFT_SAVED_FINAL_REVIEW_READY",
        "endpoint_policy": {
            "path": "references/endpoint-policy.v1.json",
            "sha256": "6" * 64,
            "policy_mode": "FAIL_CLOSED_AUDIT_ONLY",
        },
        "workspace_profile": {
            "profile_id": "fixture-profile",
            "path": "profiles/fixture.json",
            "sha256": "7" * 64,
        },
    }


def build_action_plan(run_spec_sha256: str) -> dict[str, object]:
    title_value = "Commercial SUS304 Pre-Rinse Faucet"
    model_value = "MODEL-003"
    title_target = localized_target("multilingual_product_title", title_value)
    model_target = category_target("model")
    price_target = field_target("trade_price", "TRADE")
    title_hash = object_sha256(title_value)
    model_hash = object_sha256(model_value)
    title_rule = title_target["max_length_rule"]
    assert isinstance(title_rule, dict)
    write_operations = [
        {
            "order": 1,
            "operation": "WRITE_APPROVED_FIELD_VALUE",
            "target": title_target,
            "locator": locator("field.multilingual_product_title_runtime"),
            "value": title_value,
            "value_sha256": title_hash,
            "approval": {"source_id": "official-MODEL-003", "decision_id": "decision-title"},
            "readback": {
                "required": True,
                "compare": "LOCALIZED_CANONICAL_IDENTITY_AND_VALUE_SHA256",
                "expected_sha256": title_hash,
                "identity_sha256": title_target["identity_sha256"],
                "max_length_rule_hash": title_target["max_length_rule_hash"],
                "expected_direction": "LTR",
                "expected_live_language_label": "English",
                "require_current_counter_within_rule": True,
            },
            "length_validation": {
                "rule_hash": title_target["max_length_rule_hash"],
                "unit": title_rule["unit"],
                "maximum": title_rule["maximum"],
                "expected_count": title_rule["observed_count"],
                "page_fingerprint_sha256": title_rule["page_fingerprint_sha256"],
                "require_current_counter_readback": True,
                "failure_action": "STOP_PRESERVE_EVIDENCE",
            },
        },
        {
            "order": 2,
            "operation": "WRITE_APPROVED_FIELD_VALUE",
            "target": model_target,
            "locator": locator("field.category_property_runtime"),
            "value": model_value,
            "value_sha256": model_hash,
            "approval": {"source_id": "official-MODEL-003", "decision_id": "decision-model"},
            "readback": {
                "required": True,
                "compare": "CANONICAL_VALUE_SHA256",
                "expected_sha256": model_hash,
            },
        },
    ]
    clear_operations = [
        {
            "order": 1,
            "operation": "CLEAR_INHERITED_FIELD",
            "target": title_target,
            "locator": locator("field.multilingual_product_title_runtime"),
            "reason": "LOCALIZED_CLEAR_BEFORE_FILL",
            "readback": {"required": True, "expected_state": "EMPTY"},
        },
        {
            "order": 2,
            "operation": "CLEAR_INHERITED_FIELD",
            "target": model_target,
            "locator": locator("field.category_property_runtime"),
            "reason": "REGISTRY_CLEAR_BEFORE_FILL",
            "readback": {"required": True, "expected_state": "EMPTY"},
        },
        {
            "order": 3,
            "operation": "CLEAR_INHERITED_FIELD",
            "target": price_target,
            "locator": locator("field.trade_price"),
            "reason": "BLOCKED_FIELD_MUST_REMAIN_EMPTY",
            "readback": {"required": True, "expected_state": "EMPTY"},
        },
    ]
    triplet = {
        "approved_source_manifest_sha256": "1" * 64,
        "approved_asset_manifest_sha256": "2" * 64,
        "run_spec_sha256": run_spec_sha256,
    }
    triplet["binding_sha256"] = object_sha256(triplet)
    plan: dict[str, object] = {
        "schema_version": "alibaba.browser-action-plan.v1",
        "plan_id": "MODEL-012",
        "generated_at": "2026-08-08T12:05:00Z",
        "compiler": {
            "name": "compile_browser_action_plan.py",
            "version": "fixture-1",
            "sha256": "3" * 64,
            "browser_or_network_executed": False,
            "output_semantics": "PLAN_ONLY_NO_SIDE_EFFECTS",
        },
        "target": {
            "model": "MODEL-003",
            "run_id": "MODEL-016",
            "template_product_id": "100000000014",
            "mode": "SAVE_DRAFT_ONLY",
            "maximum_draft_save_count": 1,
        },
        "source_profile": "PARTIAL_DRAFT_FILL_APPROVED_ONLY",
        "quality": {
            "terminal_quality": "REVIEW_REQUIRED",
            "publish_ready": False,
            "incomplete_draft_allowed": True,
            "final_write_count_limit": 0,
        },
        "bindings": {
            "three_party": triplet,
            "write_approval": {
                "sha256": "8" * 64,
                "approval_id": "write-approval-fixture",
                "decision_id": "write-decision-fixture",
                "approved_at": "2026-08-08T12:01:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "target_model": "MODEL-003",
                "run_id": "MODEL-016",
                "template_product_id": "100000000014",
                "execution_binding_sha256": "9" * 64,
            },
            "field_registry": {
                "sha256": "4" * 64,
                "registry_id": "fixture-registry",
                "registry_version": "1.0.0",
            },
            "endpoint_policy": {
                "sha256": "6" * 64,
                "schema_version": "alibaba.endpoint-policy.v1",
                "default_decision": "DENY",
            },
            "adapter": {
                "sha256": "5" * 64,
                "adapter_id": "fixture-adapter",
                "adapter_version": "1.0.0",
            },
        },
        "runtime_requirements": {
            "current_probe_required": True,
            "current_probe_must_be_newer_than_plan": True,
            "reparse_after_every_page_change": True,
            "discard_prior_locators": True,
            "resolve_dynamic_category_keys_from_current_page_only": True,
            "fail_on_non_unique_locator": True,
            "fail_on_captcha_or_login": True,
            "fail_on_unknown_write_request": True,
            "write_approval_must_be_unexpired_at_execution": True,
            "localized_fields_require_live_language_label": True,
            "localized_fields_require_direction_match": True,
            "localized_max_length_rule_must_match_current_page": True,
            "localized_locator_requires_adapter_capability": True,
        },
        "invariants": {
            "clear_all_inherited_fields_before_any_write": True,
            "write_values_only_from_approved_field_values": True,
            "blocked_fields_never_written": True,
            "asset_path_bytes_sha256_bound": True,
            "asset_upload_order_bound": True,
            "save_draft_visible_ui_only": True,
            "save_draft_requires_valid_write_approval": True,
            "direct_endpoint_write_allowed": False,
            "submit_or_final_write_decision": "HARD_DENY",
            "final_write_permanently_disabled": True,
            "localized_field_identity_hash_bound": True,
            "localized_field_values_are_per_locale": True,
        },
        "blocked_fields": ["price", "minimum_order_quantity"],
        "phases": [
            {
                "order": 1,
                "name": "CLEAR_INHERITED_FIELDS",
                "must_complete_before_next_phase": True,
                "operations": clear_operations,
            },
            {
                "order": 2,
                "name": "WRITE_APPROVED_FIELD_VALUES",
                "must_complete_before_next_phase": True,
                "operations": write_operations,
            },
            {
                "order": 3,
                "name": "UPLOAD_APPROVED_ASSETS",
                "must_complete_before_next_phase": True,
                "operations": [],
            },
            {
                "order": 4,
                "name": "VERIFY_READBACK",
                "must_complete_before_next_phase": True,
                "operations": [
                    {
                        "order": index,
                        "operation": "VERIFY_FIELD_READBACK",
                        "target": operation["target"],
                        "expected_sha256": operation["value_sha256"],
                        **(
                            {
                                "localized_identity_sha256": operation["target"]["identity_sha256"],
                                "max_length_rule_hash": operation["target"]["max_length_rule_hash"],
                            }
                            if operation["target"].get("kind") == "LOCALIZED_FIELD"
                            else {}
                        ),
                        "failure_action": "STOP_PRESERVE_EVIDENCE",
                    }
                    for index, operation in enumerate(write_operations, 1)
                ],
            },
            {
                "order": 5,
                "name": "SAVE_DRAFT_VISIBLE_UI",
                "must_complete_before_next_phase": True,
                "operations": [
                    {
                        "order": 1,
                        "operation": "SAVE_DRAFT",
                        "locator": locator("action.save_draft_runtime"),
                        "visible_ui_required": True,
                        "direct_endpoint_call_allowed": False,
                        "maximum_execution_count": 1,
                        "requires_all_prior_phase_readbacks": True,
                        "approval": {
                            "approval_id": "write-approval-fixture",
                            "decision_id": "write-decision-fixture",
                            "write_approval_sha256": "8" * 64,
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    }
                ],
            },
            {
                "order": 6,
                "name": "STOP_BEFORE_FINAL_WRITE",
                "must_complete_before_next_phase": True,
                "operations": [
                    {
                        "order": 1,
                        "operation": "STOP_AND_PRESERVE_REVIEW_STATE",
                        "terminal_quality": "REVIEW_REQUIRED",
                        "publish_ready": False,
                    }
                ],
            },
        ],
        "forbidden_actions": [
            {
                "logical_name": "submit_product",
                "classification": "FINAL_WRITE",
                "decision": "HARD_DENY",
                "permanent": True,
                "visible_ui_click_allowed": False,
                "direct_endpoint_call_allowed": False,
                "adapter_ref": "guard.final_submit",
            },
            {
                "logical_name": "all_final_write_endpoints",
                "classification": "FINAL_WRITE",
                "decision": "HARD_DENY",
                "permanent": True,
                "visible_ui_click_allowed": False,
                "direct_endpoint_call_allowed": False,
                "endpoint_policy_sha256": "6" * 64,
            },
        ],
    }
    plan["plan_body_sha256"] = object_sha256(plan)
    return plan


def expected_set_hashes(action_plan: dict[str, object]) -> tuple[str, str, str]:
    phases = action_plan["phases"]
    assert isinstance(phases, list)
    writes = phases[1]["operations"]
    clears = phases[0]["operations"]
    assert isinstance(writes, list) and isinstance(clears, list)
    field_rows = sorted(
        (
            {
                "target": operation["target"],
                "expected_sha256": operation["value_sha256"],
                "source_id": operation["approval"]["source_id"],
                "decision_id": operation["approval"]["decision_id"],
            }
            for operation in writes
        ),
        key=lambda row: canonical_json(row["target"]),
    )
    clear_rows = sorted(
        (
            {"target": operation["target"], "reason": operation["reason"]}
            for operation in clears
        ),
        key=lambda row: canonical_json(row["target"]),
    )
    write_target_keys = {canonical_json(operation["target"]) for operation in writes}
    post_refresh_empty_rows = [
        row for row in clear_rows if canonical_json(row["target"]) not in write_target_keys
    ]
    return (
        object_sha256(field_rows),
        object_sha256(clear_rows),
        object_sha256(post_refresh_empty_rows),
    )


def build_valid_case() -> dict[str, object]:
    run_spec = build_run_spec()
    run_spec_sha256 = object_sha256(run_spec)
    action_plan = build_action_plan(run_spec_sha256)
    action_plan_sha256 = object_sha256(action_plan)
    probe = {
        "schema_version": "sellerkit.browser_probe_receipt.v3",
        "generated_at": "2026-08-08T12:08:00Z",
        "probe_captured_at": "2026-08-08T12:07:00Z",
        "input_probe_sha256": "a" * 64,
        "target_model": "MODEL-003",
        "template_product_id": "100000000014",
        "browser_session_sha256": "c" * 64,
        "page_fingerprint": "b" * 64,
        "bindings": {
            "run_spec_sha256": run_spec_sha256,
            "action_plan_sha256": action_plan_sha256,
            "action_plan_id": action_plan["plan_id"],
            "endpoint_policy_sha256": "6" * 64,
            "adapter_sha256": "5" * 64,
            "probe_read_activation_request_sha256": "4" * 64,
        },
        "request_classifications": [
            {"classification": "READ", "decision": "ALLOW"}
        ],
        "errors": [],
        "zero_write_confirmed": True,
        "authorization_errors": [],
        "execution_authorized": True,
        "adapter_production_execution_ready": True,
        "production_authorization_errors": [],
        "production_execution_authorized": True,
        "status": "PASS_ZERO_WRITE",
    }
    probe_sha256 = object_sha256(probe)
    field_set_sha256, clear_set_sha256, post_refresh_empty_set_sha256 = expected_set_hashes(
        action_plan
    )
    common_bindings = {
        "action_plan_sha256": action_plan_sha256,
        "action_plan_body_sha256": action_plan["plan_body_sha256"],
        "run_spec_sha256": run_spec_sha256,
        "probe_receipt_sha256": probe_sha256,
        "expected_field_set_sha256": field_set_sha256,
        "expected_clear_set_sha256": clear_set_sha256,
        "expected_post_refresh_empty_set_sha256": post_refresh_empty_set_sha256,
    }
    phases = action_plan["phases"]
    assert isinstance(phases, list)
    writes = phases[1]["operations"]
    clears = phases[0]["operations"]
    assert isinstance(writes, list) and isinstance(clears, list)
    field_readbacks = []
    for operation in writes:
        row = {
            "target": copy.deepcopy(operation["target"]),
            "expected_sha256": operation["value_sha256"],
            "observed_sha256": operation["value_sha256"],
            "source_id": operation["approval"]["source_id"],
            "decision_id": operation["approval"]["decision_id"],
        }
        target = operation["target"]
        assert isinstance(target, dict)
        if target.get("kind") == "LOCALIZED_FIELD":
            rule = target["max_length_rule"]
            assert isinstance(rule, dict)
            row.update(
                {
                    "observed_locale": target["locale"],
                    "observed_live_language_label": target["live_language_label"],
                    "observed_direction": target["direction"],
                    "observed_count": rule["observed_count"],
                    "observed_maximum": rule["maximum"],
                    "observed_unit": rule["unit"],
                    "max_length_rule_hash": target["max_length_rule_hash"],
                    "current_page_fingerprint_sha256": rule["page_fingerprint_sha256"],
                    "post_write_counter_readback": True,
                }
            )
        field_readbacks.append(row)
    clear_readbacks = [
        {
            "target": copy.deepcopy(operation["target"]),
            "reason": operation["reason"],
            "captured_phase": "CLEAR_INHERITED_FIELDS",
            "expected_state": "EMPTY",
            "observed_state": "EMPTY",
            "expected_sha256": EMPTY_READBACK_SHA256,
            "observed_sha256": EMPTY_READBACK_SHA256,
        }
        for operation in clears
    ]
    write_target_keys = {canonical_json(operation["target"]) for operation in writes}
    post_refresh_clear_readbacks = [
        {
            **copy.deepcopy(row),
            "captured_phase": "POST_REFRESH_REOPEN",
        }
        for row in clear_readbacks
        if canonical_json(row["target"]) not in write_target_keys
    ]
    non_target_sha256 = object_sha256({"price": None, "moq": None})
    fill_execution = {
        "schema_version": "sellerkit.fill-execution.v1",
        "execution_id": "fill-execution-fixture",
        "run_id": "MODEL-016",
        "captured_at": "2026-08-08T12:10:00Z",
        "phase": "POST_SAVE_PRE_REFRESH",
        "target_model": "MODEL-003",
        "template_product_id": "100000000014",
        "draft_id": "draft-100000000016",
        "browser_session_sha256": "c" * 64,
        "page_fingerprint_sha256": "b" * 64,
        "bindings": copy.deepcopy(common_bindings),
        "field_readbacks": copy.deepcopy(field_readbacks),
        "cleared_targets": copy.deepcopy(clear_readbacks),
        "non_target_snapshot_before_sha256": non_target_sha256,
        "non_target_snapshot_after_sha256": non_target_sha256,
        "non_target_field_diffs": [],
        "inherited_content_cleared": True,
        "template_video_present": False,
        "all_planned_actions_executed": True,
        "visible_ui_save_used": True,
        "direct_endpoint_write_used": False,
        "draft_save_count": 1,
        "final_write_count": 0,
        "final_submit_triggered": False,
    }
    post_refresh = {
        "schema_version": "sellerkit.post-refresh-verification.v1",
        "verification_id": "post-refresh-fixture",
        "run_id": "MODEL-016",
        "captured_at": "2026-08-08T12:12:00Z",
        "phase": "POST_REFRESH_REOPEN",
        "target_model": "MODEL-003",
        "template_product_id": "100000000014",
        "draft_id": "draft-100000000016",
        "browser_session_sha256": "c" * 64,
        "page_fingerprint_sha256": "b" * 64,
        "bindings": copy.deepcopy(common_bindings),
        "field_readbacks": copy.deepcopy(field_readbacks),
        "cleared_targets": copy.deepcopy(post_refresh_clear_readbacks),
        "non_target_snapshot_before_sha256": non_target_sha256,
        "non_target_snapshot_after_sha256": non_target_sha256,
        "non_target_field_diffs": [],
        "inherited_content_cleared": True,
        "template_video_present": False,
        "refresh_performed": True,
        "same_draft_reopened": True,
        "persistence_verified": True,
        "draft_save_count": 1,
        "final_write_count": 0,
        "final_submit_triggered": False,
    }
    return {
        "run_spec": run_spec,
        "run_spec_sha256": run_spec_sha256,
        "action_plan": action_plan,
        "action_plan_sha256": action_plan_sha256,
        "probe": probe,
        "probe_sha256": probe_sha256,
        "fill": fill_execution,
        "refresh": post_refresh,
    }


def verify_case(case: dict[str, object]) -> dict[str, object]:
    return draft_guard.strict_verify_fill(
        case["fill"],
        case["refresh"],
        case["action_plan"],
        case["run_spec"],
        case["probe"],
        action_plan_sha256=case["action_plan_sha256"],
        run_spec_sha256=case["run_spec_sha256"],
        probe_receipt_sha256=case["probe_sha256"],
    )


def rebind_probe(case: dict[str, object]) -> None:
    probe_sha256 = object_sha256(case["probe"])
    case["probe_sha256"] = probe_sha256
    for key in ("fill", "refresh"):
        document = case[key]
        assert isinstance(document, dict)
        bindings = document["bindings"]
        assert isinstance(bindings, dict)
        bindings["probe_receipt_sha256"] = probe_sha256


class StrictFillGateTests(unittest.TestCase):
    def assert_failed(self, case: dict[str, object], needle: str | None = None) -> None:
        result = verify_case(case)
        self.assertEqual(result["status"], "FAIL", result)
        if needle:
            self.assertTrue(any(needle in error for error in result["errors"]), result)

    def test_exact_execution_and_post_refresh_contract_passes(self) -> None:
        case = build_valid_case()
        result = verify_case(case)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["expected_field_count"], 2)
        self.assertEqual(result["expected_clear_count"], 3)
        self.assertEqual(result["expected_post_refresh_empty_count"], 1)
        schema = json.loads(
            (SCHEMA_DIR / "fill-verification-receipt.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(result)

    def test_capture_timestamps_must_be_nonfuture_and_ordered(self) -> None:
        mutations = (
            (
                "future fill",
                lambda case: case["fill"].__setitem__("captured_at", "2099-01-01T00:00:00Z"),
                "fill execution captured_at is in the future",
            ),
            (
                "future refresh",
                lambda case: case["refresh"].__setitem__("captured_at", "2099-01-01T00:00:00Z"),
                "post-refresh captured_at is in the future",
            ),
            (
                "refresh before fill",
                lambda case: case["refresh"].__setitem__("captured_at", "2026-08-08T12:09:59Z"),
                "post-refresh captured_at is earlier than fill execution captured_at",
            ),
            (
                "historical fill before probe",
                lambda case: (
                    case["fill"].__setitem__("captured_at", "2000-01-01T00:00:00Z"),
                    case["refresh"].__setitem__("captured_at", "2000-01-01T00:00:01Z"),
                ),
                "fill execution captured_at predates the verified current probe",
            ),
            (
                "fill too long after probe",
                lambda case: (
                    case["fill"].__setitem__("captured_at", "2026-08-08T12:40:00Z"),
                    case["refresh"].__setitem__("captured_at", "2026-08-08T12:41:00Z"),
                ),
                "fill execution started more than 30 minutes",
            ),
            (
                "refresh too long after fill",
                lambda case: case["refresh"].__setitem__("captured_at", "2026-08-08T12:41:00Z"),
                "post-refresh capture is more than 30 minutes",
            ),
        )
        for name, mutate, needle in mutations:
            with self.subTest(name=name):
                case = build_valid_case()
                mutate(case)
                self.assert_failed(case, needle)

    def test_fill_and_refresh_must_share_current_browser_and_page_bindings(self) -> None:
        mutations = (
            (
                "missing browser session",
                lambda case: case["fill"].pop("browser_session_sha256"),
                "fill execution browser session binding is invalid",
            ),
            (
                "different browser session",
                lambda case: case["refresh"].__setitem__("browser_session_sha256", "d" * 64),
                "browser session bindings differ",
            ),
            (
                "coordinated but not probed browser session",
                lambda case: (
                    case["fill"].__setitem__("browser_session_sha256", "d" * 64),
                    case["refresh"].__setitem__("browser_session_sha256", "d" * 64),
                ),
                "fill execution browser session does not match the current probe",
            ),
            (
                "different page fingerprint",
                lambda case: case["refresh"].__setitem__("page_fingerprint_sha256", "d" * 64),
                "page fingerprint bindings differ",
            ),
            (
                "fill page not current probe",
                lambda case: (
                    case["fill"].__setitem__("page_fingerprint_sha256", "d" * 64),
                    case["refresh"].__setitem__("page_fingerprint_sha256", "d" * 64),
                ),
                "fill execution page fingerprint does not match the current probe",
            ),
        )
        for name, mutate, needle in mutations:
            with self.subTest(name=name):
                case = build_valid_case()
                mutate(case)
                self.assert_failed(case, needle)

    def test_localized_readback_must_bind_unit_and_post_write_counter(self) -> None:
        mutations = (
            (
                "wrong observed unit",
                lambda case: case["fill"]["field_readbacks"][0].__setitem__(
                    "observed_unit", "UTF8_BYTES"
                ),
                "localized observed_unit mismatch",
            ),
            (
                "missing post-write counter proof",
                lambda case: case["refresh"]["field_readbacks"][0].pop(
                    "post_write_counter_readback"
                ),
                "localized post_write_counter_readback mismatch",
            ),
            (
                "false post-write counter proof",
                lambda case: case["fill"]["field_readbacks"][0].__setitem__(
                    "post_write_counter_readback", False
                ),
                "localized post_write_counter_readback mismatch",
            ),
        )
        for name, mutate, needle in mutations:
            with self.subTest(name=name):
                case = build_valid_case()
                mutate(case)
                self.assert_failed(case, needle)

    def test_non_allow_probe_decisions_never_pass_fill_gate(self) -> None:
        for decision in ("DENY", "HARD_DENY", "UNKNOWN", "ALLOW_UI_ONLY"):
            with self.subTest(decision=decision):
                case = build_valid_case()
                probe = case["probe"]
                assert isinstance(probe, dict)
                classifications = probe["request_classifications"]
                assert isinstance(classifications, list) and isinstance(classifications[0], dict)
                classifications[0]["decision"] = decision
                probe["execution_authorized"] = False
                probe["authorization_errors"] = [f"request classification 0 is not ALLOW: {decision}"]
                rebind_probe(case)
                self.assert_failed(case, "does not authorize controlled execution")

    def test_read_authorization_cannot_bypass_adapter_production_readiness(self) -> None:
        case = build_valid_case()
        probe = case["probe"]
        assert isinstance(probe, dict)
        probe["adapter_production_execution_ready"] = False
        probe["production_execution_authorized"] = False
        probe["production_authorization_errors"] = [
            "browser adapter production execution readiness is not certified"
        ]
        rebind_probe(case)
        self.assert_failed(case, "does not authorize production execution")
        self.assert_failed(case, "adapter production readiness is not certified")

    def test_forged_execution_authorized_flag_cannot_override_denied_request(self) -> None:
        case = build_valid_case()
        probe = case["probe"]
        assert isinstance(probe, dict)
        classifications = probe["request_classifications"]
        assert isinstance(classifications, list) and isinstance(classifications[0], dict)
        classifications[0]["decision"] = "DENY"
        probe["execution_authorized"] = True
        probe["authorization_errors"] = []
        rebind_probe(case)
        self.assert_failed(case, "probe request classification 0 is not ALLOW")

    def test_legacy_v2_probe_receipt_cannot_authorize_fill(self) -> None:
        case = build_valid_case()
        probe = case["probe"]
        assert isinstance(probe, dict)
        probe["schema_version"] = "sellerkit.browser_probe_receipt.v2"
        rebind_probe(case)
        self.assert_failed(case, "execution-gated v3 contract")

    def test_legacy_arbitrary_nonempty_field_readbacks_fail_closed(self) -> None:
        legacy = {
            "target_model": "MODEL-003",
            "template_product_id": "100000000014",
            "field_readbacks": {"anything": "passes-old-gate"},
            "inherited_content_cleared": True,
            "template_video_present": False,
            "draft_save_count": 1,
            "final_submit_triggered": False,
        }
        result = draft_guard.strict_verify_fill(legacy)
        self.assertEqual(result["status"], "FAIL", result)
        self.assertTrue(any("post-refresh" in error for error in result["errors"]), result)

    def test_missing_extra_wrong_or_refresh_lost_field_fails(self) -> None:
        mutations = (
            ("missing fill field", lambda case: case["fill"]["field_readbacks"].pop(), "missing expected"),
            (
                "extra fill field",
                lambda case: case["fill"]["field_readbacks"].append(
                    {
                        "target": field_target("unexpected", "BASIC_INFO"),
                        "expected_sha256": "9" * 64,
                        "observed_sha256": "9" * 64,
                        "source_id": "unexpected-source",
                        "decision_id": "unexpected-decision",
                    }
                ),
                "unexpected field readback",
            ),
            (
                "wrong observed hash",
                lambda case: case["fill"]["field_readbacks"][0].__setitem__("observed_sha256", "9" * 64),
                "observed hash",
            ),
            (
                "wrong expected hash",
                lambda case: case["fill"]["field_readbacks"][0].__setitem__("expected_sha256", "9" * 64),
                "expected hash",
            ),
            (
                "wrong source",
                lambda case: case["fill"]["field_readbacks"][0].__setitem__("source_id", "wrong-source"),
                "source binding",
            ),
            (
                "wrong decision",
                lambda case: case["fill"]["field_readbacks"][0].__setitem__("decision_id", "wrong-decision"),
                "decision binding",
            ),
            (
                "refresh lost field",
                lambda case: case["refresh"]["field_readbacks"][0].__setitem__("observed_sha256", "8" * 64),
                "post-refresh observed hash",
            ),
            (
                "refresh missing field",
                lambda case: case["refresh"]["field_readbacks"].pop(),
                "post-refresh missing expected",
            ),
        )
        for name, mutate, needle in mutations:
            with self.subTest(name=name):
                case = build_valid_case()
                mutate(case)
                self.assert_failed(case, needle)

    def test_clear_set_non_target_diff_save_and_final_gates_fail(self) -> None:
        mutations = (
            (
                "missing clear",
                lambda case: case["fill"]["cleared_targets"].pop(),
                "missing expected cleared target",
            ),
            (
                "extra clear",
                lambda case: case["fill"]["cleared_targets"].append(
                    {
                        "target": field_target("unexpected_clear", "BASIC_INFO"),
                        "reason": "REGISTRY_CLEAR_BEFORE_FILL",
                        "captured_phase": "CLEAR_INHERITED_FIELDS",
                        "expected_state": "EMPTY",
                        "observed_state": "EMPTY",
                        "expected_sha256": EMPTY_READBACK_SHA256,
                        "observed_sha256": EMPTY_READBACK_SHA256,
                    }
                ),
                "unexpected cleared target",
            ),
            (
                "clear repopulated after refresh",
                lambda case: case["refresh"]["cleared_targets"][0].__setitem__("observed_state", "NON_EMPTY"),
                "not proven empty",
            ),
            (
                "non-target diff",
                lambda case: case["fill"].__setitem__(
                    "non_target_field_diffs", [{"field": "price", "before": None, "after": "29"}]
                ),
                "non-target field diffs",
            ),
            (
                "non-target snapshot drift",
                lambda case: case["refresh"].__setitem__("non_target_snapshot_after_sha256", "a" * 64),
                "normalized non-target snapshots changed",
            ),
            (
                "second save",
                lambda case: case["refresh"].__setitem__("draft_save_count", 2),
                "draft_save_count",
            ),
            (
                "final write",
                lambda case: case["fill"].__setitem__("final_write_count", 1),
                "final_write_count",
            ),
            (
                "final submit",
                lambda case: case["refresh"].__setitem__("final_submit_triggered", True),
                "final submit",
            ),
        )
        for name, mutate, needle in mutations:
            with self.subTest(name=name):
                case = build_valid_case()
                mutate(case)
                self.assert_failed(case, needle)

    def test_action_plan_run_and_probe_hash_bindings_are_exact(self) -> None:
        for binding_name, case_key in (
            ("action_plan_sha256", "action_plan_sha256"),
            ("run_spec_sha256", "run_spec_sha256"),
            ("probe_receipt_sha256", "probe_sha256"),
        ):
            with self.subTest(binding=binding_name):
                case = build_valid_case()
                case[case_key] = "f" * 64
                self.assert_failed(case, binding_name)

    def test_cli_hashes_actual_files_and_writes_only_a_pass_receipt(self) -> None:
        case = build_valid_case()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "action": root / "action-plan.json",
                "run": root / "run-spec.json",
                "probe": root / "probe-receipt.json",
                "fill": root / "fill-execution.json",
                "refresh": root / "post-refresh.json",
                "output": root / "fill-verification.json",
            }
            write_json(paths["action"], case["action_plan"])
            write_json(paths["run"], case["run_spec"])
            action_file_hash = sha256_file(paths["action"])
            run_file_hash = sha256_file(paths["run"])
            action_plan = case["action_plan"]
            probe = case["probe"]
            fill = case["fill"]
            refresh = case["refresh"]
            assert isinstance(action_plan, dict) and isinstance(probe, dict)
            assert isinstance(fill, dict) and isinstance(refresh, dict)
            action_plan["bindings"]["three_party"]["run_spec_sha256"] = run_file_hash
            triplet = dict(action_plan["bindings"]["three_party"])
            triplet.pop("binding_sha256", None)
            action_plan["bindings"]["three_party"]["binding_sha256"] = object_sha256(triplet)
            action_plan_without_hash = dict(action_plan)
            action_plan_without_hash.pop("plan_body_sha256", None)
            action_plan["plan_body_sha256"] = object_sha256(action_plan_without_hash)
            write_json(paths["action"], action_plan)
            action_file_hash = sha256_file(paths["action"])
            probe["bindings"]["run_spec_sha256"] = run_file_hash
            probe["bindings"]["action_plan_sha256"] = action_file_hash
            write_json(paths["probe"], probe)
            probe_file_hash = sha256_file(paths["probe"])
            for document in (fill, refresh):
                document["bindings"]["action_plan_sha256"] = action_file_hash
                document["bindings"]["action_plan_body_sha256"] = action_plan["plan_body_sha256"]
                document["bindings"]["run_spec_sha256"] = run_file_hash
                document["bindings"]["probe_receipt_sha256"] = probe_file_hash
            write_json(paths["fill"], fill)
            write_json(paths["refresh"], refresh)

            status = draft_guard.main(
                [
                    "verify-fill",
                    "--action-plan",
                    str(paths["action"]),
                    "--run-spec",
                    str(paths["run"]),
                    "--probe-receipt",
                    str(paths["probe"]),
                    "--fill-execution",
                    str(paths["fill"]),
                    "--post-refresh",
                    str(paths["refresh"]),
                    "--output",
                    str(paths["output"]),
                ]
            )
            self.assertEqual(status, 0)
            receipt = json.loads(paths["output"].read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "PASS", receipt)
            self.assertEqual(receipt["bindings"]["action_plan_sha256"], action_file_hash)
            self.assertEqual(receipt["bindings"]["probe_receipt_sha256"], probe_file_hash)


if __name__ == "__main__":
    unittest.main()
