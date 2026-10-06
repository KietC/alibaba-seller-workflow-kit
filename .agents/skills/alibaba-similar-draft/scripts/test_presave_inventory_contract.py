from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from localized_field_contract import build_localized_target

from test_draft_save_activation import ActivationFixture, timestamp
from validate_draft_save_activation import ACTIVATION_RECEIPT_SCHEMA, sha256_file, validate_schema
from validate_post_save_inventory import object_sha256 as inventory_object_sha256
from validate_post_save_inventory import verify_inventory_delta
from validate_pre_save_readback import (
    EMPTY_READBACK_SHA256,
    expected_contract,
    object_sha256,
    verify_pre_save,
)


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def with_body_hash(document: dict, field: str) -> dict:
    body = copy.deepcopy(document)
    body.pop(field, None)
    document[field] = inventory_object_sha256(body)
    return document


class PresaveInventoryFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.activation = ActivationFixture(root / "activation")
        self.plan_path = self.activation.action_plan_path
        self.run_path = self.activation.paths["run"]
        self.probe_source_path = self.activation.probe_source_path
        self.probe_verification_path = self.activation.probe_verification_path
        self.pre_save_path = root / "pre-save-readback.json"
        self.pre_save_verification_path = root / "pre-save-verification.json"
        self.before_path = root / "inventory-before.json"
        self.after_path = root / "inventory-after.json"
        self.post_refresh_path = root / "post-refresh.json"
        self.save_result_path = root / "save-result.json"
        self.saved_id = "100000000018"
        self._build_pre_save()
        self.pre_save_receipt = self.verify_pre_save()
        if self.pre_save_receipt["status"] != "PASS_PRE_SAVE_NO_WRITE":
            raise AssertionError(self.pre_save_receipt)
        write_json(self.pre_save_verification_path, self.pre_save_receipt)
        self.activation_receipt = self._build_synthetic_activation_receipt()
        self._build_post_save()

    def close(self) -> None:
        self.activation.close()

    def _build_pre_save(self) -> None:
        plan = read_json(self.plan_path)
        probe_source = read_json(self.probe_source_path)
        errors: list[str] = []
        contract = expected_contract(plan, errors)
        if errors:
            raise AssertionError(errors)
        completed = datetime.fromisoformat(probe_source["completed_at"].replace("Z", "+00:00"))
        captured = completed + timedelta(seconds=1)
        fields = []
        for row in contract["fields"].values():
            fields.append({
                "target": copy.deepcopy(row["target"]), "expected_sha256": row["expected_sha256"],
                "observed_sha256": row["expected_sha256"], "source_id": row["source_id"], "decision_id": row["decision_id"],
            })
        clears = []
        for row in contract["clears"].values():
            clears.append({
                "target": copy.deepcopy(row["target"]), "reason": row["reason"], "captured_phase": "CLEAR_INHERITED_FIELDS",
                "expected_state": "EMPTY", "observed_state": "EMPTY", "expected_sha256": EMPTY_READBACK_SHA256,
                "observed_sha256": EMPTY_READBACK_SHA256,
            })
        assets = []
        for row in contract["assets"].values():
            assets.append({**copy.deepcopy(row), "observed_sha256": row["expected_sha256"]})
        non_target_hash = object_sha256({"visible_non_targets": "unchanged"})
        readback = {
            "schema_version": "sellerkit.alibaba.pre-save-readback.v1", "readback_id": "pre-save-fixture",
            "run_id": plan["target"]["run_id"], "captured_at": timestamp(captured), "phase": "PRE_SAVE_NO_DRAFT_WRITE",
            "target_model": plan["target"]["model"], "template_product_id": plan["target"]["template_product_id"],
            "current_page_fingerprint_sha256": probe_source["page"]["fingerprint_sha256"],
            "browser_session_sha256": probe_source["network"]["interception_session_sha256"],
            "bindings": {
                "action_plan_sha256": sha256_file(self.plan_path), "action_plan_body_sha256": plan["plan_body_sha256"],
                "run_spec_sha256": sha256_file(self.run_path), "probe_source_sha256": sha256_file(self.probe_source_path),
                "probe_verification_sha256": sha256_file(self.probe_verification_path),
                "expected_field_set_sha256": contract["field_set_sha256"], "expected_clear_set_sha256": contract["clear_set_sha256"],
                "expected_asset_set_sha256": contract["asset_set_sha256"],
                "expected_non_target_scope_sha256": contract["non_target_scope_sha256"],
            },
            "field_readbacks": fields, "cleared_targets": clears, "asset_readbacks": assets,
            "non_target_scope_sha256": contract["non_target_scope_sha256"],
            "non_target_snapshot_before_sha256": non_target_hash, "non_target_snapshot_after_sha256": non_target_hash,
            "non_target_field_diffs": [], "all_planned_actions_executed": True, "inherited_content_cleared": True,
            "template_video_present": False, "visible_ui_save_used": False, "direct_endpoint_write_used": False,
            "draft_save_count": 0, "final_write_count": 0, "final_submit_triggered": False, "unknown_write_count": 0,
            "request_guard_active": True, "final_ui_guard_active": True, "final_network_guard_active": True,
        }
        write_json(self.pre_save_path, readback)

    def verify_pre_save(self) -> dict:
        return verify_pre_save(
            action_plan_path=self.plan_path, run_spec_path=self.run_path,
            probe_source_path=self.probe_source_path, probe_verification_path=self.probe_verification_path,
            pre_save_readback_path=self.pre_save_path, now=self.activation.now,
        )

    def _build_synthetic_activation_receipt(self) -> dict:
        """Build test-only downstream input without invoking the live consume gate.

        This receipt exists only in TemporaryDirectory, is named and identified
        SYNTHETIC_TEST/LOCAL_CONTRACT_ONLY, and does not create a one-shot
        production ledger claim or grant browser/network authority.
        """

        activation = read_json(self.activation.activation_path)
        event = read_json(self.activation.event_path)
        checkpoint = read_json(self.activation.checkpoint_path)
        receipt = {
            "schema_version": "sellerkit.alibaba.draft-save-activation-receipt.v1",
            "receipt_id": "SYNTHETIC_TEST-LOCAL_CONTRACT_ONLY-draft-save",
            "generated_at": timestamp(self.activation.now),
            "expires_at": activation["expires_at"],
            "status": "PASS_ONE_SHOT_DRAFT_SAVE_ACTIVATED",
            "result_semantics": "ATTEMPT_CONSUMED_NOT_DRAFT_SUCCESS",
            "activation_request_sha256": sha256_file(self.activation.activation_path),
            "scope": {
                "run_id": activation["scope"]["run_id"],
                "target_model": activation["scope"]["target_model"],
                "template_product_id": activation["scope"]["template_product_id"],
                "browser_session_sha256": activation["scope"]["browser_session_sha256"],
            },
            "bindings": {
                "action_plan_sha256": activation["bindings"]["action_plan_sha256"],
                "action_plan_body_sha256": activation["bindings"]["action_plan_body_sha256"],
                "verified_probe_receipt_sha256": activation["bindings"]["verified_probe_receipt_sha256"],
                "probe_source_sha256": activation["bindings"]["probe_source_sha256"],
                "endpoint_policy_sha256": activation["bindings"]["endpoint_policy_sha256"],
                "probe_read_activation_sha256": activation["bindings"]["probe_read_activation_sha256"],
                "current_page_fingerprint_sha256": activation["current_page"]["fingerprint_sha256"],
                "redacted_body_semantic_sha256": activation["bindings"]["redacted_body_semantic_sha256"],
                "visible_ui_event_sha256": activation["bindings"]["visible_ui_event_sha256"],
                "run_spec_sha256": activation["bindings"]["run_spec_sha256"],
                "pre_save_readback_sha256": activation["bindings"]["pre_save_readback_sha256"],
                "pre_save_verification_sha256": activation["bindings"]["pre_save_verification_sha256"],
                "checkpoint_sha256": activation["bindings"]["checkpoint_sha256"],
                "checkpoint_revision": checkpoint["revision"],
                "attempt_claim_receipt_sha256": inventory_object_sha256({
                    "trust": "SYNTHETIC_TEST",
                    "kind": "ATTEMPT_CLAIM_NOT_AUTHORIZATION",
                }),
            },
            "endpoint_request": {
                "origin": activation["endpoint_request"]["origin"],
                "path": activation["endpoint_request"]["path"],
                "method": activation["endpoint_request"]["method"],
                "visible_ui_only": True,
                "direct_replay_allowed": False,
            },
            "visible_ui_event": {
                "event_id": event["event_id"],
                "event_at": event["captured_at"],
                "event_evidence_sha256": sha256_file(self.activation.event_path),
            },
            "attempt_transition": {
                "before": 0,
                "after": 1,
                "maximum": 1,
                "atomic_ledger_claimed": True,
                "retry_allowed": False,
            },
            "guard_assertions": {
                "global_policy_remains_default_deny": True,
                "unknown_requests_denied": True,
                "draft_route_not_globally_allowed": True,
                "direct_replay_denied": True,
                "final_write_ui_hard_denied": True,
                "final_write_network_hard_denied": True,
                "request_guard_must_release_exactly_this_request_once": True,
            },
            "ledger_entry_sha256": inventory_object_sha256({
                "trust": "SYNTHETIC_TEST",
                "scope": "LOCAL_CONTRACT_ONLY",
                "browser_or_network_authority": False,
            }),
            "checkpoint_transition": {
                "state": "SAVE_ATTEMPTED",
                "evidence_path": str(
                    self.activation.root
                    / "SYNTHETIC_TEST-LOCAL_CONTRACT_ONLY.checkpoint-transition-evidence.json"
                ),
                "checkpoint_revision_before": checkpoint["revision"],
                "checkpoint_revision_after": checkpoint["revision"] + 1,
                "checkpoint_sha256_after": inventory_object_sha256({
                    "trust": "SYNTHETIC_TEST",
                    "state": "SAVE_ATTEMPTED",
                }),
            },
            "release_authorized": True,
        }
        validate_schema(receipt, ACTIVATION_RECEIPT_SCHEMA, "synthetic local-contract activation receipt")
        write_json(self.activation.receipt_path, receipt)
        return receipt

    def _id_set(self, values: list[str]) -> dict:
        values = sorted(values, key=lambda item: (len(item), item))
        return {"count": len(values), "ids": values, "id_set_sha256": inventory_object_sha256(values)}

    def _capture_material(
        self,
        *,
        role: str,
        captured_at: str,
        browser_session_sha256: str,
        page_fingerprint_sha256: str,
        payload: dict,
    ) -> dict:
        capture_id = f"capture-{role.lower()}"
        material = {
            "schema_version": "sellerkit.alibaba.browser-capture-material.v1",
            "capture_id": capture_id,
            "role": role,
            "captured_at": captured_at,
            "source": "SYNTHETIC_TEST",
            "browser_session_sha256": browser_session_sha256,
            "page_fingerprint_sha256": page_fingerprint_sha256,
            "tool_result_id_sha256": inventory_object_sha256({"tool_result": role}),
            "credential_values_recorded": False,
            "raw_secret_values_recorded": False,
            "payload": payload,
            "payload_sha256": inventory_object_sha256(payload),
        }
        path = (self.root / "capture-material" / f"{role.lower()}.json").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, material)
        return {
            "role": role,
            "capture_id": capture_id,
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    def _snapshot(self, *, phase: str, captured_at: str, activation_hash: str | None, drafts: list[str]) -> dict:
        plan = read_json(self.plan_path)
        prefix = "INVENTORY_BEFORE" if phase == "BEFORE_DRAFT_SAVE" else "INVENTORY_AFTER"
        page_role = f"{prefix}_VISIBLE_PAGE"
        network_role = f"{prefix}_NETWORK_RESPONSE"
        session = self.activation_receipt["scope"]["browser_session_sha256"]
        page_fingerprint = inventory_object_sha256({"phase": phase, "page": "product-manager"})
        online = self._id_set(["100000000008", "100000000009"])
        pending = self._id_set(["100000000010"])
        draft = self._id_set(drafts)
        capture_evidence = [
            self._capture_material(
                role=page_role,
                captured_at=captured_at,
                browser_session_sha256=session,
                page_fingerprint_sha256=page_fingerprint,
                payload={
                    "phase": phase,
                    "collection_source": "CODEX_IN_APP_BROWSER_VISIBLE_UI",
                    "page_fingerprint_sha256": page_fingerprint,
                },
            ),
            self._capture_material(
                role=network_role,
                captured_at=captured_at,
                browser_session_sha256=session,
                page_fingerprint_sha256=page_fingerprint,
                payload={
                    "phase": phase,
                    "online_ids": online["ids"],
                    "pending_ids": pending["ids"],
                    "draft_ids": draft["ids"],
                    "complete": True,
                    "pagination_exhausted": True,
                    "all_pages_read": True,
                },
            ),
        ]
        snapshot = {
            "schema_version": "sellerkit.alibaba.product-inventory-snapshot.v1",
            "snapshot_id": f"inventory-{phase.lower()}", "run_id": plan["target"]["run_id"],
            "captured_at": captured_at, "phase": phase, "target_model": plan["target"]["model"],
            "template_product_id": plan["target"]["template_product_id"],
            "browser_session_sha256": session,
            "page_fingerprint_sha256": page_fingerprint,
            "capture_evidence": capture_evidence,
            "bindings": {
                "action_plan_sha256": sha256_file(self.plan_path), "action_plan_body_sha256": plan["plan_body_sha256"],
                "run_spec_sha256": sha256_file(self.run_path), "probe_verification_sha256": sha256_file(self.probe_verification_path),
                "pre_save_verification_sha256": sha256_file(self.pre_save_verification_path),
                "draft_save_activation_receipt_sha256": activation_hash,
            },
            "collection": {
                "source": "CODEX_IN_APP_BROWSER_VISIBLE_UI", "complete": True, "pagination_exhausted": True,
                "all_pages_read": True, "duplicate_ids_observed": 0, "credential_values_recorded": False,
                "raw_response_bodies_recorded": False, "evidence_sha256": inventory_object_sha256({"phase": phase}),
            },
            "online": online,
            "pending": pending, "draft": draft,
            "final_write_count": 0, "final_submit_triggered": False,
        }
        return with_body_hash(snapshot, "snapshot_body_sha256")

    def _build_post_save(self) -> None:
        plan = read_json(self.plan_path)
        pre_save = read_json(self.pre_save_path)
        event_at = datetime.fromisoformat(self.activation_receipt["visible_ui_event"]["event_at"].replace("Z", "+00:00"))
        response_at = event_at + timedelta(milliseconds=200)
        after_at = response_at + timedelta(milliseconds=200)
        reopen_at = response_at + timedelta(milliseconds=400)
        before = self._snapshot(
            phase="BEFORE_DRAFT_SAVE", captured_at=pre_save["captured_at"], activation_hash=None,
            drafts=["100000000012"],
        )
        after = self._snapshot(
            phase="AFTER_DRAFT_SAVE", captured_at=timestamp(after_at),
            activation_hash=sha256_file(self.activation.receipt_path), drafts=["100000000012", self.saved_id],
        )
        write_json(self.before_path, before)
        write_json(self.after_path, after)

        contract_errors: list[str] = []
        contract = expected_contract(plan, contract_errors)
        if contract_errors:
            raise AssertionError(contract_errors)
        write_target_keys = set(contract["fields"])
        uploaded_logical = {str(row["target"].get("logical_name") or "") for row in contract["assets"].values()}
        persistent_empty = []
        for key, row in contract["clears"].items():
            target = row["target"]
            if key in write_target_keys or (target.get("kind") == "FIELD_REGISTRY" and target.get("logical_name") in uploaded_logical):
                continue
            persistent_empty.append({
                "target": copy.deepcopy(target), "reason": row["reason"], "captured_phase": "POST_REFRESH_REOPEN",
                "expected_state": "EMPTY", "observed_state": "EMPTY", "expected_sha256": EMPTY_READBACK_SHA256,
                "observed_sha256": EMPTY_READBACK_SHA256,
            })
        field_readbacks = [{
            "target": copy.deepcopy(row["target"]), "expected_sha256": row["expected_sha256"],
            "observed_sha256": row["expected_sha256"], "source_id": row["source_id"], "decision_id": row["decision_id"],
        } for row in contract["fields"].values()]
        non_target_hash = pre_save["non_target_snapshot_after_sha256"]
        refresh = {
            "schema_version": "sellerkit.post-refresh-verification.v1", "verification_id": "post-refresh-fixture",
            "run_id": plan["target"]["run_id"], "captured_at": timestamp(reopen_at), "phase": "POST_REFRESH_REOPEN",
            "target_model": plan["target"]["model"], "template_product_id": plan["target"]["template_product_id"],
            "draft_id": self.saved_id,
            "bindings": {
                "action_plan_sha256": sha256_file(self.plan_path), "action_plan_body_sha256": plan["plan_body_sha256"],
                "run_spec_sha256": sha256_file(self.run_path), "probe_receipt_sha256": sha256_file(self.probe_verification_path),
                "expected_field_set_sha256": contract["field_set_sha256"], "expected_clear_set_sha256": contract["clear_set_sha256"],
                "expected_post_refresh_empty_set_sha256": object_sha256([
                    {"target": row["target"], "reason": row["reason"]}
                    for row in sorted(persistent_empty, key=lambda item: json.dumps(item["target"], sort_keys=True))
                ]),
            },
            "field_readbacks": field_readbacks, "cleared_targets": persistent_empty,
            "non_target_snapshot_before_sha256": non_target_hash, "non_target_snapshot_after_sha256": non_target_hash,
            "non_target_field_diffs": [], "inherited_content_cleared": True, "template_video_present": False,
            "refresh_performed": True, "same_draft_reopened": True, "persistence_verified": True,
            "draft_save_count": 1, "final_write_count": 0, "final_submit_triggered": False,
        }
        write_json(self.post_refresh_path, refresh)
        save = {
            "schema_version": "sellerkit.alibaba.draft-save-result-evidence.v1", "evidence_id": "save-result-fixture",
            "run_id": plan["target"]["run_id"], "captured_at": timestamp(reopen_at),
            "target_model": plan["target"]["model"], "template_product_id": plan["target"]["template_product_id"],
            "saved_draft_id": self.saved_id, "browser_session_sha256": self.activation_receipt["scope"]["browser_session_sha256"],
            "bindings": {
                "action_plan_sha256": sha256_file(self.plan_path), "action_plan_body_sha256": plan["plan_body_sha256"],
                "run_spec_sha256": sha256_file(self.run_path), "probe_verification_sha256": sha256_file(self.probe_verification_path),
                "pre_save_verification_sha256": sha256_file(self.pre_save_verification_path),
                "draft_save_activation_receipt_sha256": sha256_file(self.activation.receipt_path),
                "before_inventory_sha256": sha256_file(self.before_path), "after_inventory_sha256": sha256_file(self.after_path),
                "post_refresh_receipt_sha256": sha256_file(self.post_refresh_path),
            },
            "save_response": {
                "captured_at": timestamp(response_at), "origin": "https://post.alibaba.com", "path": "/product/submitDraft.json",
                "method": "POST", "http_status": 200, "business_success": True, "business_status": "SUCCESS",
                "response_draft_id": self.saved_id, "error_code": None, "redacted_response_sha256": inventory_object_sha256({"draftId": self.saved_id}),
                "raw_response_persisted": False, "credential_values_persisted": False,
            },
            "reopen": {
                "captured_at": timestamp(reopen_at),
                "url": f"https://post.alibaba.com/product/publish.htm?itemId={self.saved_id}&pubAction=draft",
                "origin": "https://post.alibaba.com", "path": "/product/publish.htm",
                "query": {"itemId": self.saved_id, "pubAction": "draft"}, "draft_id_readback": self.saved_id,
                "same_draft_reopened": True, "login_active": True, "captcha_present": False,
                "page_fingerprint_sha256": inventory_object_sha256({"draft": self.saved_id}), "credential_values_recorded": False,
            },
            "draft_save_count": 1, "final_write_count": 0, "final_submit_triggered": False,
        }
        response_payload = {
            key: save["save_response"].get(key)
            for key in (
                "origin", "path", "method", "http_status", "business_success",
                "business_status", "response_draft_id", "error_code",
            )
        }
        reopen_payload = {
            key: save["reopen"].get(key)
            for key in (
                "url", "query", "draft_id_readback", "same_draft_reopened",
                "login_active", "captcha_present", "page_fingerprint_sha256",
            )
        }
        save["capture_evidence"] = [
            self._capture_material(
                role="DRAFT_SAVE_NETWORK_RESPONSE",
                captured_at=save["save_response"]["captured_at"],
                browser_session_sha256=save["browser_session_sha256"],
                page_fingerprint_sha256=read_json(self.probe_verification_path)["page_fingerprint"],
                payload=response_payload,
            ),
            self._capture_material(
                role="DRAFT_REOPEN_VISIBLE_PAGE",
                captured_at=save["reopen"]["captured_at"],
                browser_session_sha256=save["browser_session_sha256"],
                page_fingerprint_sha256=save["reopen"]["page_fingerprint_sha256"],
                payload=reopen_payload,
            ),
        ]
        write_json(self.save_result_path, with_body_hash(save, "result_body_sha256"))

    def verify_inventory(self) -> dict:
        return verify_inventory_delta(
            action_plan_path=self.plan_path, run_spec_path=self.run_path,
            probe_verification_path=self.probe_verification_path,
            pre_save_verification_path=self.pre_save_verification_path,
            activation_receipt_path=self.activation.receipt_path,
            before_inventory_path=self.before_path, after_inventory_path=self.after_path,
            save_result_path=self.save_result_path, post_refresh_path=self.post_refresh_path,
            adapter_path=self.activation.paths["adapter"],
            now=self.activation.now,
        )


class PreSaveReadbackContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = PresaveInventoryFixture(Path(self.temporary.name) / "case")

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def assert_pre_save_blocked(self, needle: str) -> None:
        receipt = self.fixture.verify_pre_save()
        self.assertEqual(receipt["status"], "BLOCKED_PRE_SAVE", receipt)
        self.assertTrue(any(needle in row for row in receipt["errors"]), receipt)

    def test_exact_fields_clears_assets_and_no_save_pass(self) -> None:
        receipt = self.fixture.verify_pre_save()
        self.assertEqual(receipt["status"], "PASS_PRE_SAVE_NO_WRITE", receipt)
        schema = read_json(SCHEMA_DIR / "pre-save-verification-receipt.schema.json")
        Draft202012Validator(schema).validate(receipt)
        self.assertGreater(receipt["expected_asset_count"], 0)
        self.assertEqual(receipt["draft_save_count"], 0)

    def test_missing_field_and_wrong_asset_are_blocked(self) -> None:
        value = read_json(self.fixture.pre_save_path)
        value["field_readbacks"].pop()
        write_json(self.fixture.pre_save_path, value)
        self.assert_pre_save_blocked("missing expected field")

        self.fixture._build_pre_save()
        value = read_json(self.fixture.pre_save_path)
        value["asset_readbacks"][0]["observed_sha256"] = "f" * 64
        write_json(self.fixture.pre_save_path, value)
        self.assert_pre_save_blocked("asset observed_sha256 mismatch")

    def test_clear_repopulation_and_non_target_diff_are_blocked(self) -> None:
        value = read_json(self.fixture.pre_save_path)
        value["cleared_targets"][0]["observed_state"] = "EMPTY"  # retain schema; corrupt digest instead
        value["cleared_targets"][0]["observed_sha256"] = "f" * 64
        write_json(self.fixture.pre_save_path, value)
        self.assert_pre_save_blocked("clear empty hash mismatch")

        self.fixture._build_pre_save()
        value = read_json(self.fixture.pre_save_path)
        value["non_target_snapshot_after_sha256"] = "f" * 64
        write_json(self.fixture.pre_save_path, value)
        self.assert_pre_save_blocked("non-target snapshot changed")

    def test_any_save_or_final_write_signal_is_blocked(self) -> None:
        value = read_json(self.fixture.pre_save_path)
        value["draft_save_count"] = 1
        value["visible_ui_save_used"] = True
        write_json(self.fixture.pre_save_path, value)
        self.assert_pre_save_blocked("captured after a draft-save")

        self.fixture._build_pre_save()
        value = read_json(self.fixture.pre_save_path)
        value["final_write_count"] = 1
        write_json(self.fixture.pre_save_path, value)
        self.assert_pre_save_blocked("final-write signal")

    def test_probe_action_run_and_session_bindings_are_exact(self) -> None:
        value = read_json(self.fixture.pre_save_path)
        value["bindings"]["action_plan_sha256"] = "f" * 64
        write_json(self.fixture.pre_save_path, value)
        self.assert_pre_save_blocked("action_plan_sha256")

        self.fixture._build_pre_save()
        value = read_json(self.fixture.pre_save_path)
        value["browser_session_sha256"] = "f" * 64
        write_json(self.fixture.pre_save_path, value)
        self.assert_pre_save_blocked("browser session")

    def test_zero_write_without_exact_execution_authorization_is_blocked(self) -> None:
        verification = read_json(self.fixture.probe_verification_path)
        verification["request_classifications"][0]["decision"] = "DENY"
        verification["execution_authorized"] = False
        verification["authorization_errors"] = ["request classification 0 is not ALLOW: DENY"]
        write_json(self.fixture.probe_verification_path, verification)
        readback = read_json(self.fixture.pre_save_path)
        readback["bindings"]["probe_verification_sha256"] = sha256_file(self.fixture.probe_verification_path)
        write_json(self.fixture.pre_save_path, readback)
        self.assert_pre_save_blocked("does not authorize controlled execution")
        self.assert_pre_save_blocked("not exactly ALLOW")

    def test_exact_read_authorization_without_adapter_readiness_is_blocked(self) -> None:
        verification = read_json(self.fixture.probe_verification_path)
        verification["adapter_production_execution_ready"] = False
        verification["production_execution_authorized"] = False
        verification["production_authorization_errors"] = [
            "browser adapter production execution readiness is not certified"
        ]
        write_json(self.fixture.probe_verification_path, verification)
        readback = read_json(self.fixture.pre_save_path)
        readback["bindings"]["probe_verification_sha256"] = sha256_file(
            self.fixture.probe_verification_path
        )
        write_json(self.fixture.pre_save_path, readback)
        self.assert_pre_save_blocked("does not authorize production execution")
        self.assert_pre_save_blocked("adapter production readiness is not certified")


class LocalizedPreSaveTargetTests(unittest.TestCase):
    def test_localized_target_is_page_bound_and_clear_must_pair_with_write(self) -> None:
        now = datetime.now(timezone.utc)
        value = "Commercial pre-rinse faucet"
        fingerprint = "a" * 64
        target = build_localized_target(
            logical_name="localized.product_title.en", locale="en", live_language_label="English",
            value=value, page_fingerprint_sha256=fingerprint, live_control_evidence_sha256="b" * 64,
            maximum=128, unit="UNICODE_CODE_POINTS", observed_count=len(value),
            captured_at=timestamp(now), expires_at=timestamp(now + timedelta(minutes=10)),
        )
        value_hash = inventory_object_sha256(value)
        plan = {"phases": [
            {"name": "CLEAR_INHERITED_FIELDS", "operations": [{
                "operation": "CLEAR_INHERITED_FIELD", "target": copy.deepcopy(target),
                "reason": "LOCALIZED_CLEAR_BEFORE_FILL",
            }]},
            {"name": "WRITE_APPROVED_FIELD_VALUES", "operations": [{
                "operation": "WRITE_APPROVED_FIELD_VALUE", "target": copy.deepcopy(target),
                "value": value, "value_sha256": value_hash,
                "approval": {"source_id": "localized-source", "decision_id": "localized-decision"},
                "readback": {"expected_sha256": value_hash},
            }]},
            {"name": "UPLOAD_APPROVED_ASSETS", "operations": []},
        ]}
        errors: list[str] = []
        contract = expected_contract(plan, errors, expected_page_fingerprint_sha256=fingerprint)
        self.assertEqual(errors, [])
        self.assertEqual(len(contract["fields"]), 1)
        self.assertEqual(len(contract["clears"]), 1)

        drift_errors: list[str] = []
        expected_contract(plan, drift_errors, expected_page_fingerprint_sha256="c" * 64)
        self.assertTrue(any("page fingerprint" in row for row in drift_errors), drift_errors)

        unpaired = copy.deepcopy(plan)
        unpaired["phases"][1]["operations"] = []
        unpaired_errors: list[str] = []
        expected_contract(unpaired, unpaired_errors, expected_page_fingerprint_sha256=fingerprint)
        self.assertTrue(any("no identical approved write" in row for row in unpaired_errors), unpaired_errors)


class InventoryDeltaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = PresaveInventoryFixture(Path(self.temporary.name) / "case")

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def assert_inventory_blocked(self, needle: str) -> None:
        receipt = self.fixture.verify_inventory()
        self.assertEqual(receipt["status"], "BLOCKED_INVENTORY_DELTA", receipt)
        self.assertTrue(any(needle in row for row in receipt["errors"]), receipt)

    def mutate_snapshot(self, path: Path, function) -> None:
        value = read_json(path)
        function(value)
        with_body_hash(value, "snapshot_body_sha256")
        write_json(path, value)

    def refresh_save_bindings(self) -> None:
        save = read_json(self.fixture.save_result_path)
        save["bindings"]["before_inventory_sha256"] = sha256_file(self.fixture.before_path)
        save["bindings"]["after_inventory_sha256"] = sha256_file(self.fixture.after_path)
        with_body_hash(save, "result_body_sha256")
        write_json(self.fixture.save_result_path, save)

    def test_synthetic_exact_delta_is_consistency_only_not_live_pass(self) -> None:
        receipt = self.fixture.verify_inventory()
        self.assertEqual(receipt["status"], "BLOCKED_LIVE_ATTESTATION", receipt)
        self.assertEqual(receipt["trust_status"], "CONSISTENCY_ONLY")
        self.assertEqual(receipt["result_semantics"], "CONSISTENCY_ONLY_NOT_LIVE_ATTESTED")
        self.assertEqual(receipt["delta"]["draft_added"], [self.fixture.saved_id])
        self.assertEqual(receipt["delta"]["online_added"], [])
        self.assertEqual(receipt["delta"]["pending_added"], [])
        schema = read_json(SCHEMA_DIR / "inventory-delta-verification-receipt.schema.json")
        Draft202012Validator(schema).validate(receipt)

    def test_missing_or_tampered_capture_material_is_blocked(self) -> None:
        after = read_json(self.fixture.after_path)
        after["capture_evidence"] = []
        with_body_hash(after, "snapshot_body_sha256")
        write_json(self.fixture.after_path, after)
        self.refresh_save_bindings()
        self.assert_inventory_blocked("trusted capture")

        self.fixture._build_post_save()
        after = read_json(self.fixture.after_path)
        material_path = Path(after["capture_evidence"][0]["path"])
        material = read_json(material_path)
        material["payload"]["phase"] = "BEFORE_DRAFT_SAVE"
        write_json(material_path, material)
        self.assert_inventory_blocked("sha256 mismatch")

    def test_online_or_pending_changes_are_blocked(self) -> None:
        def add_online(value: dict) -> None:
            ids = value["online"]["ids"] + ["100000000011"]
            value["online"] = self.fixture._id_set(ids)
        self.mutate_snapshot(self.fixture.after_path, add_online)
        self.refresh_save_bindings()
        self.assert_inventory_blocked("online product ID set changed")

        self.fixture._build_post_save()
        def remove_pending(value: dict) -> None:
            value["pending"] = self.fixture._id_set([])
        self.mutate_snapshot(self.fixture.after_path, remove_pending)
        self.refresh_save_bindings()
        self.assert_inventory_blocked("pending product ID set changed")

    def test_extra_or_preexisting_draft_is_blocked(self) -> None:
        def add_extra(value: dict) -> None:
            value["draft"] = self.fixture._id_set(value["draft"]["ids"] + ["100000000017"])
        self.mutate_snapshot(self.fixture.after_path, add_extra)
        self.refresh_save_bindings()
        self.assert_inventory_blocked("draft delta")

        self.fixture._build_post_save()
        def preexist(value: dict) -> None:
            value["draft"] = self.fixture._id_set(value["draft"]["ids"] + [self.fixture.saved_id])
        self.mutate_snapshot(self.fixture.before_path, preexist)
        self.refresh_save_bindings()
        self.assert_inventory_blocked("already existed")

    def test_response_reopen_and_post_refresh_ids_must_all_match(self) -> None:
        save = read_json(self.fixture.save_result_path)
        save["save_response"]["response_draft_id"] = "100000000017"
        with_body_hash(save, "result_body_sha256")
        write_json(self.fixture.save_result_path, save)
        self.assert_inventory_blocked("save response draft ID")

        self.fixture._build_post_save()
        save = read_json(self.fixture.save_result_path)
        save["reopen"]["url"] = "https://post.alibaba.com/product/publish.htm?itemId=100000000017&pubAction=draft"
        with_body_hash(save, "result_body_sha256")
        write_json(self.fixture.save_result_path, save)
        self.assert_inventory_blocked("reopen URL")

        self.fixture._build_post_save()
        refresh = read_json(self.fixture.post_refresh_path)
        refresh["draft_id"] = "100000000017"
        write_json(self.fixture.post_refresh_path, refresh)
        self.assert_inventory_blocked("post-refresh receipt")

    def test_inventory_hash_session_and_activation_bindings_are_fail_closed(self) -> None:
        after = read_json(self.fixture.after_path)
        after["draft"]["id_set_sha256"] = "f" * 64
        with_body_hash(after, "snapshot_body_sha256")
        write_json(self.fixture.after_path, after)
        self.refresh_save_bindings()
        self.assert_inventory_blocked("id_set_sha256")

        self.fixture._build_post_save()
        after = read_json(self.fixture.after_path)
        after["browser_session_sha256"] = "f" * 64
        with_body_hash(after, "snapshot_body_sha256")
        write_json(self.fixture.after_path, after)
        self.refresh_save_bindings()
        self.assert_inventory_blocked("one browser session")

    def test_before_inventory_must_strictly_precede_visible_save_event(self) -> None:
        event_at = self.fixture.activation_receipt["visible_ui_event"]["event_at"]
        before = read_json(self.fixture.before_path)
        before["captured_at"] = event_at
        for descriptor in before["capture_evidence"]:
            material_path = Path(descriptor["path"])
            material = read_json(material_path)
            material["captured_at"] = event_at
            write_json(material_path, material)
            descriptor["bytes"] = material_path.stat().st_size
            descriptor["sha256"] = sha256_file(material_path)
        with_body_hash(before, "snapshot_body_sha256")
        write_json(self.fixture.before_path, before)
        self.refresh_save_bindings()

        self.assert_inventory_blocked(
            "before inventory must be captured strictly before the visible save click"
        )


if __name__ == "__main__":
    unittest.main()
