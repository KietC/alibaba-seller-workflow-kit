from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import draft_guard  # noqa: E402
from draft_core import canonical_json, sha256_bytes, sha256_file  # noqa: E402


POLICY_PATH = SKILL_DIR / "references" / "endpoint-policy.v1.json"


def object_sha256(value: object) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_valid_probe() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    run_at = now - timedelta(minutes=10)
    plan_at = now - timedelta(minutes=8)
    started_at = now - timedelta(minutes=3)
    interception_at = now - timedelta(minutes=2, seconds=55)
    navigation_at = now - timedelta(minutes=2, seconds=50)
    captured_at = now - timedelta(minutes=2)
    capture_completed_at = now - timedelta(minutes=1, seconds=55)
    completed_at = now - timedelta(minutes=1, seconds=50)

    target_model = "MODEL-003"
    template_product_id = "100000000014"
    run_id = "MODEL-016"
    current_url = (
        "https://post.alibaba.com/product/publish.htm?"
        "pubType=similarPost&behavior=copyNew&itemId=100000000014"
    )
    policy_sha256 = object_sha256(policy)
    run_spec = {
        "schema_version": "sellerkit.similar_draft_run_spec.v1",
        "run_kind": "CREATE_SIMILAR_DRAFT",
        "run_id": run_id,
        "generated_at": timestamp(run_at),
        "target_model": target_model,
        "template_product_id": template_product_id,
        "endpoint_policy": {"sha256": policy_sha256},
    }
    run_spec_sha256 = object_sha256(run_spec)
    adapter = {
        "schema_version": "alibaba.iab-logical-adapter.v1",
        "adapter_id": "codex-in-app-browser-logical-similar-draft",
        "adapter_version": "1.0.0",
        "runtime_resolution": {
            "current_probe_required": True,
            "probe_origin": "https://post.alibaba.com",
            "probe_path": "/product/publish.htm",
        },
    }
    adapter_sha256 = object_sha256(adapter)
    action_plan = {
        "schema_version": "alibaba.browser-action-plan.v1",
        "plan_id": "MODEL-013",
        "generated_at": timestamp(plan_at),
        "target": {
            "model": target_model,
            "run_id": run_id,
            "template_product_id": template_product_id,
        },
        "bindings": {
            "three_party": {"run_spec_sha256": run_spec_sha256},
            "endpoint_policy": {"sha256": policy_sha256},
            "adapter": {"sha256": adapter_sha256},
        },
        "runtime_requirements": {
            "current_probe_required": True,
            "current_probe_must_be_newer_than_plan": True,
            "fail_on_unknown_write_request": True,
        },
    }
    action_plan_sha256 = object_sha256(action_plan)
    aria_sha256 = object_sha256({"aria": "current visible form"})
    dom_sha256 = object_sha256({"dom": "current visible form"})
    bundle_hashes = [object_sha256({"bundle": "publish-v1"})]
    title = "Alibaba.com Product Publishing"
    fingerprint_material = {
        "page_kind": "icbu_product_publish_similar",
        "title_sha256": object_sha256(title),
        "aria_snapshot_sha256": aria_sha256,
        "dom_snapshot_sha256": dom_sha256,
        "bundle_hashes": bundle_hashes,
    }
    probe = {
        "schema_version": "alibaba.probe-receipt.v2",
        "receipt_id": "probe-receipt-fixture",
        "run_id": run_id,
        "target_model": target_model,
        "template_product_id": template_product_id,
        "current_url": current_url,
        "started_at": timestamp(started_at),
        "captured_at": timestamp(captured_at),
        "completed_at": timestamp(completed_at),
        "mode": "READ_ONLY_PROBE",
        "result": "READ_ONLY_VERIFIED",
        "route": {
            "origin": "https://post.alibaba.com",
            "path": "/product/publish.htm",
            "query": {
                "pubType": "similarPost",
                "behavior": "copyNew",
                "itemId": template_product_id,
            },
            "origin_match": True,
            "path_match": True,
            "query_match": True,
            "redirect_count": 0,
            "redirect_origin_changed": False,
        },
        "page": {
            "title": title,
            "page_kind": "icbu_product_publish_similar",
            "page_kind_match": True,
            "login_active": True,
            "login_challenge_present": False,
            "captcha_present": False,
            "fingerprint_sha256": object_sha256(fingerprint_material),
            "fingerprint_material": fingerprint_material,
            "fingerprint_accepted": True,
            "raw_probe_sha256": object_sha256({"raw": "probe"}),
            "redacted_probe_sha256": object_sha256({"redacted": "probe"}),
            "aria_snapshot_sha256": aria_sha256,
            "dom_snapshot_sha256": dom_sha256,
            "screenshot_sha256": object_sha256({"screenshot": "probe"}),
            "bundle_hashes": bundle_hashes,
            "credential_values_recorded": False,
        },
        "bindings": {
            "run_spec_sha256": run_spec_sha256,
            "action_plan_sha256": action_plan_sha256,
            "action_plan_id": action_plan["plan_id"],
            "action_plan_generated_at": action_plan["generated_at"],
            "field_registry_sha256": object_sha256({"registry": "v1"}),
            "field_registry_version": "1.0.0",
            "schema_bundle_sha256": object_sha256({"schema": "v1"}),
            "probe_tool_sha256": object_sha256({"tool": "v2"}),
            "adapter_sha256": adapter_sha256,
            "adapter_id": adapter["adapter_id"],
            "adapter_version": adapter["adapter_version"],
            "endpoint_policy_sha256": policy_sha256,
        },
        "locator_results": [
            {
                "logical_name": "page.current_probe",
                "registry_entry_sha256": object_sha256({"locator": "page.current_probe"}),
                "critical": True,
                "status": "UNIQUE_VISIBLE_MATCH",
                "match_count": 1,
                "visible_match_count": 1,
                "runtime_locator_sha256": object_sha256({"runtime": "current-page"}),
                "inherited_value_present": True,
            }
        ],
        "dynamic_bindings": [],
        "network": {
            "interception_mode": "ACTIVE_FAIL_CLOSED",
            "interception_active": True,
            "interception_session_sha256": object_sha256({"session": "fixture"}),
            "interception_started_at": timestamp(interception_at),
            "navigation_started_at": timestamp(navigation_at),
            "capture_completed_at": timestamp(capture_completed_at),
            "network_guard_installed_before_navigation": True,
            "requests_complete": True,
            "allowed_methods": ["GET", "HEAD", "OPTIONS"],
            "observed_methods": ["GET"],
            "read_request_count": 1,
            "read_requests": [
                {
                    "method": "GET",
                    "url": current_url,
                    "response_status": 200,
                    "captured_at": timestamp(captured_at),
                }
            ],
            "write_requests_observed": 0,
            "write_requests_allowed": 0,
            "all_observed_writes_blocked": True,
            "unknown_write_count": 0,
            "final_write_count": 0,
            "blocked_write_attempts": [],
            "request_bodies_recorded": False,
            "credential_values_recorded": False,
        },
        "counts": {
            "online_before": 189,
            "online_after": 189,
            "draft_before": 4,
            "draft_after": 4,
        },
        "errors": [],
        "safe_to_continue_read_only": True,
        "safe_to_write": False,
        "external_write_enabled": False,
        "final_write_count": 0,
        "submit_triggered": False,
    }
    context = {
        "run_spec": run_spec,
        "action_plan": action_plan,
        "adapter": adapter,
        "loaded_policy": policy,
        "loaded_policy_sha256": policy_sha256,
        "hashes": {
            "run_spec_sha256": run_spec_sha256,
            "action_plan_sha256": action_plan_sha256,
            "endpoint_policy_sha256": policy_sha256,
            "adapter_sha256": adapter_sha256,
        },
    }
    return probe, policy, context


def authorize_fixture_read_requests(
    probe: dict[str, object],
    policy: dict[str, object],
    context: dict[str, object],
) -> None:
    """Activate only the synthetic exact GET rule and rebind every dependent hash."""

    rules = policy["rules"]
    assert isinstance(rules, list)
    rule = next(row for row in rules if row["id"] == "observed-page-init-route-only")
    rule["methods"] = ["GET"]
    rule["method_evidence"] = "SYNTHETIC_TEST_ONLY"
    rule["evidence_state"] = "LIVE_VERIFIED_ACTIVE"
    rule["evidence_id_required"] = False
    policy_sha256 = object_sha256(policy)

    run_spec = context["run_spec"]
    action_plan = context["action_plan"]
    hashes = context["hashes"]
    assert isinstance(run_spec, dict)
    assert isinstance(action_plan, dict)
    assert isinstance(hashes, dict)
    run_spec["endpoint_policy"] = {"sha256": policy_sha256}
    run_spec_sha256 = object_sha256(run_spec)
    plan_bindings = action_plan["bindings"]
    assert isinstance(plan_bindings, dict)
    plan_bindings["three_party"] = {"run_spec_sha256": run_spec_sha256}
    plan_bindings["endpoint_policy"] = {"sha256": policy_sha256}
    action_plan_sha256 = object_sha256(action_plan)

    probe_bindings = probe["bindings"]
    assert isinstance(probe_bindings, dict)
    probe_bindings["run_spec_sha256"] = run_spec_sha256
    probe_bindings["action_plan_sha256"] = action_plan_sha256
    probe_bindings["endpoint_policy_sha256"] = policy_sha256
    context["loaded_policy"] = policy
    context["loaded_policy_sha256"] = policy_sha256
    hashes["run_spec_sha256"] = run_spec_sha256
    hashes["action_plan_sha256"] = action_plan_sha256
    hashes["endpoint_policy_sha256"] = policy_sha256


def build_read_activation_request(
    probe: dict[str, object],
    context: dict[str, object],
) -> dict[str, object]:
    network = probe["network"]
    page = probe["page"]
    hashes = context["hashes"]
    assert isinstance(network, dict)
    assert isinstance(page, dict)
    assert isinstance(hashes, dict)
    requests = network["read_requests"]
    assert isinstance(requests, list)
    completed = datetime.fromisoformat(str(probe["completed_at"]).replace("Z", "+00:00"))
    generated = completed + timedelta(seconds=1)
    hashes["probe_source_sha256"] = object_sha256(probe)
    request: dict[str, object] = {
        "schema_version": "sellerkit.alibaba.probe-read-activation-request.v1",
        "generated_at": timestamp(generated),
        "expires_at": timestamp(generated + timedelta(minutes=10)),
        "status": "PENDING_CURRENT_SESSION_EXACT_READ_ACTIVATION",
        "authority": "EXACT_CAPTURED_READS_ONLY_NOT_GLOBAL_POLICY",
        "scope": {
            "run_id": probe["run_id"],
            "target_model": probe["target_model"],
            "template_product_id": probe["template_product_id"],
            "browser_session_sha256": network["interception_session_sha256"],
            "page_fingerprint_sha256": page["fingerprint_sha256"],
        },
        "bindings": {
            "global_endpoint_policy_sha256": hashes["endpoint_policy_sha256"],
            "run_spec_sha256": hashes["run_spec_sha256"],
            "action_plan_sha256": hashes["action_plan_sha256"],
            "adapter_sha256": hashes["adapter_sha256"],
            "probe_source_sha256": hashes["probe_source_sha256"],
        },
        "request_activations": [
            {
                "method": row["method"],
                "url": row["url"],
                "source_request_sha256": object_sha256(row),
                "classification": "READ",
                "decision": "ALLOW",
            }
            for row in requests
            if isinstance(row, dict)
        ],
        "global_policy_unchanged": True,
        "draft_write_enabled": False,
        "final_write_enabled": False,
        "errors": [],
    }
    request["body_sha256"] = object_sha256(request)
    context["probe_read_activation"] = request
    hashes["probe_read_activation_request_sha256"] = object_sha256(request)
    return request


class StrictProbeGateTests(unittest.TestCase):
    def assert_blocked(
        self,
        probe: dict[str, object],
        policy: dict[str, object],
        context: dict[str, object] | None,
        needle: str | None = None,
    ) -> None:
        result = draft_guard.strict_verify_probe(probe, policy, binding_context=context)
        self.assertEqual(result["status"], "BLOCKED_PROBE", result)
        if needle:
            self.assertTrue(any(needle in error for error in result["errors"]), result)

    def test_audit_only_policy_confirms_zero_write_but_does_not_authorize_execution(self) -> None:
        probe, policy, context = build_valid_probe()
        result = draft_guard.strict_verify_probe(probe, policy, binding_context=context)
        self.assertEqual(result["status"], "PASS_ZERO_WRITE", result)
        self.assertEqual(result["errors"], [])
        self.assertIs(result["zero_write_confirmed"], True)
        self.assertIs(result["execution_authorized"], False)
        self.assertTrue(any("is not ALLOW" in error for error in result["authorization_errors"]), result)
        self.assertTrue(all(row["decision"] != "ALLOW" for row in result["request_classifications"]), result)

    def test_all_exact_allow_decisions_authorize_controlled_execution(self) -> None:
        probe, policy, context = build_valid_probe()
        authorize_fixture_read_requests(probe, policy, context)
        result = draft_guard.strict_verify_probe(probe, policy, binding_context=context)
        self.assertEqual(result["status"], "PASS_ZERO_WRITE", result)
        self.assertIs(result["zero_write_confirmed"], True)
        self.assertIs(result["execution_authorized"], True)
        self.assertEqual(result["authorization_errors"], [])
        self.assertTrue(all(row["decision"] == "ALLOW" for row in result["request_classifications"]), result)

    def test_exact_read_overlay_authorizes_without_mutating_global_policy(self) -> None:
        probe, policy, context = build_valid_probe()
        original_policy = copy.deepcopy(policy)
        build_read_activation_request(probe, context)
        result = draft_guard.strict_verify_probe(probe, policy, binding_context=context)
        self.assertEqual(result["status"], "PASS_ZERO_WRITE", result)
        self.assertIs(result["execution_authorized"], True, result)
        self.assertEqual(result["authorization_errors"], [], result)
        self.assertTrue(all(row["decision"] == "ALLOW" for row in result["request_classifications"]), result)
        self.assertIs(result["adapter_production_execution_ready"], False, result)
        self.assertIs(result["production_execution_authorized"], False, result)
        self.assertEqual(policy, original_policy, "session READ overlay mutated the global policy")

    def test_caller_forged_adapter_readiness_cannot_authorize_production(self) -> None:
        probe, policy, context = build_valid_probe()
        request = build_read_activation_request(probe, context)
        adapter = context["adapter"]
        hashes = context["hashes"]
        action_plan = context["action_plan"]
        assert isinstance(adapter, dict)
        assert isinstance(hashes, dict)
        assert isinstance(action_plan, dict)
        adapter["readiness"] = {"production_execution_ready": True}
        forged_adapter_hash = object_sha256(adapter)
        hashes["adapter_sha256"] = forged_adapter_hash
        plan_bindings = action_plan["bindings"]
        assert isinstance(plan_bindings, dict)
        plan_bindings["adapter"] = {"sha256": forged_adapter_hash}
        forged_plan_hash = object_sha256(action_plan)
        hashes["action_plan_sha256"] = forged_plan_hash
        probe_bindings = probe["bindings"]
        assert isinstance(probe_bindings, dict)
        probe_bindings["adapter_sha256"] = forged_adapter_hash
        probe_bindings["action_plan_sha256"] = forged_plan_hash
        request_bindings = request["bindings"]
        assert isinstance(request_bindings, dict)
        request_bindings["adapter_sha256"] = forged_adapter_hash
        request_bindings["action_plan_sha256"] = forged_plan_hash
        request["body_sha256"] = object_sha256(
            {key: value for key, value in request.items() if key != "body_sha256"}
        )
        hashes["probe_read_activation_request_sha256"] = object_sha256(request)

        result = draft_guard.strict_verify_probe(probe, policy, binding_context=context)
        self.assertEqual(result["status"], "PASS_ZERO_WRITE", result)
        self.assertIs(result["execution_authorized"], True, result)
        self.assertIs(result["adapter_production_execution_ready"], False, result)
        self.assertIs(result["production_execution_authorized"], False, result)
        self.assertTrue(
            any("production execution readiness" in error for error in result["production_authorization_errors"]),
            result,
        )

    def test_read_overlay_cannot_authorize_draft_or_final_routes(self) -> None:
        for forbidden_url, needle in (
            ("https://post.alibaba.com/product/submitDraft.json", "draft-write route"),
            ("https://post.alibaba.com/product/submit.htm", "final-write route"),
        ):
            with self.subTest(forbidden_url=forbidden_url):
                probe, policy, context = build_valid_probe()
                network = probe["network"]
                assert isinstance(network, dict)
                requests = network["read_requests"]
                assert isinstance(requests, list)
                requests.append({
                    "method": "GET",
                    "url": forbidden_url,
                    "response_status": 200,
                    "captured_at": probe["captured_at"],
                })
                network["read_request_count"] = 2
                build_read_activation_request(probe, context)
                result = draft_guard.strict_verify_probe(probe, policy, binding_context=context)
                self.assertEqual(result["status"], "BLOCKED_PROBE", result)
                self.assertTrue(any(needle in error for error in result["errors"]), result)

    def test_read_overlay_is_run_and_session_scoped(self) -> None:
        for field, replacement, needle in (
            ("run_id", "other-run", "run_id mismatch"),
            ("browser_session_sha256", "f" * 64, "browser session mismatch"),
        ):
            with self.subTest(field=field):
                probe, policy, context = build_valid_probe()
                request = build_read_activation_request(probe, context)
                scope = request["scope"]
                assert isinstance(scope, dict)
                scope[field] = replacement
                request["body_sha256"] = object_sha256({k: v for k, v in request.items() if k != "body_sha256"})
                hashes = context["hashes"]
                assert isinstance(hashes, dict)
                hashes["probe_read_activation_request_sha256"] = object_sha256(request)
                result = draft_guard.strict_verify_probe(probe, policy, binding_context=context)
                self.assertEqual(result["status"], "BLOCKED_PROBE", result)
                self.assertTrue(any(needle in error for error in result["errors"]), result)

    def test_cli_binds_actual_run_plan_policy_and_adapter_files(self) -> None:
        probe, policy, context = build_valid_probe()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_spec_path = root / "run_spec.json"
            adapter_path = root / "adapter.json"
            action_plan_path = root / "browser_action_plan.json"
            probe_path = root / "probe.json"
            output_path = root / "verification.json"

            run_spec = copy.deepcopy(context["run_spec"])
            adapter = copy.deepcopy(context["adapter"])
            action_plan = copy.deepcopy(context["action_plan"])
            assert isinstance(run_spec, dict)
            assert isinstance(adapter, dict)
            assert isinstance(action_plan, dict)
            policy_sha256 = sha256_file(POLICY_PATH)
            run_spec["endpoint_policy"] = {"sha256": policy_sha256}
            run_spec_path.write_text(
                json.dumps(run_spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            adapter_path.write_text(
                json.dumps(adapter, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_spec_sha256 = sha256_file(run_spec_path)
            adapter_sha256 = sha256_file(adapter_path)
            plan_bindings = action_plan["bindings"]
            assert isinstance(plan_bindings, dict)
            plan_bindings["three_party"] = {"run_spec_sha256": run_spec_sha256}
            plan_bindings["endpoint_policy"] = {"sha256": policy_sha256}
            plan_bindings["adapter"] = {"sha256": adapter_sha256}
            action_plan_path.write_text(
                json.dumps(action_plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            action_plan_sha256 = sha256_file(action_plan_path)
            bindings = probe["bindings"]
            assert isinstance(bindings, dict)
            bindings["run_spec_sha256"] = run_spec_sha256
            bindings["action_plan_sha256"] = action_plan_sha256
            bindings["endpoint_policy_sha256"] = policy_sha256
            bindings["adapter_sha256"] = adapter_sha256
            probe_path.write_text(
                json.dumps(probe, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = draft_guard.main(
                    [
                        "verify-probe",
                        "--probe",
                        str(probe_path),
                        "--policy",
                        str(POLICY_PATH),
                        "--run-spec",
                        str(run_spec_path),
                        "--action-plan",
                        str(action_plan_path),
                        "--adapter",
                        str(adapter_path),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 4)
            receipt = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "PASS_ZERO_WRITE", receipt)
            self.assertIs(receipt["zero_write_confirmed"], True)
            self.assertIs(receipt["execution_authorized"], False)

    def test_cli_exact_read_overlay_is_reachable_but_bundled_adapter_blocks_production(self) -> None:
        probe, _, context = build_valid_probe()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_spec_path = root / "run_spec.json"
            action_plan_path = root / "browser_action_plan.json"
            probe_path = root / "probe.json"
            activation_request_path = root / "probe-read-activation-request.json"
            verification_path = root / "probe-verification.json"
            activation_receipt_path = root / "probe-read-activation-receipt.json"
            adapter_path = SKILL_DIR / "references" / "adapters" / "iab-v1.json"

            run_spec = copy.deepcopy(context["run_spec"])
            action_plan = copy.deepcopy(context["action_plan"])
            adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
            assert isinstance(run_spec, dict)
            assert isinstance(action_plan, dict)
            policy_sha256 = sha256_file(POLICY_PATH)
            adapter_sha256 = sha256_file(adapter_path)
            run_spec["endpoint_policy"] = {"sha256": policy_sha256}
            run_spec_path.write_text(
                json.dumps(run_spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_spec_sha256 = sha256_file(run_spec_path)
            plan_bindings = action_plan["bindings"]
            assert isinstance(plan_bindings, dict)
            plan_bindings["three_party"] = {"run_spec_sha256": run_spec_sha256}
            plan_bindings["endpoint_policy"] = {"sha256": policy_sha256}
            plan_bindings["adapter"] = {"sha256": adapter_sha256}
            action_plan_path.write_text(
                json.dumps(action_plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            action_plan_sha256 = sha256_file(action_plan_path)
            probe_bindings = probe["bindings"]
            assert isinstance(probe_bindings, dict)
            probe_bindings["run_spec_sha256"] = run_spec_sha256
            probe_bindings["action_plan_sha256"] = action_plan_sha256
            probe_bindings["endpoint_policy_sha256"] = policy_sha256
            probe_bindings["adapter_sha256"] = adapter_sha256
            probe_path.write_text(
                json.dumps(probe, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            overlay_context: dict[str, object] = {
                "run_spec": run_spec,
                "action_plan": action_plan,
                "adapter": adapter,
                "hashes": {
                    "run_spec_sha256": run_spec_sha256,
                    "action_plan_sha256": action_plan_sha256,
                    "endpoint_policy_sha256": policy_sha256,
                    "adapter_sha256": adapter_sha256,
                    "probe_source_sha256": sha256_file(probe_path),
                },
            }
            activation_request = build_read_activation_request(probe, overlay_context)
            activation_bindings = activation_request["bindings"]
            overlay_hashes = overlay_context["hashes"]
            assert isinstance(activation_bindings, dict)
            assert isinstance(overlay_hashes, dict)
            activation_bindings["probe_source_sha256"] = sha256_file(probe_path)
            overlay_hashes["probe_source_sha256"] = sha256_file(probe_path)
            activation_request["body_sha256"] = object_sha256(
                {k: v for k, v in activation_request.items() if k != "body_sha256"}
            )
            activation_request_path.write_text(
                json.dumps(activation_request, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = draft_guard.main(
                    [
                        "verify-probe",
                        "--probe", str(probe_path),
                        "--policy", str(POLICY_PATH),
                        "--run-spec", str(run_spec_path),
                        "--action-plan", str(action_plan_path),
                        "--adapter", str(adapter_path),
                        "--probe-read-activation", str(activation_request_path),
                        "--probe-read-activation-output", str(activation_receipt_path),
                        "--output", str(verification_path),
                    ]
                )
            self.assertEqual(exit_code, 4)
            receipt = json.loads(verification_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "PASS_ZERO_WRITE", receipt)
            self.assertIs(receipt["execution_authorized"], True, receipt)
            self.assertIs(receipt["adapter_production_execution_ready"], False, receipt)
            self.assertIs(receipt["production_execution_authorized"], False, receipt)
            self.assertTrue(activation_receipt_path.is_file())
            activation_receipt = json.loads(activation_receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                activation_receipt["bindings"]["probe_verification_sha256"],
                sha256_file(verification_path),
            )
            global_policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            self.assertEqual(global_policy["default_decision"], "DENY")
            self.assertFalse(any(row.get("decision") == "ALLOW" for row in global_policy["rules"]))
            self.assertFalse(global_policy["final_write_enabled"])

    def test_cli_policy_loader_rejects_a_globally_activated_read_rule(self) -> None:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        rule = next(row for row in policy["rules"] if row["id"] == "observed-page-init-route-only")
        rule["methods"] = ["GET"]
        rule["evidence_state"] = "LIVE_VERIFIED_ACTIVE"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "globally-active-policy.json"
            path.write_text(
                json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(draft_guard.GuardError, "zero active ALLOW rules"):
                draft_guard._load_policy(path)

    def test_wrong_publish_path_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        probe["current_url"] = str(probe["current_url"]).replace(
            "/product/publish.htm?", "/product/publish.htm/forged?"
        )
        self.assert_blocked(probe, policy, context, "exact https://post.alibaba.com/product/publish.htm")

    def test_empty_read_requests_are_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        network = probe["network"]
        assert isinstance(network, dict)
        network["read_requests"] = []
        network["read_request_count"] = 0
        network["observed_methods"] = []
        self.assert_blocked(probe, policy, context, "non-empty network read evidence")

    def test_random_sha256_fingerprint_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        page = probe["page"]
        assert isinstance(page, dict)
        page["fingerprint_sha256"] = "f" * 64
        self.assert_blocked(probe, policy, context, "does not hash")

    def test_login_inactive_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        page = probe["page"]
        assert isinstance(page, dict)
        page["login_active"] = False
        self.assert_blocked(probe, policy, context, "login is inactive")

    def test_inactive_interception_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        network = probe["network"]
        assert isinstance(network, dict)
        network["interception_active"] = False
        self.assert_blocked(probe, policy, context, "active fail-closed interception")

    def test_interception_after_navigation_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        network = probe["network"]
        assert isinstance(network, dict)
        network["interception_started_at"] = probe["captured_at"]
        self.assert_blocked(probe, policy, context, "probe timing")

    def test_each_external_binding_hash_mismatch_is_blocked(self) -> None:
        for key in (
            "run_spec_sha256",
            "action_plan_sha256",
            "endpoint_policy_sha256",
            "adapter_sha256",
        ):
            with self.subTest(key=key):
                probe, policy, context = build_valid_probe()
                bindings = probe["bindings"]
                assert isinstance(bindings, dict)
                bindings[key] = "0" * 64
                self.assert_blocked(probe, policy, context, "actual bound file")

    def test_missing_actual_binding_context_is_blocked(self) -> None:
        probe, policy, _ = build_valid_probe()
        self.assert_blocked(probe, policy, None, "binding context is required")

    def test_probe_older_than_plan_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        action_plan = context["action_plan"]
        assert isinstance(action_plan, dict)
        network = probe["network"]
        assert isinstance(network, dict)
        action_plan["generated_at"] = network["navigation_started_at"]
        bindings = probe["bindings"]
        assert isinstance(bindings, dict)
        bindings["action_plan_generated_at"] = action_plan["generated_at"]
        action_plan_sha256 = object_sha256(action_plan)
        bindings["action_plan_sha256"] = action_plan_sha256
        hashes = context["hashes"]
        assert isinstance(hashes, dict)
        hashes["action_plan_sha256"] = action_plan_sha256
        self.assert_blocked(probe, policy, context, "newer than the action plan")

    def test_stale_probe_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        probe["completed_at"] = timestamp(old)
        self.assert_blocked(probe, policy, context, "older than 15 minutes")

    def test_read_evidence_for_another_url_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        network = probe["network"]
        assert isinstance(network, dict)
        requests = network["read_requests"]
        assert isinstance(requests, list) and isinstance(requests[0], dict)
        requests[0]["url"] = "https://post.alibaba.com/product/asyRender.json"
        self.assert_blocked(probe, policy, context, "exact current publish URL")

    def test_count_change_is_blocked(self) -> None:
        probe, policy, context = build_valid_probe()
        counts = probe["counts"]
        assert isinstance(counts, dict)
        counts["draft_after"] = 5
        self.assert_blocked(probe, policy, context, "draft count changed")

    def test_unknown_extra_input_is_blocked_by_contract(self) -> None:
        probe, policy, context = build_valid_probe()
        probe["forged_pass"] = True
        self.assert_blocked(probe, policy, context, "schema probe-receipt.schema.json")

    def test_legacy_flat_probe_cannot_forge_pass(self) -> None:
        probe = {
            "target_model": "MODEL-003",
            "template_product_id": "100000000014",
            "current_url": (
                "https://post.alibaba.com/product/publish.htm?"
                "pubType=similarPost&behavior=copyNew&itemId=100000000014"
            ),
            "captcha_detected": False,
            "network_guard_installed_before_navigation": True,
            "cookie_or_storage_read": False,
            "page_fingerprint": "fixture-page-fingerprint",
            "online_count_before": 189,
            "online_count_after": 189,
            "draft_count_before": 4,
            "draft_count_after": 4,
            "requests": [],
        }
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        self.assert_blocked(probe, policy, None, "strict probe schema_version")


if __name__ == "__main__":
    unittest.main()
