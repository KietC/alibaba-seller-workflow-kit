from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from compile_browser_action_plan import compile_action_plan
from draft_core import GuardError, classify_endpoint
from dynamic_schema_gate import validate_dynamic_schema_capture
from dynamic_schema_test_support import build_official_dynamic_schema
import test_action_plan as action_plan_fixture
import test_probe_gate as probe_fixture
from validate_draft_save_activation import (
    ACTIVATION_RECEIPT_SCHEMA,
    ActivationError,
    consume_activation,
    object_sha256,
    recompute_action_plan_body_hash,
    sha256_file,
    validate_activation,
    validate_schema,
    validation_only_report,
)
from validate_pre_save_readback import EMPTY_READBACK_SHA256, expected_contract, verify_pre_save
import validate_draft_save_activation as activation_module
from state_store import abort_checkpoint_no_write, checkpoint_hash


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
PRODUCTION_POLICY = SKILL_DIR / "references" / "endpoint-policy.v1.json"


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ActivationFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True)
        self.plan_fixture = action_plan_fixture.ActionPlanCompilerTests(methodName="test_atomic_writer_refuses_overwrite")
        self.plan_fixture.setUp()
        self.paths = self.plan_fixture.paths
        self.now = datetime.now(timezone.utc) + timedelta(seconds=20)
        self.activation_path = self.root / "activation.json"
        self.action_plan_path = self.root / "browser_action_plan.json"
        self.probe_source_path = self.root / "probe_source.json"
        self.probe_verification_path = self.root / "probe_verification.json"
        self.probe_read_activation_path = self.root / "probe_read_activation.json"
        self.body_path = self.root / "body_semantics.json"
        self.event_path = self.root / "visible_ui_event.json"
        self.pre_save_readback_path = self.root / "pre_save_readback.json"
        self.pre_save_verification_path = self.root / "pre_save_verification.json"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.policy_path = self.paths["policy"]
        self.ledger_dir = self.checkpoint_path.parent / "draft-save-attempt-ledger"
        self.receipt_path = self.root / "activation_receipt.json"
        self._build()

    def close(self) -> None:
        self.plan_fixture.tearDown()

    def _build(self) -> None:
        source_manifest = json.loads(self.paths["source"].read_text(encoding="utf-8"))
        approved_fields = sorted(source_manifest["approved_field_values"])
        fields: list[dict[str, Any]] = []
        for logical_name in approved_fields:
            fields.append(
                {
                    "logical_name": logical_name,
                    "observed_label": logical_name,
                    "required": logical_name == "product_title",
                    "max_length": 128 if logical_name == "product_title" else None,
                    "max_length_unit": "PLATFORM_COUNTER" if logical_name == "product_title" else "NOT_APPLICABLE",
                }
            )
        dynamic_capture_path, _, _ = build_official_dynamic_schema(
            self.root,
            fields=fields,
            run_id=source_manifest["run_id"],
            target_model=source_manifest["target_model"],
            template_product_id=source_manifest["bindings"]["template_product_id"],
            category_id="100007076",
            product_type="wholesale",
            stem="activation",
        )
        dynamic_binding = validate_dynamic_schema_capture(
            dynamic_capture_path,
            expected_target_model=source_manifest["target_model"],
            expected_template_product_id=source_manifest["bindings"]["template_product_id"],
            expected_run_id=source_manifest["run_id"],
        )
        dynamic_binding["decision_id"] = "synthetic-dynamic-schema-decision"
        source_manifest["dynamic_schema"] = dynamic_binding
        action_plan_fixture.write_json(self.paths["source"], source_manifest)

        asset_manifest = json.loads(self.paths["asset"].read_text(encoding="utf-8"))
        asset_manifest["bindings"]["approved_source_manifest_sha256"] = sha256_file(self.paths["source"])
        action_plan_fixture.write_json(self.paths["asset"], asset_manifest)

        production_policy = json.loads(PRODUCTION_POLICY.read_text(encoding="utf-8"))
        action_plan_fixture.write_json(self.policy_path, production_policy)
        policy_hash = sha256_file(self.policy_path)

        run_spec = json.loads(self.paths["run"].read_text(encoding="utf-8"))
        run_spec["endpoint_policy"]["sha256"] = policy_hash
        action_plan_fixture.write_json(self.paths["run"], run_spec)
        write_approval = json.loads(self.paths["write_approval"].read_text(encoding="utf-8"))
        write_approval["bindings"]["approved_source_manifest_sha256"] = sha256_file(self.paths["source"])
        write_approval["bindings"]["approved_asset_manifest_sha256"] = sha256_file(self.paths["asset"])
        write_approval["bindings"]["endpoint_policy_sha256"] = policy_hash
        write_approval["bindings"]["run_spec_sha256"] = sha256_file(self.paths["run"])
        action_plan_fixture.write_json(self.paths["write_approval"], write_approval)

        plan = compile_action_plan(
            approved_source_manifest_path=self.paths["source"],
            approved_asset_manifest_path=self.paths["asset"],
            run_spec_path=self.paths["run"],
            write_approval_path=self.paths["write_approval"],
            field_registry_path=self.paths["registry"],
            endpoint_policy_path=self.policy_path,
            adapter_path=self.paths["adapter"],
        )
        action_plan_fixture.write_json(self.action_plan_path, plan)
        plan_hash = sha256_file(self.action_plan_path)

        plan_generated_at = datetime.fromisoformat(plan["generated_at"].replace("Z", "+00:00"))
        started = plan_generated_at + timedelta(seconds=1)
        captured = started + timedelta(seconds=2)
        completed = captured + timedelta(seconds=2)
        issued = completed + timedelta(seconds=3)
        self.now = issued + timedelta(seconds=1)

        probe, _, _ = probe_fixture.build_valid_probe()
        probe["run_id"] = plan["target"]["run_id"]
        probe["target_model"] = plan["target"]["model"]
        probe["template_product_id"] = plan["target"]["template_product_id"]
        probe["started_at"] = timestamp(started)
        probe["captured_at"] = timestamp(captured)
        probe["completed_at"] = timestamp(completed)
        probe["bindings"]["run_spec_sha256"] = plan["bindings"]["three_party"]["run_spec_sha256"]
        probe["bindings"]["action_plan_sha256"] = plan_hash
        probe["bindings"]["action_plan_id"] = plan["plan_id"]
        probe["bindings"]["action_plan_generated_at"] = plan["generated_at"]
        probe["bindings"]["field_registry_sha256"] = plan["bindings"]["field_registry"]["sha256"]
        probe["bindings"]["adapter_sha256"] = plan["bindings"]["adapter"]["sha256"]
        probe["bindings"]["adapter_id"] = plan["bindings"]["adapter"]["adapter_id"]
        probe["bindings"]["adapter_version"] = plan["bindings"]["adapter"]["adapter_version"]
        probe["bindings"]["endpoint_policy_sha256"] = policy_hash
        network = probe["network"]
        network["interception_started_at"] = timestamp(started + timedelta(milliseconds=100))
        network["navigation_started_at"] = timestamp(started + timedelta(milliseconds=500))
        network["capture_completed_at"] = timestamp(completed - timedelta(milliseconds=100))
        network["read_requests"][0]["captured_at"] = timestamp(captured)
        action_plan_fixture.write_json(self.probe_source_path, probe)

        request = network["read_requests"][0]
        parsed_url = request["url"].split("https://post.alibaba.com", 1)[1]
        ephemeral_policy = copy.deepcopy(production_policy)
        ephemeral_policy["rules"] = [{
            "id": "session-read-0",
            "classification": "READ",
            "methods": [request["method"]],
            "method_evidence": "CURRENT_SESSION_VERIFIED",
            "evidence_state": "LIVE_VERIFIED_ACTIVE",
            "url_regex": f"^{__import__('re').escape(parsed_url)}$",
            "query_allowlist": ["behavior", "itemId", "pubType"],
            "query_values": {
                "behavior": ["copyNew"], "itemId": [plan["target"]["template_product_id"]],
                "pubType": ["similarPost"],
            },
        }] + ephemeral_policy["rules"]
        verification = {
            "schema_version": "sellerkit.browser_probe_receipt.v3",
            "generated_at": timestamp(completed + timedelta(seconds=1)),
            "probe_captured_at": probe["captured_at"],
            "input_probe_sha256": sha256_file(self.probe_source_path),
            "target_model": plan["target"]["model"],
            "template_product_id": plan["target"]["template_product_id"],
            "browser_session_sha256": network["interception_session_sha256"],
            "page_fingerprint": probe["page"]["fingerprint_sha256"],
            "bindings": {
                "run_spec_sha256": plan["bindings"]["three_party"]["run_spec_sha256"],
                "action_plan_sha256": plan_hash,
                "action_plan_id": plan["plan_id"],
                "endpoint_policy_sha256": policy_hash,
                "adapter_sha256": plan["bindings"]["adapter"]["sha256"],
                "probe_read_activation_request_sha256": "4" * 64,
            },
            "request_classifications": [
                classify_endpoint(
                    ephemeral_policy,
                    network["read_requests"][0]["url"],
                    network["read_requests"][0]["method"],
                )
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
        action_plan_fixture.write_json(self.probe_verification_path, verification)
        read_activation = {
            "schema_version": "sellerkit.alibaba.probe-read-activation.v1",
            "generated_at": timestamp(completed + timedelta(seconds=1, milliseconds=100)),
            "expires_at": timestamp(issued + timedelta(minutes=10)),
            "status": "PASS_CURRENT_SESSION_EXACT_READ_ACTIVATION",
            "scope": {
                "run_id": plan["target"]["run_id"],
                "target_model": plan["target"]["model"],
                "template_product_id": plan["target"]["template_product_id"],
                "browser_session_sha256": network["interception_session_sha256"],
                "page_fingerprint_sha256": probe["page"]["fingerprint_sha256"],
            },
            "bindings": {
                "global_endpoint_policy_sha256": policy_hash,
                "action_plan_sha256": plan_hash,
                "probe_source_sha256": sha256_file(self.probe_source_path),
                "probe_verification_sha256": sha256_file(self.probe_verification_path),
            },
            "producer": {
                "validator_id": "draft_core.verify_probe.v3",
                "validator_sha256": sha256_file(SCRIPT_DIR / "draft_core.py"),
            },
            "request_activations": [{
                "method": request["method"], "url": request["url"],
                "source_request_sha256": object_sha256(request),
                "classification": "READ", "decision": "ALLOW",
            }],
            "global_policy_unchanged": True,
            "draft_write_enabled": False,
            "final_write_enabled": False,
            "errors": [],
        }
        read_activation["body_sha256"] = object_sha256(read_activation)
        action_plan_fixture.write_json(self.probe_read_activation_path, read_activation)

        business_fields = {
            "model": object_sha256("MODEL-003"),
            "product.subject": object_sha256("synthetic approved title"),
            "templateProductId": object_sha256(plan["target"]["template_product_id"]),
        }
        ephemeral = ["_csrf"]
        volatile = ["clientTimestamp"]
        body = {
            "schema_version": "sellerkit.alibaba.draft-save-body-semantics.v1",
            "operation": "SAVE_DRAFT",
            "run_id": plan["target"]["run_id"],
            "target_model": plan["target"]["model"],
            "template_product_id": plan["target"]["template_product_id"],
            "action_plan_body_sha256": plan["plan_body_sha256"],
            "form_readback_sha256": object_sha256({"readback": "verified"}),
            "content_type_family": "application/x-www-form-urlencoded",
            "body_key_names": sorted(set(business_fields) | set(ephemeral) | set(volatile)),
            "body_key_set_sha256": "0" * 64,
            "business_field_digests": business_fields,
            "ephemeral_credential_field_names": ephemeral,
            "volatile_nonsemantic_field_names": volatile,
            "semantic_projection_sha256": "0" * 64,
            "redaction": {
                "policy": "BUSINESS_VALUES_SHA256_EPHEMERAL_VALUES_OMITTED",
                "complete": True,
                "raw_body_persisted": False,
                "credential_values_persisted": False,
                "cookie_or_storage_values_read": False,
            },
        }
        body["body_key_set_sha256"] = object_sha256(body["body_key_names"])
        projection = copy.deepcopy(body)
        projection.pop("semantic_projection_sha256")
        body["semantic_projection_sha256"] = object_sha256(projection)
        action_plan_fixture.write_json(self.body_path, body)

        session_sha256 = probe["network"]["interception_session_sha256"]
        fingerprint = probe["page"]["fingerprint_sha256"]
        event_at = issued - timedelta(seconds=1)
        event = {
            "schema_version": "sellerkit.alibaba.visible-ui-draft-save-event.v1",
            "event_id": "save-draft-visible-click-fixture",
            "captured_at": timestamp(event_at),
            "source": "CODEX_IN_APP_BROWSER_VISIBLE_UI",
            "event_type": "CLICK",
            "semantic_action": "SAVE_DRAFT",
            "page": {
                "origin": "https://post.alibaba.com",
                "path": "/product/publish.htm",
                "query": {
                    "pubType": "similarPost",
                    "behavior": "copyNew",
                    "itemId": plan["target"]["template_product_id"],
                },
                "fingerprint_sha256": fingerprint,
                "browser_session_sha256": session_sha256,
                "login_active": True,
                "captcha_present": False,
            },
            "element": {
                "semantic_name": "action.save_draft_runtime",
                "aria_role": "button",
                "accessible_name_sha256": object_sha256("Save as draft"),
                "locator_evidence_sha256": object_sha256({"role": "button", "name": "Save as draft"}),
                "match_count": 1,
                "visible_match_count": 1,
                "enabled": True,
                "resolved_from_current_dom": True,
                "cached_handle_reused": False,
            },
            "evidence": {
                "dom_snapshot_sha256": object_sha256("dom-event"),
                "aria_snapshot_sha256": object_sha256("aria-event"),
                "screenshot_sha256": object_sha256("screenshot-event"),
                "event_trace_sha256": object_sha256("event-trace"),
            },
            "security": {
                "direct_endpoint_call_used": False,
                "request_replay_used": False,
                "coordinates_used": False,
                "cookie_or_storage_values_read": False,
            },
        }
        action_plan_fixture.write_json(self.event_path, event)

        self._build_pre_save_and_checkpoint(plan, probe, verification, issued)

        activation = {
            "schema_version": "sellerkit.alibaba.draft-save-activation-request.v1",
            "activation_id": "one-shot-visible-ui-draft-save-fixture",
            "status": "PENDING_INTERCEPT_RELEASE",
            "authority": "SINGLE_REQUEST_GUARD_OVERLAY_NOT_GLOBAL_POLICY",
            "issued_at": timestamp(issued),
            "expires_at": timestamp(issued + timedelta(seconds=60)),
            "scope": {
                "run_kind": "CREATE_SIMILAR_DRAFT",
                "run_id": plan["target"]["run_id"],
                "target_model": plan["target"]["model"],
                "template_product_id": plan["target"]["template_product_id"],
                "browser_session_sha256": session_sha256,
            },
            "bindings": {
                "action_plan_sha256": plan_hash,
                "action_plan_body_sha256": plan["plan_body_sha256"],
                "verified_probe_receipt_sha256": sha256_file(self.probe_verification_path),
                "probe_source_sha256": sha256_file(self.probe_source_path),
                "endpoint_policy_sha256": policy_hash,
                "probe_read_activation_sha256": sha256_file(self.probe_read_activation_path),
                "redacted_body_semantics_file_sha256": sha256_file(self.body_path),
                "redacted_body_semantic_sha256": body["semantic_projection_sha256"],
                "visible_ui_event_sha256": sha256_file(self.event_path),
                "run_spec_sha256": sha256_file(self.paths["run"]),
                "pre_save_readback_sha256": sha256_file(self.pre_save_readback_path),
                "pre_save_verification_sha256": sha256_file(self.pre_save_verification_path),
                "checkpoint_sha256": sha256_file(self.checkpoint_path),
            },
            "current_page": {
                "observed_at": timestamp(event_at - timedelta(milliseconds=200)),
                "origin": "https://post.alibaba.com",
                "path": "/product/publish.htm",
                "query": {
                    "pubType": "similarPost",
                    "behavior": "copyNew",
                    "itemId": plan["target"]["template_product_id"],
                },
                "fingerprint_sha256": fingerprint,
                "login_active": True,
                "captcha_present": False,
            },
            "endpoint_request": {
                "origin": "https://post.alibaba.com",
                "path": "/product/submitDraft.json",
                "method": "POST",
                "query_present": False,
                "redirect_allowed": False,
                "visible_ui_only": True,
                "direct_replay_allowed": False,
                "redacted_body_semantic_sha256": body["semantic_projection_sha256"],
            },
            "attempt_transition": {
                "before": 0,
                "consume_to": 1,
                "maximum": 1,
                "compare_and_swap_required": True,
                "consume_even_if_request_fails": True,
            },
            "deny_policy": {
                "global_endpoint_policy_unchanged": True,
                "default_decision": "DENY",
                "unknown_request_decision": "DENY",
                "final_write_ui_decision": "HARD_DENY_PERMANENT",
                "final_write_network_decision": "HARD_DENY_PERMANENT",
                "activation_cannot_authorize_final_write": True,
            },
        }
        action_plan_fixture.write_json(self.activation_path, activation)

    def _build_pre_save_and_checkpoint(
        self,
        plan: dict[str, Any],
        probe_source: dict[str, Any],
        verification: dict[str, Any],
        issued: datetime,
    ) -> None:
        errors: list[str] = []
        contract = expected_contract(
            plan,
            errors,
            expected_page_fingerprint_sha256=probe_source["page"]["fingerprint_sha256"],
        )
        if errors:
            raise AssertionError(errors)
        fields = [
            {
                "target": copy.deepcopy(row["target"]),
                "expected_sha256": row["expected_sha256"],
                "observed_sha256": row["expected_sha256"],
                "source_id": row["source_id"],
                "decision_id": row["decision_id"],
            }
            for row in contract["fields"].values()
        ]
        clears = [
            {
                "target": copy.deepcopy(row["target"]),
                "reason": row["reason"],
                "captured_phase": "CLEAR_INHERITED_FIELDS",
                "expected_state": "EMPTY",
                "observed_state": "EMPTY",
                "expected_sha256": EMPTY_READBACK_SHA256,
                "observed_sha256": EMPTY_READBACK_SHA256,
            }
            for row in contract["clears"].values()
        ]
        assets = [
            {**copy.deepcopy(row), "observed_sha256": row["expected_sha256"]}
            for row in contract["assets"].values()
        ]
        non_target = object_sha256({"visible_non_targets": "unchanged"})
        captured = datetime.fromisoformat(probe_source["completed_at"].replace("Z", "+00:00")) + timedelta(seconds=2)
        readback = {
            "schema_version": "sellerkit.alibaba.pre-save-readback.v1",
            "readback_id": "activation-pre-save-fixture",
            "run_id": plan["target"]["run_id"],
            "captured_at": timestamp(captured),
            "phase": "PRE_SAVE_NO_DRAFT_WRITE",
            "target_model": plan["target"]["model"],
            "template_product_id": plan["target"]["template_product_id"],
            "current_page_fingerprint_sha256": probe_source["page"]["fingerprint_sha256"],
            "browser_session_sha256": probe_source["network"]["interception_session_sha256"],
            "bindings": {
                "action_plan_sha256": sha256_file(self.action_plan_path),
                "action_plan_body_sha256": plan["plan_body_sha256"],
                "run_spec_sha256": sha256_file(self.paths["run"]),
                "probe_source_sha256": sha256_file(self.probe_source_path),
                "probe_verification_sha256": sha256_file(self.probe_verification_path),
                "expected_field_set_sha256": contract["field_set_sha256"],
                "expected_clear_set_sha256": contract["clear_set_sha256"],
                "expected_asset_set_sha256": contract["asset_set_sha256"],
                "expected_non_target_scope_sha256": contract["non_target_scope_sha256"],
            },
            "field_readbacks": fields,
            "cleared_targets": clears,
            "asset_readbacks": assets,
            "non_target_scope_sha256": contract["non_target_scope_sha256"],
            "non_target_snapshot_before_sha256": non_target,
            "non_target_snapshot_after_sha256": non_target,
            "non_target_field_diffs": [],
            "all_planned_actions_executed": True,
            "inherited_content_cleared": True,
            "template_video_present": False,
            "visible_ui_save_used": False,
            "direct_endpoint_write_used": False,
            "draft_save_count": 0,
            "final_write_count": 0,
            "final_submit_triggered": False,
            "unknown_write_count": 0,
            "request_guard_active": True,
            "final_ui_guard_active": True,
            "final_network_guard_active": True,
        }
        action_plan_fixture.write_json(self.pre_save_readback_path, readback)
        pre_save_receipt = verify_pre_save(
            action_plan_path=self.action_plan_path,
            run_spec_path=self.paths["run"],
            probe_source_path=self.probe_source_path,
            probe_verification_path=self.probe_verification_path,
            pre_save_readback_path=self.pre_save_readback_path,
            now=self.now,
        )
        if pre_save_receipt["status"] != "PASS_PRE_SAVE_NO_WRITE":
            raise AssertionError(pre_save_receipt)
        action_plan_fixture.write_json(self.pre_save_verification_path, pre_save_receipt)
        checkpoint = {
            "schema_version": "sellerkit.similar_draft_checkpoint.v2",
            "run_id": plan["target"]["run_id"],
            "revision": 6,
            "previous_checkpoint_sha256": "a" * 64,
            "state": "VALIDATED",
            "run_status": "ACTIVE",
            "target_model": plan["target"]["model"],
            "template_product_id": plan["target"]["template_product_id"],
            "input_hashes": {
                "run_spec_sha256": sha256_file(self.paths["run"]),
                "action_plan_sha256": sha256_file(self.action_plan_path),
                "probe_verification_sha256": sha256_file(self.probe_verification_path),
                "pre_save_readback_sha256": sha256_file(self.pre_save_readback_path),
                "pre_save_verification_sha256": sha256_file(self.pre_save_verification_path),
                "browser_session_sha256": probe_source["network"]["interception_session_sha256"],
                "page_fingerprint_sha256": probe_source["page"]["fingerprint_sha256"],
            },
            "draft_id": None,
            "save_attempt_count": 0,
            "draft_save_count": 0,
            "final_write_count": 0,
            "submit_triggered": False,
            "publish_triggered": False,
            "external_publish_status": "NOT_ATTEMPTED",
            "validation_status": "VALIDATED",
            "created_at": timestamp(issued - timedelta(minutes=1)),
            "updated_at": timestamp(issued - timedelta(seconds=1)),
            "lock_path": str((self.root / "locks" / "MODEL-003.lock.json").resolve()),
            "lock_state": "HELD",
            "evidence": [{
                "state": "VALIDATED",
                "evidence_id": "synthetic-forged-checkpoint-must-not-authorize",
                "bundle_path": str((self.root / "synthetic-evidence.json").resolve()),
                "bundle_sha256": "b" * 64,
                "roles": ["PRE_SAVE_CHECKLIST_RECEIPT"],
                "artifacts": [{
                    "role": "PRE_SAVE_CHECKLIST_RECEIPT",
                    "path": str((self.root / "synthetic-checklist-receipt.json").resolve()),
                    "bytes": 2,
                    "sha256": "c" * 64,
                    "schema_version": "sellerkit.alibaba.next-product-checklist-verification.v1",
                    "status": "PASS",
                }],
            }],
        }
        action_plan_fixture.write_json(self.checkpoint_path, checkpoint)

    def kwargs(self) -> dict[str, Any]:
        return {
            "activation_path": self.activation_path,
            "action_plan_path": self.action_plan_path,
            "probe_verification_path": self.probe_verification_path,
            "probe_source_path": self.probe_source_path,
            "endpoint_policy_path": self.policy_path,
            "probe_read_activation_path": self.probe_read_activation_path,
            "body_semantics_path": self.body_path,
            "visible_ui_event_path": self.event_path,
            "run_spec_path": self.paths["run"],
            "pre_save_readback_path": self.pre_save_readback_path,
            "pre_save_verification_path": self.pre_save_verification_path,
            "checkpoint_path": self.checkpoint_path,
            "now": self.now,
        }

    @staticmethod
    def load(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def save(path: Path, value: dict[str, Any]) -> None:
        action_plan_fixture.write_json(path, value)


class DraftSaveActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ActivationFixture(Path(self.temporary.name) / "activation-fixture")

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def assert_blocked(self, needle: str | None = None) -> None:
        with self.assertRaises(ActivationError) as caught:
            validate_activation(**self.fixture.kwargs())
        if needle:
            self.assertIn(needle, str(caught.exception))

    def test_valid_check_is_explicitly_non_authorizing(self) -> None:
        self.assert_blocked("checkpoint VALIDATED evidence roles are incomplete")

    def test_missing_presave_or_checkpoint_artifact_is_fail_closed(self) -> None:
        kwargs = self.fixture.kwargs()
        kwargs["pre_save_verification_path"] = self.fixture.root / "missing-pre-save.json"
        with self.assertRaises(FileNotFoundError):
            validate_activation(**kwargs)
        kwargs = self.fixture.kwargs()
        kwargs["checkpoint_path"] = self.fixture.root / "missing-checkpoint.json"
        with self.assertRaises(FileNotFoundError):
            validate_activation(**kwargs)

    def test_form_filled_checkpoint_cannot_activate_save(self) -> None:
        checkpoint = self.fixture.load(self.fixture.checkpoint_path)
        checkpoint["state"] = "FORM_FILLED"
        checkpoint["validation_status"] = "NOT_VALIDATED"
        self.fixture.save(self.fixture.checkpoint_path, checkpoint)
        activation = self.fixture.load(self.fixture.activation_path)
        activation["bindings"]["checkpoint_sha256"] = sha256_file(self.fixture.checkpoint_path)
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked("checkpoint state must be VALIDATED")

    def test_schema_valid_handwritten_validated_checkpoint_cannot_activate(self) -> None:
        self.assert_blocked("checkpoint VALIDATED evidence roles are incomplete")

    def test_production_gate_blocks_consume_before_attempt(self) -> None:
        with self.assertRaisesRegex(ActivationError, "checkpoint VALIDATED evidence roles"):
            consume_activation(
                ledger_dir=self.fixture.ledger_dir,
                receipt_path=self.fixture.receipt_path,
                **self.fixture.kwargs(),
            )
        self.assertEqual(len(list(self.fixture.ledger_dir.glob("*.consumed.json"))), 0)

    def test_synthetic_gate_override_atomically_commits_save_attempted(self) -> None:
        with patch.object(activation_module, "validate_checkpoint_checklist_evidence", lambda checkpoint: None), \
             patch.object(activation_module, "CHECKLIST_IMPLEMENTATION_READY", True), \
             patch.object(
                 activation_module.draft_core_module,
                 "PRODUCTION_BROWSER_ATTESTATION_VERIFIER_IMPLEMENTED",
                 True,
             ):
            receipt = consume_activation(
                ledger_dir=self.fixture.ledger_dir,
                receipt_path=self.fixture.receipt_path,
                **self.fixture.kwargs(),
            )
        checkpoint = self.fixture.load(self.fixture.checkpoint_path)
        self.assertEqual(receipt["result_semantics"], "ATTEMPT_CONSUMED_NOT_DRAFT_SUCCESS")
        self.assertEqual(receipt["checkpoint_transition"]["state"], "SAVE_ATTEMPTED")
        self.assertEqual(checkpoint["state"], "SAVE_ATTEMPTED")
        self.assertEqual(checkpoint["save_attempt_count"], 1)
        self.assertEqual(checkpoint["draft_save_count"], 0)
        self.assertEqual(checkpoint["run_status"], "SAVE_ATTEMPTED_UNCONFIRMED")
        self.assertEqual(checkpoint["lock_state"], "HELD_SAVE_ATTEMPT_UNCONFIRMED")
        self.assertEqual(len(list(self.fixture.ledger_dir.glob("*.consumed.json"))), 1)
        self.assertTrue(
            Path(receipt["checkpoint_transition"]["evidence_path"]).is_file()
        )
        self.assertFalse(
            self.fixture.checkpoint_path.with_name("checkpoint.json.mutation.lock").exists()
        )

    def test_transition_failure_leaves_only_non_authorizing_claim(self) -> None:
        with patch.object(activation_module, "validate_checkpoint_checklist_evidence", lambda checkpoint: None), \
             patch.object(activation_module, "CHECKLIST_IMPLEMENTATION_READY", True), \
             patch.object(
                 activation_module.draft_core_module,
                 "PRODUCTION_BROWSER_ATTESTATION_VERIFIER_IMPLEMENTED",
                 True,
             ), \
             patch.object(
                 activation_module,
                 "transition_checkpoint_under_guard",
                 side_effect=GuardError("synthetic checkpoint commit failure"),
             ):
            with self.assertRaisesRegex(ActivationError, "synthetic checkpoint commit failure"):
                consume_activation(
                    ledger_dir=self.fixture.ledger_dir,
                    receipt_path=self.fixture.receipt_path,
                    **self.fixture.kwargs(),
                )
        claim_path = self.fixture.receipt_path.with_name(
            f"{self.fixture.receipt_path.stem}.attempt-claim.json"
        )
        evidence_path = self.fixture.receipt_path.with_name(
            f"{self.fixture.receipt_path.stem}.checkpoint-transition-evidence.json"
        )
        self.assertFalse(self.fixture.receipt_path.exists(), "failed commit emitted a PASS/release receipt")
        self.assertTrue(claim_path.is_file())
        self.assertTrue(evidence_path.is_file())
        claim = self.fixture.load(claim_path)
        self.assertEqual(claim["status"], "ATTEMPT_CLAIMED_CHECKPOINT_COMMIT_PENDING")
        self.assertFalse(claim["release_authorized"])
        checkpoint = self.fixture.load(self.fixture.checkpoint_path)
        self.assertEqual(checkpoint["state"], "VALIDATED")
        self.assertEqual(checkpoint["save_attempt_count"], 0)
        self.assertEqual(len(list(self.fixture.ledger_dir.glob("*.consumed.json"))), 1)
        mutation_lock = self.fixture.checkpoint_path.with_name("checkpoint.json.mutation.lock")
        self.assertEqual(
            self.fixture.load(mutation_lock)["state"],
            "HELD_FAIL_CLOSED_IRREVERSIBLE_ATTEMPT",
        )

    def test_nonfixed_ledger_directory_is_rejected_before_validation(self) -> None:
        with self.assertRaisesRegex(ActivationError, "fixed run-scoped"):
            consume_activation(
                ledger_dir=self.fixture.root / "alternate-ledger",
                receipt_path=self.fixture.receipt_path,
                **self.fixture.kwargs(),
            )

    def test_atomic_new_output_preserves_racing_destination(self) -> None:
        destination = self.fixture.root / "racing-output.json"
        original_link = activation_module.os.link

        def create_destination_then_link(source: Path, target: Path) -> None:
            target.write_text("operator-owned\n", encoding="utf-8")
            original_link(source, target)

        with patch.object(activation_module.os, "link", side_effect=create_destination_then_link):
            with self.assertRaisesRegex(ActivationError, "refusing to overwrite"):
                activation_module._write_json_atomic_new(destination, {"new": "value"})
        self.assertEqual(destination.read_text(encoding="utf-8"), "operator-owned\n")

    def test_second_consume_is_denied_even_with_another_receipt_path(self) -> None:
        for receipt in (self.fixture.receipt_path, self.fixture.root / "second-receipt.json"):
            with self.assertRaisesRegex(ActivationError, "checkpoint VALIDATED evidence roles"):
                consume_activation(
                    ledger_dir=self.fixture.ledger_dir,
                    receipt_path=receipt,
                    **self.fixture.kwargs(),
                )
        self.assertEqual(len(list(self.fixture.ledger_dir.glob("*.consumed.json"))), 0)

    def test_checkpoint_drift_after_claim_burns_attempt_and_blocks_release(self) -> None:
        original_claim = activation_module._claim_one_shot_ledger

        def claim_then_drift(ledger_dir: Path, validated: dict[str, Any]):
            result = original_claim(ledger_dir, validated)
            checkpoint = self.fixture.load(self.fixture.checkpoint_path)
            checkpoint["updated_at"] = timestamp(self.fixture.now + timedelta(seconds=1))
            self.fixture.save(self.fixture.checkpoint_path, checkpoint)
            return result

        with patch.object(activation_module, "validate_checkpoint_checklist_evidence", lambda checkpoint: None), \
             patch.object(activation_module, "CHECKLIST_IMPLEMENTATION_READY", True), \
             patch.object(
                 activation_module.draft_core_module,
                 "PRODUCTION_BROWSER_ATTESTATION_VERIFIER_IMPLEMENTED",
                 True,
             ), \
             patch.object(activation_module, "_claim_one_shot_ledger", claim_then_drift):
            with self.assertRaisesRegex(ActivationError, "checkpoint after consume"):
                consume_activation(
                    ledger_dir=self.fixture.ledger_dir,
                    receipt_path=self.fixture.receipt_path,
                    **self.fixture.kwargs(),
                )
        self.assertEqual(len(list(self.fixture.ledger_dir.glob("*.consumed.json"))), 1)
        self.assertFalse(self.fixture.receipt_path.exists())
        mutation_lock = self.fixture.checkpoint_path.with_name("checkpoint.json.mutation.lock")
        self.assertTrue(mutation_lock.is_file())
        self.assertEqual(
            self.fixture.load(mutation_lock)["state"],
            "HELD_FAIL_CLOSED_IRREVERSIBLE_ATTEMPT",
        )
        checkpoint = self.fixture.load(self.fixture.checkpoint_path)
        with self.assertRaisesRegex(GuardError, "CHECKPOINT_MUTATION_LOCKED"):
            abort_checkpoint_no_write(
                self.fixture.checkpoint_path,
                checkpoint["revision"],
                checkpoint_hash(checkpoint),
                "must not hide a consumed draft-save attempt",
            )

    def test_new_activation_id_cannot_reset_the_same_run_attempt(self) -> None:
        activation = self.fixture.load(self.fixture.activation_path)
        activation["activation_id"] = "new-id-must-not-reset-run-attempt"
        self.fixture.save(self.fixture.activation_path, activation)
        with self.assertRaisesRegex(ActivationError, "checkpoint VALIDATED evidence roles"):
            consume_activation(
                ledger_dir=self.fixture.ledger_dir,
                receipt_path=self.fixture.root / "new-id-receipt.json",
                **self.fixture.kwargs(),
            )

    def test_concurrent_consumers_get_exactly_one_claim(self) -> None:
        def attempt(index: int) -> str:
            try:
                consume_activation(
                    ledger_dir=self.fixture.ledger_dir,
                    receipt_path=self.fixture.root / f"race-{index}.json",
                    **self.fixture.kwargs(),
                )
                return "PASS"
            except ActivationError:
                return "BLOCKED"

        with patch.object(activation_module, "validate_checkpoint_checklist_evidence", lambda checkpoint: None), \
             patch.object(activation_module, "CHECKLIST_IMPLEMENTATION_READY", True), \
             patch.object(
                 activation_module.draft_core_module,
                 "PRODUCTION_BROWSER_ATTESTATION_VERIFIER_IMPLEMENTED",
                 True,
             ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = sorted(pool.map(attempt, (1, 2)))
        self.assertEqual(outcomes, ["BLOCKED", "PASS"])
        self.assertEqual(len(list(self.fixture.ledger_dir.glob("*.consumed.json"))), 1)
        checkpoint = self.fixture.load(self.fixture.checkpoint_path)
        self.assertEqual(checkpoint["state"], "SAVE_ATTEMPTED")
        self.assertEqual(checkpoint["save_attempt_count"], 1)

    def test_run_target_and_template_are_each_bound(self) -> None:
        for key, value in (("run_id", "other-run"), ("target_model", "OTHER"), ("template_product_id", "999999")):
            with self.subTest(key=key):
                activation = self.fixture.load(self.fixture.activation_path)
                activation["scope"][key] = value
                self.fixture.save(self.fixture.activation_path, activation)
                self.assert_blocked()
                self.fixture._build()

    def test_each_file_hash_binding_is_enforced(self) -> None:
        keys = (
            "action_plan_sha256",
            "verified_probe_receipt_sha256",
            "probe_source_sha256",
            "endpoint_policy_sha256",
            "probe_read_activation_sha256",
            "redacted_body_semantics_file_sha256",
            "visible_ui_event_sha256",
            "run_spec_sha256",
            "pre_save_readback_sha256",
            "pre_save_verification_sha256",
            "checkpoint_sha256",
        )
        for key in keys:
            with self.subTest(binding=key):
                activation = self.fixture.load(self.fixture.activation_path)
                activation["bindings"][key] = "0" * 64
                self.fixture.save(self.fixture.activation_path, activation)
                self.assert_blocked("hash mismatch")
                self.fixture._build()

    def test_page_fingerprint_and_session_are_current_and_exact(self) -> None:
        activation = self.fixture.load(self.fixture.activation_path)
        activation["current_page"]["fingerprint_sha256"] = "f" * 64
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked("fingerprint")
        self.fixture._build()
        event = self.fixture.load(self.fixture.event_path)
        event["page"]["browser_session_sha256"] = "e" * 64
        self.fixture.save(self.fixture.event_path, event)
        activation = self.fixture.load(self.fixture.activation_path)
        activation["bindings"]["visible_ui_event_sha256"] = sha256_file(self.fixture.event_path)
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked("browser session")

    def test_expired_activation_stale_probe_and_old_event_are_blocked(self) -> None:
        activation = self.fixture.load(self.fixture.activation_path)
        activation["expires_at"] = timestamp(self.fixture.now - timedelta(seconds=1))
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked("expired")

        self.fixture._build()
        activation = self.fixture.load(self.fixture.activation_path)
        issued = datetime.fromisoformat(activation["issued_at"].replace("Z", "+00:00"))
        probe = self.fixture.load(self.fixture.probe_source_path)
        probe["completed_at"] = timestamp(issued - timedelta(minutes=16))
        self.fixture.save(self.fixture.probe_source_path, probe)
        activation["bindings"]["probe_source_sha256"] = sha256_file(self.fixture.probe_source_path)
        self.fixture.save(self.fixture.activation_path, activation)
        verification = self.fixture.load(self.fixture.probe_verification_path)
        verification["input_probe_sha256"] = sha256_file(self.fixture.probe_source_path)
        self.fixture.save(self.fixture.probe_verification_path, verification)
        activation = self.fixture.load(self.fixture.activation_path)
        activation["bindings"]["verified_probe_receipt_sha256"] = sha256_file(self.fixture.probe_verification_path)
        read_activation = self.fixture.load(self.fixture.probe_read_activation_path)
        read_activation["bindings"]["probe_source_sha256"] = sha256_file(self.fixture.probe_source_path)
        read_activation["bindings"]["probe_verification_sha256"] = sha256_file(self.fixture.probe_verification_path)
        read_activation.pop("body_sha256")
        read_activation["body_sha256"] = object_sha256(read_activation)
        self.fixture.save(self.fixture.probe_read_activation_path, read_activation)
        activation["bindings"]["probe_read_activation_sha256"] = sha256_file(self.fixture.probe_read_activation_path)
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked()

        self.fixture._build()
        event = self.fixture.load(self.fixture.event_path)
        event["captured_at"] = timestamp(self.fixture.now - timedelta(seconds=40))
        self.fixture.save(self.fixture.event_path, event)
        activation = self.fixture.load(self.fixture.activation_path)
        activation["current_page"]["observed_at"] = timestamp(self.fixture.now - timedelta(seconds=41))
        activation["bindings"]["visible_ui_event_sha256"] = sha256_file(self.fixture.event_path)
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked()

    def test_exact_https_origin_path_method_and_no_direct_replay(self) -> None:
        mutations = (
            ("origin", "http://post.alibaba.com"),
            ("origin", "https://post.alibaba.com.evil.test"),
            ("path", "/product/submitDraft.json/extra"),
            ("method", "PUT"),
            ("direct_replay_allowed", True),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                activation = self.fixture.load(self.fixture.activation_path)
                activation["endpoint_request"][key] = value
                self.fixture.save(self.fixture.activation_path, activation)
                self.assert_blocked("schema validation failed")
                self.fixture._build()

    def test_global_policy_never_allows_the_draft_route_or_unknown_requests(self) -> None:
        policy = self.fixture.load(self.fixture.policy_path)
        policy["default_decision"] = "ALLOW"
        self.fixture.save(self.fixture.policy_path, policy)
        self.assert_blocked("default decision")

        self.fixture._build()
        policy = self.fixture.load(self.fixture.policy_path)
        draft = next(row for row in policy["rules"] if row["id"] == "ui-save-draft-unverified-seed")
        draft["evidence_state"] = "LIVE_VERIFIED_ACTIVE"
        draft["methods"] = ["POST"]
        self.fixture.save(self.fixture.policy_path, policy)
        self.assert_blocked("globally activated")

    def test_non_allow_probe_decision_blocks_one_shot_draft_activation(self) -> None:
        for decision in ("DENY", "HARD_DENY", "UNKNOWN", "ALLOW_UI_ONLY"):
            with self.subTest(decision=decision):
                verification = self.fixture.load(self.fixture.probe_verification_path)
                verification["request_classifications"][0]["decision"] = decision
                verification["execution_authorized"] = False
                verification["authorization_errors"] = [
                    f"request classification 0 is not ALLOW: {decision}"
                ]
                self.fixture.save(self.fixture.probe_verification_path, verification)
                activation = self.fixture.load(self.fixture.activation_path)
                activation["bindings"]["verified_probe_receipt_sha256"] = sha256_file(
                    self.fixture.probe_verification_path
                )
                self.fixture.save(self.fixture.activation_path, activation)
                self.assert_blocked()
                self.fixture._build()

    def test_handwritten_allow_verification_cannot_bypass_policy_recomputation(self) -> None:
        verification = self.fixture.load(self.fixture.probe_verification_path)
        verification["request_classifications"][0]["reason"] = "handwritten-forged-allow"
        self.fixture.save(self.fixture.probe_verification_path, verification)
        read_activation = self.fixture.load(self.fixture.probe_read_activation_path)
        read_activation["bindings"]["probe_verification_sha256"] = sha256_file(self.fixture.probe_verification_path)
        read_activation.pop("body_sha256")
        read_activation["body_sha256"] = object_sha256(read_activation)
        self.fixture.save(self.fixture.probe_read_activation_path, read_activation)
        activation = self.fixture.load(self.fixture.activation_path)
        activation["bindings"]["verified_probe_receipt_sha256"] = sha256_file(self.fixture.probe_verification_path)
        activation["bindings"]["probe_read_activation_sha256"] = sha256_file(self.fixture.probe_read_activation_path)
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked()

    def test_final_write_has_both_permanent_ui_and_network_denies(self) -> None:
        plan = self.fixture.load(self.fixture.action_plan_path)
        plan["forbidden_actions"] = [row for row in plan["forbidden_actions"] if row["logical_name"] != "submit_product"]
        plan_without_hash = copy.deepcopy(plan)
        plan_without_hash.pop("plan_body_sha256")
        plan["plan_body_sha256"] = object_sha256(plan_without_hash)
        self.fixture.save(self.fixture.action_plan_path, plan)
        self.assert_blocked()

        self.fixture._build()
        policy = self.fixture.load(self.fixture.policy_path)
        policy["rules"] = [row for row in policy["rules"] if row["id"] != "final-publish-route-mutation-hard-deny"]
        self.fixture.save(self.fixture.policy_path, policy)
        self.assert_blocked("final publish mutation hard deny")

    def test_visible_ui_event_must_be_unique_and_not_programmatic(self) -> None:
        for branch, key, value in (
            ("element", "match_count", 2),
            ("security", "direct_endpoint_call_used", True),
            ("security", "request_replay_used", True),
        ):
            with self.subTest(key=key):
                event = self.fixture.load(self.fixture.event_path)
                event[branch][key] = value
                self.fixture.save(self.fixture.event_path, event)
                activation = self.fixture.load(self.fixture.activation_path)
                activation["bindings"]["visible_ui_event_sha256"] = sha256_file(self.fixture.event_path)
                self.fixture.save(self.fixture.activation_path, activation)
                self.assert_blocked("schema validation failed")
                self.fixture._build()

    def test_body_semantics_exclude_raw_credentials_and_are_self_hashing(self) -> None:
        body = self.fixture.load(self.fixture.body_path)
        body["redaction"]["raw_body_persisted"] = True
        self.fixture.save(self.fixture.body_path, body)
        activation = self.fixture.load(self.fixture.activation_path)
        activation["bindings"]["redacted_body_semantics_file_sha256"] = sha256_file(self.fixture.body_path)
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked("schema validation failed")

        self.fixture._build()
        body = self.fixture.load(self.fixture.body_path)
        body["business_field_digests"]["_csrf"] = object_sha256("must-not-persist")
        body["body_key_names"] = sorted(set(body["body_key_names"]) | {"_csrf"})
        self.fixture.save(self.fixture.body_path, body)
        activation = self.fixture.load(self.fixture.activation_path)
        activation["bindings"]["redacted_body_semantics_file_sha256"] = sha256_file(self.fixture.body_path)
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked("overlap")

    def test_action_plan_body_tamper_is_detected_even_if_file_binding_is_updated(self) -> None:
        plan = self.fixture.load(self.fixture.action_plan_path)
        plan["target"]["maximum_draft_save_count"] = 2
        self.fixture.save(self.fixture.action_plan_path, plan)
        activation = self.fixture.load(self.fixture.activation_path)
        activation["bindings"]["action_plan_sha256"] = sha256_file(self.fixture.action_plan_path)
        self.fixture.save(self.fixture.activation_path, activation)
        self.assert_blocked()


if __name__ == "__main__":
    unittest.main(verbosity=2)
