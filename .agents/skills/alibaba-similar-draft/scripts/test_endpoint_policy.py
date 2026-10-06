from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
POLICY_PATH = SKILL_DIR / "references" / "endpoint-policy.v1.json"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from draft_core import classify_endpoint  # noqa: E402


# These remaining controls are not fully expressible through classify_endpoint.
# The nearest enforceable deny is tested below; draft_core still needs explicit
# evidence for complete checks.
CORE_INTERFACE_GAPS = (
    "explicit confirmation-checkbox state",
    "redirect response status and target evidence beyond a boolean",
)


class EndpointPolicyNegativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def classify(self, url: str, method: str, **kwargs: object) -> dict[str, object]:
        return classify_endpoint(self.policy, url, method, **kwargs)

    def assert_denied(self, result: dict[str, object]) -> None:
        self.assertIn(result["decision"], {"DENY", "HARD_DENY"}, result)

    def test_n01_init_routes_are_candidates_with_unknown_method(self) -> None:
        self.assertEqual(self.policy["default_decision"], "DENY")
        self.assertFalse(self.policy["final_write_enabled"])
        self.assertEqual(self.policy["http_method_evidence"], "UNKNOWN")
        self.assertFalse(self.policy["bundle_body_frozen"])
        self.assertFalse(
            any(rule.get("evidence_state") == "LIVE_VERIFIED_ACTIVE" for rule in self.policy["rules"])
        )
        for url in (
            "https://post.alibaba.com/product/publish.htm?pubType=similarPost&itemId=1&behavior=copyNew",
            "https://post.alibaba.com/product/asyRender.json?pubType=similarPost&itemId=1",
        ):
            result = self.classify(url, "GET", evidence_id="fixture-evidence")
            self.assertEqual(result["classification"], "DENY_METHOD_UNVERIFIED")
            self.assertEqual(result["decision"], "DENY")
            self.assertEqual(result["policy_classification"], "READ")
            self.assertEqual(result["reason"], "route is frozen but HTTP method is unverified")

    def test_n02_observed_validation_route_remains_denied_for_post(self) -> None:
        result = self.classify(
            "https://post.alibaba.com/product/asyncOpt.htm?optType=titleCheckAsyncOpt",
            "POST",
            evidence_id="fixture-evidence",
        )
        self.assertEqual(result["classification"], "DENY_METHOD_UNVERIFIED")
        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(result["policy_classification"], "READ")
        self.assertEqual(result["rule_id"], "observed-async-validation-route-only")

    def test_n03_unknown_or_conflicting_validation_option_is_denied(self) -> None:
        for query in (
            "optType=unknownOperation",
            "optType=titleCheckAsyncOpt&optType=unknownOperation",
        ):
            result = self.classify(
                f"https://post.alibaba.com/product/asyncOpt.htm?{query}",
                "GET",
                evidence_id="fixture-evidence",
            )
            self.assertEqual(result["classification"], "DENY_QUERY")
            self.assertEqual(result["decision"], "DENY")

    def test_n04_guessed_remote_preview_route_is_denied(self) -> None:
        result = self.classify("https://post.alibaba.com/product/preview.json", "GET")
        self.assertEqual(result["classification"], "READ_UNVERIFIED")
        self.assertEqual(result["decision"], "DENY")
        self.assertIsNone(result["rule_id"])

    def test_n05_unverified_draft_save_seed_is_never_allowed(self) -> None:
        for method in ("GET", "POST", "UNKNOWN"):
            result = self.classify(
                "https://post.alibaba.com/product/submitDraft.json",
                method,
                evidence_id="fixture-evidence",
            )
            self.assertEqual(result["classification"], "DENY_METHOD_UNVERIFIED")
            self.assertEqual(result["decision"], "DENY")
            self.assertEqual(result["policy_classification"], "REVERSIBLE_DRAFT_WRITE")
            self.assertEqual(result["reason"], "route is frozen but HTTP method is unverified")

    def test_n06_final_submit_and_publish_mutation_are_hard_denied(self) -> None:
        cases = (
            ("https://post.alibaba.com/product/submit.htm", "POST"),
            ("https://post.alibaba.com/product/submitFinal.json", "GET"),
            ("https://post.alibaba.com/product/publish.htm", "POST"),
        )
        for url, method in cases:
            result = self.classify(url, method)
            self.assertEqual(result["classification"], "FINAL_WRITE")
            self.assertEqual(result["decision"], "HARD_DENY")

    def test_n07_scheme_port_and_host_confusion_are_denied(self) -> None:
        for url in (
            "http://post.alibaba.com/product/asyRender.json",
            "https://post.alibaba.com:444/product/asyRender.json",
            "https://post.alibaba.com.evil.test/product/asyRender.json",
        ):
            result = self.classify(url, "GET")
            self.assertEqual(result["classification"], "DENY_ORIGIN")
            self.assertEqual(result["decision"], "DENY")

    def test_n08_ambiguous_or_encoded_paths_are_denied(self) -> None:
        for url in (
            "https://post.alibaba.com/product/../submit.htm",
            "https://post.alibaba.com/product//submit.htm",
            "https://post.alibaba.com/product%2Fsubmit.htm",
            "https://post.alibaba.com/product/%252e%252e/submit.htm",
        ):
            result = self.classify(url, "GET")
            self.assertEqual(result["classification"], "DENY_PATH_NORMALIZATION")
            self.assertEqual(result["decision"], "DENY")

    def test_n09_read_body_and_method_override_are_denied(self) -> None:
        with_body = self.classify(
            "https://post.alibaba.com/product/asyRender.json",
            "GET",
            body_present=True,
        )
        self.assertEqual(with_body["classification"], "DENY_METHOD_SHAPE")
        override = self.classify(
            "https://post.alibaba.com/product/asyRender.json?_method=POST",
            "GET",
        )
        self.assertEqual(override["classification"], "DENY_METHOD_SHAPE")

    def test_n10_token_bearing_capture_url_is_not_authorized(self) -> None:
        token_query = self.classify(
            "https://post.alibaba.com/product/asyRender.json?X-XSRF-TOKEN=TEST_PLACEHOLDER_ONLY",
            "GET",
            evidence_id="fixture-evidence",
        )
        self.assertEqual(token_query["classification"], "DENY_QUERY")
        self.assertNotIn("url", token_query)
        serialized = json.dumps(token_query, ensure_ascii=False)
        self.assertNotIn("TEST_PLACEHOLDER_ONLY", serialized)
        self.assertEqual(
            len(token_query["url_shape"]["query"]["sensitive_value_sha256"]["X-XSRF-TOKEN"][0]),
            64,
        )
        cookie_header = self.classify(
            "https://post.alibaba.com/product/asyRender.json",
            "GET",
            headers={"Cookie": "TEST_PLACEHOLDER_ONLY"},
        )
        self.assertEqual(cookie_header["classification"], "DENY_CREDENTIAL_SOURCE")
        storage = self.classify(
            "https://post.alibaba.com/product/asyRender.json",
            "GET",
            storage_accessed=True,
        )
        self.assertEqual(storage["classification"], "DENY_CREDENTIAL_SOURCE")
        self.assertEqual(self.policy["credential_source_policy"], "DENY_COOKIE_STORAGE_AND_CAPTURED_TOKEN_REUSE")

    def test_n11_redirect_target_outside_exact_origin_is_denied(self) -> None:
        result = self.classify(
            "https://post.alibaba.com/product/asyRender.json",
            "GET",
            redirected=True,
        )
        self.assertEqual(result["classification"], "DENY_REDIRECT")
        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(self.policy["redirect_policy"], "DENY")

    def test_n12_bundle_url_without_frozen_body_or_hash_is_denied(self) -> None:
        result = self.classify(
            "https://s.alicdn.com/@g/posting/main-cgs/7.1.56/publish-splitpacking.js?enable_br=true",
            "GET",
            evidence_id="fixture-evidence",
        )
        self.assertEqual(result["classification"], "DENY_METHOD_UNVERIFIED")
        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(result["policy_classification"], "READ")
        self.assertEqual(result["reason"], "route is frozen but HTTP method is unverified")

    def test_n13_declared_draft_cannot_downgrade_submit_endpoint(self) -> None:
        result = self.classify(
            "https://post.alibaba.com/product/submit.htm",
            "POST",
            action="draft",
            dom_intent="stepSubmit",
        )
        self.assertEqual(result["classification"], "DENY_ACTION_MISMATCH")
        self.assertEqual(result["decision"], "DENY")

    def test_n14_claimed_confirmation_cannot_enable_final_write(self) -> None:
        blocked = self.classify(
            "https://post.alibaba.com/product/submitFinal.json",
            "POST",
            blockers=["confirmation-checkbox-unchecked"],
        )
        self.assertEqual(blocked["classification"], "DENY_GATE")
        final_intent = self.classify(
            "https://post.alibaba.com/product/submitFinal.json",
            "POST",
            dom_intent="stepSubmit",
        )
        self.assertEqual(final_intent["classification"], "FINAL_WRITE")
        self.assertEqual(final_intent["decision"], "HARD_DENY")

    def test_n15_unknown_routes_fail_closed_for_read_and_write(self) -> None:
        wrong_content_type = self.classify(
            "https://post.alibaba.com/product/newWrite.json",
            "POST",
            content_type="text/plain",
        )
        self.assertEqual(wrong_content_type["classification"], "DENY_DEFAULT")
        read_result = self.classify("https://post.alibaba.com/product/newRead.json", "GET")
        self.assertEqual(read_result["classification"], "READ_UNVERIFIED")
        self.assertEqual(read_result["decision"], "DENY")
        write_result = self.classify("https://post.alibaba.com/product/newWrite.json", "POST")
        self.assertEqual(write_result["classification"], "UNKNOWN_WRITE")
        self.assertEqual(write_result["decision"], "DENY")
        self.assertGreaterEqual(len(CORE_INTERFACE_GAPS), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
