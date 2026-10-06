import importlib.util
import argparse
import contextlib
import io
import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("rfq_match.py")
SPEC = importlib.util.spec_from_file_location("rfq_match", MODULE_PATH)
assert SPEC and SPEC.loader
rfq_match = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rfq_match)


def sink_record(actuation="Knee"):
    return {
        "canonical_model": "SYNTHETIC1",
        "name": f"Stainless Steel {actuation} Operated Hand Wash Sink - Wall Mount",
        "category": "Wash Sinks",
        "attributes": [
            {"name": "Material", "raw_value": "SUS304"},
            {"name": "Installation", "raw_value": "Wall Mount"},
            {"name": "Shape", "raw_value": "Single Bowl Rectangular"},
            {"name": "Size", "raw_value": "500 x 400 x 395 mm"},
        ],
    }


def rfq_for(actuation="foot", suffix=""):
    return (
        f"SUS304 {actuation} operated single bowl rectangular hand wash sink, "
        f"wall mount 500 x 400 x 395 mm. {suffix}"
    )


def run_main_with_synthetic_files(rfq_text, record, overlay_rows=None):
    with tempfile.TemporaryDirectory(prefix="rfq-regression-") as directory:
        root = Path(directory)
        official = root / "official.jsonl"
        catalog = root / "catalog.jsonl"
        overlay = root / "overlay.jsonl"
        official.write_text(json.dumps(record) + "\n", encoding="utf-8")
        catalog.write_text(json.dumps({
            "model": "SYNTHETIC1", "product_id": "synthetic-p1", "currency": "USD",
            "sku_min_price": 99, "sku_max_price": 101, "min_order_quantity": 1,
        }) + "\n", encoding="utf-8")
        if overlay_rows is not None:
            overlay.write_text(
                "".join(json.dumps(row) + "\n" for row in overlay_rows), encoding="utf-8"
            )
        args = argparse.Namespace(
            rfq_text=rfq_text, rfq_file=None, official_current=official,
            catalog_index=catalog, disposition=overlay, top=5,
        )
        output = io.StringIO()
        with mock.patch.object(rfq_match, "parse_args", return_value=args):
            with contextlib.redirect_stdout(output):
                result = rfq_match.main()
        assert result == 0
        return json.loads(output.getvalue())


class RfqMatchTests(unittest.TestCase):
    def test_combined_201_304_material_is_not_split_wrongly(self):
        found = rfq_match.concepts("Material: Stainless Steel 201/304")
        self.assertIn("SUS201", found)
        self.assertIn("SUS304", found)

    def test_description_does_not_promote_wrong_product_family(self):
        record = {
            "canonical_model": "F20",
            "name": "Stainless Steel Drinking Fountain",
            "category": "Drinking Taps",
            "subcategory": "Drinking Fountains",
            "description_text": "hand wash sink knee operated wash basin",
            "attributes": [
                {"name": "Body Material", "raw_value": "SUS304"},
                {"name": "Body Mount", "raw_value": "Wall Mount"},
            ],
        }
        result = rfq_match.score_candidate(
            "SUS304 foot operated hand washing sink, customization welcomed",
            record,
        )
        self.assertNotIn("product_family", result["matched"])
        self.assertNotIn("hands_free_actuation_family", result["matched"])
        self.assertEqual("NO_MATCH", result["family_match"])
        self.assertLess(result["score"], 6)

    def test_real_wash_sink_keeps_family_priority(self):
        record = {
            "canonical_model": "SSKNBS",
            "name": "Stainless Steel Knee Operated Wash Sink - Wall Mount",
            "category": "Wash Sinks",
            "subcategory": "For Knee Operated",
            "attributes": [
                {"name": "Body Material", "raw_value": "SUS304"},
                {"name": "Body Mount", "raw_value": "Wall Mount"},
                {"name": "Product Size", "raw_value": "500 x 400 x 395 mm"},
            ],
        }
        result = rfq_match.score_candidate(
            "Stainless Steel 201/304 foot operated hand washing sink, "
            "460*460*1250 mm floor standing, customization welcomed",
            record,
        )
        self.assertEqual("EXACT_OR_INTENDED_APPLICATION", result["family_match"])
        self.assertIn("product_family", result["matched"])
        self.assertIn("foot-operated requested; knee-operated base", result["mismatches"])
        self.assertGreaterEqual(result["score"], 6)


class StructuralGateTests(unittest.TestCase):
    def test_high_scoring_foot_request_knee_base_requires_review(self):
        text = rfq_for("foot")
        result = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertGreaterEqual(result["score"], 6)
        self.assertEqual("REVIEW", result["mode"])
        self.assertIn("foot-operated requested; knee-operated base", result["mismatches"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, result))

    def test_high_scoring_knee_request_foot_base_requires_review(self):
        text = rfq_for("knee")
        result = rfq_match.score_candidate(text, sink_record("Foot"))
        self.assertGreaterEqual(result["score"], 6)
        self.assertEqual("REVIEW", result["mode"])
        self.assertIn("knee-operated requested; foot-operated base", result["mismatches"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, result))

    def test_unknown_knee_actuation_requires_review(self):
        text = rfq_for("knee")
        result = rfq_match.score_candidate(text, sink_record("Manual"))
        self.assertIn("knee-operated actuation not shown in base facts", result["mismatches"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, result))

    def test_exact_actuation_remains_fillable(self):
        for actuation in ("foot", "knee"):
            with self.subTest(actuation=actuation):
                text = rfq_for(actuation)
                result = rfq_match.score_candidate(text, sink_record(actuation))
                self.assertEqual([], result["mismatches"])
                self.assertEqual("EXACT_OR_NEAR", result["mode"])
                self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, result))

    def test_explicit_custom_permission_preserves_custom_shortlist(self):
        for suffix in ("customization welcomed", "Customization is allowed", "custom made sink required"):
            with self.subTest(suffix=suffix):
                text = rfq_for("knee", suffix)
                result = rfq_match.score_candidate(text, sink_record("Foot"))
                self.assertEqual("CUSTOM", result["mode"])
                self.assertTrue(result["mismatches"])
                self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, result))

    def test_bare_keywords_or_negative_permission_never_allow_custom(self):
        for suffix in (
            "reliable suppliers welcomed", "customization", "no customization",
            "customization is not allowed", "customization not supported",
            "customization welcomed, but no customization for actuation",
        ):
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                result = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertNotIn("CUSTOM_ALLOWED", rfq_match.concepts(text))
                self.assertEqual("REVIEW", result["mode"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, result))

    def test_material_conflict_not_overridden_by_high_score(self):
        text = rfq_for("knee").replace("SUS304", "SUS201")
        result = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertGreaterEqual(result["score"], 6)
        self.assertIn("material", result["mismatches"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, result))

    def test_dimensions_conflict_not_overridden_by_high_score(self):
        text = rfq_for("knee").replace("500 x 400 x 395", "900 x 800 x 700")
        result = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertGreaterEqual(result["score"], 6)
        self.assertTrue(any(item.startswith("dimensions requested") for item in result["mismatches"]))
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, result))

    def test_custom_mode_cannot_be_injected_without_rfq_permission(self):
        text = rfq_for("foot")
        result = rfq_match.score_candidate(text, sink_record("Knee"))
        result["mode"] = "CUSTOM"
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, result))

    def test_custom_mode_requires_permission_even_without_reported_mismatch(self):
        text = rfq_for("knee")
        result = rfq_match.score_candidate(text, sink_record("Knee"))
        result["mode"] = "CUSTOM"
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, result))

    def test_family_gate_still_blocks_generic_sink(self):
        result = {"score": 99, "mode": "EXACT_OR_NEAR", "mismatches": [], "family_match": "GENERIC_ONLY"}
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(rfq_for(), result))

    def test_low_score_and_no_candidate_still_skip(self):
        self.assertEqual("SKIP_RFQ", rfq_match.recommended_action(rfq_for(), None))
        result = {"score": 3, "mode": "EXACT_OR_NEAR", "mismatches": [], "family_match": "EXACT_OR_INTENDED_APPLICATION"}
        self.assertEqual("SKIP_RFQ", rfq_match.recommended_action(rfq_for(), result))


class HistoricalReferenceGateTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [{"model": "SYNTHETIC1", "product_id": "synthetic-p1", "sku_min_price": 99}]

    def refs(self, overlay):
        mapping = {} if overlay is None else {"synthetic-p1": overlay}
        return rfq_match.historical_refs(self.catalog, "SYNTHETIC1", mapping)

    def test_explicit_known_allowed_reference_is_preserved(self):
        result = self.refs({"record_disposition": "HISTORICAL_REFERENCE_ONLY", "allow_rag": True})
        self.assertEqual(1, len(result))
        self.assertEqual("REFERENCE_ONLY", result[0]["commercial_authority"])
        self.assertEqual(99, result[0]["min_price"])

    def test_missing_or_unknown_disposition_is_excluded(self):
        for overlay in (None, {}, {"allow_rag": True}, {"record_disposition": "BLOCKED_UNCLASSIFIED", "allow_rag": True}):
            with self.subTest(overlay=overlay):
                self.assertEqual([], self.refs(overlay))

    def test_missing_false_or_non_boolean_reuse_is_excluded(self):
        for permission in (None, False, "true", 1, [], {}):
            with self.subTest(permission=permission):
                self.assertEqual([], self.refs({
                    "record_disposition": "HISTORICAL_REFERENCE_ONLY", "allow_rag": permission,
                }))

    def test_waste_record_is_excluded_even_if_reuse_is_true(self):
        self.assertEqual([], self.refs({"record_disposition": "UNUSABLE_WASTE_RECORD", "allow_rag": True}))

    def test_conflicting_alias_is_excluded(self):
        self.assertEqual([], self.refs({
            "record_disposition": "HISTORICAL_REFERENCE_ONLY",
            "disposition": "UNUSABLE_WASTE_RECORD", "allow_rag": True,
        }))

    def test_matching_alias_is_compatible(self):
        self.assertEqual(1, len(self.refs({
            "record_disposition": "HISTORICAL_REFERENCE_ONLY",
            "disposition": "HISTORICAL_REFERENCE_ONLY", "allow_rag": True,
        })))

    def test_malformed_overlay_is_excluded(self):
        for overlay in ([], "HISTORICAL_REFERENCE_ONLY", True):
            with self.subTest(overlay=overlay):
                self.assertEqual([], self.refs(overlay))


class PublicMainPathTests(unittest.TestCase):
    def test_both_actuation_conflicts_block_fill_in_public_main(self):
        for requested, base in (("foot", "Knee"), ("knee", "Foot")):
            with self.subTest(requested=requested, base=base):
                result = run_main_with_synthetic_files(rfq_for(requested), sink_record(base))
                self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                self.assertEqual("REVIEW", result["candidates"][0]["mode"])

    def test_custom_and_exact_positive_controls_in_public_main(self):
        for requested, base, suffix, mode in (
            ("knee", "Knee", "", "EXACT_OR_NEAR"),
            ("foot", "Knee", "customization welcomed", "CUSTOM"),
        ):
            with self.subTest(mode=mode):
                result = run_main_with_synthetic_files(rfq_for(requested, suffix), sink_record(base))
                self.assertEqual("FILL_TO_SUBMIT", result["recommended_action"])
                self.assertEqual(mode, result["candidates"][0]["mode"])

    def test_missing_overlay_file_excludes_price_in_public_main(self):
        result = run_main_with_synthetic_files(rfq_for("knee"), sink_record("Knee"))
        self.assertEqual([], result["candidates"][0]["historical_price_references"])

    def test_unknown_and_missing_permission_exclude_prices_in_public_main(self):
        for overlay in (
            {"product_id": "synthetic-p1", "record_disposition": "BLOCKED_UNCLASSIFIED", "allow_rag": True},
            {"product_id": "synthetic-p1", "record_disposition": "HISTORICAL_REFERENCE_ONLY"},
        ):
            with self.subTest(overlay=overlay):
                result = run_main_with_synthetic_files(rfq_for("knee"), sink_record("Knee"), [overlay])
                self.assertEqual([], result["candidates"][0]["historical_price_references"])

    def test_known_allowed_reference_in_public_main_is_still_only_reference(self):
        overlay = {"product_id": "synthetic-p1", "record_disposition": "HISTORICAL_REFERENCE_ONLY", "allow_rag": True}
        result = run_main_with_synthetic_files(rfq_for("knee"), sink_record("Knee"), [overlay])
        refs = result["candidates"][0]["historical_price_references"]
        self.assertEqual(1, len(refs))
        self.assertEqual("REFERENCE_ONLY", refs[0]["commercial_authority"])
        self.assertEqual("HEURISTIC_SHORTLIST_ONLY", result["status"])


class NegationRegressionTests(unittest.TestCase):
    def test_negated_actuation_is_not_a_positive_requirement(self):
        text = rfq_for("foot", "Not knee operated.")
        positive, excluded, unresolved = rfq_match.requirement_concepts(text)
        self.assertIn("FOOT_OPERATED", positive)
        self.assertNotIn("KNEE_OPERATED", positive)
        self.assertIn("KNEE_OPERATED", excluded)
        self.assertFalse(unresolved)

    def test_negated_material_is_not_a_positive_requirement(self):
        positive, excluded, unresolved = rfq_match.requirement_concepts("SUS304 required, SUS201 not acceptable.")
        self.assertIn("SUS304", positive)
        self.assertNotIn("SUS201", positive)
        self.assertIn("SUS201", excluded)
        self.assertFalse(unresolved)

    def test_explicitly_rejected_actuation_candidate_is_excluded(self):
        text = rfq_for("foot", "Not knee operated. Customization welcomed.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual("REJECTED", candidate["mode"])
        self.assertIn("KNEE_OPERATED", candidate["rejected_requirements"])
        self.assertEqual("SKIP_RFQ", rfq_match.recommended_action(text, candidate))

    def test_explicitly_rejected_material_candidate_is_excluded(self):
        text = rfq_for("foot", "SUS201 not acceptable.")
        record = sink_record("Foot")
        record["attributes"][0]["raw_value"] = "SUS201"
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual("REJECTED", candidate["mode"])
        self.assertEqual("SKIP_RFQ", rfq_match.recommended_action(text, candidate))

    def test_rejected_alternative_does_not_block_valid_candidate(self):
        text = rfq_for("foot", "Not knee operated.")
        candidate = rfq_match.score_candidate(text, sink_record("Foot"))
        self.assertEqual("EXACT_OR_NEAR", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_unknown_negation_requires_review(self):
        for suffix in ("Not necessarily knee operated.", "Not all options should use this configuration.", "Avoid the wrong option.", "SUS201 isn't appropriate."):
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, sink_record("Foot"))
                self.assertTrue(candidate["negation_requires_review"])
                self.assertEqual("REVIEW", candidate["mode"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_optional_negation_is_not_misreported_as_explicit_exclusion(self):
        for suffix in ("SUS201 not required.", "Do not require knee operated."):
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, sink_record("Foot"))
                self.assertEqual([], candidate["rejected_requirements"])
                self.assertTrue(candidate["negation_requires_review"])
                self.assertEqual("REVIEW", candidate["mode"])

    def test_ambiguous_negation_cannot_silently_drop_positive_grade(self):
        text = rfq_for("foot").replace("SUS304", "SUS304 not SUS201")
        candidate = rfq_match.score_candidate(text, sink_record("Foot"))
        self.assertTrue(candidate["negation_requires_review"])
        self.assertEqual("REVIEW", candidate["mode"])

    def test_normal_not_contact_free_words_are_not_negation(self):
        text = rfq_for("foot", "Hands-free non-contact operation.")
        candidate = rfq_match.score_candidate(text, sink_record("Foot"))
        self.assertFalse(candidate["negation_requires_review"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_exact_candidate_with_explicit_no_customization_remains_supported(self):
        text = rfq_for("foot", "No customization.")
        candidate = rfq_match.score_candidate(text, sink_record("Foot"))
        self.assertEqual("EXACT_OR_NEAR", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))


class ScopedCustomizationRegressionTests(unittest.TestCase):
    def test_formal_dimensions_only_permission_does_not_cover_actuation(self):
        text = rfq_for("foot", "Customization accepted for dimensions only; actuation must remain foot-operated.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual(["ACTUATION"], candidate["mismatch_scopes"])
        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_dimensions_only_permission_covers_actual_dimension_difference(self):
        text = rfq_for("foot", "Customization accepted for dimensions only; actuation must remain foot-operated.")
        record = sink_record("Foot")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual(["DIMENSIONS"], candidate["mismatch_scopes"])
        self.assertEqual("CUSTOM", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_dimensions_only_permission_does_not_cover_material(self):
        text = rfq_for("foot", "Customization accepted for dimensions only.")
        record = sink_record("Foot")
        record["attributes"][0]["raw_value"] = "SUS201"
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertIn("MATERIAL", candidate["mismatch_scopes"])

    def test_dimensions_only_permission_does_not_cover_installation(self):
        text = rfq_for("foot", "Customization accepted for dimensions only.")
        record = sink_record("Foot")
        record["name"] = record["name"].replace("Wall Mount", "Floor Standing")
        record["attributes"][1]["raw_value"] = "Floor Standing"
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertIn("INSTALLATION", candidate["mismatch_scopes"])
        self.assertNotEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_every_mismatch_requires_coverage(self):
        text = rfq_for("foot", "Customization accepted for dimensions only.")
        record = sink_record("Knee")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual({"ACTUATION", "DIMENSIONS"}, set(candidate["mismatch_scopes"]))
        self.assertEqual("REVIEW", candidate["mode"])

    def test_specific_actuation_permission_covers_actuation(self):
        text = rfq_for("foot", "Customization accepted for actuation only.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual("CUSTOM", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_custom_size_request_is_not_global_permission(self):
        text = rfq_for("foot", "Custom size required.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])

    def test_unknown_custom_scope_is_not_global_permission(self):
        for suffix in ("Customization accepted for color only.", "Color customization accepted."):
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertEqual([], candidate["customization_scopes"])
                self.assertEqual("REVIEW", candidate["mode"])

    def test_must_remain_restriction_wins_over_generic_permission(self):
        text = rfq_for("foot", "Customization welcomed; actuation must remain foot-operated.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertNotIn("ACTUATION", candidate["customization_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])

    def test_except_actuation_still_permits_dimension_change(self):
        text = rfq_for("foot", "Customization welcomed except actuation.")
        record = sink_record("Foot")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        candidate = rfq_match.score_candidate(text, record)
        self.assertNotIn("ACTUATION", candidate["customization_scopes"])
        self.assertEqual("CUSTOM", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_recommendation_independently_rechecks_scope_even_if_mode_is_custom(self):
        text = rfq_for("foot", "Customization accepted for dimensions only.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        candidate["mode"] = "CUSTOM"
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_scoped_prefix_does_not_include_earlier_product_requirements(self):
        scopes = rfq_match.customization_scopes("SUS304 foot operated sink; dimensions customization accepted.")
        self.assertEqual({"DIMENSIONS"}, scopes)

    def test_prefix_only_restriction_survives_generic_permission_elsewhere(self):
        scopes = rfq_match.customization_scopes("Only dimensions customization accepted; customization welcomed.")
        self.assertEqual({"DIMENSIONS"}, scopes)

    def test_missing_required_material_or_installation_does_not_silently_pass(self):
        for missing_scope in ("MATERIAL", "INSTALLATION"):
            with self.subTest(scope=missing_scope):
                record = sink_record("Foot")
                if missing_scope == "MATERIAL":
                    record["attributes"][0]["raw_value"] = "SUS316"
                else:
                    record["name"] = record["name"].replace("Wall Mount", "Unknown Mount")
                    record["attributes"][1]["raw_value"] = "Unknown"
                candidate = rfq_match.score_candidate(rfq_for("foot"), record)
                self.assertIn(missing_scope, candidate["mismatch_scopes"])
                self.assertEqual("REVIEW", candidate["mode"])


class BowlCountRegressionTests(unittest.TestCase):
    @staticmethod
    def record(bowls):
        record = sink_record("Foot")
        record["attributes"][2]["raw_value"] = f"{bowls} Bowl Rectangular"
        return record

    def test_single_request_double_base_requires_review(self):
        text = rfq_for("foot")
        candidate = rfq_match.score_candidate(text, self.record("Double"))
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertIn("BOWL_COUNT", candidate["mismatch_scopes"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_double_request_single_base_requires_review(self):
        text = rfq_for("foot").replace("single bowl", "double bowl")
        candidate = rfq_match.score_candidate(text, self.record("Single"))
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertIn("BOWL_COUNT", candidate["mismatch_scopes"])

    def test_double_request_double_base_is_exact(self):
        text = rfq_for("foot").replace("single bowl", "double bowl")
        candidate = rfq_match.score_candidate(text, self.record("Double"))
        self.assertEqual("EXACT_OR_NEAR", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_specific_bowl_permission_covers_bowl_change(self):
        text = rfq_for("foot", "Customization accepted for bowl count only.")
        candidate = rfq_match.score_candidate(text, self.record("Double"))
        self.assertEqual("CUSTOM", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_dimensions_permission_does_not_cover_bowl_change(self):
        text = rfq_for("foot", "Customization accepted for dimensions only.")
        candidate = rfq_match.score_candidate(text, self.record("Double"))
        self.assertEqual("REVIEW", candidate["mode"])

    def test_missing_or_ambiguous_bowl_count_requires_review(self):
        for bowl in ("Unknown", "Single Bowl and Double"):
            with self.subTest(bowl=bowl):
                candidate = rfq_match.score_candidate(rfq_for("foot"), self.record(bowl))
                self.assertEqual("REVIEW", candidate["mode"])
                self.assertIn("BOWL_COUNT", candidate["mismatch_scopes"])

    def test_bowl_numeric_tokens_have_boundaries(self):
        for text, expected in (("one bowl", "SINGLE_BOWL"), ("1 bowl", "SINGLE_BOWL"), ("two bowls", "DOUBLE_BOWL"), ("2 bowls", "DOUBLE_BOWL")):
            with self.subTest(text=text):
                self.assertIn(expected, rfq_match.concepts(text))
        self.assertNotIn("DOUBLE_BOWL", rfq_match.concepts("12 bowls"))


class RfqRealCliRegressions(unittest.TestCase):
    def cli(self, text, records):
        with tempfile.TemporaryDirectory(prefix="rfq-real-cli-", dir=r"C:\Windows\Temp") as directory:
            root = Path(directory)
            official, catalog, overlay = [root / name for name in ("official.jsonl", "catalog.jsonl", "overlay.jsonl")]
            official.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            catalog.write_text("", encoding="utf-8")
            overlay.write_text("", encoding="utf-8")
            process = subprocess.run(
                [sys.executable, "-B", str(MODULE_PATH), "--rfq-text", text, "--official-current", str(official), "--catalog-index", str(catalog), "--disposition", str(overlay)],
                cwd=r"C:\Windows\Temp", capture_output=True, text=True, encoding="utf-8", timeout=15,
                env={"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows", "TEMP": r"C:\Windows\Temp", "TMP": r"C:\Windows\Temp"},
            )
            self.assertEqual(0, process.returncode, process.stderr)
            self.assertNotIn("Traceback", process.stderr)
            return json.loads(process.stdout)

    def test_formal_negation_and_scoped_customization_cli(self):
        cases = (
            (rfq_for("foot", "Not knee operated."), sink_record("Knee"), "SKIP_RFQ", None),
            (rfq_for("foot", "Not knee operated."), sink_record("Foot"), "FILL_TO_SUBMIT", "EXACT_OR_NEAR"),
            (rfq_for("foot", "Not necessarily knee operated."), sink_record("Foot"), "PREPARE_AND_REVIEW", "REVIEW"),
            (rfq_for("foot", "Customization accepted for dimensions only; actuation must remain foot-operated."), sink_record("Knee"), "PREPARE_AND_REVIEW", "REVIEW"),
            (rfq_for("foot", "Customization accepted for actuation only."), sink_record("Knee"), "FILL_TO_SUBMIT", "CUSTOM"),
            (rfq_for("foot", "Customization welcomed."), sink_record("Knee"), "FILL_TO_SUBMIT", "CUSTOM"),
        )
        for text, record, action, mode in cases:
            with self.subTest(action=action, mode=mode, text=text):
                result = self.cli(text, [record])
                self.assertEqual(action, result["recommended_action"])
                if mode:
                    self.assertEqual(mode, result["candidates"][0]["mode"])
                else:
                    self.assertEqual([], result["candidates"])
                    self.assertEqual(1, result["rejected_candidate_count"])

    def test_material_exclusion_and_normal_material_cli(self):
        text = rfq_for("foot", "SUS201 not acceptable.")
        invalid = sink_record("Foot")
        invalid["attributes"][0]["raw_value"] = "SUS201"
        self.assertEqual("SKIP_RFQ", self.cli(text, [invalid])["recommended_action"])
        self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [sink_record("Foot")])["recommended_action"])

    def test_rejected_candidate_does_not_displace_eligible_candidate_cli(self):
        rejected = sink_record("Knee")
        rejected["canonical_model"] = "SYNTHETIC0"
        result = self.cli(rfq_for("foot", "Not knee operated."), [rejected, sink_record("Foot")])
        self.assertEqual("SYNTHETIC1", result["recommended_model"])
        self.assertEqual(1, result["rejected_candidate_count"])
        self.assertEqual("FILL_TO_SUBMIT", result["recommended_action"])

    def test_bowl_count_both_directions_and_custom_cli(self):
        for request, base, suffix, action, mode in (
            ("single", "Double", "", "PREPARE_AND_REVIEW", "REVIEW"),
            ("double", "Single", "", "PREPARE_AND_REVIEW", "REVIEW"),
            ("double", "Double", "", "FILL_TO_SUBMIT", "EXACT_OR_NEAR"),
            ("single", "Double", "Customization accepted for bowl count only.", "FILL_TO_SUBMIT", "CUSTOM"),
            ("single", "Double", "Customization accepted for dimensions only.", "PREPARE_AND_REVIEW", "REVIEW"),
        ):
            with self.subTest(request=request, base=base, suffix=suffix):
                text = rfq_for("foot", suffix).replace("single bowl", f"{request} bowl")
                result = self.cli(text, [BowlCountRegressionTests.record(base)])
                self.assertEqual(action, result["recommended_action"])
                self.assertEqual(mode, result["candidates"][0]["mode"])

    def test_dimension_change_positive_cli(self):
        record = sink_record("Foot")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        result = self.cli(rfq_for("foot", "Customization accepted for dimensions only."), [record])
        self.assertEqual("FILL_TO_SUBMIT", result["recommended_action"])
        self.assertEqual("CUSTOM", result["candidates"][0]["mode"])


class CandidatePolarityBoundaryTests(unittest.TestCase):
    @staticmethod
    def uncertain_record(actuation="Knee"):
        record = sink_record(actuation)
        record["name"] = f"{actuation} operated without faucet hand wash sink"
        return record

    def test_formal_candidate_polarity_cannot_bypass_exclusion(self):
        text = rfq_for("foot", "Not knee operated. Customization welcomed.")
        candidate = rfq_match.score_candidate(text, self.uncertain_record())
        self.assertTrue(candidate["candidate_negation_requires_review"])
        self.assertIn("UNRESOLVED_CANDIDATE_POLARITY", candidate["mismatch_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_generic_permission_cannot_cover_candidate_uncertainty(self):
        text = rfq_for("foot", "Customization welcomed.")
        candidate = rfq_match.score_candidate(text, self.uncertain_record())
        self.assertTrue(candidate["candidate_negation_requires_review"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertNotEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_recommendation_independently_checks_candidate_uncertainty(self):
        text = rfq_for("foot", "Not knee operated. Customization welcomed.")
        candidate = rfq_match.score_candidate(text, self.uncertain_record())
        candidate.update(score=99, mode="CUSTOM", mismatches=["actuation"], mismatch_scopes=["ACTUATION"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_clear_separate_candidate_exclusion_keeps_exact_match(self):
        text = rfq_for("foot", "Not knee operated. Customization welcomed.")
        record = sink_record("Foot")
        record["name"] = "Foot operated hand wash sink. Without faucet."
        candidate = rfq_match.score_candidate(text, record)
        self.assertFalse(candidate["candidate_negation_requires_review"])
        self.assertEqual("EXACT_OR_NEAR", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_ordinary_candidate_has_no_polarity_block(self):
        candidate = rfq_match.score_candidate(rfq_for("foot"), sink_record("Foot"))
        self.assertFalse(candidate["candidate_negation_requires_review"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(rfq_for("foot"), candidate))


class PermissionPhraseBoundaryTests(unittest.TestCase):
    def test_formal_only_does_not_collect_later_required_actuation(self):
        text = rfq_for("foot", "Customization accepted for dimensions only, foot operated actuation required.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_prefix_only_scope_stops_before_later_requirement(self):
        scopes = rfq_match.customization_scopes("Customization accepted only for dimensions, foot operated actuation required.")
        self.assertEqual({"DIMENSIONS"}, scopes)

    def test_trailing_only_stops_before_conjoined_requirement(self):
        scopes = rfq_match.customization_scopes("Customization accepted for dimensions only and foot operated actuation required.")
        self.assertEqual({"DIMENSIONS"}, scopes)

    def test_explicit_scope_list_preserves_both_allowed_scopes(self):
        scopes = rfq_match.customization_scopes("Customization accepted for dimensions, actuation only, foot operated actuation required.")
        self.assertEqual({"DIMENSIONS", "ACTUATION"}, scopes)

    def test_later_requirement_does_not_grant_even_without_only(self):
        scopes = rfq_match.customization_scopes("Customization accepted for dimensions, foot operated actuation required.")
        self.assertEqual({"DIMENSIONS"}, scopes)

    def test_recommendation_rechecks_permission_phrase(self):
        text = rfq_for("foot", "Customization accepted for dimensions only, foot operated actuation required.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        candidate["mode"] = "CUSTOM"
        candidate["customization_scopes"] = ["ACTUATION", "DIMENSIONS"]
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_real_dimension_difference_remains_authorized(self):
        text = rfq_for("foot", "Customization accepted for dimensions only, foot operated actuation required.")
        record = sink_record("Foot")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual(["DIMENSIONS"], candidate["mismatch_scopes"])
        self.assertEqual("CUSTOM", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_later_material_requirement_does_not_grant_material_customization(self):
        text = rfq_for("foot", "Customization accepted for dimensions only, SUS304 material required.")
        record = sink_record("Foot")
        record["attributes"][0]["raw_value"] = "SUS201"
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])


class RfqFormalBoundaryCliTests(unittest.TestCase):
    # Reuse the real CLI helper without inheriting the original test methods.
    cli = RfqRealCliRegressions.cli

    def test_formal_candidate_polarity_cli_and_controls(self):
        text = rfq_for("foot", "Not knee operated. Customization welcomed.")
        result = self.cli(text, [CandidatePolarityBoundaryTests.uncertain_record()])
        self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
        self.assertEqual("REVIEW", result["candidates"][0]["mode"])
        self.assertTrue(result["candidates"][0]["candidate_negation_requires_review"])
        self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [sink_record("Foot")])["recommended_action"])
        record = sink_record("Foot")
        record["name"] = "Foot operated hand wash sink. Without faucet."
        self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [record])["recommended_action"])

    def test_reverse_candidate_polarity_cli(self):
        text = rfq_for("knee", "Not foot operated. Customization welcomed.")
        result = self.cli(text, [CandidatePolarityBoundaryTests.uncertain_record("Foot")])
        self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
        self.assertEqual("REVIEW", result["candidates"][0]["mode"])

    def test_formal_permission_phrase_cli_and_controls(self):
        text = rfq_for("foot", "Customization accepted for dimensions only, foot operated actuation required.")
        result = self.cli(text, [sink_record("Knee")])
        self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
        self.assertEqual(["DIMENSIONS"], result["candidates"][0]["customization_scopes"])
        self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [sink_record("Foot")])["recommended_action"])
        record = sink_record("Foot")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [record])["recommended_action"])
        text = rfq_for("foot", "Customization accepted for actuation only, foot operated actuation required.")
        result = self.cli(text, [sink_record("Knee")])
        self.assertEqual("FILL_TO_SUBMIT", result["recommended_action"])
        self.assertEqual("CUSTOM", result["candidates"][0]["mode"])


class PermissionSubjectGrammarTests(unittest.TestCase):
    REQUIREMENTS = (
        "foot operated actuation required",
        "foot operated actuation mandatory",
        "foot operated actuation must remain foot-operated",
        "foot operated actuation shall remain foot-operated",
        "we need foot operated actuation",
        "foot operated actuation needed",
    )

    @classmethod
    def limited_phrases(cls):
        for qualifier in ("only for dimensions", "for dimensions only"):
            for delimiter in (" and ", ", "):
                for requirement in cls.REQUIREMENTS:
                    yield f"Customization accepted {qualifier}{delimiter}{requirement}."

    def test_formal_prefix_only_conjoined_requirement_is_not_permission(self):
        text = rfq_for("foot", "Customization accepted only for dimensions and foot operated actuation required.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_only_position_delimiter_requirement_terminator_matrix(self):
        for suffix in self.limited_phrases():
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                self.assertEqual("REVIEW", candidate["mode"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_requirement_matrix_keeps_actual_dimension_change_allowed(self):
        record = sink_record("Foot")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        for suffix in self.limited_phrases():
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, record)
                self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                self.assertEqual(["DIMENSIONS"], candidate["mismatch_scopes"])
                self.assertEqual("CUSTOM", candidate["mode"])
                self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_explicit_multi_scope_permissions_remain_supported(self):
        record = sink_record("Knee")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        suffixes = (
            "Dimensions and actuation customization accepted.",
            "Only dimensions and actuation customization accepted.",
            "Dimensions, actuation customization accepted.",
            "Customization accepted for dimensions and actuation.",
            "Customization accepted only for dimensions and actuation.",
            "Customization accepted for dimensions and actuation only.",
            "Customization accepted for dimensions, actuation only.",
        )
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, record)
                self.assertEqual({"DIMENSIONS", "ACTUATION"}, set(candidate["customization_scopes"]))
                self.assertEqual("CUSTOM", candidate["mode"])
                self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_unknown_or_ambiguous_scope_qualification_is_not_global(self):
        for suffix in (
            "Customization accepted only for dimensions and color.",
            "Customization accepted only for dimensions if needed.",
            "Customization accepted only for dimensions or actuation.",
            "Customization accepted only for dimensions actuation.",
            "Color and dimensions customization accepted.",
            "Dimensions or actuation customization accepted.",
            "Customization welcomed subject to design approval.",
        ):
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertEqual([], candidate["customization_scopes"])
                self.assertEqual("REVIEW", candidate["mode"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_prefix_list_does_not_include_earlier_product_facts(self):
        scopes = rfq_match.customization_scopes("SUS304 foot operated hand wash sink, dimensions and actuation customization accepted.")
        self.assertEqual({"DIMENSIONS", "ACTUATION"}, scopes)

    def test_recommendation_reparses_permission_subject_independently(self):
        text = rfq_for("foot", "Customization accepted only for dimensions and foot operated actuation required.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        candidate.update(mode="CUSTOM", score=99, customization_scopes=["ACTUATION", "DIMENSIONS"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_later_other_requirement_does_not_expand_scope(self):
        for requirement, field, value in (
            ("SUS304 material required", 0, "SUS201"),
            ("wall mount installation mandatory", 1, "Floor Standing"),
        ):
            with self.subTest(requirement=requirement):
                record = sink_record("Foot")
                record["attributes"][field]["raw_value"] = value
                if field == 1:
                    record["name"] = record["name"].replace("Wall Mount", "Floor Standing")
                text = rfq_for("foot", f"Customization accepted only for dimensions and {requirement}.")
                candidate = rfq_match.score_candidate(text, record)
                self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                self.assertEqual("REVIEW", candidate["mode"])

    def test_need_verb_variants_terminate_requirement_branch(self):
        for requirement in ("buyer needs foot operated actuation", "require foot operated actuation", "buyer requires foot operated actuation"):
            with self.subTest(requirement=requirement):
                scopes = rfq_match.customization_scopes(f"Customization accepted only for dimensions and {requirement}.")
                self.assertEqual({"DIMENSIONS"}, scopes)

    def test_unknown_qualification_requires_review_even_for_exact_base(self):
        text = rfq_for("foot", "Customization accepted only for dimensions if needed.")
        candidate = rfq_match.score_candidate(text, sink_record("Foot"))
        self.assertTrue(candidate["customization_requires_review"])
        self.assertIn("UNRESOLVED_CUSTOMIZATION_SCOPE", candidate["mismatch_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_recommendation_independently_blocks_unknown_qualification(self):
        text = rfq_for("foot", "Customization accepted only for dimensions if needed.")
        candidate = rfq_match.score_candidate(text, sink_record("Foot"))
        candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], customization_requires_review=False)
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))


class RfqPermissionSubjectCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli

    def test_formal_prefix_only_cli_and_exact_dimension_multi_scope_controls(self):
        text = rfq_for("foot", "Customization accepted only for dimensions and foot operated actuation required.")
        self.assertEqual("PREPARE_AND_REVIEW", self.cli(text, [sink_record("Knee")])["recommended_action"])
        self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [sink_record("Foot")])["recommended_action"])
        record = sink_record("Foot")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [record])["recommended_action"])
        record = sink_record("Knee")
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        text = rfq_for("foot", "Dimensions and actuation customization accepted.")
        self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [record])["recommended_action"])

    def test_requirement_terminators_cli_keep_dimensions_and_block_actuation(self):
        for requirement in ("foot operated actuation mandatory", "foot operated actuation must remain foot-operated", "we need foot operated actuation", "foot operated actuation needed"):
            with self.subTest(requirement=requirement):
                text = rfq_for("foot", f"Customization accepted only for dimensions and {requirement}.")
                self.assertEqual("PREPARE_AND_REVIEW", self.cli(text, [sink_record("Knee")])["recommended_action"])
                record = sink_record("Foot")
                record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
                self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [record])["recommended_action"])

    def test_unknown_qualification_and_multi_scope_list_cli_controls(self):
        cases = (
            ("Customization accepted only for dimensions and color.", "PREPARE_AND_REVIEW"),
            ("Dimensions and actuation customization accepted.", "FILL_TO_SUBMIT"),
            ("Customization accepted only for dimensions and actuation.", "FILL_TO_SUBMIT"),
            ("Customization accepted only for dimensions, foot operated actuation required.", "PREPARE_AND_REVIEW"),
        )
        for suffix, expected in cases:
            with self.subTest(suffix=suffix):
                self.assertEqual(expected, self.cli(rfq_for("foot", suffix), [sink_record("Knee")])["recommended_action"])


class ConditionalPermissionGrammarTests(unittest.TestCase):
    CONDITIONS = (
        "subject to design approval",
        "provided drawings are approved",
        "if drawings are approved",
        "unless drawings are rejected",
    )
    REQUIREMENTS = PermissionSubjectGrammarTests.REQUIREMENTS
    PERMISSIONS = (
        "Only dimensions customization accepted",
        "Dimensions customization accepted",
        "Customization accepted only for dimensions",
        "Customization accepted for dimensions only",
    )

    @staticmethod
    def different_size_record(actuation="Foot"):
        record = sink_record(actuation)
        record["attributes"][3]["raw_value"] = "600 x 400 x 395 mm"
        return record

    def test_formal_trailing_only_preserves_approval_condition(self):
        text = rfq_for("foot", "Customization accepted for dimensions only subject to design approval.")
        candidate = rfq_match.score_candidate(text, self.different_size_record())
        self.assertEqual([], candidate["customization_scopes"])
        self.assertTrue(candidate["customization_requires_review"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_prefix_and_suffix_only_conditions_remain_unknown(self):
        for qualifier in ("only for dimensions", "for dimensions only"):
            for condition in self.CONDITIONS:
                with self.subTest(qualifier=qualifier, condition=condition):
                    text = rfq_for("foot", f"Customization accepted {qualifier} {condition}.")
                    candidate = rfq_match.score_candidate(text, self.different_size_record())
                    self.assertTrue(candidate["customization_requires_review"])
                    self.assertEqual("REVIEW", candidate["mode"])
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_unknown_requirement_qualification_cannot_be_discarded(self):
        branches = (
            "drawings approval required",
            "foot operated actuation required if drawings are approved",
            "foot operated actuation required provided drawings are approved",
            "foot operated actuation must remain foot-operated subject to approval",
            "foot operated actuation required unless drawings are rejected",
        )
        for delimiter in (" and ", ", ", " & "):
            for branch in branches:
                with self.subTest(delimiter=delimiter, branch=branch):
                    self.assertFalse(rfq_match.explicit_requirement_branch(branch))
                    text = rfq_for("foot", f"Only dimensions customization accepted{delimiter}{branch}.")
                    candidate = rfq_match.score_candidate(text, self.different_size_record())
                    self.assertTrue(candidate["customization_requires_review"])
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_leading_requirement_boundaries_are_recognized(self):
        for delimiter in ("and ", ", ", "& "):
            for requirement in self.REQUIREMENTS:
                with self.subTest(delimiter=delimiter, requirement=requirement):
                    self.assertEqual(("", requirement), rfq_match.split_permission_requirement(delimiter + requirement))

    def test_known_requirement_branch_keeps_dimension_customization(self):
        record = self.different_size_record()
        for permission in self.PERMISSIONS:
            for delimiter in (" and ", ", ", " & "):
                for requirement in self.REQUIREMENTS:
                    with self.subTest(permission=permission, delimiter=delimiter, requirement=requirement):
                        text = rfq_for("foot", f"{permission}{delimiter}{requirement}.")
                        candidate = rfq_match.score_candidate(text, record)
                        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                        self.assertEqual(["DIMENSIONS"], candidate["mismatch_scopes"])
                        self.assertFalse(candidate["customization_requires_review"])
                        self.assertEqual("CUSTOM", candidate["mode"])
                        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_known_requirement_branch_does_not_authorize_actuation(self):
        for permission in self.PERMISSIONS:
            for delimiter in (" and ", ", ", " & "):
                for requirement in self.REQUIREMENTS:
                    with self.subTest(permission=permission, delimiter=delimiter, requirement=requirement):
                        text = rfq_for("foot", f"{permission}{delimiter}{requirement}.")
                        candidate = rfq_match.score_candidate(text, self.different_size_record("Knee"))
                        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                        self.assertIn("ACTUATION", candidate["mismatch_scopes"])
                        self.assertEqual("REVIEW", candidate["mode"])
                        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_explicit_multiscope_prefix_with_separate_requirement_remains_supported(self):
        text = rfq_for("foot", "Only dimensions and actuation customization accepted and foot operated actuation required.")
        candidate = rfq_match.score_candidate(text, self.different_size_record("Knee"))
        self.assertEqual({"DIMENSIONS", "ACTUATION"}, set(candidate["customization_scopes"]))
        self.assertEqual("CUSTOM", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_recommendation_independently_blocks_trailing_condition(self):
        text = rfq_for("foot", "Customization accepted for dimensions only if drawings are approved.")
        candidate = rfq_match.score_candidate(text, self.different_size_record())
        candidate.update(mode="CUSTOM", score=99, mismatches=["dimensions"], mismatch_scopes=["DIMENSIONS"], customization_scopes=["DIMENSIONS"], customization_requires_review=False)
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))


class RfqConditionalPermissionCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli

    def test_trailing_only_conditions_cli(self):
        for condition in ConditionalPermissionGrammarTests.CONDITIONS:
            for actuation in ("Foot", "Knee"):
                with self.subTest(condition=condition, actuation=actuation):
                    text = rfq_for("foot", f"Customization accepted for dimensions only {condition}.")
                    result = self.cli(text, [ConditionalPermissionGrammarTests.different_size_record(actuation)])
                    self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                    self.assertTrue(result["candidates"][0]["customization_requires_review"])

    def test_leading_conjunction_cli_compatibility_and_actuation_negative(self):
        suffixes = (
            "Only dimensions customization accepted and foot operated actuation required.",
            "Only dimensions customization accepted, foot operated actuation mandatory.",
            "Only dimensions customization accepted & foot operated actuation required.",
            "Only dimensions customization accepted and foot operated actuation must remain foot-operated.",
            "Only dimensions customization accepted and foot operated actuation shall remain foot-operated.",
            "Only dimensions customization accepted and we need foot operated actuation.",
            "Customization accepted for dimensions only and foot operated actuation required.",
            "Customization accepted only for dimensions & foot operated actuation required.",
        )
        for suffix in suffixes:
            for actuation, expected in (("Foot", "FILL_TO_SUBMIT"), ("Knee", "PREPARE_AND_REVIEW")):
                with self.subTest(suffix=suffix, actuation=actuation):
                    result = self.cli(rfq_for("foot", suffix), [ConditionalPermissionGrammarTests.different_size_record(actuation)])
                    self.assertEqual(expected, result["recommended_action"])
                    self.assertEqual(["DIMENSIONS"], result["candidates"][0]["customization_scopes"])

    def test_exact_unknown_and_unconditional_custom_cli_controls(self):
        for suffix in (
            "Customization accepted for dimensions only if drawings are approved.",
            "Only dimensions customization accepted and foot operated actuation required if drawings are approved.",
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual("PREPARE_AND_REVIEW", self.cli(rfq_for("foot", suffix), [sink_record("Foot")])["recommended_action"])
        for suffix in ("Customization accepted for dimensions only.", "Customization accepted only for dimensions."):
            with self.subTest(suffix=suffix):
                self.assertEqual("FILL_TO_SUBMIT", self.cli(rfq_for("foot", suffix), [ConditionalPermissionGrammarTests.different_size_record()])["recommended_action"])
        suffix = "Only dimensions and actuation customization accepted and foot operated actuation required."
        self.assertEqual("FILL_TO_SUBMIT", self.cli(rfq_for("foot", suffix), [ConditionalPermissionGrammarTests.different_size_record("Knee")])["recommended_action"])


class FullPrefixPermissionTests(unittest.TestCase):
    PREFIXES = ("For dimensions only,", "Only dimensions,", "Dimensions only,")
    CONDITIONS = (
        "Subject to design approval, dimensions",
        "For dimensions only subject to approval,",
        "For dimensions only if drawings are approved,",
        "Provided drawings are approved, dimensions",
        "Unless drawings are rejected, dimensions",
        "For dimensions only, subject to design approval,",
    )

    def test_formal_comma_prefix_never_expands_dimension_permission(self):
        text = rfq_for("foot", "For dimensions only, customization accepted and foot operated actuation required.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_full_scope_prefix_with_finite_punctuation_is_consumed(self):
        for prefix in self.PREFIXES + ("For dimensions only:", "On dimensions only,"):
            with self.subTest(prefix=prefix):
                self.assertEqual(({"DIMENSIONS"}, True), rfq_match.customization_prefix_scope(prefix.lower()))
        self.assertEqual(({"DIMENSIONS", "ACTUATION"}, False), rfq_match.customization_prefix_scope("for dimensions and actuation,"))

    def test_comma_prefix_requirement_matrix_keeps_size_positive(self):
        for prefix in self.PREFIXES:
            for delimiter in (" and ", ", ", " & "):
                for requirement in PermissionSubjectGrammarTests.REQUIREMENTS:
                    with self.subTest(prefix=prefix, delimiter=delimiter, requirement=requirement):
                        text = rfq_for("foot", f"{prefix} customization accepted{delimiter}{requirement}.")
                        candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
                        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                        self.assertEqual(["DIMENSIONS"], candidate["mismatch_scopes"])
                        self.assertEqual("CUSTOM", candidate["mode"])
                        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_comma_prefix_requirement_matrix_blocks_actuation_negative(self):
        for prefix in self.PREFIXES:
            for delimiter in (" and ", ", ", " & "):
                for requirement in PermissionSubjectGrammarTests.REQUIREMENTS:
                    with self.subTest(prefix=prefix, delimiter=delimiter, requirement=requirement):
                        text = rfq_for("foot", f"{prefix} customization accepted{delimiter}{requirement}.")
                        candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record("Knee"))
                        self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                        self.assertIn("ACTUATION", candidate["mismatch_scopes"])
                        self.assertEqual("REVIEW", candidate["mode"])
                        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_complete_prefix_approval_conditions_cannot_be_dropped(self):
        for prefix in self.CONDITIONS:
            with self.subTest(prefix=prefix):
                text = rfq_for("foot", f"{prefix} customization accepted and foot operated actuation required.")
                candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
                self.assertEqual([], candidate["customization_scopes"])
                self.assertTrue(candidate["customization_requires_review"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_unconsumed_scope_only_or_unknown_prefix_never_is_generic(self):
        for prefix in (
            "Color only,", "For dimensions only with approval,", "For dimensions or actuation,",
            "Color and dimensions,", "Only,", "Dimensions only, unknown,"):
            with self.subTest(prefix=prefix):
                self.assertEqual((set(), True), rfq_match.customization_prefix_scope(prefix.lower()))

    def test_product_fact_preface_requires_complete_consumption(self):
        facts = "SUS304 foot operated hand wash sink, "
        for subject in ("dimensions", "for dimensions only", "dimensions and actuation"):
            with self.subTest(subject=subject):
                text = f"{facts}{subject}, customization accepted."
                expected = {"DIMENSIONS", "ACTUATION"} if "actuation" in subject else {"DIMENSIONS"}
                self.assertEqual(expected, rfq_match.customization_scopes(text))
        self.assertEqual(set(), rfq_match.customization_scopes(f"Subject to approval, {facts}dimensions, customization accepted."))
        self.assertEqual(set(), rfq_match.customization_scopes(f"{facts}subject to approval, dimensions, customization accepted."))

    def test_recommendation_rechecks_full_prefix_despite_injected_scopes(self):
        for suffix in (
            "For dimensions only, customization accepted and foot operated actuation required.",
            "Subject to design approval, dimensions customization accepted and foot operated actuation required.",
        ):
            with self.subTest(suffix=suffix):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record("Knee"))
                candidate.update(mode="CUSTOM", score=99, mismatches=["actuation"], mismatch_scopes=["ACTUATION"], customization_scopes=sorted(rfq_match.CUSTOM_SCOPES), customization_requires_review=False)
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_valid_multiscope_comma_prefix_remains_supported(self):
        text = rfq_for("foot", "For dimensions and actuation, customization accepted and foot operated actuation required.")
        candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record("Knee"))
        self.assertEqual({"DIMENSIONS", "ACTUATION"}, set(candidate["customization_scopes"]))
        self.assertEqual("CUSTOM", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))


class RfqFullPrefixPermissionCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli

    def test_comma_prefix_real_cli_positive_negative_matrix(self):
        for prefix in FullPrefixPermissionTests.PREFIXES:
            for delimiter in (" and ", ", ", " & "):
                for actuation, expected in (("Foot", "FILL_TO_SUBMIT"), ("Knee", "PREPARE_AND_REVIEW")):
                    with self.subTest(prefix=prefix, delimiter=delimiter, actuation=actuation):
                        suffix = f"{prefix} customization accepted{delimiter}foot operated actuation required."
                        result = self.cli(rfq_for("foot", suffix), [ConditionalPermissionGrammarTests.different_size_record(actuation)])
                        self.assertEqual(expected, result["recommended_action"])
                        self.assertEqual(["DIMENSIONS"], result["candidates"][0]["customization_scopes"])

    def test_full_prefix_conditions_real_cli(self):
        for prefix in FullPrefixPermissionTests.CONDITIONS:
            with self.subTest(prefix=prefix):
                suffix = f"{prefix} customization accepted and foot operated actuation required."
                result = self.cli(rfq_for("foot", suffix), [ConditionalPermissionGrammarTests.different_size_record()])
                self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                self.assertTrue(result["candidates"][0]["customization_requires_review"])

    def test_context_generic_and_multiscope_real_cli_controls(self):
        texts = (
            ("SUS304 foot operated hand wash sink, dimensions, customization accepted.", "Foot", "FILL_TO_SUBMIT", ["DIMENSIONS"]),
            ("SUS304 foot operated hand wash sink, dimensions, customization accepted.", "Knee", "PREPARE_AND_REVIEW", ["DIMENSIONS"]),
            (rfq_for("foot", "For dimensions and actuation, customization accepted and foot operated actuation required."), "Knee", "FILL_TO_SUBMIT", ["ACTUATION", "DIMENSIONS"]),
            (rfq_for("foot", "Any customization welcomed."), "Knee", "FILL_TO_SUBMIT", sorted(rfq_match.CUSTOM_SCOPES)),
        )
        for text, actuation, action, scopes in texts:
            with self.subTest(text=text, actuation=actuation):
                result = self.cli(text, [ConditionalPermissionGrammarTests.different_size_record(actuation)])
                self.assertEqual(action, result["recommended_action"])
                self.assertEqual(scopes, result["candidates"][0]["customization_scopes"])


class ExceptionTailQualificationTests(unittest.TestCase):
    CONDITIONS = (
        "subject to design approval",
        "provided drawings are approved",
        "unless drawings are rejected",
        "if drawings are approved",
    )
    PERMISSIONS = (
        "Customization accepted for dimensions only except actuation",
        "Customization welcomed except actuation",
        "Customization accepted for dimensions and actuation except material",
        "Dimensions customization accepted except installation",
    )
    PURE_EXCEPTIONS = (
        ("Customization welcomed except actuation.", rfq_match.CUSTOM_SCOPES - {"ACTUATION"}),
        ("Customization welcomed except material and actuation.", rfq_match.CUSTOM_SCOPES - {"MATERIAL", "ACTUATION"}),
        ("Customization accepted for dimensions only except actuation.", {"DIMENSIONS"}),
        ("Customization accepted for dimensions and actuation except material.", {"DIMENSIONS", "ACTUATION"}),
    )

    def test_formal_exception_tail_approval_is_review_only(self):
        text = rfq_for("foot", "Customization accepted for dimensions only except actuation, subject to design approval.")
        candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
        self.assertEqual([], candidate["customization_scopes"])
        self.assertTrue(candidate["customization_requires_review"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_complete_exception_tail_condition_matrix(self):
        for permission in self.PERMISSIONS:
            for condition in self.CONDITIONS:
                for delimiter in (", ", " and ", " "):
                    for actuation in ("Foot", "Knee"):
                        with self.subTest(permission=permission, condition=condition, delimiter=delimiter, actuation=actuation):
                            text = rfq_for("foot", permission + delimiter + condition + ".")
                            candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record(actuation))
                            self.assertEqual([], candidate["customization_scopes"])
                            self.assertTrue(candidate["customization_requires_review"])
                            self.assertEqual("REVIEW", candidate["mode"])
                            self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_pure_exception_lists_preserve_other_permission(self):
        for suffix, scopes in self.PURE_EXCEPTIONS:
            for actuation in ("Foot", "Knee"):
                with self.subTest(suffix=suffix, actuation=actuation):
                    text = rfq_for("foot", suffix)
                    candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record(actuation))
                    expected_custom = actuation == "Foot" or "ACTUATION" in scopes
                    self.assertEqual(scopes, set(candidate["customization_scopes"]))
                    self.assertFalse(candidate["customization_requires_review"])
                    self.assertEqual("CUSTOM" if expected_custom else "REVIEW", candidate["mode"])
                    self.assertEqual("FILL_TO_SUBMIT" if expected_custom else "PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_unknown_exception_remainder_is_not_permission(self):
        for remainder in ("actuation, color", "actuation or installation", "unknown", "actuation unless", "actuation, foot operated actuation required if approved"):
            with self.subTest(remainder=remainder):
                text = rfq_for("foot", "Customization accepted for dimensions only except " + remainder + ".")
                candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
                self.assertEqual([], candidate["customization_scopes"])
                self.assertTrue(candidate["customization_requires_review"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_known_requirement_after_exception_is_not_an_exception_scope(self):
        for delimiter in (", ", " and ", " & "):
            for actuation in ("Foot", "Knee"):
                with self.subTest(delimiter=delimiter, actuation=actuation):
                    text = rfq_for("foot", "Customization accepted for dimensions and actuation except material" + delimiter + "foot operated actuation required.")
                    candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record(actuation))
                    self.assertEqual({"DIMENSIONS", "ACTUATION"}, set(candidate["customization_scopes"]))
                    self.assertFalse(candidate["customization_requires_review"])
                    # Existing negation grammar treats an unpunctuated except
                    # near a later fact as ambiguous; do not weaken that gate.
                    ambiguous_negation = delimiter != ", "
                    self.assertEqual(ambiguous_negation, candidate["negation_requires_review"])
                    self.assertEqual("REVIEW" if ambiguous_negation else "CUSTOM", candidate["mode"])
                    self.assertEqual("PREPARE_AND_REVIEW" if ambiguous_negation else "FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_recommendation_rechecks_complete_exception_tail(self):
        text = rfq_for("foot", "Customization accepted for dimensions only except actuation, subject to design approval.")
        candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
        candidate.update(mode="CUSTOM", score=99, mismatches=["dimensions"], mismatch_scopes=["DIMENSIONS"], customization_scopes=["DIMENSIONS"], customization_requires_review=False)
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))


class RfqExceptionTailCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli

    def test_exception_conditions_real_cli(self):
        for condition in ExceptionTailQualificationTests.CONDITIONS:
            for delimiter in (", ", " "):
                for actuation in ("Foot", "Knee"):
                    with self.subTest(condition=condition, delimiter=delimiter, actuation=actuation):
                        text = rfq_for("foot", "Customization accepted for dimensions only except actuation" + delimiter + condition + ".")
                        result = self.cli(text, [ConditionalPermissionGrammarTests.different_size_record(actuation)])
                        self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                        self.assertEqual([], result["candidates"][0]["customization_scopes"])
                        self.assertTrue(result["candidates"][0]["customization_requires_review"])
                        self.assertEqual("REVIEW", result["candidates"][0]["mode"])

    def test_pure_exception_real_cli_controls(self):
        for suffix, scopes in ExceptionTailQualificationTests.PURE_EXCEPTIONS:
            for actuation in ("Foot", "Knee"):
                with self.subTest(suffix=suffix, actuation=actuation):
                    result = self.cli(rfq_for("foot", suffix), [ConditionalPermissionGrammarTests.different_size_record(actuation)])
                    expected_custom = actuation == "Foot" or "ACTUATION" in scopes
                    self.assertEqual("FILL_TO_SUBMIT" if expected_custom else "PREPARE_AND_REVIEW", result["recommended_action"])
                    self.assertEqual(scopes, set(result["candidates"][0]["customization_scopes"]))
                    self.assertFalse(result["candidates"][0]["customization_requires_review"])

    def test_exact_base_conditions_and_known_requirement_real_cli(self):
        for condition in ExceptionTailQualificationTests.CONDITIONS:
            with self.subTest(condition=condition):
                text = rfq_for("foot", "Customization accepted for dimensions only except actuation, " + condition + ".")
                result = self.cli(text, [sink_record("Foot")])
                self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                self.assertTrue(result["candidates"][0]["customization_requires_review"])
        for actuation in ("Foot", "Knee"):
            with self.subTest(known_requirement=True, actuation=actuation):
                text = rfq_for("foot", "Customization accepted for dimensions and actuation except material, foot operated actuation required.")
                result = self.cli(text, [ConditionalPermissionGrammarTests.different_size_record(actuation)])
                self.assertEqual("FILL_TO_SUBMIT", result["recommended_action"])
                self.assertEqual({"DIMENSIONS", "ACTUATION"}, set(result["candidates"][0]["customization_scopes"]))


class MeasurementPermissionCompatibilityTests(unittest.TestCase):
    CONDITIONS = (
        "subject to design approval", "provided drawings are approved",
        "unless drawings are rejected", "if drawings are approved",
    )

    @staticmethod
    def sizes(separator):
        return (
            f"500 {separator} 400 {separator} 395 mm",
            f"500.5{separator}400{separator}395 mm",
            f"50.05{separator}40{separator}39.5 cm",
            f"500.5{separator}400{separator}395",
        )

    @staticmethod
    def facts(size):
        return "SUS304 foot operated single bowl hand wash sink wall mount " + size

    def test_formal_x_requirement_keeps_explicit_multiscope_permission(self):
        text = rfq_for("foot", "Dimensions and actuation customization accepted and dimensions 500 x 400 x 395 mm required.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual({"DIMENSIONS", "ACTUATION"}, set(candidate["customization_scopes"]))
        self.assertFalse(candidate["customization_requires_review"])
        self.assertEqual("CUSTOM", candidate["mode"])
        self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_measurement_requirement_separator_decimal_unit_matrix(self):
        for separator in ("x", "*", "×"):
            for size in self.sizes(separator):
                with self.subTest(size=size):
                    self.assertTrue(rfq_match.explicit_requirement_branch("dimensions " + size + " required"))
                    text = rfq_for("foot", "Dimensions and actuation customization accepted and dimensions " + size + " required.")
                    candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                    self.assertEqual({"DIMENSIONS", "ACTUATION"}, set(candidate["customization_scopes"]))
                    self.assertFalse(candidate["customization_requires_review"])
                    self.assertEqual("CUSTOM", candidate["mode"])
                    self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_decimal_product_preface_is_not_truncated(self):
        for separator in ("x", "*", "×"):
            for size in self.sizes(separator):
                with self.subTest(size=size):
                    text = self.facts(size) + ", customization welcomed."
                    candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                    self.assertEqual(rfq_match.CUSTOM_SCOPES, set(candidate["customization_scopes"]))
                    self.assertFalse(candidate["customization_requires_review"])
                    self.assertEqual("CUSTOM", candidate["mode"])
                    self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_existing_dimension_conversion_is_unchanged(self):
        for separator in ("x", "*", "×"):
            for value, expected in (
                (f"500.5{separator}400{separator}395 mm", [500.5, 400, 395]),
                (f"50.05{separator}40{separator}39.5 cm", [500.5, 400, 395]),
                (f"500.5{separator}400{separator}395", [500.5, 400, 395]),
                (f"50.05{separator}40 cm", [500.5, 400]),
            ):
                with self.subTest(value=value):
                    self.assertEqual(expected, rfq_match.dimensions(value))

    def test_unknown_conditions_survive_measurement_consumption_and_decimal_split(self):
        for condition in self.CONDITIONS:
            for size in self.sizes("x"):
                texts = (
                    rfq_for("foot", "Dimensions and actuation customization accepted and " + condition + " dimensions " + size + " required."),
                    rfq_for("foot", "Dimensions and actuation customization accepted and dimensions " + size + " required " + condition + "."),
                    condition + ", " + self.facts(size) + ", customization welcomed.",
                    self.facts(size) + " " + condition + ", customization welcomed.",
                )
                for text in texts:
                    with self.subTest(condition=condition, text=text):
                        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                        self.assertTrue(candidate["customization_requires_review"])
                        self.assertEqual("REVIEW", candidate["mode"])
                        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_measurement_does_not_whitelist_unknown_words_or_structural_conflicts(self):
        for branch in ("dimensions x required", "dimensions 500 x 400 unknown required", "dimensions 500 x 400 feet required", "dimensions 500 x 400 x required"):
            with self.subTest(branch=branch):
                self.assertFalse(rfq_match.explicit_requirement_branch(branch))
        suffix = "Dimensions customization accepted and dimensions 500.5 x 400 x 395 mm required."
        for record in (sink_record("Knee"), sink_record("Foot")):
            if record["name"].find("Foot") >= 0:
                record["attributes"][2]["raw_value"] = "Double Bowl Rectangular"
            with self.subTest(record=record):
                text = rfq_for("foot", suffix)
                candidate = rfq_match.score_candidate(text, record)
                self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                self.assertEqual("REVIEW", candidate["mode"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))
        record = sink_record("Knee")
        record["attributes"][0]["raw_value"] = "SUS201"
        text = self.facts("500.5 x 400 x 395 mm") + ", customization welcomed. SUS201 not acceptable."
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual("REJECTED", candidate["mode"])
        self.assertEqual("SKIP_RFQ", rfq_match.recommended_action(text, candidate))


class RfqMeasurementCompatibilityCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli

    def test_measurement_requirement_and_decimal_preface_real_cli(self):
        for separator in ("x", "*", "×"):
            for size in MeasurementPermissionCompatibilityTests.sizes(separator):
                for text in (
                    rfq_for("foot", "Dimensions and actuation customization accepted and dimensions " + size + " required."),
                    MeasurementPermissionCompatibilityTests.facts(size) + ", customization welcomed.",
                ):
                    with self.subTest(text=text):
                        result = self.cli(text, [sink_record("Knee")])
                        self.assertEqual("FILL_TO_SUBMIT", result["recommended_action"])
                        self.assertEqual("CUSTOM", result["candidates"][0]["mode"])
                        self.assertFalse(result["candidates"][0]["customization_requires_review"])

    def test_unknown_leading_trailing_conditions_real_cli(self):
        for condition in MeasurementPermissionCompatibilityTests.CONDITIONS:
            texts = (
                rfq_for("foot", "Dimensions and actuation customization accepted and " + condition + " dimensions 500.5 x 400 x 395 mm required."),
                rfq_for("foot", "Dimensions and actuation customization accepted and dimensions 500.5 x 400 x 395 mm required " + condition + "."),
                condition + ", " + MeasurementPermissionCompatibilityTests.facts("500.5*400*395 mm") + ", customization welcomed.",
                MeasurementPermissionCompatibilityTests.facts("500.5*400*395 mm") + " " + condition + ", customization welcomed.",
            )
            for text in texts:
                with self.subTest(text=text):
                    result = self.cli(text, [sink_record("Knee")])
                    self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                    self.assertEqual("REVIEW", result["candidates"][0]["mode"])
                    self.assertTrue(result["candidates"][0]["customization_requires_review"])

    def test_measurement_permission_still_blocks_hard_conflicts_real_cli(self):
        text = rfq_for("foot", "Dimensions customization accepted and dimensions 500.5 x 400 x 395 mm required.")
        self.assertEqual("PREPARE_AND_REVIEW", self.cli(text, [sink_record("Knee")])["recommended_action"])
        record = sink_record("Foot")
        record["attributes"][2]["raw_value"] = "Double Bowl Rectangular"
        self.assertEqual("PREPARE_AND_REVIEW", self.cli(text, [record])["recommended_action"])
        record = sink_record("Knee")
        record["attributes"][0]["raw_value"] = "SUS201"
        text = MeasurementPermissionCompatibilityTests.facts("500.5 x 400 x 395 mm") + ", customization welcomed. SUS201 not acceptable."
        self.assertEqual("SKIP_RFQ", self.cli(text, [record])["recommended_action"])


class DetachedQualificationContextTests(unittest.TestCase):
    CONDITIONS = (
        "subject to design approval", "provided drawings are approved",
        "unless drawings are rejected", "if drawings are approved",
        "design approval required",
    )
    SEPARATORS = ("; ", ". ", "\n", "! ", "? ")
    PERMISSION = "Customization accepted for dimensions only except actuation"

    def test_formal_semicolon_condition_requires_review(self):
        text = rfq_for("foot", self.PERMISSION + "; subject to design approval.")
        candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
        self.assertEqual([], candidate["customization_scopes"])
        self.assertTrue(candidate["customization_requires_review"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_trailing_qualification_context_matrix(self):
        for permission in (self.PERMISSION, "Customization welcomed", "Dimensions and actuation customization accepted"):
            for condition in self.CONDITIONS:
                for separator in self.SEPARATORS:
                    for actuation in ("Foot", "Knee"):
                        with self.subTest(permission=permission, condition=condition, separator=separator, actuation=actuation):
                            text = rfq_for("foot", permission + separator + condition + ".")
                            candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record(actuation))
                            self.assertEqual([], candidate["customization_scopes"])
                            self.assertTrue(candidate["customization_requires_review"])
                            self.assertEqual("REVIEW", candidate["mode"])
                            self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_leading_and_known_fact_bridge_conditions_remain_pending(self):
        for condition in self.CONDITIONS:
            for separator in self.SEPARATORS:
                for suffix in (
                    condition + separator + self.PERMISSION + ".",
                    self.PERMISSION + separator + "SUS304 material required" + separator + condition + ".",
                ):
                    with self.subTest(suffix=suffix):
                        text = rfq_for("foot", suffix)
                        candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
                        self.assertEqual([], candidate["customization_scopes"])
                        self.assertTrue(candidate["customization_requires_review"])
                        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_independent_facts_requirements_global_permission_and_decimals(self):
        for separator in self.SEPARATORS:
            for following in ("SUS304 foot operated hand wash sink wall mount", "SUS304 material required", "foot operated actuation required", "dimensions 500.5 x 400 x 395 mm required"):
                with self.subTest(separator=separator, following=following):
                    text = rfq_for("foot", self.PERMISSION + separator + following + ".")
                    candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
                    self.assertEqual(["DIMENSIONS"], candidate["customization_scopes"])
                    self.assertFalse(candidate["customization_requires_review"])
                    self.assertEqual("CUSTOM", candidate["mode"])
                    self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))
            text = rfq_for("foot", "Customization welcomed" + separator + "SUS304 material required.")
            candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record("Knee"))
            self.assertFalse(candidate["customization_requires_review"])
            self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))
        for dimension_separator in ("x", "*", "×"):
            with self.subTest(dimension_separator=dimension_separator):
                text = MeasurementPermissionCompatibilityTests.facts(f"500.5{dimension_separator}400{dimension_separator}395 mm") + ", customization welcomed. SUS304 material required."
                candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertFalse(candidate["customization_requires_review"])
                self.assertEqual("CUSTOM", candidate["mode"])
                self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_later_generic_permission_cannot_fulfill_pending_condition(self):
        for separator in self.SEPARATORS:
            text = rfq_for("foot", self.PERMISSION + separator + "subject to design approval" + separator + "Customization welcomed.")
            with self.subTest(separator=separator):
                candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record("Knee"))
                self.assertEqual([], candidate["customization_scopes"])
                self.assertTrue(candidate["customization_requires_review"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_recommendation_rechecks_detached_approval_context(self):
        text = rfq_for("foot", self.PERMISSION + ". subject to design approval.")
        candidate = rfq_match.score_candidate(text, ConditionalPermissionGrammarTests.different_size_record())
        candidate.update(mode="CUSTOM", score=99, mismatches=["dimensions"], mismatch_scopes=["DIMENSIONS"], customization_scopes=["DIMENSIONS"], customization_requires_review=False)
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))


class RfqDetachedContextCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli

    def test_detached_conditions_real_cli(self):
        for condition in DetachedQualificationContextTests.CONDITIONS:
            for separator in ("; ", ". ", "\n"):
                for actuation in ("Foot", "Knee"):
                    with self.subTest(condition=condition, separator=separator, actuation=actuation):
                        text = rfq_for("foot", DetachedQualificationContextTests.PERMISSION + separator + condition + ".")
                        result = self.cli(text, [ConditionalPermissionGrammarTests.different_size_record(actuation)])
                        self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                        self.assertEqual([], result["candidates"][0]["customization_scopes"])
                        self.assertTrue(result["candidates"][0]["customization_requires_review"])

    def test_leading_and_fact_bridge_conditions_real_cli(self):
        for separator in ("; ", ". ", "\n"):
            suffixes = [condition + separator + DetachedQualificationContextTests.PERMISSION + "." for condition in DetachedQualificationContextTests.CONDITIONS]
            suffixes.append(DetachedQualificationContextTests.PERMISSION + separator + "SUS304 material required" + separator + "subject to design approval.")
            for suffix in suffixes:
                with self.subTest(suffix=suffix):
                    result = self.cli(rfq_for("foot", suffix), [ConditionalPermissionGrammarTests.different_size_record()])
                    self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                    self.assertTrue(result["candidates"][0]["customization_requires_review"])

    def test_independent_context_decimals_and_conflicts_real_cli(self):
        for separator in ("; ", ". ", "\n"):
            for following in ("SUS304 foot operated hand wash sink wall mount", "dimensions 500.5 x 400 x 395 mm required"):
                with self.subTest(separator=separator, following=following):
                    text = rfq_for("foot", DetachedQualificationContextTests.PERMISSION + separator + following + ".")
                    result = self.cli(text, [ConditionalPermissionGrammarTests.different_size_record()])
                    self.assertEqual("FILL_TO_SUBMIT", result["recommended_action"])
                    self.assertFalse(result["candidates"][0]["customization_requires_review"])
            text = rfq_for("foot", "Customization welcomed" + separator + "SUS304 material required.")
            self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [ConditionalPermissionGrammarTests.different_size_record("Knee")])["recommended_action"])
        for separator in ("x", "*", "×"):
            text = MeasurementPermissionCompatibilityTests.facts(f"500.5{separator}400{separator}395 mm") + ", customization welcomed. SUS304 material required."
            self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [sink_record("Knee")])["recommended_action"])
        text = rfq_for("foot", DetachedQualificationContextTests.PERMISSION + "; SUS304 material required.")
        self.assertEqual("PREPARE_AND_REVIEW", self.cli(text, [ConditionalPermissionGrammarTests.different_size_record("Knee")])["recommended_action"])
        record = ConditionalPermissionGrammarTests.different_size_record()
        record["attributes"][2]["raw_value"] = "Double Bowl Rectangular"
        self.assertEqual("PREPARE_AND_REVIEW", self.cli(text, [record])["recommended_action"])
        record = sink_record("Knee")
        record["attributes"][0]["raw_value"] = "SUS201"
        self.assertEqual("SKIP_RFQ", self.cli(rfq_for("foot", "Customization welcomed; SUS201 not acceptable."), [record])["recommended_action"])


class FailClosedDetachedContextTests(unittest.TestCase):
    QUALIFICATIONS = (
        "on condition that drawings are accepted", "pending confirmation",
        "upon confirmation of drawings", "when drawings are accepted",
        "after drawing confirmation", "drawings must be confirmed",
        "only following accepted drawings", "subject to design approval",
    )
    ORDINARY_PROSE = (
        "Please send your catalogue", "Please share product details",
        "Please quote your best price", "Thank you for your reply",
        "We need a reliable supplier",
    )
    KNOWN_FACTS = (
        "SUS304 material required", "Foot operated actuation required",
        "SUS304 foot operated single bowl rectangular hand wash sink wall mount 500.5*400*395 mm",
        "dimensions 500.5 x 400 x 395 mm required",
    )
    PARTIAL_CONDITIONS = (
        "Please send your catalogue pending confirmation",
        "Please quote your best price on condition that drawings are accepted",
        "SUS304 material required pending confirmation",
        "Please share product details provided drawings are accepted",
        "Thank you for your reply pending confirmation",
    )

    def test_formal_unlisted_condition_and_recommendation_recheck(self):
        text = rfq_for("foot", "Customization accepted; on condition that drawings are accepted.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertEqual([], candidate["customization_scopes"])
        self.assertTrue(candidate["customization_requires_review"])
        self.assertEqual("REVIEW", candidate["mode"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))
        candidate.update(mode="CUSTOM", score=99, mismatches=["actuation"], mismatch_scopes=["ACTUATION"], customization_scopes=["ACTUATION"], customization_requires_review=False)
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_unknown_detached_clauses_fail_closed_before_and_after_permission(self):
        for qualification in self.QUALIFICATIONS + ("future scope determination",):
            for separator in DetachedQualificationContextTests.SEPARATORS:
                for suffix in ("Customization accepted" + separator + qualification + ".", qualification + separator + "Customization accepted."):
                    for actuation in ("Foot", "Knee"):
                        with self.subTest(suffix=suffix, actuation=actuation):
                            text = rfq_for("foot", suffix)
                            candidate = rfq_match.score_candidate(text, sink_record(actuation))
                            self.assertEqual([], candidate["customization_scopes"])
                            self.assertTrue(candidate["customization_requires_review"])
                            self.assertEqual("REVIEW", candidate["mode"])
                            self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_finite_facts_requirements_restrictions_are_fully_consumed(self):
        for clause in self.KNOWN_FACTS + ("SUS304 material", "wall mount installation", "Not knee operated", "except actuation", "no changes to actuation", "actuation cannot be changed"):
            with self.subTest(clause=clause):
                self.assertTrue(rfq_match.independent_rfq_clause(clause.lower()))
                self.assertFalse(rfq_match.independent_rfq_clause(clause.lower() + " pending confirmation"))
        for separator in DetachedQualificationContextTests.SEPARATORS:
            for fact in self.KNOWN_FACTS:
                with self.subTest(separator=separator, fact=fact):
                    text = rfq_for("foot", "Customization accepted" + separator + fact + ".")
                    candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                    self.assertFalse(candidate["customization_requires_review"])
                    self.assertEqual("CUSTOM", candidate["mode"])
                    self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))

    def test_ordinary_rfq_text_exemption_requires_the_whole_clause(self):
        for sentence in self.ORDINARY_PROSE:
            with self.subTest(sentence=sentence):
                self.assertTrue(rfq_match.independent_rfq_clause(sentence.lower()))
                self.assertFalse(rfq_match.independent_rfq_clause(sentence.lower() + " pending confirmation"))
                text = rfq_for("foot", "Customization accepted; " + sentence + ".")
                candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertFalse(candidate["customization_requires_review"])
                self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))
        for sentence in self.PARTIAL_CONDITIONS:
            with self.subTest(sentence=sentence):
                self.assertFalse(rfq_match.independent_rfq_clause(sentence.lower()))
                text = rfq_for("foot", "Customization accepted; " + sentence + ".")
                candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertTrue(candidate["customization_requires_review"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_known_facts_prose_or_later_permission_cannot_fulfill_unknown_context(self):
        for middle in self.KNOWN_FACTS + self.ORDINARY_PROSE + ("Customization welcomed",):
            with self.subTest(middle=middle):
                text = rfq_for("foot", "Customization accepted; pending confirmation; " + middle + ".")
                candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertEqual([], candidate["customization_scopes"])
                self.assertTrue(candidate["customization_requires_review"])
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))

    def test_decimal_scope_and_exclusion_compatibility(self):
        for separator in ("x", "*", "×"):
            with self.subTest(separator=separator):
                text = MeasurementPermissionCompatibilityTests.facts(f"500.5{separator}400{separator}395 mm") + ", customization welcomed. Please send your catalogue."
                candidate = rfq_match.score_candidate(text, sink_record("Knee"))
                self.assertFalse(candidate["customization_requires_review"])
                self.assertEqual("CUSTOM", candidate["mode"])
                self.assertEqual("FILL_TO_SUBMIT", rfq_match.recommended_action(text, candidate))
        text = rfq_for("foot", "Customization accepted for dimensions only; Please send your catalogue.")
        candidate = rfq_match.score_candidate(text, sink_record("Knee"))
        self.assertFalse(candidate["customization_requires_review"])
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate))
        record = sink_record("Knee")
        record["attributes"][0]["raw_value"] = "SUS201"
        text = rfq_for("foot", "Customization welcomed; SUS201 not acceptable.")
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual("REJECTED", candidate["mode"])
        self.assertEqual("SKIP_RFQ", rfq_match.recommended_action(text, candidate))


class RfqFailClosedDetachedContextCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli

    def test_unknown_detached_conditions_before_after_real_cli(self):
        for qualification in FailClosedDetachedContextTests.QUALIFICATIONS:
            for separator in ("; ", ". ", "\n"):
                for suffix in ("Customization accepted" + separator + qualification + ".", qualification + separator + "Customization accepted."):
                    with self.subTest(suffix=suffix):
                        result = self.cli(rfq_for("foot", suffix), [sink_record("Knee")])
                        self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                        self.assertEqual([], result["candidates"][0]["customization_scopes"])
                        self.assertTrue(result["candidates"][0]["customization_requires_review"])

    def test_fully_consumed_facts_and_unrelated_prose_real_cli(self):
        for separator in ("; ", ". ", "\n"):
            for sentence in FailClosedDetachedContextTests.KNOWN_FACTS + FailClosedDetachedContextTests.ORDINARY_PROSE:
                with self.subTest(separator=separator, sentence=sentence):
                    result = self.cli(rfq_for("foot", "Customization accepted" + separator + sentence + "."), [sink_record("Knee")])
                    self.assertEqual("FILL_TO_SUBMIT", result["recommended_action"])
                    self.assertEqual("CUSTOM", result["candidates"][0]["mode"])
                    self.assertFalse(result["candidates"][0]["customization_requires_review"])

    def test_partial_exemptions_decimal_and_conflicts_real_cli(self):
        for sentence in FailClosedDetachedContextTests.PARTIAL_CONDITIONS:
            with self.subTest(sentence=sentence):
                result = self.cli(rfq_for("foot", "Customization accepted; " + sentence + "."), [sink_record("Knee")])
                self.assertEqual("PREPARE_AND_REVIEW", result["recommended_action"])
                self.assertTrue(result["candidates"][0]["customization_requires_review"])
        for separator in ("x", "*", "×"):
            text = MeasurementPermissionCompatibilityTests.facts(f"500.5{separator}400{separator}395 mm") + ", customization welcomed. Please send your catalogue."
            self.assertEqual("FILL_TO_SUBMIT", self.cli(text, [sink_record("Knee")])["recommended_action"])
        self.assertEqual("PREPARE_AND_REVIEW", self.cli(rfq_for("foot", "Customization accepted for dimensions only; Please send your catalogue."), [sink_record("Knee")])["recommended_action"])
        record = sink_record("Knee")
        record["attributes"][0]["raw_value"] = "SUS201"
        self.assertEqual("SKIP_RFQ", self.cli(rfq_for("foot", "Customization welcomed; SUS201 not acceptable."), [record])["recommended_action"])


def mandatory_matrix_record(actuation="Foot", material="SUS304", mount="Wall Mount", bowl="Single Bowl", size="500 x 400 x 395 mm"):
    return {"canonical_model": "SYNTHETIC1", "name": f"Stainless Steel {actuation} Operated Hand Wash Sink - {mount}", "category": "Wash Sinks", "attributes": [{"name": "Material", "raw_value": material}, {"name": "Installation", "raw_value": mount}, {"name": "Shape", "raw_value": bowl + " Rectangular"}, {"name": "Size", "raw_value": size}]}


def mandatory_scope_cases():
    return (
        ("ACTUATION", "SUS304 single bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm. ", ("Foot operated", "Knee operated"), (mandatory_matrix_record("Foot"), mandatory_matrix_record("Knee"))),
        ("MATERIAL", "Foot operated single bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm. ", ("SUS304 material", "SUS201 material"), (mandatory_matrix_record(material="SUS304"), mandatory_matrix_record(material="SUS201"))),
        ("INSTALLATION", "SUS304 foot operated single bowl rectangular hand wash sink 500 x 400 x 395 mm. ", ("Wall mount installation", "Floor standing installation"), (mandatory_matrix_record(mount="Wall Mount"), mandatory_matrix_record(mount="Floor Standing"))),
        ("BOWL_COUNT", "SUS304 foot operated rectangular hand wash sink wall mount 500 x 400 x 395 mm. ", ("Single bowl", "Double bowl"), (mandatory_matrix_record(bowl="Single Bowl"), mandatory_matrix_record(bowl="Double Bowl"))),
    )


def mandatory_matrix_cases():
    rows = []
    def add(name, text, record, scope=None):
        rows.append((name, text, record, "PREPARE_AND_REVIEW" if scope else "FILL_TO_SUBMIT", "REVIEW" if scope else "EXACT_OR_NEAR", False, scope))
    for scope, base, phrases, candidates in mandatory_scope_cases():
        for index, separator in enumerate(("; ", ". ", "\n")):
            for choice, record in enumerate(candidates):
                text = base + phrases[0] + " required" + separator + phrases[1] + " required."
                add(f"mandatory_{scope}_{index}_{choice}", text, record, scope)
                add(f"mandatory_custom_{scope}_{index}_{choice}", text + " Customization welcomed.", record, scope)
        for choice, record in enumerate(candidates):
            add(f"single_{scope}_{choice}", base + phrases[choice] + " required.", record)
            add(f"or_{scope}_{choice}", base + " or ".join(phrases) + " required.", record)
        add(f"and_{scope}", base + " and ".join(phrases) + " required.", candidates[0], scope)
        add(f"note_{scope}", base + "Note: " + phrases[0] + "; " + phrases[1] + " required.", candidates[0], scope)
    base = mandatory_scope_cases()[0][1]
    add("or_plus_mandatory", base + "Foot operated or knee operated required; foot operated required.", mandatory_matrix_record("Knee"), "ACTUATION")
    add("mixed_and_or", base + "Foot operated or knee operated and foot operated required.", mandatory_matrix_record("Foot"), "ACTUATION")
    add("dual_mandatory_actuation", base + "Foot operated and knee operated required.", mandatory_matrix_record("Foot Operated and Knee"))
    dimension_base = "SUS304 foot operated single bowl rectangular hand wash sink wall mount. "
    sizes = ("500 x 400 x 395 mm", "900 x 800 x 795 mm")
    for index, separator in enumerate(("; ", ". ", "\n")):
        for choice, size in enumerate(sizes):
            text = dimension_base + sizes[0] + " required" + separator + sizes[1] + " required."
            add(f"dimensions_{index}_{choice}", text, mandatory_matrix_record(size=size), "DIMENSIONS")
            add(f"dimensions_custom_{index}_{choice}", text + " Customization welcomed.", mandatory_matrix_record(size=size), "DIMENSIONS")
    for choice, size in enumerate(sizes):
        add(f"single_dimensions_{choice}", dimension_base + size + " required.", mandatory_matrix_record(size=size))
    add("repeat_dimensions_same", dimension_base + sizes[0] + " required; " + sizes[0] + " required.", mandatory_matrix_record())
    add("repeat_dimensions_decimal_tolerance", dimension_base + sizes[0] + " required; 500.5*400*395 mm required.", mandatory_matrix_record())
    return rows


class HardRequirementLogicTests(unittest.TestCase):
    def check_case(self, row):
        name, text, record, action, mode, qualification, scope = row
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual(mode, candidate["mode"], name)
        self.assertEqual(action, rfq_match.recommended_action(text, candidate), name)
        self.assertEqual(qualification, candidate["customization_requires_review"], name)
        self.assertEqual(bool(scope), candidate["hard_requirements_requires_review"], name)
        if scope:
            self.assertIn(scope, candidate["mismatch_scopes"], name)
            self.assertIn("UNRESOLVED_HARD_REQUIREMENTS", candidate["mismatch_scopes"], name)
        else:
            self.assertEqual([], candidate["mismatches"], name)

    def test_formal_mandatory_actuation_and_forged_recommendation_recheck(self):
        row = next(row for row in mandatory_matrix_cases() if row[0] == "mandatory_ACTUATION_0_1")
        self.check_case(row)
        candidate = rfq_match.score_candidate(row[1], row[2])
        candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False)
        self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(row[1], candidate))

    def test_all_four_domains_keep_mandatory_clauses_with_or_without_customization(self):
        for row in mandatory_matrix_cases():
            if row[0].startswith("mandatory_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_complete_single_and_explicit_or_requirements_remain_fillable(self):
        for row in mandatory_matrix_cases():
            if row[0].startswith(("single_", "or_ACTUATION_", "or_MATERIAL_", "or_INSTALLATION_", "or_BOWL_COUNT_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_and_notes_mixed_logic_and_verified_dual_actuation(self):
        for row in mandatory_matrix_cases():
            if row[0].startswith(("and_", "note_")) or row[0] in {"or_plus_mandatory", "mixed_and_or", "dual_mandatory_actuation"}:
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_every_dimension_span_is_checked_without_losing_decimal_tolerance(self):
        for row in mandatory_matrix_cases():
            if row[0].startswith(("dimensions_", "repeat_dimensions_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_grouping_is_conjunctive_unless_the_relation_is_fully_recognized(self):
        for scope, base, phrases, candidates in mandatory_scope_cases():
            with self.subTest(scope=scope):
                required = base + phrases[0] + " required; " + phrases[1] + " required."
                groups, unknown = rfq_match.hard_requirement_groups(required, rfq_match.concepts(required))
                self.assertEqual(2, len(groups[scope]))
                self.assertNotIn(scope, unknown)
                self.assertFalse(rfq_match.hard_scope_matches(groups, unknown, scope, rfq_match.concepts(rfq_match.structured_blob(candidates[0]))))
                alternative = base + " or ".join(phrases) + " required."
                groups, unknown = rfq_match.hard_requirement_groups(alternative, rfq_match.concepts(alternative))
                self.assertEqual(1, len(groups[scope]))
                self.assertNotIn(scope, unknown)
                self.assertTrue(rfq_match.hard_scope_matches(groups, unknown, scope, rfq_match.concepts(rfq_match.structured_blob(candidates[0]))))


class RfqHardRequirementLogicCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli

    def check_case(self, row):
        name, text, record, action, mode, qualification, scope = row
        result = self.cli(text, [record])
        self.assertEqual(action, result["recommended_action"], name)
        candidate = result["candidates"][0]
        self.assertEqual(mode, candidate["mode"], name)
        self.assertEqual(qualification, candidate["customization_requires_review"], name)
        self.assertEqual(bool(scope), candidate["hard_requirements_requires_review"], name)
        if scope:
            self.assertIn(scope, candidate["mismatch_scopes"], name)
            self.assertIn("UNRESOLVED_HARD_REQUIREMENTS", candidate["mismatch_scopes"], name)
        else:
            self.assertEqual([], candidate["mismatches"], name)

    def test_mandatory_four_domain_matrix_real_cli(self):
        for row in mandatory_matrix_cases():
            if row[0].startswith("mandatory_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_explicit_alternatives_and_ambiguous_relations_real_cli(self):
        for row in mandatory_matrix_cases():
            if not row[0].startswith(("mandatory_", "dimensions_", "single_dimensions_", "repeat_dimensions_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_all_dimension_requirements_and_tolerance_controls_real_cli(self):
        for row in mandatory_matrix_cases():
            if row[0].startswith(("dimensions_", "single_dimensions_", "repeat_dimensions_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)


def candidate_mandatory_cases():
    rows = []
    for scope, base, phrases, records in mandatory_scope_cases():
        if scope == "BOWL_COUNT":
            continue
        record = mandatory_matrix_record("Foot Operated or Knee") if scope == "ACTUATION" else mandatory_matrix_record(material="SUS304 or SUS201") if scope == "MATERIAL" else mandatory_matrix_record(mount="Wall Mount or Floor Standing")
        rows.append(("candidate_or_" + scope, base + phrases[0] + " required; " + phrases[1] + " required.", record, "PREPARE_AND_REVIEW", "REVIEW", False, scope))
    base = mandatory_scope_cases()[0][1]
    record = mandatory_matrix_record("Foot Operated and Knee")
    record["attributes"].append({"name": "Actuation note", "raw_value": "Foot operated pending confirmation"})
    rows.append(("candidate_unknown_note", base + "Foot operated required; knee operated required.", record, "PREPARE_AND_REVIEW", "REVIEW", False, "ACTUATION"))
    return rows


class CandidateMandatoryFactsTests(unittest.TestCase):
    check_case = HardRequirementLogicTests.check_case

    def test_candidate_or_and_unknown_notes_do_not_prove_simultaneous_facts(self):
        for row in candidate_mandatory_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)
                candidate = rfq_match.score_candidate(row[1], row[2])
                candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False)
                self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(row[1], candidate))


class CandidateMandatoryFactsCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli
    check_case = RfqHardRequirementLogicCliTests.check_case

    def test_candidate_or_and_unknown_notes_real_cli(self):
        for row in candidate_mandatory_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)


def single_domain_candidate_cases():
    rows = []
    for scope, base, phrases, candidates in mandatory_scope_cases():
        record = mandatory_matrix_record("Foot Operated or Knee") if scope == "ACTUATION" else mandatory_matrix_record(material="SUS304 or SUS201") if scope == "MATERIAL" else mandatory_matrix_record(mount="Wall Mount or Floor Standing") if scope == "INSTALLATION" else mandatory_matrix_record(bowl="Single Bowl or Double Bowl")
        for choice, fixed in enumerate(candidates):
            text = base + phrases[choice] + " required."
            for custom in ("", " Customization welcomed."):
                rows.append((f"candidate_or_{scope}_{choice}_{bool(custom)}", text + custom, record, "PREPARE_AND_REVIEW", "REVIEW", False, scope))
            rows.append((f"fixed_{scope}_{choice}", text, fixed, "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
            rows.append((f"rfq_or_{scope}_{choice}", base + " or ".join(phrases) + " required.", fixed, "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
    base = mandatory_scope_cases()[0][1]
    rows.append(("dual_and", base + "Foot operated and knee operated required.", mandatory_matrix_record("Foot Operated and Knee"), "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
    return rows


class SingleDomainCandidateOptionsTests(unittest.TestCase):
    check_case = HardRequirementLogicTests.check_case

    def test_single_requested_hard_domain_candidate_options_require_review(self):
        for row in single_domain_candidate_cases():
            if row[0].startswith("candidate_or_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)
                    candidate = rfq_match.score_candidate(row[1], row[2])
                    candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False)
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(row[1], candidate))

    def test_known_fixed_facts_and_explicit_rfq_alternatives_remain_exact(self):
        for row in single_domain_candidate_cases():
            if row[0].startswith(("fixed_", "rfq_or_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_verified_dual_and_facts_remain_exact(self):
        self.check_case(next(row for row in single_domain_candidate_cases() if row[0] == "dual_and"))


class RfqSingleDomainCandidateOptionsCliTests(unittest.TestCase):
    cli = RfqRealCliRegressions.cli
    check_case = RfqHardRequirementLogicCliTests.check_case

    def test_all_four_requested_domains_candidate_options_real_cli(self):
        for row in single_domain_candidate_cases():
            if row[0].startswith("candidate_or_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_fixed_facts_rfq_or_and_real_dual_and_positive_controls_real_cli(self):
        for row in single_domain_candidate_cases():
            if not row[0].startswith("candidate_or_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)


def partial_candidate_option_cases():
    rows = []
    profiles = {scope: (base, phrases, candidates) for scope, base, phrases, candidates in mandatory_scope_cases()}
    unknowns = {
        "MATERIAL": ("SUS304 or SUS316", "SUS316 or SUS304", "SUS316 or SUS430"),
        "ACTUATION": ("Foot Operated or Sensor", "Sensor Operated or Foot", "Sensor Operated or Manual"),
        "INSTALLATION": ("Wall Mount or Undermount", "Undermount or Wall Mount", "Undermount or Flushmount"),
        "BOWL_COUNT": ("Single Bowl or Triple Bowl", "Triple Bowl or Single Bowl", "Triple Bowl or Quadruple Bowl"),
        "DIMENSIONS": ("500 x 400 x 395 mm or TBD", "TBD or 500 x 400 x 395 mm", "TBD or per drawing"),
    }
    for scope, values in unknowns.items():
        if scope == "DIMENSIONS":
            text = "SUS304 foot operated single bowl rectangular hand wash sink wall mount. 500 x 400 x 395 mm required."
        else:
            base, phrases, candidates = profiles[scope]
            text = base + phrases[0] + " required."
        for index, value in enumerate(values):
            record = mandatory_matrix_record(value) if scope == "ACTUATION" else mandatory_matrix_record(material=value) if scope == "MATERIAL" else mandatory_matrix_record(mount=value) if scope == "INSTALLATION" else mandatory_matrix_record(bowl=value) if scope == "BOWL_COUNT" else mandatory_matrix_record(size=value)
            for custom in ("", " Customization welcomed."):
                rows.append((f"unknown_or_{scope}_{index}_{bool(custom)}", text + custom, record, "PREPARE_AND_REVIEW", "REVIEW", False, scope))
    for scope, (base, phrases, candidates) in profiles.items():
        rows.append((f"fixed_{scope}", base + phrases[0] + " required.", candidates[0], "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
        rows.append((f"rfq_or_{scope}", base + " or ".join(phrases) + " required.", candidates[0], "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
    base = "SUS304 foot operated single bowl rectangular hand wash sink wall mount. 500 x 400 x 395 mm required."
    for index, size in enumerate(("50 x 40 x 39.5 cm", "500.5*400*395 mm", "500.5×400×395 mm")):
        rows.append((f"fixed_dimensions_{index}", base, mandatory_matrix_record(size=size), "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
    base = profiles["ACTUATION"][0] + "Foot operated and knee operated required."
    rows.append(("dual_and", base, mandatory_matrix_record("Foot Operated and Knee"), "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
    base = profiles["ACTUATION"][0] + "Foot operated required."
    record = mandatory_matrix_record()
    record["description_text"] = "SUS304 or SUS316; foot operated or sensor operated"
    rows.append(("ignored_description_or", base, record, "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
    record = mandatory_matrix_record()
    record["attributes"].append({"name": "Packaging", "raw_value": "Carton or pallet"})
    rows.append(("unrelated_attribute_or", base, record, "FILL_TO_SUBMIT", "EXACT_OR_NEAR", False, None))
    return rows


class PartialCandidateOptionTests(unittest.TestCase):
    check_case = HardRequirementLogicTests.check_case

    def test_unknown_or_operands_never_prove_an_approved_hard_configuration(self):
        for row in partial_candidate_option_cases():
            if row[0].startswith("unknown_or_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)
                    candidate = rfq_match.score_candidate(row[1], row[2])
                    candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False)
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(row[1], candidate))

    def test_fixed_facts_rfq_or_dual_and_and_unrelated_metadata_stay_fillable(self):
        for row in partial_candidate_option_cases():
            if not row[0].startswith("unknown_or_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_option_binding_stays_in_its_actual_hard_domain(self):
        for row in partial_candidate_option_cases():
            with self.subTest(name=row[0]):
                options = rfq_match.candidate_option_scopes(row[2])
                self.assertEqual({row[6]} if row[6] else set(), options)


class RfqPartialCandidateOptionCliTests(unittest.TestCase):
    check_case = RfqHardRequirementLogicCliTests.check_case

    def cli(self, text, records):
        with tempfile.TemporaryDirectory(prefix="rfq-partial-options-cli-", dir=r"C:\Windows\Temp") as directory:
            root = Path(directory)
            official, catalog, overlay = [root / name for name in ("official.jsonl", "catalog.jsonl", "overlay.jsonl")]
            official.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            catalog.write_text("", encoding="utf-8")
            overlay.write_text("", encoding="utf-8")
            process = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", str(MODULE_PATH), "--rfq-text", text, "--official-current", str(official), "--catalog-index", str(catalog), "--disposition", str(overlay)],
                cwd=r"C:\Windows\Temp", capture_output=True, text=True, encoding="utf-8", timeout=15,
                env={"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows", "TEMP": r"C:\Windows\Temp", "TMP": r"C:\Windows\Temp"},
            )
            self.assertEqual(0, process.returncode, process.stderr)
            self.assertNotIn("Traceback", process.stderr)
            return json.loads(process.stdout)

    def test_all_five_domains_partial_or_and_unknown_only_options_real_cli(self):
        for row in partial_candidate_option_cases():
            if row[0].startswith("unknown_or_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_fixed_decimal_units_rfq_or_dual_and_and_ignored_metadata_real_cli(self):
        for row in partial_candidate_option_cases():
            if not row[0].startswith("unknown_or_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)


def qualified_candidate_option_cases():
    """Fixed synthetic inputs shared by method, CLI and pre/post evidence checks."""
    rows = []
    targets = {
        "MATERIAL": ("SUS304", "SUS316", "SUS201", "Material"),
        "ACTUATION": ("Foot Operated", "Sensor Operated", "Knee Operated", None),
        "INSTALLATION": ("Wall Mount", "Undermount", "Floor Standing", "Installation"),
        "BOWL_COUNT": ("Single Bowl", "Triple Bowl", "Double Bowl", "Shape"),
        "DIMENSIONS": ("500 x 400 x 395 mm", "TBD", "900 x 800 x 795 mm", "Size"),
    }

    def record_for(scope, value):
        record = mandatory_matrix_record()
        record["name"] = "SUS304 Foot Operated Single Bowl Rectangular Hand Wash Sink - Wall Mount"
        known, _, _, attribute = targets[scope]
        if attribute:
            record["attributes"] = [item for item in record["attributes"] if item["name"] != attribute]
        if scope == "DIMENSIONS":
            record["name"] += " - " + value
        else:
            record["name"] = record["name"].replace(known, value, 1)
        return record

    def add(name, text, record, options=(), gated=None, action=None):
        options = set(options)
        gated = options if gated is None else set(gated)
        rows.append((name, text, record, options, gated, action or ("PREPARE_AND_REVIEW" if gated else "FILL_TO_SUBMIT")))

    for scope, (known, unknown, other_known, attribute) in targets.items():
        variants = (
            ("standard", f"{known} (standard) or {unknown}"),
            ("optional", f"{known} (optional) or {unknown}"),
            ("unknown_modifier", f"{known} (manufacturer default) or {unknown}"),
            ("unknown_condition", f"{known} (available on request) or {unknown}"),
            ("reverse_unknown", f"{unknown} or (manufacturer default) {known}"),
            ("double_modifier", f"{known} (manufacturer default) or (available on request) {unknown}"),
            ("reverse_double", f"{unknown} (available on request) or (manufacturer default) {known}"),
            ("known_branch", f"{known} (standard) or {other_known} (optional)"),
            ("bracket_modifier", f"{known} [manufacturer default] or {unknown}"),
            ("nested_modifier", f"{known} (standard (manufacturer default)) or {unknown}"),
            ("punctuated_modifier", f"{known} (standard, available; pending approval) or {unknown}"),
            ("scope_word_modifier", f"{known} ({scope.lower().replace('_', ' ')} default) or {unknown}"),
            ("bare_standard", f"{known} standard or {unknown}"),
            ("bare_optional", f"{known} optional or {unknown}"),
            ("bare_default", f"{known} as default or {unknown}"),
        )
        for modifier, value in variants:
            for custom in (False, True):
                add(f"qualified_{scope}_{modifier}_{custom}", rfq_for() + (" Customization welcomed." if custom else ""), record_for(scope, value), {scope})
        for chosen, value in (("matching", known), ("conflicting", other_known)):
            record = record_for(scope, f"{known} (standard) or {unknown}")
            record["attributes"].append({"name": attribute or "Actuation", "raw_value": value})
            add(f"explicit_attribute_{scope}_{chosen}", rfq_for(), record, {scope})
        record = record_for(scope, known)
        record["attributes"].append({"name": attribute or "Actuation", "raw_value": known})
        add(f"fixed_attribute_{scope}", rfq_for(), record)
        # Both values are unknown, but the typed field still binds the OR.
        record = mandatory_matrix_record()
        record["attributes"] = [item for item in record["attributes"] if item["name"] != (attribute or "Actuation")]
        record["attributes"].append({"name": attribute or "Actuation", "raw_value": f"{unknown} (manufacturer default) or unlisted (available on request)"})
        add(f"typed_unknown_{scope}", rfq_for(), record, {scope})
        # Preserve the original exploratory input as a separate real-CLI case:
        # a second same-name attribute must still block actual option approval.
        record = mandatory_matrix_record()
        record["attributes"].append({"name": attribute or "Actuation", "raw_value": f"{unknown} (manufacturer default) or unlisted (available on request)"})
        add(f"duplicate_typed_unknown_{scope}", rfq_for(), record, {scope})

    record = record_for("INSTALLATION", "Wall Mount (standard) or Undermount")
    record["name"] = "SUS304 Foot Operated Single Bowl Hand Wash Sink - wall mount (standard) or undermount"
    add("formal_review_installation", rfq_for(), record, {"INSTALLATION"})
    record = record_for("ACTUATION", "Foot Operated (optional) or Sensor Operated")
    add("formal_review_actuation", rfq_for(), record, {"ACTUATION"})
    record = mandatory_matrix_record()
    record["name"] = "SUS304 single bowl rectangular hand wash sink - foot operated (optional) or sensor operated; wall mount (standard) or undermount; 500 x 400 x 395 mm (manufacturer default) or TBD"
    add("multi_scope_qualified", rfq_for(), record, {"ACTUATION", "INSTALLATION", "DIMENSIONS"}, action="SKIP_RFQ")
    add("multi_scope_qualified_custom", rfq_for(suffix="Customization welcomed."), record, {"ACTUATION", "INSTALLATION", "DIMENSIONS"})
    record = record_for("DIMENSIONS", "500 x 400 x 395 mm (standard) or TBD")
    add("multi_dimension_compatible", rfq_for(suffix="500.5*400*395 mm required."), record, {"DIMENSIONS"})
    add("multi_dimension_incompatible", rfq_for(suffix="900 x 800 x 795 mm required. Customization welcomed."), record, {"DIMENSIONS"})
    for scope, text in (
        ("ACTUATION", "SUS304 single bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm."),
        ("INSTALLATION", "SUS304 foot operated single bowl rectangular hand wash sink 500 x 400 x 395 mm."),
        ("DIMENSIONS", "SUS304 foot operated single bowl rectangular hand wash sink wall mount."),
    ):
        known, unknown, _, _ = targets[scope]
        add(f"unrequested_{scope}", text, record_for(scope, f"{known} (standard) or {unknown}"), {scope}, gated=())
    for name, value in (
        ("carton", " with carton or pallet"),
        ("qualified_carton", " with carton (standard) or pallet (optional)"),
        ("separate_carton", "; carton (manufacturer default) or pallet (available on request)"),
    ):
        record = mandatory_matrix_record()
        record["name"] += " (standard)" + value
        add("unrelated_title_" + name, rfq_for(), record)
    record = mandatory_matrix_record()
    record["attributes"].append({"name": "Packaging", "raw_value": "Carton (standard) or pallet (optional)"})
    add("unrelated_packaging_attribute", rfq_for(), record)
    record = mandatory_matrix_record()
    record["description_text"] = "Foot operated (optional) or sensor operated; wall mount (standard) or undermount"
    add("ignored_qualified_description", rfq_for(), record)
    record = mandatory_matrix_record()
    record["name"] += "; foot operated (optional) or sensor operated"
    add("actual_actuation_not_installation", rfq_for(), record, {"ACTUATION"})
    return rows


class QualifiedCandidateOptionTests(unittest.TestCase):
    def check_case(self, row):
        name, text, record, options, gated, action = row
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual(options, rfq_match.candidate_option_scopes(record), name)
        self.assertEqual(gated, set(candidate["unresolved_hard_requirement_scopes"]), name)
        self.assertEqual(bool(gated), candidate["hard_requirements_requires_review"], name)
        self.assertEqual("REVIEW" if gated else "EXACT_OR_NEAR", candidate["mode"], name)
        self.assertEqual(action, rfq_match.recommended_action(text, candidate), name)
        self.assertFalse(candidate["customization_requires_review"], name)
        if gated:
            self.assertTrue(gated <= set(candidate["mismatch_scopes"]), name)
            self.assertIn("UNRESOLVED_HARD_REQUIREMENTS", candidate["mismatch_scopes"], name)
        else:
            self.assertEqual([], candidate["mismatches"], name)

    def test_formal_qualified_installation_and_actuation_reproductions(self):
        for row in qualified_candidate_option_cases():
            if row[0].startswith("formal_review_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_all_five_domains_unknown_known_reverse_and_double_modifiers(self):
        for row in qualified_candidate_option_cases():
            if row[0].startswith("qualified_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_explicit_attributes_do_not_select_options_but_fixed_facts_remain_exact(self):
        for row in qualified_candidate_option_cases():
            if row[0].startswith(("explicit_attribute_", "fixed_attribute_", "typed_unknown_", "duplicate_typed_unknown_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_multi_scope_and_repeated_dimension_requirements_keep_all_uncertainty(self):
        for row in qualified_candidate_option_cases():
            if row[0].startswith(("multi_scope_", "multi_dimension_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_unrelated_or_and_unrequested_domains_are_not_cross_contaminated(self):
        for row in qualified_candidate_option_cases():
            if row[0].startswith(("unrequested_", "unrelated_", "ignored_", "actual_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_recommendation_rechecks_qualified_options_after_forged_approval(self):
        for row in qualified_candidate_option_cases():
            name, text, record, options, gated, action = row
            if gated and not name.startswith("duplicate_typed_unknown_"):
                with self.subTest(name=name):
                    candidate = rfq_match.score_candidate(text, record)
                    candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False, unresolved_hard_requirement_scopes=[])
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate), name)

    def test_enclosed_modifiers_are_consumed_whole_without_consuming_unrelated_text(self):
        self.assertTrue(rfq_match.option_operand_glue(" (manufacturer default) "))
        self.assertTrue(rfq_match.option_operand_glue(" [available on request] "))
        self.assertTrue(rfq_match.option_operand_glue(" (standard, installation default; pending approval) "))
        self.assertTrue(rfq_match.option_operand_glue(" (standard (manufacturer default)) "))
        for value in (" (standard) with carton ", " carton (standard) ", " (unfinished ", " installation pending approval "):
            with self.subTest(value=value):
                self.assertFalse(rfq_match.option_operand_glue(value))
        self.assertEqual(["wall mount (standard, optional; pending approval) or undermount", " carton or pallet"], rfq_match.option_fact_clauses("wall mount (standard, optional; pending approval) or undermount; carton or pallet"))


class RfqQualifiedCandidateOptionCliTests(unittest.TestCase):
    cli = RfqPartialCandidateOptionCliTests.cli

    def check_case(self, row):
        name, text, record, options, gated, action = row
        result = self.cli(text, [record])
        candidate = result["candidates"][0]
        self.assertEqual(action, result["recommended_action"], name)
        self.assertEqual("REVIEW" if gated else "EXACT_OR_NEAR", candidate["mode"], name)
        self.assertEqual(gated, set(candidate["unresolved_hard_requirement_scopes"]), name)
        self.assertEqual(bool(gated), candidate["hard_requirements_requires_review"], name)
        self.assertFalse(candidate["customization_requires_review"], name)
        if gated:
            self.assertTrue(gated <= set(candidate["mismatch_scopes"]), name)
            self.assertIn("UNRESOLVED_HARD_REQUIREMENTS", candidate["mismatch_scopes"], name)
        else:
            self.assertEqual([], candidate["mismatches"], name)

    def test_qualified_five_domain_modifier_matrix_real_cli(self):
        for row in qualified_candidate_option_cases():
            if row[0].startswith("qualified_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_formal_attributes_multi_scope_and_dimension_qualification_real_cli(self):
        for row in qualified_candidate_option_cases():
            if row[0].startswith(("formal_review_", "explicit_attribute_", "fixed_attribute_", "typed_unknown_", "duplicate_typed_unknown_", "multi_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_unrelated_or_unrequested_domain_and_fixed_metadata_controls_real_cli(self):
        for row in qualified_candidate_option_cases():
            if row[0].startswith(("unrequested_", "unrelated_", "ignored_", "actual_")):
                with self.subTest(name=row[0]):
                    self.check_case(row)


def unconsumed_candidate_option_cases():
    """Additional fixed cases; do not alter attempt-15's 190 input records."""
    rows = []

    def add(name, text, record, options=(), gated=None, action=None):
        options = set(options)
        gated = options if gated is None else set(gated)
        rows.append((name, text, record, options, gated, action or ("PREPARE_AND_REVIEW" if gated else "FILL_TO_SUBMIT")))

    # Exact original root six inputs/expectations, including original key order.
    root_clauses = (
        ("bare_qualifier", "wall mount with standard finish or undermount", "PREPARE_AND_REVIEW"),
        ("bare_unknown", "wall mount preferred by customers or undermount", "PREPARE_AND_REVIEW"),
        ("unclosed_modifier", "wall mount (standard or undermount", "PREPARE_AND_REVIEW"),
        ("unclosed_bracket", "wall mount [manufacturer default or undermount", "PREPARE_AND_REVIEW"),
        ("balanced_control", "wall mount (manufacturer default) or undermount", "PREPARE_AND_REVIEW"),
        ("separate_packaging_control", "wall mount; packaging blue or red", "FILL_TO_SUBMIT"),
    )
    root_rfq = "Need SUS304 Foot Operated Single Bowl Rectangular Hand Wash Sink, wall mount, 500 x 400 x 395 mm."
    for name, clause, action in root_clauses:
        record = {"canonical_model": "SYNTHETIC_EXTRA", "category": "Wash Sinks", "name": "SUS304 Foot Operated Single Bowl Rectangular Hand Wash Sink - " + clause, "attributes": [{"name": "Material", "raw_value": "SUS304"}, {"name": "Shape", "raw_value": "Single Bowl Rectangular"}, {"name": "Size", "raw_value": "500 x 400 x 395 mm"}]}
        add("root6_" + name, root_rfq, record, {"INSTALLATION"} if action == "PREPARE_AND_REVIEW" else (), action=action)

    profiles = {
        "MATERIAL": ("SUS304", "SUS316", "SUS201", "Material", "installation"),
        "ACTUATION": ("Foot Operated", "Sensor Operated", "Knee Operated", None, "material"),
        "INSTALLATION": ("Wall Mount", "Undermount", "Floor Standing", "Installation", "actuation"),
        "BOWL_COUNT": ("Single Bowl", "Triple Bowl", "Double Bowl", "Shape", "dimensions"),
        "DIMENSIONS": ("500 x 400 x 395 mm", "TBD", "900 x 800 x 795 mm", "Size", "installation"),
    }

    def record_for(scope, value):
        record = mandatory_matrix_record()
        record["name"] = "SUS304 Foot Operated Single Bowl Rectangular Hand Wash Sink - Wall Mount"
        known, _, _, attribute, _ = profiles[scope]
        if attribute:
            record["attributes"] = [item for item in record["attributes"] if item["name"] != attribute]
        if scope == "DIMENSIONS":
            record["name"] += " - " + value
        else:
            record["name"] = record["name"].replace(known, value, 1)
        return record

    for scope, (known, unknown, other, attribute, other_scope_word) in profiles.items():
        variants = (
            ("bare_qualifier", f"{known} with standard finish or {unknown}"),
            ("bare_unknown", f"{known} preferred by customers or {unknown}"),
            ("unknown_opposite", f"{known} pending customer choice or future configuration"),
            ("reverse_unknown", f"{unknown} preferred by customers or specially configured {known}"),
            ("unclosed_parenthesis", f"{known} (standard or {unknown}"),
            ("unclosed_bracket", f"{known} [manufacturer default or {unknown}"),
            ("unclosed_other_scope_word", f"{known} ({other_scope_word} pending or {unknown}"),
            ("reverse_unclosed", f"{unknown} (manufacturer default or preferred {known}"),
            ("known_opposite", f"{known} preferred by customers or {other}"),
            ("unclosed_double", f"{known} [manufacturer default (pending confirmation or {unknown}"),
        )
        for variant, value in variants:
            for custom in (False, True):
                add(f"unconsumed_{scope}_{variant}_{custom}", rfq_for() + (" Customization welcomed." if custom else ""), record_for(scope, value), {scope})

    record = mandatory_matrix_record()
    record["name"] += "; foot operated preferred by customers or sensor operated; wall mount with standard finish or undermount"
    add("new_multi_actuation_installation", rfq_for(), record, {"ACTUATION", "INSTALLATION"})
    record = mandatory_matrix_record()
    record["name"] += "; SUS304 preferred by customers or special grade; 500 x 400 x 395 mm (default or TBD"
    add("new_multi_material_dimensions", rfq_for(), record, {"MATERIAL", "DIMENSIONS"})
    record = record_for("INSTALLATION", "Wall Mount preferred or future configuration with foot operated")
    add("new_other_fixed_scope_after_unknown_operand", rfq_for(), record, {"INSTALLATION"})
    record = mandatory_matrix_record()
    record["name"] += " - special grade preferred or selected SUS304"
    add("new_reverse_section_material_not_installation", rfq_for(), record, {"MATERIAL"})

    for name, suffix in (
        ("carton_pallet", " with carton or pallet"),
        ("qualified_carton_pallet", " with carton preferred by customers or pallet available on request"),
        ("box_crate", " with box or crate"),
        ("explicit_packaging_color", " with packaging blue or red"),
        ("separate_packaging", "; packaging blue or red"),
        ("separate_unknown", "; preferred carton or custom pallet"),
    ):
        record = mandatory_matrix_record()
        record["name"] += suffix
        add("new_unrelated_" + name, rfq_for(), record)
    for scope, text in (
        ("MATERIAL", "Foot operated single bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm."),
        ("ACTUATION", "SUS304 single bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm."),
        ("INSTALLATION", "SUS304 foot operated single bowl rectangular hand wash sink 500 x 400 x 395 mm."),
        ("BOWL_COUNT", "SUS304 foot operated rectangular hand wash sink wall mount 500 x 400 x 395 mm."),
        ("DIMENSIONS", "SUS304 foot operated single bowl rectangular hand wash sink wall mount."),
    ):
        known, unknown, _, _, _ = profiles[scope]
        add("new_unrequested_" + scope, text, record_for(scope, f"{known} preferred by customers or {unknown}"), {scope}, gated=())
    for name, suffix, options, text in (
        ("inner_material", " (material SUS304 or SUS316)", {"MATERIAL"}, rfq_for()),
        ("inner_actuation", " (foot operated or sensor operated)", {"ACTUATION"}, rfq_for()),
        ("inner_dimensions", " (size 500 x 400 x 395 mm or TBD)", {"DIMENSIONS"}, rfq_for()),
        ("inner_material_unknown_values", " (material SUS316 or SUS430)", {"MATERIAL"}, rfq_for()),
        ("inner_same_installation", " (wall mount preferred by customers or undermount)", {"INSTALLATION"}, rfq_for()),
        ("inner_material_unclosed", " (material SUS304 preferred by customers or SUS316", {"MATERIAL"}, rfq_for()),
        ("inner_dimensions_unclosed", " [dimensions 500 x 400 x 395 mm preferred or future configuration", {"DIMENSIONS"}, rfq_for()),
        ("inner_material_unrequested_outer", " (material SUS304 or SUS316)", {"MATERIAL"}, "SUS304 foot operated single bowl rectangular hand wash sink 500 x 400 x 395 mm."),
        ("inner_actuation_unrequested_outer", " (foot operated or sensor operated)", {"ACTUATION"}, "SUS304 foot operated single bowl rectangular hand wash sink 500 x 400 x 395 mm."),
        ("inner_dimensions_unrequested_outer", " (dimensions 500 x 400 x 395 mm or TBD)", {"DIMENSIONS"}, "SUS304 foot operated single bowl rectangular hand wash sink 500 x 400 x 395 mm."),
    ):
        record = mandatory_matrix_record()
        record["name"] += suffix
        add("new_" + name, text, record, options)
    return rows


class UnconsumedCandidateOptionTests(unittest.TestCase):
    check_case = QualifiedCandidateOptionTests.check_case

    def test_exact_root_six_original_inputs_and_expectations(self):
        for row in unconsumed_candidate_option_cases():
            if row[0].startswith("root6_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_unknown_connectors_unclosed_modifiers_and_reverse_operands_all_five_fields(self):
        for row in unconsumed_candidate_option_cases():
            if row[0].startswith("unconsumed_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_field_ownership_multiscope_packaging_and_unrequested_positive_controls(self):
        for row in unconsumed_candidate_option_cases():
            if row[0].startswith("new_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_recommendation_cannot_turn_unconsumed_or_into_approval_by_score(self):
        for row in unconsumed_candidate_option_cases():
            name, text, record, options, gated, action = row
            if gated:
                with self.subTest(name=name):
                    candidate = rfq_match.score_candidate(text, record)
                    candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False, unresolved_hard_requirement_scopes=[])
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate), name)


class RfqUnconsumedCandidateOptionCliTests(unittest.TestCase):
    cli = RfqPartialCandidateOptionCliTests.cli
    check_case = RfqQualifiedCandidateOptionCliTests.check_case

    def test_exact_root_six_real_cli(self):
        for row in unconsumed_candidate_option_cases():
            if row[0].startswith("root6_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_five_fields_unknown_connectors_unclosed_and_reverse_real_cli(self):
        for row in unconsumed_candidate_option_cases():
            if row[0].startswith("unconsumed_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)

    def test_multiscope_packaging_and_unrequested_field_controls_real_cli(self):
        for row in unconsumed_candidate_option_cases():
            if row[0].startswith("new_"):
                with self.subTest(name=row[0]):
                    self.check_case(row)


def packaging_option_ownership_cases():
    """Packing words only exempt their own actual OR operands, not a title."""
    prefix = "SUS304 Foot Operated Single Bowl Rectangular Hand Wash Sink - "
    titles = (
        ("earlier_packaging_word", "Packaging choice " + prefix + "Wall Mount preferred or future configuration", True),
        ("earlier_carton_word", "Carton " + prefix + "Wall Mount preferred or pallet", True),
        ("packaging_word_inside_modifier", prefix + "Wall Mount (packaging standard) preferred by customers or future configuration", True),
        ("actual_packaging_color", prefix + "Wall Mount with packaging blue or red", False),
        ("actual_carton_pallet", prefix + "Wall Mount with carton preferred or pallet", False),
        ("enclosed_actual_carton_pallet", prefix + "Wall Mount (carton or pallet)", False),
    )
    text = "Need SUS304 Foot Operated Single Bowl Rectangular Hand Wash Sink, wall mount, 500 x 400 x 395 mm."
    rows = []
    for name, title, unresolved in titles:
        record = {"canonical_model": "SYNTHETIC_PACK", "category": "Wash Sinks", "name": title, "attributes": [{"name": "Material", "raw_value": "SUS304"}, {"name": "Shape", "raw_value": "Single Bowl Rectangular"}, {"name": "Size", "raw_value": "500 x 400 x 395 mm"}]}
        scopes = {"INSTALLATION"} if unresolved else set()
        rows.append(("packing_" + name, text, record, scopes, scopes, "PREPARE_AND_REVIEW" if unresolved else "FILL_TO_SUBMIT"))
    return rows


class PackagingOptionOwnershipTests(unittest.TestCase):
    check_case = QualifiedCandidateOptionTests.check_case

    def test_packing_evidence_belongs_to_actual_operand_not_earlier_facts_or_modifier_words(self):
        for row in packaging_option_ownership_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)

    def test_recommendation_score_cannot_turn_unrelated_packing_word_into_structural_approval(self):
        for row in packaging_option_ownership_cases():
            name, text, record, options, gated, action = row
            if gated:
                with self.subTest(name=name):
                    candidate = rfq_match.score_candidate(text, record)
                    candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False, unresolved_hard_requirement_scopes=[])
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate), name)


class RfqPackagingOptionOwnershipCliTests(unittest.TestCase):
    cli = RfqPartialCandidateOptionCliTests.cli
    check_case = RfqQualifiedCandidateOptionCliTests.check_case

    def test_actual_packing_operand_ownership_real_cli(self):
        for row in packaging_option_ownership_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)


def unresolved_conjunction_cases():
    """Fixed r15 RFQ requirements, including unsupported mandatory operands."""
    rows = []
    profiles = {
        "MATERIAL": ("SUS304", "SUS316", "SUS201"),
        "ACTUATION": ("foot operated", "sensor operated", "knee operated"),
        "INSTALLATION": ("wall mount", "undermount", "deck mount"),
        "BOWL_COUNT": ("single bowl", "triple bowl", "double bowl"),
        "DIMENSIONS": ("500 x 400 x 395 mm", "buyer drawing", "900 x 800 x 795 mm"),
    }
    for scope, (known, unknown, other) in profiles.items():
        for connector in ("and", "&"):
            for kind, phrase in (
                ("unknown_value", f"Require {known} {connector} {unknown}."),
                ("unknown_reverse", f"Require {unknown} {connector} {known}."),
                ("unknown_operand", f"Require {known} {connector} future configuration."),
                ("unknown_operand_reverse", f"Require future configuration {connector} {known}."),
            ):
                rows.append((f"and_{scope}_{connector}_{kind}", rfq_for() + " " + phrase, mandatory_matrix_record(), {scope}, "PREPARE_AND_REVIEW"))
    rows.extend((
        ("and_formal_sus304_sus316", "Require SUS304 and SUS316 foot operated single bowl hand wash sink wall mount 500 x 400 x 395 mm.", mandatory_matrix_record(), {"MATERIAL"}, "PREPARE_AND_REVIEW"),
        ("and_formal_shared_actuation", rfq_for() + " Both foot and knee operated required.", mandatory_matrix_record("Knee"), {"ACTUATION"}, "PREPARE_AND_REVIEW"),
        ("and_shared_installation", rfq_for() + " Both wall and deck mount required.", mandatory_matrix_record(), {"INSTALLATION"}, "PREPARE_AND_REVIEW"),
        ("and_shared_bowl", rfq_for() + " Both single and double bowl required.", mandatory_matrix_record(), {"BOWL_COUNT"}, "PREPARE_AND_REVIEW"),
        ("and_shared_grade", rfq_for() + " Both SUS304 and 316 grade required.", mandatory_matrix_record(), {"MATERIAL"}, "PREPARE_AND_REVIEW"),
        ("and_shared_size", rfq_for() + " Both 500 x 400 x 395 mm and buyer drawing dimensions required.", mandatory_matrix_record(), {"DIMENSIONS"}, "PREPARE_AND_REVIEW"),
        ("and_known_dual_missing", rfq_for() + " Foot operated and knee operated required.", mandatory_matrix_record(), {"ACTUATION"}, "PREPARE_AND_REVIEW"),
        ("and_known_dual_verified", rfq_for() + " Foot operated and knee operated required.", mandatory_matrix_record("Foot Operated and Knee"), set(), "FILL_TO_SUBMIT"),
        ("and_known_crossfield", "Require SUS304 and foot operated single bowl hand wash sink wall mount 500 x 400 x 395 mm.", mandatory_matrix_record(), set(), "FILL_TO_SUBMIT"),
        ("and_ordinary_request", rfq_for() + " Require SUS304 and please send catalogue.", mandatory_matrix_record(), set(), "FILL_TO_SUBMIT"),
        ("and_permission_scope_list", rfq_for() + " Dimensions and actuation customization accepted.", mandatory_matrix_record(), set(), "FILL_TO_SUBMIT"),
        ("and_fixed_no_conjunction", rfq_for(), mandatory_matrix_record(), set(), "FILL_TO_SUBMIT"),
    ))
    return rows


def independent_option_operand_cases():
    """Every OR side keeps its own field, including unconsumed modifiers."""
    rows = []
    profiles = {
        "MATERIAL": ("SUS304", "SUS316"),
        "ACTUATION": ("Foot Operated", "Sensor Operated"),
        "INSTALLATION": ("Wall Mount", "Undermount"),
        "BOWL_COUNT": ("Single Bowl", "Triple Bowl"),
        "DIMENSIONS": ("500 x 400 x 395 mm", "size TBD"),
    }
    requirements = {
        "MATERIAL": "SUS304", "ACTUATION": "Foot Operated", "INSTALLATION": "Wall Mount",
        "BOWL_COUNT": "Single Bowl", "DIMENSIONS": "500 x 400 x 395 mm",
    }
    modifiers = ("preferred by customers", "with manufacturer default finish", "(available on request)")
    for left_scope, (left_known, _) in profiles.items():
        for right_scope, (right_known, right_unknown) in profiles.items():
            if left_scope == right_scope:
                continue
            text = "Require " + " ".join(value for scope, value in requirements.items() if scope != right_scope) + " rectangular hand wash sink."
            for index, modifier in enumerate(modifiers):
                for kind, right in (("known", right_known), ("unknown", right_unknown)):
                    record = mandatory_matrix_record()
                    record["name"] += f"; {left_known} {modifier} or {right}"
                    rows.append((f"side_{left_scope}_{right_scope}_{index}_{kind}", text, record, {left_scope, right_scope}, {left_scope}, "PREPARE_AND_REVIEW"))
    for name, phrase in (
        ("formal", "Wall Mount preferred by customers or Sensor Operated"),
        ("reverse", "Sensor Operated preferred by customers or Wall Mount"),
        ("both_qualified", "Wall Mount preferred by customers or Sensor Operated available on request"),
    ):
        record = mandatory_matrix_record()
        record["name"] += "; " + phrase
        text = "SUS304 single bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm."
        rows.append(("side_installation_only_" + name, text, record, {"INSTALLATION", "ACTUATION"}, {"INSTALLATION"}, "PREPARE_AND_REVIEW"))
    return rows


def enclosed_packaging_operand_cases():
    """A packing noun inside a completed structural modifier is not an operand."""
    rows = []
    for noun, opposite in (("carton", "pallet"), ("pallet", "carton"), ("box", "crate"), ("crate", "box")):
        for enclosure, ending in (("(", ")"), ("[", "]")):
            for modifier in ("preferred by customers", "with manufacturer default finish"):
                record = mandatory_matrix_record()
                record["name"] += f" {enclosure}{noun}{ending} {modifier} or {opposite}"
                rows.append((f"packing_enclosed_{noun}_{enclosure}_{modifier}", rfq_for(), record, {"INSTALLATION"}, {"INSTALLATION"}, "PREPARE_AND_REVIEW"))
        for kind, suffix in (
            ("bare_pair", f" with {noun} preferred by customers or {opposite}"),
            ("shared_enclosure", f" ({noun} preferred by customers or {opposite})"),
            ("separate_field", f"; packing {noun} or {opposite}"),
        ):
            record = mandatory_matrix_record()
            record["name"] += suffix
            rows.append((f"packing_true_{noun}_{kind}", rfq_for(), record, set(), set(), "FILL_TO_SUBMIT"))
    return rows


def shared_suffix_requirement_cases():
    """No fully spelled left value elsewhere may mask a lost shared suffix."""
    return [
        ("and_shared_suffix_actuation_only", "Require SUS304 both foot and knee operated single bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm.", mandatory_matrix_record("Knee"), {"ACTUATION"}, "PREPARE_AND_REVIEW"),
        ("and_shared_suffix_installation_only", "Require SUS304 foot operated single bowl rectangular hand wash sink both wall and deck mount 500 x 400 x 395 mm.", mandatory_matrix_record(mount="Deck Mount"), {"INSTALLATION"}, "PREPARE_AND_REVIEW"),
        ("and_shared_suffix_bowl_only", "Require SUS304 foot operated both single and double bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm.", mandatory_matrix_record(bowl="Double Bowl"), {"BOWL_COUNT"}, "PREPARE_AND_REVIEW"),
        ("and_shared_suffix_grade_only", "Require both SUS304 and 316 grade foot operated single bowl rectangular hand wash sink wall mount 500 x 400 x 395 mm.", mandatory_matrix_record(), {"MATERIAL"}, "PREPARE_AND_REVIEW"),
    ]


def low_score_option_action_corrections():
    """Exact successor predicate IDs; never broaden old327 or positive controls."""
    return {
        "side_INSTALLATION_MATERIAL_0_known", "side_INSTALLATION_MATERIAL_0_unknown",
        "side_INSTALLATION_MATERIAL_1_known", "side_INSTALLATION_MATERIAL_1_unknown",
        "side_INSTALLATION_MATERIAL_2_known", "side_INSTALLATION_MATERIAL_2_unknown",
        "side_INSTALLATION_ACTUATION_0_known", "side_INSTALLATION_ACTUATION_0_unknown",
        "side_INSTALLATION_ACTUATION_1_known", "side_INSTALLATION_ACTUATION_1_unknown",
        "side_INSTALLATION_ACTUATION_2_known", "side_INSTALLATION_ACTUATION_2_unknown",
        "side_installation_only_formal", "side_installation_only_reverse", "side_installation_only_both_qualified",
    }


def unknown_only_conjunction_cases():
    """Explicit unknown AND values cannot become an empty hard requirement."""
    rows = []
    for scope, phrase in (
        ("MATERIAL", "SUS316 and SUS430"),
        ("ACTUATION", "sensor operated and lever operated"),
        ("INSTALLATION", "undermount and flushmount"),
        ("BOWL_COUNT", "triple bowl and quad bowl"),
        ("DIMENSIONS", "dimensions buyer drawing and dimensions engineer drawing"),
    ):
        for connector in ("and", "&"):
            text = rfq_for() + " Require " + phrase.replace(" and ", f" {connector} ") + "."
            rows.append((f"and_unknown_only_{scope}_{connector}", text, mandatory_matrix_record(), {scope}, "PREPARE_AND_REVIEW"))
    return rows


def conjunction_nonstructural_controls():
    rows = []
    for connector in ("and", "&"):
        for kind, value in (("nouns", f"carton {connector} pallet"), ("label", f"packing blue {connector} red")):
            rows.append((f"and_true_packing_{kind}_{connector}", rfq_for().rstrip(" .") + " with " + value + ".", mandatory_matrix_record(), set(), "FILL_TO_SUBMIT"))
    return rows


class UnresolvedConjunctionTests(unittest.TestCase):
    def check_case(self, row):
        name, text, record, scopes, action = row
        candidate = rfq_match.score_candidate(text, record)
        self.assertEqual(sorted(scopes), candidate["unresolved_hard_requirement_scopes"], name)
        self.assertEqual("REVIEW" if scopes else "EXACT_OR_NEAR", candidate["mode"], name)
        self.assertEqual(action, rfq_match.recommended_action(text, candidate), name)
        self.assertFalse(candidate["customization_requires_review"], name)

    def test_unknown_and_operands_and_shared_suffix_requirements_all_five_fields(self):
        for row in unresolved_conjunction_cases() + shared_suffix_requirement_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)

    def test_recommendation_rechecks_unconsumed_mandatory_and_before_high_score(self):
        for name, text, record, scopes, action in unresolved_conjunction_cases() + shared_suffix_requirement_cases():
            if scopes:
                with self.subTest(name=name):
                    candidate = rfq_match.score_candidate(text, record)
                    candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False, unresolved_hard_requirement_scopes=[])
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate), name)

    def test_explicit_unknown_and_values_all_five_fields(self):
        for row in unknown_only_conjunction_cases() + conjunction_nonstructural_controls():
            with self.subTest(name=row[0]):
                self.check_case(row)


class RfqUnresolvedConjunctionCliTests(unittest.TestCase):
    cli = RfqPartialCandidateOptionCliTests.cli

    def test_unknown_and_shared_suffix_requirements_real_cli(self):
        for name, text, record, scopes, action in unresolved_conjunction_cases() + shared_suffix_requirement_cases():
            with self.subTest(name=name):
                result = self.cli(text, [record])
                candidate = result["candidates"][0]
                self.assertEqual(sorted(scopes), candidate["unresolved_hard_requirement_scopes"], name)
                self.assertEqual("REVIEW" if scopes else "EXACT_OR_NEAR", candidate["mode"], name)
                self.assertEqual(action, result["recommended_action"], name)

    def test_explicit_unknown_and_values_all_five_fields_real_cli(self):
        for name, text, record, scopes, action in unknown_only_conjunction_cases() + conjunction_nonstructural_controls():
            with self.subTest(name=name):
                result = self.cli(text, [record])
                candidate = result["candidates"][0]
                self.assertEqual(sorted(scopes), candidate["unresolved_hard_requirement_scopes"], name)
                self.assertEqual("REVIEW" if scopes else "EXACT_OR_NEAR", candidate["mode"], name)
                self.assertEqual(action, result["recommended_action"], name)


class IndependentOptionOperandTests(unittest.TestCase):
    def check_case(self, row):
        name, text, record, options, gated, action = row
        candidate = rfq_match.score_candidate(text, record)
        if name in low_score_option_action_corrections() and rfq_match.recommended_action(text, candidate) == "SKIP_RFQ":
            self.assertLess(candidate["score"], 4, name)
            row = (name, text, record, options, gated, "SKIP_RFQ")
        QualifiedCandidateOptionTests.check_case(self, row)

    def test_each_or_operand_retains_its_own_requested_or_unrequested_field(self):
        for row in independent_option_operand_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)

    def test_enclosed_packaging_nouns_and_true_operand_controls(self):
        for row in enclosed_packaging_operand_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)


class RfqIndependentOptionOperandCliTests(unittest.TestCase):
    cli = RfqPartialCandidateOptionCliTests.cli
    def check_case(self, row):
        name, text, record, options, gated, action = row
        result = self.cli(text, [record])
        candidate = result["candidates"][0]
        if name in low_score_option_action_corrections() and result["recommended_action"] == "SKIP_RFQ":
            self.assertLess(candidate["score"], 4, name)
            action = "SKIP_RFQ"
        self.assertEqual(action, result["recommended_action"], name)
        self.assertEqual("REVIEW" if gated else "EXACT_OR_NEAR", candidate["mode"], name)
        self.assertEqual(gated, set(candidate["unresolved_hard_requirement_scopes"]), name)
        self.assertEqual(bool(gated), candidate["hard_requirements_requires_review"], name)
        self.assertFalse(candidate["customization_requires_review"], name)
        if gated:
            self.assertTrue(gated <= set(candidate["mismatch_scopes"]), name)
            self.assertIn("UNRESOLVED_HARD_REQUIREMENTS", candidate["mismatch_scopes"], name)
        else:
            self.assertEqual([], candidate["mismatches"], name)

    def test_each_or_operand_scope_real_cli(self):
        for row in independent_option_operand_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)

    def test_enclosed_packaging_nouns_and_true_operands_real_cli(self):
        for row in enclosed_packaging_operand_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)


def known_shared_actuation_conjunction_cases():
    """Exact legacy dual shorthand, plus its reversed known operand order."""
    rows = []
    for name, actuation in (("known_shared_foot_then_knee", "Foot and Knee"), ("known_shared_knee_then_foot", "Knee and Foot")):
        record = {"canonical_model": "SYNTHETICDUAL", "name": f"SUS304 {actuation} Operated Hand Wash Sink - Wall Mount", "category": "Wash Sinks", "attributes": [{"name": "Material", "raw_value": "SUS304"}, {"name": "Installation", "raw_value": "Wall Mount"}, {"name": "Shape", "raw_value": "Single Bowl Rectangular"}, {"name": "Size", "raw_value": "500 x 400 x 395 mm"}]}
        text = f"SUS304 {actuation} operated single bowl rectangular hand wash sink, wall mount 500 x 400 x 395 mm. "
        rows.append((name, text, record, set(), set(), "FILL_TO_SUBMIT"))
    return rows


class KnownSharedActuationConjunctionTests(unittest.TestCase):
    check_case = QualifiedCandidateOptionTests.check_case

    def test_legacy_dual_known_shorthand_and_reverse_are_two_mandatory_values(self):
        for row in known_shared_actuation_conjunction_cases():
            name, text, record, _, _, _ = row
            with self.subTest(name=name):
                groups, ambiguous = rfq_match.hard_requirement_groups(text, rfq_match.concepts(text))
                self.assertEqual([{"FOOT_OPERATED"}, {"KNEE_OPERATED"}], groups["ACTUATION"], name)
                self.assertNotIn("ACTUATION", ambiguous, name)
                self.assertTrue({"FOOT_OPERATED", "KNEE_OPERATED"} <= rfq_match.concepts(rfq_match.structured_blob(record)), name)
                self.check_case(row)


class RfqKnownSharedActuationConjunctionCliTests(unittest.TestCase):
    cli = RfqPartialCandidateOptionCliTests.cli
    check_case = RfqQualifiedCandidateOptionCliTests.check_case

    def test_legacy_dual_known_shorthand_and_reverse_real_cli(self):
        for row in known_shared_actuation_conjunction_cases():
            with self.subTest(name=row[0]):
                self.check_case(row)


def formal20_boundary_cases():
    """Frozen minimal punctuation, unsupported-mandatory and slash repairs."""
    rows = []
    def add(name, text, record, scope=None, action="FILL_TO_SUBMIT", mode="EXACT_OR_NEAR"):
        rows.append((name, text, record, {scope} if scope else set(), "PREPARE_AND_REVIEW" if scope else action, "REVIEW" if scope else mode))
    for index, separator in enumerate((", ", ",\n", ", \t ", ", , ", ",\n, ")):
        record = mandatory_matrix_record()
        record["name"] += " (standard)" + separator + "or another option"
        add(f"formal20_leading_or_{index}", rfq_for(), record, "INSTALLATION")
    for index, qualifier in enumerate((" standard", " preferred by customers")):
        record = mandatory_matrix_record()
        record["name"] += qualifier + ", or another option"
        add(f"formal20_naked_or_{index}", rfq_for(), record, "INSTALLATION")
    for scope, attribute, phrase in (
        ("MATERIAL", "Material", "SUS304 (standard), or another option"),
        ("ACTUATION", "Operation", "Foot Operated (optional), or Sensor Operated"),
        ("BOWL_COUNT", "Shape", "Single Bowl (standard), or Triple Bowl"),
        ("DIMENSIONS", "Size", "500 x 400 x 395 mm (standard), or another option"),
    ):
        record = mandatory_matrix_record()
        record["attributes"] = [item for item in record["attributes"] if item["name"] != attribute] + [{"name": attribute, "raw_value": phrase}]
        add(f"formal20_leading_or_{scope}", rfq_for(), record, scope)
    for index, separator in enumerate((". ", "; ", "\n")):
        add(f"formal20_mandatory_material_{index}", rfq_for() + "Require SUS316" + separator + "Dimensions customization accepted.", mandatory_matrix_record(), "MATERIAL")
    add("formal20_mandatory_material_before", "Require SUS316. " + rfq_for(suffix="Dimensions customization accepted."), mandatory_matrix_record(), "MATERIAL")
    add("formal20_mandatory_material_no_permission", rfq_for(suffix="Require SUS316."), mandatory_matrix_record(), "MATERIAL")
    for scope, phrase in (("ACTUATION", "sensor operated"), ("INSTALLATION", "undermount"), ("BOWL_COUNT", "triple bowl")):
        add(f"formal20_mandatory_unknown_{scope}", rfq_for(suffix=f"Require {phrase}. Dimensions customization accepted."), mandatory_matrix_record(), scope)
    add("formal20_mandatory_unknown_material_and", rfq_for(suffix="Require SUS304 and SUS316. Dimensions customization accepted."), mandatory_matrix_record(), "MATERIAL")
    for index, phrase in enumerate(("SUS304/SUS316", "SUS304 / SUS316", "SUS304 (standard) / SUS316", "SUS316 / SUS304 (standard)")):
        add(f"formal20_slash_{index}", rfq_for(), mandatory_matrix_record(material=phrase), "MATERIAL")
    add("formal20_known_material_slash", rfq_for(), mandatory_matrix_record(material="SUS304/SUS201"), "MATERIAL")
    add("formal20_fixed_modifier", rfq_for(), mandatory_matrix_record(mount="Wall Mount (standard)"))
    add("formal20_rfq_explicit_or", rfq_for().replace("SUS304", "SUS304 or SUS201"), mandatory_matrix_record())
    for row in known_shared_actuation_conjunction_cases():
        add("formal20_" + row[0], row[1], row[2])
    add("formal20_approved_material_custom", rfq_for(suffix="Material customization accepted."), mandatory_matrix_record(material="SUS201"), action="FILL_TO_SUBMIT", mode="CUSTOM")
    add("formal20_ordinary_request", rfq_for(suffix="Dimensions customization accepted. Please send your catalogue. Thanks."), mandatory_matrix_record())
    for name, suffix in (("slash", "; packing blue/red"), ("leading_or", "; packing blue, or red")):
        record = mandatory_matrix_record()
        record["name"] += suffix
        add("formal20_packaging_" + name, rfq_for(), record)
    text = rfq_for().replace("SUS304 ", "")
    add("formal20_unrequested_material_slash", text, mandatory_matrix_record(material="SUS304/SUS316"))
    return rows


class Formal20BoundaryTests(unittest.TestCase):
    def test_punctuation_mandatory_unknown_and_slash_require_complete_facts(self):
        for name, text, record, scopes, action, mode in formal20_boundary_cases():
            with self.subTest(name=name):
                candidate = rfq_match.score_candidate(text, record)
                self.assertEqual(sorted(scopes), candidate["unresolved_hard_requirement_scopes"], name)
                self.assertEqual(mode, candidate["mode"], name)
                self.assertEqual(action, rfq_match.recommended_action(text, candidate), name)
                if scopes:
                    self.assertTrue(scopes <= set(candidate["mismatch_scopes"]), name)
                    self.assertIn("UNRESOLVED_HARD_REQUIREMENTS", candidate["mismatch_scopes"], name)
                    self.assertTrue(candidate["hard_requirements_requires_review"], name)

    def test_recommendation_rechecks_each_unresolved_formal20_domain(self):
        for name, text, record, scopes, _, _ in formal20_boundary_cases():
            if scopes:
                with self.subTest(name=name):
                    candidate = rfq_match.score_candidate(text, record)
                    candidate.update(mode="EXACT_OR_NEAR", score=99, mismatches=[], mismatch_scopes=[], hard_requirements_requires_review=False, unresolved_hard_requirement_scopes=[])
                    self.assertEqual("PREPARE_AND_REVIEW", rfq_match.recommended_action(text, candidate), name)


class RfqFormal20BoundaryCliTests(unittest.TestCase):
    def test_punctuation_mandatory_unknown_and_slash_strict_three_second_cli(self):
        for name, text, record, scopes, action, mode in formal20_boundary_cases():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(prefix="rfq-formal20-cli-", dir=r"C:\Windows\Temp") as directory:
                    root = Path(directory)
                    official, catalog, overlay = [root / filename for filename in ("official.jsonl", "catalog.jsonl", "overlay.jsonl")]
                    official.write_text(json.dumps(record) + "\n", encoding="utf-8")
                    catalog.write_text("", encoding="utf-8")
                    overlay.write_text("", encoding="utf-8")
                    process = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(MODULE_PATH), "--rfq-text", text, "--official-current", str(official), "--catalog-index", str(catalog), "--disposition", str(overlay)], cwd=r"C:\Windows\Temp", capture_output=True, text=True, encoding="utf-8", timeout=3, env={"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows", "TEMP": r"C:\Windows\Temp", "TMP": r"C:\Windows\Temp"})
                    self.assertEqual(0, process.returncode, process.stderr)
                    self.assertNotIn("Traceback", process.stderr)
                    result = json.loads(process.stdout)
                    candidate = result["candidates"][0]
                    self.assertEqual(action, result["recommended_action"], name)
                    self.assertEqual(mode, candidate["mode"], name)
                    self.assertEqual(sorted(scopes), candidate["unresolved_hard_requirement_scopes"], name)


if __name__ == "__main__":
    unittest.main()
