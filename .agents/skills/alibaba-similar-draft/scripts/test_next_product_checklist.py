#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import validate_next_product_checklist as validator


class NextProductChecklistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.artifacts: dict[str, dict[str, str]] = {}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def artifact(self, name: str, payload: object | None = None) -> dict[str, str]:
        path = self.root / name
        if payload is None:
            raw = f"evidence:{name}\n".encode()
        else:
            raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        result = {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        self.artifacts[name] = result
        return result

    def make_document(self, stage: str = "pre_write") -> dict:
        run_id = "RUN-20300101-0001"
        model = "MODEL-001"
        template_id = "100000000001"
        action_plan_payload = {
            "target": {
                "run_id": run_id,
                "model": model,
                "template_product_id": template_id,
                "mode": "SAVE_DRAFT_ONLY",
                "maximum_draft_save_count": 1
            },
            "quality": {"publish_ready": False, "final_write_count_limit": 0}
        }
        plan_artifact = self.artifact("action-plan.json", action_plan_payload)
        probe_artifact = self.artifact("probe.json")

        def ev(name: str) -> dict[str, str]:
            return self.artifact(name)

        steps = []
        last = {"pre_write": 18, "pre_save": 23, "post_save": 27}[stage]
        for index in range(28):
            passed = index <= last
            steps.append({
                "id": f"P{index:02d}",
                "status": "PASS" if passed else "NOT_STARTED",
                "evidence": [ev(f"step-P{index:02d}.json")] if passed else [],
            })

        doc = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "run_kind": "CREATE_SIMILAR_DRAFT",
            "target_model": model,
            "template_product_id": template_id,
            "mode": "save_draft",
            "unresolved_p0": [],
            "policy": {
                "binding": ev("policy-binding.json"),
                "target_disposition": "APPROVED_INTERNAL_SOURCE",
                "template_disposition": "HISTORICAL_REFERENCE_ONLY",
                "target_publish_draft": True,
                "target_external_write_enabled": True,
                "target_allow_rag": True,
                "template_allowed_as_ui_skeleton": True
            },
            "source_freeze": {"manifest": ev("source.json"), "all_inputs_hashed": True, "fact_conflicts_resolved": True},
            "dynamic_schema": {
                "artifact": ev("schema.xml"),
                "category_id": "12345",
                "captured_at_utc": (self.now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "max_age_minutes": 15,
                "required_field_ids": ["product_title"],
                "current_for_account_and_category": True
            },
            "approvals": {
                "source": ev("approved-source.json"),
                "assets": ev("approved-assets.json"),
                "commercial": ev("approved-commercial.json"),
                "content": ev("approved-content.json"),
                "write": {
                    "artifact": ev("write-approval.json"),
                    "status": "APPROVED",
                    "scope": "SAVE_ONE_DRAFT_VISIBLE_UI_ONLY",
                    "max_save_attempts": 1,
                    "expires_at_utc": (self.now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
                }
            },
            "action_plan": {
                "artifact": plan_artifact,
                "run_id": run_id,
                "target_model": model,
                "template_product_id": template_id,
                "approved_field_ids": ["product_title"],
                "save_draft_operations": 1,
                "final_write_operations": 0
            },
            "probe": {
                "artifact": probe_artifact,
                "verification": ev("probe-verification.json"),
                "status": "PASS_ZERO_WRITE",
                "origin": "https://post.alibaba.com",
                "path": "/product/publish.htm",
                "template_product_id": template_id,
                "request_count": 3,
                "write_request_count": 0,
                "final_write_request_count": 0,
                "login_ok": True,
                "captcha_absent": True,
                "page_fingerprint_sha256": "1" * 64,
                "performed_after_guard_install": True
            },
            "endpoint_guard": {
                "policy": ev("endpoint-policy.json"),
                "unknown_writes_default": "BLOCK",
                "draft_write_mode": "VISIBLE_UI_SINGLE_SAVE_ONLY",
                "final_write_enabled": False,
                "final_write_double_blocked": True,
                "bound_action_plan_sha256": plan_artifact["sha256"],
                "bound_probe_sha256": probe_artifact["sha256"]
            },
            "baseline": {
                "artifact": ev("baseline.json"),
                "counts": {"online": 189, "pending": 2, "draft": 5, "ids_sha256": "2" * 64},
                "target_duplicate_count": 0,
                "captured_after_probe": True
            },
            "execution": {
                "fill_receipt": None,
                "approved_field_ids": [],
                "readback_field_ids": [],
                "template_residue_count": 0,
                "non_target_diff_count": 0,
                "all_required_fields_valid": False,
                "asset_hashes_match": False
            },
            "save": {
                "attempt_count": 0,
                "ui_receipt": None,
                "network_receipt": None,
                "business_success": False,
                "business_error_count": 0,
                "draft_id": None,
                "final_write_request_count": 0
            },
            "post_save": {
                "reopen_receipt": None,
                "after_artifact": None,
                "after_counts": None,
                "persistence_match": False,
                "new_target_draft_count": 0,
                "online_id_set_unchanged": False,
                "pending_id_set_unchanged": False,
                "stopped_before_final_submit": True
            },
            "steps": steps
        }
        if stage in {"pre_save", "post_save"}:
            doc["execution"].update({
                "fill_receipt": ev("fill.json"),
                "approved_field_ids": ["product_title"],
                "readback_field_ids": ["product_title"],
                "all_required_fields_valid": True,
                "asset_hashes_match": True
            })
        if stage == "post_save":
            doc["save"].update({
                "attempt_count": 1,
                "ui_receipt": ev("save-ui.json"),
                "network_receipt": ev("save-network.json"),
                "business_success": True,
                "draft_id": "100000000006"
            })
            doc["post_save"].update({
                "reopen_receipt": ev("reopen.json"),
                "after_artifact": ev("after.json"),
                "after_counts": {"online": 189, "pending": 2, "draft": 6, "ids_sha256": "3" * 64},
                "persistence_match": True,
                "new_target_draft_count": 1,
                "online_id_set_unchanged": True,
                "pending_id_set_unchanged": True
            })
        return doc

    def assert_blocked(self, document: dict, stage: str, fragment: str) -> None:
        result = validator.validate(document, stage, self.now)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any(fragment in error for error in result["errors"]), result["errors"])

    def test_pre_write_remains_blocked_until_semantic_roles_are_certified(self) -> None:
        self.assert_blocked(
            self.make_document("pre_write"),
            "pre_write",
            "implementation readiness is not certified",
        )

    def test_unresolved_p0_blocks(self) -> None:
        doc = self.make_document("pre_write")
        doc["unresolved_p0"] = ["P0-99"]
        self.assert_blocked(doc, "pre_write", "unresolved_p0")

    def test_unusable_template_blocks_even_if_flag_claims_allowed(self) -> None:
        doc = self.make_document("pre_write")
        doc["policy"]["template_disposition"] = "UNUSABLE_WASTE_RECORD"
        self.assert_blocked(doc, "pre_write", "template disposition")

    def test_hash_drift_blocks(self) -> None:
        doc = self.make_document("pre_write")
        Path(doc["source_freeze"]["manifest"]["path"]).write_text("mutated", encoding="utf-8")
        self.assert_blocked(doc, "pre_write", "SHA-256 mismatch")

    def test_stale_schema_blocks(self) -> None:
        doc = self.make_document("pre_write")
        doc["dynamic_schema"]["captured_at_utc"] = (self.now - timedelta(hours=2)).isoformat()
        self.assert_blocked(doc, "pre_write", "dynamic schema is stale")

    def test_wrong_action_plan_target_blocks(self) -> None:
        doc = self.make_document("pre_write")
        plan_path = Path(doc["action_plan"]["artifact"]["path"])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["target"]["model"] = "WRONG"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        new_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        doc["action_plan"]["artifact"]["sha256"] = new_hash
        doc["endpoint_guard"]["bound_action_plan_sha256"] = new_hash
        self.assert_blocked(doc, "pre_write", "target.model mismatch")

    def test_pre_save_requires_exact_field_set(self) -> None:
        doc = self.make_document("pre_save")
        doc["execution"]["readback_field_ids"].append("unapproved")
        self.assert_blocked(doc, "pre_save", "readback-field set")

    def test_pre_save_blocks_template_residue(self) -> None:
        doc = self.make_document("pre_save")
        doc["execution"]["template_residue_count"] = 1
        self.assert_blocked(doc, "pre_save", "template residue")

    def test_post_save_cannot_claim_final_ready_while_implementation_is_uncertified(self) -> None:
        self.assert_blocked(
            self.make_document("post_save"),
            "post_save",
            "implementation readiness is not certified",
        )

    def test_post_save_blocks_http_success_without_business_success(self) -> None:
        doc = self.make_document("post_save")
        doc["save"]["business_success"] = False
        self.assert_blocked(doc, "post_save", "business success")

    def test_post_save_requires_fill_receipt(self) -> None:
        doc = self.make_document("post_save")
        doc["execution"]["fill_receipt"] = None
        self.assert_blocked(doc, "post_save", "filled state requires a fill receipt")

    def test_post_save_rechecks_current_required_field_set(self) -> None:
        doc = self.make_document("post_save")
        doc["dynamic_schema"]["required_field_ids"].append("required_but_missing")
        self.assert_blocked(doc, "post_save", "required fields lack readback")

    def test_post_save_blocks_online_count_change(self) -> None:
        doc = self.make_document("post_save")
        doc["post_save"]["after_counts"]["online"] = 190
        self.assert_blocked(doc, "post_save", "online count changed")

    def test_final_write_never_enabled(self) -> None:
        doc = self.make_document("pre_write")
        doc["endpoint_guard"]["final_write_enabled"] = True
        self.assert_blocked(doc, "pre_write", "final_write_enabled")

    def test_pass_step_cannot_have_empty_evidence(self) -> None:
        doc = self.make_document("pre_write")
        doc["steps"][3]["evidence"] = []
        self.assert_blocked(doc, "pre_write", "should be non-empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
