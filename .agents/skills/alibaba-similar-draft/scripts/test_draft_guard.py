from __future__ import annotations

import json
import copy
import hashlib
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
POLICY_PATH = SKILL_DIR / "references" / "endpoint-policy.v1.json"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import draft_guard  # noqa: E402
import state_store as state_store_module  # noqa: E402
from runtime_config import resolve_workspace  # noqa: E402
from draft_core import (  # noqa: E402
    GuardError,
    classify_endpoint,
    extract_candidate,
    extract_price,
    load_official_model_configurations,
    load_official_product,
    load_visual_scores,
    official_field_candidates,
    read_json,
    sha256_file,
    verify_overlay,
)
from state_store import (  # noqa: E402
    abort_checkpoint_no_write,
    checkpoint_hash,
    initialize_checkpoint,
    transition_checkpoint,
)


WORKSPACE = resolve_workspace(cwd=Path.cwd()).path


class HistoricalGateTests(unittest.TestCase):
    def test_cutoff_allow_rag_disposition_and_forbidden_ids_fail_closed(self) -> None:
        rows = [
            {
                "product_id": "eligible",
                "gmt_modified": "2000-01-01",
                "record_disposition": "HISTORICAL_REFERENCE_ONLY",
                "allow_rag": True,
            },
            {
                "product_id": "cutoff-inclusive",
                "gmt_modified": "2000-01-02",
                "record_disposition": "HISTORICAL_REFERENCE_ONLY",
                "allow_rag": True,
            },
            {
                "product_id": "rag-false",
                "gmt_modified": "1999-12-26",
                "record_disposition": "HISTORICAL_REFERENCE_ONLY",
                "allow_rag": False,
            },
            {
                "product_id": "waste",
                "gmt_modified": "1999-12-26",
                "record_disposition": "UNUSABLE_WASTE_RECORD",
                "allow_rag": True,
            },
            {
                "product_id": "bad-date",
                "gmt_modified": None,
                "record_disposition": "HISTORICAL_REFERENCE_ONLY",
                "allow_rag": True,
            },
            {
                "product_id": "template",
                "gmt_modified": "1999-12-26",
                "record_disposition": "HISTORICAL_REFERENCE_ONLY",
                "allow_rag": True,
            },
        ]
        eligible, excluded = draft_guard.partition_historical_rows(
            rows,
            cutoff=date(2000, 1, 2),
            forbidden_product_ids={"template"},
        )
        self.assertEqual([row["product_id"] for row in eligible], ["eligible"])
        by_id = {row["product_id"]: row["reason_codes"] for row in excluded}
        self.assertIn("GMT_MODIFIED_ON_OR_AFTER_CUTOFF", by_id["cutoff-inclusive"])
        self.assertIn("ALLOW_RAG_NOT_TRUE", by_id["rag-false"])
        self.assertIn("DISPOSITION_REJECTED", by_id["waste"])
        self.assertIn("GMT_MODIFIED_MISSING_OR_INVALID", by_id["bad-date"])
        self.assertIn("FORBIDDEN_PRODUCT_ID", by_id["template"])

    def test_duplicate_or_empty_product_ids_abort(self) -> None:
        base = {
            "gmt_modified": "1999-12-26",
            "record_disposition": "HISTORICAL_REFERENCE_ONLY",
            "allow_rag": True,
        }
        with self.assertRaises(GuardError):
            draft_guard.partition_historical_rows([{**base, "product_id": ""}])
        with self.assertRaises(GuardError):
            draft_guard.partition_historical_rows(
                [{**base, "product_id": "x"}, {**base, "product_id": "x"}]
            )

    def test_all_commercial_fields_default_blocked(self) -> None:
        fields = draft_guard.blocked_commercial_fields()
        self.assertEqual({row["field"] for row in fields}, set(draft_guard.COMMERCIAL_FIELDS))
        self.assertTrue(all(row["decision"] == "BLOCKED" for row in fields))
        self.assertTrue(all(row["write_allowed"] is False for row in fields))
        self.assertTrue(all(row["candidate_value"] is None for row in fields))


class VisualScoreSchemaTests(unittest.TestCase):
    @unittest.skipUnless(WORKSPACE is not None, "portable core test: business workspace not configured")
    def test_reads_real_full_match_visual_score_schema(self) -> None:
        path = (
            WORKSPACE
            / "sellerkit-alibaba-review-ready-v1"
            / "work"
            / "vision"
            / "features"
            / "full_match_visual_scores.jsonl"
        )
        scores = load_visual_scores(path, "MODEL-003")
        self.assertEqual(len(scores), 189)
        self.assertAlmostEqual(scores["1443232220"], 0.72538637)

    def test_reads_complete_synthetic_legacy_schema(self) -> None:
        rows = (
            {
                "website_product_key": "MODEL-003",
                "alibaba_product_key": "synthetic-match",
                "mean_top3_cosine": 0.625,
            },
            {
                "website_product_key": "MODEL-020",
                "alibaba_product_key": "other-model",
                "mean_top3_cosine": 0.99,
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scores.jsonl"
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            self.assertEqual(load_visual_scores(path, "MODEL-003"), {"synthetic-match": 0.625})

    def test_cross_schema_and_wrong_score_keys_are_not_misread(self) -> None:
        rows = (
            {
                "official_model": "MODEL-003",
                "alibaba_product_key": "cross-product-key",
                "visual_score": 0.91,
            },
            {
                "website_product_key": "MODEL-003",
                "alibaba_product_id": "cross-product-id",
                "mean_top3_cosine": 0.92,
            },
            {
                "official_model": "MODEL-003",
                "alibaba_product_id": "wrong-score-key",
                "mean_top3_cosine": 0.93,
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scores.jsonl"
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            self.assertEqual(load_visual_scores(path, "MODEL-003"), {})


class HistoricalCommercialSchemaTests(unittest.TestCase):
    @unittest.skipUnless(WORKSPACE is not None, "portable core test: business workspace not configured")
    def test_real_es100_keeps_bulk_and_sample_prices_separate(self) -> None:
        path = (
            WORKSPACE
            / "sellerkit-alibaba-review-ready-v1"
            / "work"
            / "alibaba_historical_sanitized.jsonl"
        )
        with path.open("r", encoding="utf-8") as stream:
            row = next(
                value
                for value in (json.loads(line) for line in stream if line.strip())
                if value.get("product_id") == "100000000019"
            )

        price = extract_price(row)
        self.assertEqual(price["price_basis"], "BULK_ONLY")
        self.assertEqual((price["low"], price["high"]), (29.0, 149.0))
        self.assertEqual(price["observations"], [29.0, 149.0])
        self.assertEqual(price["sample"]["observations"], [169.0])
        self.assertTrue(price["sample"]["excluded_from_bulk"])

        candidate = extract_candidate(row, rank=1, retrieval_score=0.0, visual_score=0.0)
        self.assertEqual(candidate["trade_price_type"], "FOB")
        self.assertIs(candidate["trade_available"], False)
        self.assertEqual(candidate["price"]["high"], 149.0)

    @unittest.skipUnless(WORKSPACE is not None, "portable core test: business workspace not configured")
    def test_strict_price_gate_excludes_add_on_spout_and_dual_handle(self) -> None:
        path = (
            WORKSPACE
            / "sellerkit-alibaba-review-ready-v1"
            / "work"
            / "alibaba_historical_sanitized.jsonl"
        )
        wanted = {"100000000019", "62388213815", "100000000020"}
        rows: dict[str, dict[str, object]] = {}
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                product_id = str(row.get("product_id") or "")
                if product_id in wanted:
                    rows[product_id] = row
        self.assertEqual(set(rows), wanted)

        es100 = extract_candidate(rows["100000000019"], 1, 0.0, 0.0)
        es106c = extract_candidate(rows["62388213815"], 2, 0.0, 0.0)
        ef100 = extract_candidate(rows["100000000020"], 3, 0.0, 0.0)
        self.assertTrue(es100["price_eligible"])
        self.assertFalse(es106c["price_eligible"])
        self.assertTrue(es106c["configuration_conflicts"]["add_on_spout"])
        self.assertFalse(ef100["price_eligible"])
        self.assertTrue(ef100["configuration_conflicts"]["dual_handle"])

    @unittest.skipUnless(WORKSPACE is not None, "portable core test: business workspace not configured")
    def test_current_official_model_configuration_disqualifies_omitted_add_on_spout(self) -> None:
        historical_path = (
            WORKSPACE
            / "sellerkit-alibaba-review-ready-v1"
            / "work"
            / "alibaba_historical_sanitized.jsonl"
        )
        official_path = WORKSPACE / "sellerkit-alibaba-review-ready-v1" / "work" / "official_current.jsonl"
        with historical_path.open("r", encoding="utf-8") as stream:
            row = next(
                value
                for value in (json.loads(line) for line in stream if line.strip())
                if value.get("product_id") == "100000000007"
            )
        configuration = load_official_model_configurations(official_path)["AA106C"]
        candidate = extract_candidate(row, 1, 0.0, 0.0, configuration)
        self.assertTrue(configuration["add_on_spout"])
        self.assertTrue(candidate["configuration_conflicts"]["add_on_spout"])
        self.assertFalse(candidate["price_eligible"])


class OfficialCurrentResolutionTests(unittest.TestCase):
    @unittest.skipUnless(WORKSPACE is not None, "portable core test: business workspace not configured")
    def test_model_100_standard_and_optional_spray_semantics_are_not_a_conflict(self) -> None:
        path = WORKSPACE / "sellerkit-alibaba-review-ready-v1" / "work" / "official_current.jsonl"
        official = load_official_product(path, "MODEL-003")
        fields = {row["field"]: row for row in official_field_candidates(official, path)}
        spray = fields["spray_valve"]
        self.assertEqual(spray["configuration_resolution"], "STANDARD_DUAL_OPTIONAL_SINGLE")
        self.assertEqual(spray["candidate_value"], "SUS304 Dual-function (Standard), MODEL-022")
        self.assertIn("SUS304 Single-function [MODEL-023] is optional", spray["note"])
        self.assertEqual(spray["source_path"], official["source"]["source_path"])
        self.assertEqual(spray["source_sha256"], official["source"]["source_sha256"])

    def test_safe_snapshot_trade_precedes_legacy_trade(self) -> None:
        row = {
            "currency": "USD",
            "safe_snapshot": {
                "product": {
                    "price": {
                        "range": {
                            "dollarPriceRangeLow": 10,
                            "dollarPriceRangeHigh": 20,
                        },
                        "unit": "unit",
                    }
                },
                "trade": {
                    "trade": {
                        "tradeAvailable": False,
                        "tradePriceType": "FOB",
                        "quantityUnitStr": "units",
                    }
                },
            },
            "trade": {
                "trade": {
                    "tradeAvailable": True,
                    "tradePriceType": "CIF",
                    "quantityUnitStr": "pieces",
                }
            },
        }
        candidate = extract_candidate(row, rank=None, retrieval_score=0.0, visual_score=0.0)
        self.assertEqual(candidate["trade_price_type"], "FOB")
        self.assertIs(candidate["trade_available"], False)

    def test_legacy_trade_schema_and_sample_outlier_regression(self) -> None:
        row = {
            "currency": "USD",
            "safe_snapshot": {
                "product": {
                    "price": {
                        "range": {
                            "dollarPriceRangeLow": 25,
                            "dollarPriceRangeHigh": 100,
                        },
                        "ladders": [{"dollarPrice": 75}],
                    },
                    "sku": {"samples": [{"price": "999.00"}]},
                }
            },
            "trade": {
                "trade": {
                    "tradeAvailable": True,
                    "tradePriceType": "EXW",
                    "quantityUnitStr": "pieces",
                }
            },
        }
        price = extract_price(row)
        self.assertEqual(price["unit"], "pieces")
        self.assertEqual((price["low"], price["high"]), (25.0, 100.0))
        self.assertEqual(price["observations"], [25.0, 75.0, 100.0])
        self.assertEqual(price["sample"]["high"], 999.0)

        candidate = extract_candidate(row, rank=None, retrieval_score=0.0, visual_score=0.0)
        self.assertEqual(candidate["trade_price_type"], "EXW")
        self.assertIs(candidate["trade_available"], True)


class PolicyAndReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def test_current_policy_has_no_allow_path(self) -> None:
        cases = (
            ("https://post.alibaba.com/product/submitFinal.json", "POST", "HARD_DENY"),
            ("https://post.alibaba.com/product/submitDraft.json", "POST", "DENY"),
            ("https://post.alibaba.com/product/asyRender.json", "GET", "DENY"),
            ("https://post.alibaba.com/product/unknown.json", "GET", "DENY"),
        )
        for url, method, expected in cases:
            result = classify_endpoint(self.policy, url, method)
            self.assertEqual(result["decision"], expected, result)

    def test_check_endpoint_exit_zero_requires_exact_allow(self) -> None:
        argv = [
            "check-endpoint", "--url", "https://post.alibaba.com/product/visible-ui",
            "--method", "GET", "--policy", str(POLICY_PATH),
        ]
        for decision, expected_code in (("ALLOW", 0), ("ALLOW_UI_ONLY", 3), ("DENY", 3)):
            with self.subTest(decision=decision), patch(
                "draft_guard.classify_endpoint", return_value={"decision": decision, "errors": []}
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(draft_guard.main(argv), expected_code)

    def valid_probe(self) -> dict[str, object]:
        return {
            "target_model": "MODEL-003",
            "template_product_id": "100000000014",
            "current_url": (
                "https://post.alibaba.com/product/publish.htm?"
                "pubType=similarPost&itemId=100000000014&behavior=copyNew"
            ),
            "captcha_detected": False,
            "network_guard_installed_before_navigation": True,
            "cookie_or_storage_read": False,
            "page_fingerprint": "fixture-page-fingerprint",
            "online_count_before": 4,
            "online_count_after": 4,
            "draft_count_before": 2,
            "draft_count_after": 2,
            "requests": [],
        }

    def test_legacy_flat_probe_cannot_forge_zero_write_pass(self) -> None:
        self.assertEqual(
            draft_guard.strict_verify_probe(self.valid_probe(), self.policy)["status"],
            "BLOCKED_PROBE",
        )
        for mutation in (
            {"captcha_detected": None},
            {"online_count_after": 5},
            {"requests": [{"url": "https://post.alibaba.com/x", "method": "UNKNOWN"}]},
            {"requests": None},
        ):
            probe = self.valid_probe()
            probe.update(mutation)
            self.assertEqual(draft_guard.strict_verify_probe(probe, self.policy)["status"], "BLOCKED_PROBE")

    def valid_fill(self) -> dict[str, object]:
        return {
            "target_model": "MODEL-003",
            "template_product_id": "100000000014",
            "field_readbacks": {"model": "MODEL-003", "body_material": "SUS304"},
            "forbidden_template_values": [
                "MODEL-020",
                "100000000014",
                "Luxury Lite Modern High-grade Brushed",
                "$29–209",
            ],
            "inherited_content_cleared": True,
            "template_video_present": False,
            "final_submit_triggered": False,
            "draft_save_count": 1,
        }

    def test_legacy_fill_receipt_fails_without_bound_execution_and_refresh_ledgers(self) -> None:
        result = draft_guard.strict_verify_fill(self.valid_fill())
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("post-refresh" in error for error in result["errors"]), result)
        self.assertTrue(any("action plan" in error for error in result["errors"]), result)


class CheckpointTests(unittest.TestCase):
    def _write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def _bundle(
        self,
        root: Path,
        checkpoint: dict,
        *,
        new_state: str,
        role: str,
        receipt_path: Path,
        evidence_id: str,
    ) -> Path:
        payload = receipt_path.read_bytes()
        descriptor = {
            "role": role,
            "path": str(receipt_path.resolve()),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        bundle = {
            "schema_version": "sellerkit.checkpoint-transition-evidence.v1",
            "evidence_id": evidence_id,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "run_id": checkpoint["run_id"],
            "target_model": checkpoint["target_model"],
            "template_product_id": checkpoint["template_product_id"],
            "new_state": new_state,
            "artifacts": [descriptor],
        }
        body = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        bundle["body_sha256"] = hashlib.sha256(body).hexdigest()
        path = root / f"{evidence_id}.json"
        self._write_json(path, bundle)
        return path

    def test_initialize_checkpoint_preserves_explicit_existing_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            checkpoint = initialize_checkpoint(
                run_dir,
                "MODEL-003",
                "100000000014",
                {"input": "0" * 64},
                run_id="MODEL-011",
            )
            self.assertEqual(checkpoint["run_id"], "MODEL-011")
            lock = json.loads(Path(checkpoint["lock_path"]).read_text(encoding="utf-8"))
            self.assertEqual(lock["run_id"], "MODEL-011")

    def test_init_run_cli_binds_prepared_run_spec_and_rejects_mismatch(self) -> None:
        for supplied_run_id, expected_code in (
            ("MODEL-014", 0),
            ("contradictory-run-id", 2),
        ):
            with self.subTest(supplied_run_id=supplied_run_id), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_dir = root / "run"
                run_dir.mkdir(parents=True)
                run_spec = {
                    "schema_version": "sellerkit.similar_draft_run_spec.v1",
                    "run_id": "MODEL-014",
                    "target_model": "MODEL-003",
                    "template_product_id": "100000000014",
                }
                run_spec_path = run_dir / "run_spec.json"
                lock_root = root / "shared-model-locks"
                self._write_json(run_spec_path, run_spec)
                with redirect_stdout(io.StringIO()):
                    exit_code = draft_guard.main([
                        "init-run",
                        "--run-dir", str(run_dir),
                        "--target-model", "MODEL-003",
                        "--template-product-id", "100000000014",
                        "--run-id", supplied_run_id,
                        "--lock-root", str(lock_root),
                        "--input", f"run_spec={run_spec_path}",
                    ])
                self.assertEqual(exit_code, expected_code)
                checkpoint_path = run_dir / "checkpoint.json"
                if expected_code == 0:
                    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    self.assertEqual(checkpoint["run_id"], run_spec["run_id"])
                else:
                    self.assertFalse(checkpoint_path.exists())
                    self.assertFalse(lock_root.exists(), "identity mismatch wrote a lock before aborting")

    def test_shared_lock_root_blocks_same_model_across_different_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_root = root / "shared-model-locks"
            first_dir = root / "runs-a" / "first"
            second_dir = root / "runs-b" / "second"
            first = initialize_checkpoint(
                first_dir,
                "MODEL-003",
                "100000000014",
                {"input": "0" * 64},
                run_id="MODEL-017",
                lock_root=lock_root,
            )
            self.assertEqual(Path(first["lock_path"]).parent, lock_root.resolve())
            with self.assertRaisesRegex(GuardError, "RUN_LOCKED"):
                initialize_checkpoint(
                    second_dir,
                    "MODEL-003",
                    "100000000014",
                    {"input": "1" * 64},
                    run_id="MODEL-018",
                    lock_root=lock_root,
                )
            self.assertFalse((second_dir / "checkpoint.json").exists())

    def test_checkpoint_cas_and_transition_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            checkpoint = initialize_checkpoint(run_dir, "MODEL-003", "100000000014", {"input": "0" * 64})
            checkpoint_path = run_dir / "checkpoint.json"
            observed = checkpoint_hash(checkpoint)
            approval_receipt = root / "approval-receipt.json"
            self._write_json(approval_receipt, {
                "schema_version": "sellerkit.approval-compile-receipt.v1",
                "status": "PASS",
                "run_id": checkpoint["run_id"],
                "target_model": checkpoint["target_model"],
                "template_product_id": checkpoint["template_product_id"],
            })
            evidence = self._bundle(
                root, checkpoint, new_state="CANDIDATE_REVIEW_APPROVED",
                role="APPROVAL_COMPILATION_RECEIPT", receipt_path=approval_receipt,
                evidence_id="candidate-approved",
            )
            with self.assertRaises(GuardError):
                transition_checkpoint(
                    checkpoint_path,
                    0,
                    "f" * 64,
                    "CANDIDATE_REVIEW_APPROVED",
                    evidence,
                )
            updated = transition_checkpoint(
                checkpoint_path,
                0,
                observed,
                "CANDIDATE_REVIEW_APPROVED",
                evidence,
            )
            self.assertEqual(updated["revision"], 1)
            self.assertEqual(updated["state"], "CANDIDATE_REVIEW_APPROVED")
            with self.assertRaises(GuardError):
                initialize_checkpoint(run_dir, "MODEL-003", "100000000014", {"input": "0" * 64})

            reused = self._bundle(
                root, updated, new_state="TEMPLATE_PROBED",
                role="PROBE_VERIFICATION_RECEIPT", receipt_path=approval_receipt,
                evidence_id="probe-with-reused-artifact",
            )
            with self.assertRaisesRegex(GuardError, "hash reuse"):
                transition_checkpoint(
                    checkpoint_path, updated["revision"], checkpoint_hash(updated),
                    "TEMPLATE_PROBED", reused,
                )

    def test_checkpoint_mutation_lock_prevents_stale_abort_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            checkpoint = initialize_checkpoint(
                run_dir,
                "MODEL-003",
                "100000000014",
                {"input": "0" * 64},
                run_id="MODEL-010",
                lock_root=root / "shared-locks",
            )
            checkpoint_path = run_dir / "checkpoint.json"
            approval_receipt = root / "approval-receipt.json"
            self._write_json(approval_receipt, {
                "schema_version": "sellerkit.approval-compile-receipt.v1",
                "status": "PASS",
                "run_id": checkpoint["run_id"],
                "target_model": checkpoint["target_model"],
                "template_product_id": checkpoint["template_product_id"],
            })
            evidence = self._bundle(
                root,
                checkpoint,
                new_state="CANDIDATE_REVIEW_APPROVED",
                role="APPROVAL_COMPILATION_RECEIPT",
                receipt_path=approval_receipt,
                evidence_id="candidate-approved-race",
            )
            entered_commit = threading.Event()
            release_commit = threading.Event()
            original_atomic_write = state_store_module.atomic_write_json
            thread_errors: list[BaseException] = []

            def delayed_atomic_write(path: Path, value: dict) -> None:
                if value.get("state") == "CANDIDATE_REVIEW_APPROVED":
                    entered_commit.set()
                    if not release_commit.wait(timeout=5):
                        raise RuntimeError("test timed out waiting to release checkpoint commit")
                original_atomic_write(path, value)

            def run_transition() -> None:
                try:
                    transition_checkpoint(
                        checkpoint_path,
                        checkpoint["revision"],
                        checkpoint_hash(checkpoint),
                        "CANDIDATE_REVIEW_APPROVED",
                        evidence,
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    thread_errors.append(exc)

            with patch.object(state_store_module, "atomic_write_json", delayed_atomic_write):
                worker = threading.Thread(target=run_transition, daemon=True)
                worker.start()
                self.assertTrue(entered_commit.wait(timeout=5))
                with self.assertRaisesRegex(GuardError, "CHECKPOINT_MUTATION_LOCKED"):
                    abort_checkpoint_no_write(
                        checkpoint_path,
                        checkpoint["revision"],
                        checkpoint_hash(checkpoint),
                        "stale abort must not overwrite an in-flight transition",
                    )
                release_commit.set()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(thread_errors, [])
            final = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(final["state"], "CANDIDATE_REVIEW_APPROVED")
            self.assertEqual(final["revision"], checkpoint["revision"] + 1)
            self.assertTrue(Path(final["lock_path"]).is_file())
            self.assertFalse(
                checkpoint_path.with_name("checkpoint.json.mutation.lock").exists()
            )

    def test_transition_cli_has_no_bare_evidence_hash_escape_hatch(self) -> None:
        parser_source = (SCRIPT_DIR / "draft_guard.py").read_text(encoding="utf-8")
        self.assertNotIn('add_argument("--evidence-sha256")', parser_source)
        parser = draft_guard._build_parser()
        transition_parser = next(
            action.choices["transition"]
            for action in parser._actions
            if getattr(action, "choices", None) and "transition" in action.choices
        )
        state_action = next(action for action in transition_parser._actions if action.dest == "new_state")
        self.assertNotIn("SAVE_ATTEMPTED", state_action.choices)

    def test_abort_no_write_releases_model_lock_and_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            checkpoint = initialize_checkpoint(
                run_dir,
                "MODEL-003",
                "100000000014",
                {"input": "0" * 64},
                run_id="MODEL-009",
                lock_root=root / "shared-locks",
            )
            checkpoint_path = run_dir / "checkpoint.json"
            lock_path = Path(checkpoint["lock_path"])
            self.assertTrue(lock_path.is_file())
            aborted = abort_checkpoint_no_write(
                checkpoint_path,
                checkpoint["revision"],
                checkpoint_hash(checkpoint),
                "operator stopped before any external write",
            )
            self.assertEqual(aborted["state"], "ABORTED_NO_WRITE")
            self.assertEqual(aborted["run_status"], "ABORTED_NO_WRITE")
            self.assertEqual(aborted["lock_state"], "RELEASED")
            self.assertEqual(aborted["revision"], 1)
            self.assertEqual(aborted["evidence"], [])
            self.assertEqual(aborted["save_attempt_count"], 0)
            self.assertFalse(lock_path.exists())
            with self.assertRaisesRegex(GuardError, "invalid transition ABORTED_NO_WRITE"):
                transition_checkpoint(
                    checkpoint_path,
                    aborted["revision"],
                    checkpoint_hash(aborted),
                    "CANDIDATE_REVIEW_APPROVED",
                    root / "never-used.json",
                )

    def test_abort_no_write_is_forbidden_after_attempt_or_any_write_signal(self) -> None:
        for mutation, expected_error in (
            ({"state": "SAVE_ATTEMPTED", "save_attempt_count": 1}, "save_attempt_count is not zero"),
            ({"draft_id": "unexpected-draft"}, "draft_id is present"),
            ({"draft_save_count": 1}, "draft_save_count is not zero"),
            ({"final_write_count": 1}, "final_write_count is not zero"),
            ({"submit_triggered": True}, "submit_triggered is not false"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_dir = root / "run"
                checkpoint = initialize_checkpoint(
                    run_dir,
                    "MODEL-003",
                    "100000000014",
                    {"input": "0" * 64},
                    run_id="MODEL-004",
                    lock_root=root / "shared-locks",
                )
                checkpoint.update(mutation)
                checkpoint_path = run_dir / "checkpoint.json"
                self._write_json(checkpoint_path, checkpoint)
                lock_path = Path(checkpoint["lock_path"])
                with self.assertRaisesRegex(GuardError, expected_error):
                    abort_checkpoint_no_write(
                        checkpoint_path,
                        checkpoint["revision"],
                        checkpoint_hash(checkpoint),
                        "must not hide a write signal",
                    )
                self.assertTrue(lock_path.is_file(), "blocked abort released the model lock")

    def test_abort_no_write_is_forbidden_when_attempt_ledger_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            checkpoint = initialize_checkpoint(
                run_dir,
                "MODEL-003",
                "100000000014",
                {"input": "0" * 64},
                run_id="MODEL-006",
                lock_root=root / "shared-locks",
            )
            checkpoint_path = run_dir / "checkpoint.json"
            ledger_dir = run_dir / "draft-save-attempt-ledger"
            ledger_dir.mkdir()
            self._write_json(ledger_dir / "burned.consumed.json", {"state": "CONSUMED_FAIL_CLOSED"})
            with self.assertRaisesRegex(GuardError, "attempt ledger exists"):
                abort_checkpoint_no_write(
                    checkpoint_path,
                    checkpoint["revision"],
                    checkpoint_hash(checkpoint),
                    "attempt ledger forbids zero-write abort",
                )
            self.assertTrue(Path(checkpoint["lock_path"]).is_file())

    def test_abort_no_write_can_finish_stale_lock_cleanup_after_checkpoint_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            checkpoint = initialize_checkpoint(
                run_dir,
                "MODEL-003",
                "100000000014",
                {"input": "0" * 64},
                run_id="MODEL-007",
                lock_root=root / "shared-locks",
            )
            checkpoint_path = run_dir / "checkpoint.json"
            lock_path = Path(checkpoint["lock_path"])
            with patch("state_store.release_model_lock", side_effect=OSError("simulated unlink failure")):
                with self.assertRaisesRegex(OSError, "simulated unlink failure"):
                    abort_checkpoint_no_write(
                        checkpoint_path,
                        checkpoint["revision"],
                        checkpoint_hash(checkpoint),
                        "safe abort with recoverable stale lock",
                    )
            committed = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(committed["state"], "ABORTED_NO_WRITE")
            self.assertTrue(lock_path.is_file())
            mutation_lock = checkpoint_path.with_name("checkpoint.json.mutation.lock")
            self._write_json(mutation_lock, {
                "schema_version": "sellerkit.checkpoint_mutation_lock.v1",
                "checkpoint_path": str(checkpoint_path.resolve()),
                "pid": 999999,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "state": "HELD",
            })
            recovered = abort_checkpoint_no_write(
                checkpoint_path,
                committed["revision"],
                checkpoint_hash(committed),
                "finish stale lock cleanup",
            )
            self.assertEqual(recovered, committed)
            self.assertFalse(lock_path.exists())
            self.assertFalse(mutation_lock.exists())

    def test_terminal_abort_cleanup_refuses_irreversible_attempt_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            checkpoint = initialize_checkpoint(
                run_dir,
                "MODEL-003",
                "100000000014",
                {"input": "0" * 64},
                run_id="MODEL-008",
                lock_root=root / "shared-locks",
            )
            checkpoint_path = run_dir / "checkpoint.json"
            checkpoint.update({
                "revision": 1,
                "previous_checkpoint_sha256": checkpoint_hash(checkpoint),
                "state": "ABORTED_NO_WRITE",
                "run_status": "ABORTED_NO_WRITE",
                "lock_state": "RELEASED",
                "aborted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "abort_reason": "synthetic terminal recovery fixture",
            })
            self._write_json(checkpoint_path, checkpoint)
            mutation_lock = checkpoint_path.with_name("checkpoint.json.mutation.lock")
            self._write_json(mutation_lock, {
                "schema_version": "sellerkit.checkpoint_mutation_lock.v1",
                "checkpoint_path": str(checkpoint_path.resolve()),
                "pid": 999999,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "state": "HELD_FAIL_CLOSED_IRREVERSIBLE_ATTEMPT",
            })
            with self.assertRaisesRegex(GuardError, "irreversible-attempt"):
                abort_checkpoint_no_write(
                    checkpoint_path,
                    checkpoint["revision"],
                    checkpoint_hash(checkpoint),
                    "must not clear an irreversible attempt marker",
                )
            self.assertTrue(mutation_lock.is_file())
            self.assertTrue(Path(checkpoint["lock_path"]).is_file())

    def test_terminal_abort_cleanup_keeps_both_locks_on_nonzero_write_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            checkpoint = initialize_checkpoint(
                run_dir,
                "MODEL-003",
                "100000000014",
                {"input": "0" * 64},
                run_id="MODEL-005",
                lock_root=root / "shared-locks",
            )
            checkpoint_path = run_dir / "checkpoint.json"
            checkpoint.update({
                "revision": 1,
                "previous_checkpoint_sha256": checkpoint_hash(checkpoint),
                "state": "ABORTED_NO_WRITE",
                "run_status": "ABORTED_NO_WRITE",
                "lock_state": "RELEASED",
                "save_attempt_count": 1,
                "aborted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "abort_reason": "synthetic damaged terminal fixture",
            })
            self._write_json(checkpoint_path, checkpoint)
            mutation_lock = checkpoint_path.with_name("checkpoint.json.mutation.lock")
            self._write_json(mutation_lock, {
                "schema_version": "sellerkit.checkpoint_mutation_lock.v1",
                "checkpoint_path": str(checkpoint_path.resolve()),
                "pid": 999999,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "state": "HELD",
            })
            model_lock = Path(checkpoint["lock_path"])
            with self.assertRaisesRegex(GuardError, "save_attempt_count is not zero"):
                abort_checkpoint_no_write(
                    checkpoint_path,
                    checkpoint["revision"],
                    checkpoint_hash(checkpoint),
                    "damaged terminal checkpoint must remain fail closed",
                )
            self.assertTrue(mutation_lock.is_file())
            self.assertTrue(model_lock.is_file())

    def test_form_filled_cannot_skip_independent_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            checkpoint = initialize_checkpoint(run_dir, "MODEL-003", "100000000014", {"input": "0" * 64})
            checkpoint.update({
                "revision": 5,
                "previous_checkpoint_sha256": "a" * 64,
                "state": "FORM_FILLED",
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
            checkpoint_path = run_dir / "checkpoint.json"
            self._write_json(checkpoint_path, checkpoint)
            with self.assertRaisesRegex(GuardError, "invalid transition FORM_FILLED -> SAVE_ATTEMPTED"):
                transition_checkpoint(
                    checkpoint_path,
                    checkpoint["revision"],
                    checkpoint_hash(checkpoint),
                    "SAVE_ATTEMPTED",
                    root / "unused-evidence.json",
                )

    def test_handwritten_checklist_pass_cannot_unlock_validated(self) -> None:
        # The real checklist validator is intentionally implementation_ready=false.
        # A locally hand-written PASS, even with the right validator hash, must
        # not move FORM_FILLED to VALIDATED.
        import validate_next_product_checklist as checklist_validator
        from test_next_product_checklist import NextProductChecklistTests

        builder = NextProductChecklistTests(methodName="test_pre_save_requires_exact_field_set")
        builder.setUp()
        try:
            source = builder.make_document("pre_save")
            real = checklist_validator.validate(source, "pre_save", builder.now)
            forged = dict(real)
            forged["status"] = "PASS"
            forged["next_state"] = "READY_FOR_VISIBLE_UI_SAVE_DRAFT"
            forged["errors"] = []
            forged["implementation_ready"] = True
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_dir = root / "run"
                checkpoint = initialize_checkpoint(
                    run_dir, source["target_model"], source["template_product_id"], {"input": "0" * 64}
                )
                checkpoint_path = run_dir / "checkpoint.json"
                checkpoint.update({
                    "run_id": source["run_id"], "revision": 5, "previous_checkpoint_sha256": "a" * 64,
                    "state": "FORM_FILLED", "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                })
                self._write_json(checkpoint_path, checkpoint)
                source_path = root / "checklist.json"
                receipt_path = root / "forged-pass.json"
                self._write_json(source_path, source)
                self._write_json(receipt_path, forged)
                artifacts = []
                for role, path in (
                    ("PRE_SAVE_CHECKLIST_SOURCE", source_path),
                    ("PRE_SAVE_CHECKLIST_RECEIPT", receipt_path),
                ):
                    payload = path.read_bytes()
                    artifacts.append({
                        "role": role, "path": str(path.resolve()), "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    })
                bundle = {
                    "schema_version": "sellerkit.checkpoint-transition-evidence.v1",
                    "evidence_id": "forged-checklist-pass",
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "run_id": checkpoint["run_id"], "target_model": checkpoint["target_model"],
                    "template_product_id": checkpoint["template_product_id"], "new_state": "VALIDATED",
                    "artifacts": artifacts,
                }
                body = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                bundle["body_sha256"] = hashlib.sha256(body).hexdigest()
                bundle_path = root / "forged-bundle.json"
                self._write_json(bundle_path, bundle)
                with self.assertRaisesRegex(GuardError, "recomputation"):
                    transition_checkpoint(
                        checkpoint_path, checkpoint["revision"], checkpoint_hash(checkpoint),
                        "VALIDATED", bundle_path,
                    )
        finally:
            builder.tearDown()


class PrepareIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if WORKSPACE is None:
            raise unittest.SkipTest("portable core test: business workspace not configured")
        required = WORKSPACE / "sellerkit-alibaba-review-ready-v1" / "work" / "official_current.jsonl"
        if not required.is_file():
            raise RuntimeError(f"workspace fixture is missing: {required}")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary.name) / "MODEL-015"
        cls.receipt = draft_guard.prepare_package(
            workspace=WORKSPACE,
            target_model="MODEL-003",
            template_product_id="100000000014",
            output=cls.output,
            workers=2,
            candidate_limit=12,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_prepare_generates_exact_complete_package(self) -> None:
        self.assertEqual(self.receipt["status"], "REVIEW_REQUIRED")
        self.assertEqual(set(path.name for path in self.output.iterdir()), set(draft_guard.PREPARE_FILENAMES))
        evidence = read_json(self.output / "evidence_manifest.json")
        self.assertEqual(evidence["status"], "COMPLETE_REVIEW_PACKAGE")
        self.assertFalse(evidence["write_boundary"]["external_write_enabled"])
        for row in evidence["generated_files"]:
            path = Path(row["path"])
            self.assertTrue(path.is_file())
            self.assertEqual(sha256_file(path), row["sha256"])

    def test_model_100_and_template_records_are_hard_excluded(self) -> None:
        review = read_json(self.output / "candidate_review.json")
        candidate_ids = {row["product_id"] for row in review["candidates"]}
        exclusions = {row["product_id"]: row for row in review["hard_exclusions"]}
        self.assertNotIn("100000000013", candidate_ids)
        self.assertNotIn("100000000014", candidate_ids)
        self.assertIn("100000000013", exclusions)
        self.assertIn("100000000014", exclusions)
        self.assertIn("GMT_MODIFIED_ON_OR_AFTER_CUTOFF", exclusions["100000000013"]["reason_codes"])
        self.assertIn("ALLOW_RAG_NOT_TRUE", exclusions["100000000013"]["reason_codes"])
        self.assertTrue(all(row["allow_rag"] is True for row in review["candidates"]))
        self.assertTrue(
            all(row["record_disposition"] == "HISTORICAL_REFERENCE_ONLY" for row in review["candidates"])
        )
        self.assertTrue(
            all(str(row["gmt_modified_proxy"])[:10] < "2000-01-02" for row in review["candidates"])
        )

    def test_templates_are_non_writable_and_commercial_fields_blocked(self) -> None:
        manifest = read_json(self.output / "approved_source_manifest.template.json")
        self.assertEqual(manifest["schema_version"], "sellerkit.approved_source_manifest_template.v1")
        self.assertEqual(manifest["target_schema_version"], "alibaba.approved-source-manifest.v1")
        commercial = {
            row["field"]: row for row in manifest["fields"] if row["field"] in draft_guard.COMMERCIAL_FIELDS
        }
        self.assertEqual(set(commercial), set(draft_guard.COMMERCIAL_FIELDS))
        self.assertTrue(all(row["decision"] == "BLOCKED" for row in commercial.values()))
        self.assertTrue(all(row["write_allowed"] is False for row in manifest["fields"]))
        assets = read_json(self.output / "asset_manifest.candidates.json")
        self.assertEqual(assets["target_schema_version"], "alibaba.approved-asset-manifest.v1")
        self.assertTrue(all(row["upload_allowed"] is False for row in assets["assets"]))
        run_spec = read_json(self.output / "run_spec.json")
        self.assertFalse(run_spec["external_write_enabled"])
        self.assertFalse(run_spec["final_write_enabled"])
        self.assertFalse(run_spec["direct_endpoint_write_enabled"])

    def test_assets_include_two_official_and_two_hash_bound_product_locks(self) -> None:
        assets = read_json(self.output / "asset_manifest.candidates.json")
        summary = assets["summary"]
        self.assertEqual(summary["official_current_unique_count"], 2)
        self.assertEqual(summary["hash_bound_derivation_count"], 2)
        self.assertEqual(summary["source_chain_incomplete_count"], 2)

        locks = [row for row in assets["assets"] if row.get("source_type") == "HASH_BOUND_PRODUCT_LOCK"]
        self.assertEqual(len(locks), 2)
        self.assertTrue(all(row["exists"] and row["hash_match"] for row in locks))
        self.assertTrue(all(row["decision"] == "PENDING" for row in locks))
        self.assertTrue(
            all(
                row["source_disposition"] == "HASH_BOUND_DERIVATION_SOURCE_MISSING_REVIEW_REQUIRED"
                for row in locks
            )
        )

        review = read_json(self.output / "candidate_review.json")
        self.assertTrue(
            any("Only 2 unique official-current images" in warning for warning in review["quality_warnings"])
        )

    def test_only_es100_enters_the_strict_price_lane(self) -> None:
        review = read_json(self.output / "candidate_review.json")
        eligible = [row for row in review["candidates"] if row["price_eligible"]]
        self.assertEqual([(row["product_id"], row["model"]) for row in eligible], [("100000000019", "ES100")])

    def test_prepare_never_overwrites_existing_output(self) -> None:
        with self.assertRaises(GuardError):
            draft_guard.prepare_package(
                workspace=WORKSPACE,
                target_model="MODEL-003",
                template_product_id="100000000014",
                output=self.output,
            )

    def test_overlay_content_hash_rebind_survives_non_ascii_relocation(self) -> None:
        overlay = max(
            path
            for path in WORKSPACE.glob("*__record_disposition_overlay__*")
            if path.is_dir()
        )
        base_name = overlay.name.split("__record_disposition_overlay__", 1)[0]
        catalog = next((WORKSPACE / base_name).rglob("catalog_index.jsonl"))
        with tempfile.TemporaryDirectory() as temporary:
            relocated = Path(temporary) / "迁移验证工作区"
            relocated.mkdir()
            shutil.copytree(overlay, relocated / overlay.name)
            target_catalog = relocated / base_name / catalog.relative_to(WORKSPACE / base_name)
            target_catalog.parent.mkdir(parents=True)
            shutil.copy2(catalog, target_catalog)
            receipt = verify_overlay(relocated)
            self.assertEqual(receipt["status"], "COMPLETE")
            self.assertEqual(receipt["verification_mode"], "CONTENT_HASH_REBOUND")
            self.assertTrue(receipt["relocation_rebound"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
