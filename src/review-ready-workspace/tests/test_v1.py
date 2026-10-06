from __future__ import annotations

import ast
import csv
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(os.environ.get("SELLERKIT_ALIBABA_WORKSPACE", str(Path(__file__).resolve().parents[1])))
PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "review-ready-plugin"
MCP_ROOT = PLUGIN_ROOT / "mcp"
TEST_DEPS = PROJECT_ROOT / ".test-deps"
for entry in (str(TEST_DEPS), str(MCP_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from review_ready.config import EXPECTED, PRIORITY_MODELS, default_paths
from review_ready.images import ALLOWED_HOSTS
from review_ready.review import review_state, validate_review_identity
from review_ready.package import compile_review_package, export_evidence_receipt
from review_ready.store import _load_aliases, inspect_sources, validate_database_state
from review_ready.utils import extract_numbers_and_units, secret_findings, secret_text_findings, sha256_file, stable_json_text


def load_mcp_server():
    spec = importlib.util.spec_from_file_location("sellerkit_review_ready_server", MCP_ROOT / "server.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load MCP server")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def walk_keys(value, location="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            yield location, str(key)
            yield from walk_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_keys(child, f"{location}[{index}]")


class ReviewReadyV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("SELLERKIT_ALIBABA_CONFIG"):
            raise unittest.SkipTest("Optional integration requires explicitly approved local config; no business data is bundled")
        cls.paths = default_paths()
        cls.index_path = cls.paths.output_root / "current_priority_packages.json"
        if not cls.index_path.is_file():
            raise unittest.SkipTest("Optional local integration fixture missing; prepare your own approved review workspace first")
        cls.index = json.loads(cls.index_path.read_text(encoding="utf-8"))
        cls.connection = sqlite3.connect(
            f"file:{cls.paths.database_path}?mode=ro", uri=True
        )
        cls.connection.row_factory = sqlite3.Row
        cls.alibaba_rows = [
            json.loads(row["product_json"])
            for row in cls.connection.execute(
                "SELECT product_json FROM alibaba_products ORDER BY ordinal"
            )
        ]
        cls.eligible = [
            row for row in cls.alibaba_rows
            if row["record_disposition"] == "HISTORICAL_REFERENCE_ONLY"
        ]
        cls.waste = [
            row for row in cls.alibaba_rows
            if row["record_disposition"] == "UNUSABLE_WASTE_RECORD"
        ]
        cls.eligible_ids = {row["product_id"] for row in cls.eligible}
        cls.waste_ids = {row["product_id"] for row in cls.waste}

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def scalar(self, query: str):
        return self.connection.execute(query).fetchone()[0]

    def test_01_frozen_sources_and_path_contract(self):
        report = inspect_sources(self.paths)
        self.assertEqual(report["status"], "PASS_FROZEN_SOURCES_VERIFIED")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["observed"]["website_archive_sha256"], EXPECTED["website_archive_sha256"])
        self.assertEqual(report["observed"]["website_assets_sha256"], EXPECTED["website_assets_sha256"])
        self.assertEqual(report["observed"]["website_dist_tree_sha256"], EXPECTED["website_dist_tree_sha256"])
        self.assertEqual(report["observed"]["alibaba_snapshot_tree_sha256"], EXPECTED["alibaba_snapshot_tree_sha256"])
        self.assertEqual(report["overlay"]["date_field"], "gmt_modified")
        self.assertEqual(report["overlay"]["date_semantics"], "last_modified_proxy_not_creation_date")
        with mock.patch.dict(
            os.environ,
            {"SELLERKIT_ALIBABA_PROJECT_ROOT": r"C:\Windows\Temp\not-allowlisted"},
        ):
            with self.assertRaises(ValueError):
                default_paths()
        with mock.patch.dict(
            os.environ,
            {"SELLERKIT_ALIBABA_PROJECT_ROOT": r"\\server\share\not-local"},
        ):
            with self.assertRaises(ValueError):
                default_paths()
        state = validate_database_state(self.paths)
        self.assertEqual(state["status"], "PASS_DATABASE_AND_SOURCE_CHAIN_VERIFIED")

    def test_02_database_counts_and_coverage(self):
        self.assertEqual(self.scalar("SELECT count(*) FROM official_products"), EXPECTED["website_products"])
        self.assertEqual(self.scalar("SELECT count(*) FROM official_specs"), EXPECTED["website_specs"])
        self.assertEqual(self.scalar("SELECT count(*) FROM official_specs WHERE trim(raw_value)=''"), EXPECTED["website_empty_specs"])
        self.assertEqual(self.scalar("SELECT count(*) FROM alibaba_products"), EXPECTED["alibaba_products"])
        self.assertEqual(len(self.eligible), EXPECTED["historical_reference"])
        self.assertEqual(len(self.waste), EXPECTED["unusable_waste"])
        coverage = Counter()
        for row in self.eligible:
            safe = row["safe_snapshot"]
            product = safe["product"]
            trade = safe["trade"]
            price = product.get("price") or {}
            logistics = trade.get("logistics") or {}
            coverage["tiers"] += bool(price.get("ladders"))
            coverage["ranges"] += bool(price.get("range"))
            coverage["catalog_prices"] += bool(row.get("sku_min_price") and row.get("sku_max_price"))
            coverage["sku_attrs"] += bool((product.get("sku") or {}).get("attributes"))
            coverage["sku_samples"] += bool((product.get("sku") or {}).get("samples"))
            coverage["lead"] += bool(trade.get("lead_time"))
            coverage["packaging"] += bool(logistics.get("packaging_properties"))
        self.assertEqual(
            coverage,
            Counter(
                tiers=EXPECTED["historical_structured_ladder_price_coverage"],
                ranges=66,
                catalog_prices=EXPECTED["historical_catalog_sku_price_coverage"],
                sku_attrs=187,
                sku_samples=EXPECTED["historical_sku_sample_coverage"],
                lead=188,
                packaging=EXPECTED["historical_packaging_coverage"],
            ),
        )
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_03_waste_is_minimal_and_absent_from_rag(self):
        forbidden_nonempty = {
            "subject", "display_status", "detail_url", "group_names", "currency",
            "minimum_order_quantity", "sku_min_price", "sku_max_price", "quality_score",
            "gmt_modified", "safe_snapshot",
        }
        for row in self.waste:
            self.assertFalse(row["allow_rag"])
            self.assertFalse(row["fact_extraction"])
            self.assertFalse(row["asset_reuse"])
            self.assertIsNone(row["safe_snapshot"])
            self.assertTrue(all(row.get(key) in (None, "", [], {}) for key in forbidden_nonempty))
        historical_rows = [
            json.loads(line)
            for line in (self.paths.work_root / "alibaba_historical_sanitized.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertEqual(len(historical_rows), EXPECTED["historical_reference"])
        self.assertTrue(all(row["allow_rag"] for row in historical_rows))
        self.assertFalse({row["product_id"] for row in historical_rows} & self.waste_ids)

    def test_04_matching_is_fail_closed(self):
        decisions = [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT decision_json FROM match_decisions ORDER BY canonical_model"
            )
        ]
        self.assertEqual(sum(bool(row["exact_product_ids"]) for row in decisions), EXPECTED["exact_intersection"])
        self.assertEqual(sum(not row["exact_product_ids"] for row in decisions), EXPECTED["exact_missing_candidates"])
        self.assertTrue(all(row["missing_is_confirmed"] is False for row in decisions))
        self.assertTrue(all(row["conflict_threshold_status"] == "UNVALIDATED_HEURISTIC_REVIEW_ONLY" for row in decisions))
        self.assertTrue(all(row["classification_effect"] == "QUEUE_FOR_HUMAN_REVIEW_ONLY_NEVER_CONFIRM_MISSING_OR_RELEASE" for row in decisions))
        allowed_states = {
            "CONFIRMED_PRESENT", "CONFIRMED_PRESENT_UNUSABLE_REFERENCE", "CANDIDATE_MISSING",
            "AMBIGUOUS_ALIAS", "VARIANT_OR_ACCESSORY", "BLOCKED_INSUFFICIENT_EVIDENCE",
        }
        self.assertTrue({row["status"] for row in decisions} <= allowed_states)
        similar = [candidate for row in decisions for candidate in row["similar_products"]]
        self.assertTrue(all(candidate["product_id"] in self.eligible_ids for candidate in similar))
        bubmi = next(row for row in decisions if row["raw_model"] == "MODEL-021")
        self.assertEqual(bubmi["status"], "VARIANT_OR_ACCESSORY")

    def test_05_core_product_images_are_locally_bound_and_verified(self):
        rows = [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT asset_json FROM official_assets WHERE role IN ('main_image','gallery')"
            )
        ]
        self.assertEqual(Counter(row["role"] for row in rows), Counter(main_image=EXPECTED["website_main_image_references"], gallery=EXPECTED["website_gallery_references"]))
        self.assertTrue(all(row["verification_status"] == "LOCAL_URL_BOUND_VERIFIED" for row in rows))
        self.assertTrue(all(row["local_file"] and row["sha256"] for row in rows))
        for row in rows:
            local_path = Path(row["local_file"]).resolve()
            local_path.relative_to(self.paths.website_image_blob_root.resolve())
            self.assertTrue(local_path.is_file())
            self.assertEqual(sha256_file(local_path), row["sha256"])
            self.assertGreater(int(row["width"]), 0)
            self.assertGreater(int(row["height"]), 0)
        self.assertEqual(len({row["sha256"] for row in rows}), 293)
        view_root = self.paths.output_root / "website_images" / "main_by_model"
        view_index = [
            json.loads(line)
            for line in (view_root / "index.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(view_index), EXPECTED["website_products"])
        self.assertEqual(len({row["canonical_model"] for row in view_index}), EXPECTED["website_products"])
        self.assertTrue(all(Path(row["view_file"]).is_file() for row in view_index))
        self.assertTrue(all(sha256_file(Path(row["view_file"])) == row["sha256"] for row in view_index))

    def test_06_priority_packages_and_receipts(self):
        self.assertEqual(self.index["status"], "PASS_PRIORITY_REVIEW_PACKAGES_BUILT_NO_REMOTE_WRITE")
        self.assertEqual(self.index["local_review_status"], "READY_FOR_LOCAL_HUMAN_REVIEW")
        self.assertEqual(self.index["remote_write_status"], "DISABLED_NOT_REQUESTED")
        self.assertEqual(self.index["release_status"], "NOT_RELEASED_V1_LOCAL_ONLY")
        entries = self.index["packages"]
        self.assertEqual({row["model"] for row in entries}, set(PRIORITY_MODELS))
        self.assertEqual(len(entries), 5)
        active_directories = {path.resolve() for path in (self.paths.output_root / "review_packages").iterdir() if path.is_dir()}
        indexed_directories = {Path(entry["package_path"]).resolve() for entry in entries}
        self.assertEqual(active_directories, indexed_directories)
        index_receipt = json.loads(
            (self.paths.output_root / "current_priority_packages.receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(index_receipt["index_sha256"], sha256_file(self.index_path))
        self.assertEqual(index_receipt["active_directory_count"], 5)
        self.assertEqual(index_receipt["unindexed_active_directory_count"], 0)
        for entry in entries:
            package_dir = Path(entry["package_path"])
            self.assertEqual(
                {path.name for path in package_dir.iterdir()},
                {"candidate_product.json", "field_provenance.jsonl", "match_decision.json", "similar_products.json", "commercial_suggestions.csv", "asset_manifest.csv", "fill_plan.preview.json", "review_state.json", "review_report.html", "receipt.json"},
            )
            receipt = json.loads((package_dir / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "PASS_LOCAL_REVIEW_PACKAGE_READY_NO_REMOTE_WRITE")
            self.assertEqual(receipt["build_status"], "PASS_LOCAL_REVIEW_PACKAGE_READY_NO_REMOTE_WRITE")
            self.assertEqual(receipt["local_review_status"], "READY_FOR_LOCAL_HUMAN_REVIEW")
            self.assertEqual(receipt["remote_write_status"], "DISABLED_NOT_REQUESTED")
            self.assertEqual(receipt["release_status"], "NOT_RELEASED_V1_LOCAL_ONLY")
            self.assertFalse(receipt["review_state"]["approved_internal_promotion_supported_in_v1"])
            for filename, expected_hash in receipt["files"].items():
                self.assertEqual(sha256_file(package_dir / filename), expected_hash)
            receipt_core = {
                key: value
                for key, value in receipt.items()
                if key not in {"files", "package_digest", "package_digest_definition"}
            }
            digest_input = {"files": receipt["files"], "receipt_core": receipt_core}
            self.assertEqual(
                hashlib.sha256(stable_json_text(digest_input).encode("utf-8")).hexdigest(),
                receipt["package_digest"],
            )
            similar = json.loads((package_dir / "similar_products.json").read_text(encoding="utf-8"))["candidates"]
            self.assertTrue(all(row["product_id"] in self.eligible_ids for row in similar))
            with (package_dir / "commercial_suggestions.csv").open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(
                    reader.fieldnames,
                    ["rank", "similarity_score", "source_product_id", "source_model", "source_subject", "field", "suggested_value", "source_locator", "source_disposition", "copy_policy", "approval_status", "approved_for_fill", "risk_note", "source_observed_at", "source_file_sha256", "comparison_status"],
                )
                suggestions = list(reader)
            self.assertTrue(all(row["source_product_id"] in self.eligible_ids for row in suggestions))
            self.assertTrue(all(row["approval_status"] == "HUMAN_CONFIRM_REQUIRED" for row in suggestions))
            self.assertTrue(all(row["source_disposition"] == "HISTORICAL_REFERENCE_ONLY" for row in suggestions))
            self.assertTrue(
                all(row["copy_policy"] in {"SUGGESTED_FROM_HISTORICAL", "REFERENCE_ONLY_NOT_COPYABLE"} for row in suggestions)
            )
            self.assertTrue(all(row["approved_for_fill"] == "False" for row in suggestions))
            self.assertTrue(all(len(row["source_file_sha256"]) == 64 for row in suggestions))
            with (package_dir / "asset_manifest.csv").open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(
                    reader.fieldnames,
                    ["model", "order", "role", "asset_class", "required_for_listing", "source_url", "source_basename", "materialization", "local_file", "local_file_exists", "size_bytes", "sha256", "width", "height", "manual_upload_required", "manual_action", "remote_asset_id", "approval_status", "approved_for_fill"],
                )
                assets = list(reader)
            core = [row for row in assets if row["role"] in {"main_image", "gallery"}]
            self.assertTrue(core)
            self.assertTrue(all(row["materialization"] == "LOCAL_URL_BOUND_VERIFIED" for row in core))
            self.assertTrue(all(row["approval_status"] == "HUMAN_CONFIRM_REQUIRED" for row in assets))
            self.assertTrue(all(row["approved_for_fill"] == "False" for row in assets))
            self.assertTrue(all(row["sha256"] and row["local_file"] for row in core))

    def test_07_json_schemas_validate_real_packages(self):
        schema_root = PROJECT_ROOT / "schemas"
        schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in schema_root.glob("*.schema.json")
        }
        registry = Registry()
        for schema in schemas.values():
            registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        mapping = {
            "candidate_product.json": "candidate_product.schema.json",
            "review_state.json": "review_state.schema.json",
            "fill_plan.preview.json": "fill_plan.preview.schema.json",
            "receipt.json": "review_package_receipt.schema.json",
            "match_decision.json": "match_decision.schema.json",
            "similar_products.json": "similar_products.schema.json",
        }
        for entry in self.index["packages"]:
            package_dir = Path(entry["package_path"])
            for data_name, schema_name in mapping.items():
                data = json.loads((package_dir / data_name).read_text(encoding="utf-8"))
                errors = sorted(
                    Draft202012Validator(schemas[schema_name], registry=registry).iter_errors(data),
                    key=lambda error: list(error.path),
                )
                self.assertEqual(errors, [], f"{entry['model']} {data_name}: {errors}")
            provenance_validator = Draft202012Validator(schemas["field_provenance.schema.json"], registry=registry)
            for line in (package_dir / "field_provenance.jsonl").read_text(encoding="utf-8").splitlines():
                errors = list(provenance_validator.iter_errors(json.loads(line)))
                self.assertEqual(errors, [], f"{entry['model']} field_provenance: {errors}")

        export_evidence_receipt(self.paths)
        current_evidence = json.loads(
            (self.paths.evidence_root / "current_evidence_receipt.json").read_text(encoding="utf-8")
        )
        excluded_prefixes = (
            "output/ad_hoc_review_packages/",
            "output/superseded_review_packages/",
            "output/plugin_package/superseded/",
        )
        self.assertTrue(
            all(not path.startswith(excluded_prefixes) for path in current_evidence["files"])
        )
        standalone = {
            self.index_path: "priority_package_index.schema.json",
            self.paths.output_root / "current_priority_packages.receipt.json": "priority_package_index_receipt.schema.json",
            self.paths.evidence_root / "database_build_receipt.json": "database_build_receipt.schema.json",
            self.paths.evidence_root / "current_evidence_receipt.json": "evidence_receipt.schema.json",
        }
        for data_path, schema_name in standalone.items():
            data = json.loads(data_path.read_text(encoding="utf-8"))
            errors = list(Draft202012Validator(schemas[schema_name], registry=registry).iter_errors(data))
            self.assertEqual(errors, [], f"{data_path.name}: {errors}")

        derivative_schemas = {
            self.paths.work_root / "official_current.jsonl": "official_current_record.schema.json",
            self.paths.work_root / "alibaba_historical_sanitized.jsonl": "historical_alibaba_record.schema.json",
            self.paths.work_root / "alibaba_presence_guard.jsonl": "presence_guard_record.schema.json",
        }
        for data_path, schema_name in derivative_schemas.items():
            validator = Draft202012Validator(schemas[schema_name], registry=registry)
            for line_number, line in enumerate(data_path.read_text(encoding="utf-8").splitlines(), start=1):
                errors = list(validator.iter_errors(json.loads(line)))
                self.assertEqual(errors, [], f"{data_path.name}:{line_number}: {errors}")

    def test_08_secret_scanner_and_outputs(self):
        probes = {
            "query": "https://x/?access_token=ABCDEFGHIJKLMNO",
            "api": "api_key=ABCDEFGHIJKLMNO",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturevalue123",
            "pem": "-----BEGIN PRIVATE KEY-----",
            "session": "sessionid=ABCDEFGHIJKLMNO",
            "bearer": "Bearer ABCDEFGHIJKLMNO",
        }
        self.assertTrue(all(secret_findings(value) for value in probes.values()))
        self.assertEqual(secret_findings({"unit_tokens": ["mm", "L"]}), [])
        forbidden_keys = {"seller", "buyer", "seo", "tokens", "globaldata", "devdata", "nodemap"}
        for entry in self.index["packages"]:
            package_dir = Path(entry["package_path"])
            for path in package_dir.iterdir():
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8-sig")
                self.assertEqual(secret_text_findings(text, path.name), [], path)
                if path.suffix == ".json":
                    value = json.loads(text)
                    bad = [
                        f"{location}.{key}"
                        for location, key in walk_keys(value)
                        if key.casefold() in forbidden_keys
                    ]
                    self.assertEqual(bad, [], path)

    def test_09_review_record_is_audit_only_without_role_hierarchy(self):
        result = review_state("0" * 64, [], [])
        self.assertFalse(result["approved_internal_promotion_supported_in_v1"])
        self.assertEqual(result["record_effect"], "LOCAL_AUDIT_ONLY_NO_APPROVED_INTERNAL_PROMOTION")
        self.assertNotIn("_".join(("responsibility", "chain")), result)
        self.assertEqual(
            validate_review_identity("someone", "free-form-role", "field checked"),
            ("someone", "free-form-role", "field checked"),
        )
        forbidden = {
            "_".join(("BLOCKED", "AI", "GOVERNANCE", "REVIEW", "REQUIRED")),
            "_".join(("responsibility", "chain")),
            "_".join(("AI", "GOVERNANCE", "REVIEW", "APPROVED")),
            "_".join(("production", "or", "engineering", "release", "allowed")),
        }
        active_files = [
            path for path in PLUGIN_ROOT.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() != ".pyc"
        ] + list((PROJECT_ROOT / "schemas").glob("*"))
        for path in active_files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertEqual({token for token in forbidden if token in text}, set(), path)

    def test_10_mcp_surface_and_public_image_network_only(self):
        server = load_mcp_server()
        names = {tool["name"] for tool in server.TOOLS}
        self.assertEqual(
            names,
            {
                "inspect_sources", "reconcile_catalogs", "compile_review_package",
                "record_review_decision", "export_evidence_receipt",
                "materialize_website_images", "build_vision_features", "build_full_match",
            },
        )
        self.assertEqual(ALLOWED_HOSTS, {"catalog.example.invalid", "catalog.example.invalid"})
        self.assertEqual(
            server.call_tool("publish_listing", {})["status"],
            "BLOCKED_REMOTE_WRITE_NOT_AVAILABLE",
        )
        self.assertEqual(
            server.call_tool("compile_review_package", {})["status"],
            "BLOCKED_INVALID_TOOL_ARGUMENTS",
        )
        messages = []
        with mock.patch.object(server, "send", side_effect=messages.append):
            server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "publish_listing", "arguments": {}}})
        self.assertTrue(messages[0]["result"]["isError"])
        blocked_import_roots = {"requests", "httpx", "aiohttp", "playwright", "selenium", "socket", "webbrowser"}
        found = []
        for path in MCP_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [name.name for name in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    if module.split(".", 1)[0] in blocked_import_roots:
                        found.append((str(path), module))
        self.assertEqual(found, [])

    def test_11_untrusted_aliases_and_false_units_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            test_paths = replace(self.paths, project_root=Path(temporary))
            test_paths.work_root.mkdir(parents=True, exist_ok=True)
            (test_paths.work_root / "aliases.jsonl").write_text(
                json.dumps({"left_model": "A", "right_model": "B", "review_status": "APPROVED"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _load_aliases(test_paths)
        self.assertEqual(extract_numbers_and_units("Style White Nano-coated")[1], [])
        self.assertEqual(extract_numbers_and_units("Flow 50 L/h")[1], ["L/h"])

    def test_12_package_compilation_is_deterministic(self):
        first = compile_review_package(self.paths, "MODEL-024")
        second = compile_review_package(self.paths, "MODEL-024")
        self.assertEqual(first["package_id"], second["package_id"])
        self.assertEqual(first["package_digest"], second["package_digest"])
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])

    def test_13_exhaustive_full_match_is_complete_and_fail_closed(self):
        tables = {
            row[0]
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertTrue(
            {"full_match_pairs", "full_match_decisions", "full_match_reverse_coverage"} <= tables
        )
        self.assertEqual(self.scalar("SELECT count(*) FROM full_match_pairs"), 36477)
        self.assertEqual(self.scalar("SELECT count(*) FROM full_match_decisions"), EXPECTED["website_products"])
        self.assertEqual(self.scalar("SELECT count(*) FROM full_match_reverse_coverage"), EXPECTED["alibaba_products"])
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM full_match_pairs p JOIN alibaba_products a "
                "ON a.product_id=p.alibaba_product_id WHERE a.record_disposition='UNUSABLE_WASTE_RECORD'"
            ),
            0,
        )
        decisions = [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT decision_json FROM full_match_decisions ORDER BY official_model"
            )
        ]
        self.assertTrue(all(len(row["top_candidates"]) == 5 for row in decisions))
        self.assertTrue(all(row["forced_best_candidate"]["rank"] == 1 for row in decisions))
        self.assertTrue(all(row["forced_ranking_is_confirmation"] is False for row in decisions))
        self.assertTrue(all(row["confirmed_same_product"] is False for row in decisions))
        self.assertEqual(sum(bool(row["exact_eligible_product_ids"]) for row in decisions), 58)
        self.assertEqual(sum(len(row["exact_eligible_product_ids"]) for row in decisions), 127)
        self.assertEqual(
            sum(bool(row["waste_presence_product_ids"]) and not row["exact_eligible_product_ids"] for row in decisions),
            15,
        )
        reverse = [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT reverse_json FROM full_match_reverse_coverage ORDER BY alibaba_product_id"
            )
        ]
        waste_reverse = [row for row in reverse if row["record_disposition"] == "UNUSABLE_WASTE_RECORD"]
        self.assertEqual(len(waste_reverse), EXPECTED["unusable_waste"])
        self.assertTrue(all(row["presence_only"] and not row["scored"] for row in waste_reverse))
        self.assertTrue(all(row["content_fields_used_for_scoring"] is False for row in waste_reverse))
        self.assertEqual(self.scalar("SELECT count(*) FROM full_match_pairs WHERE visual_score IS NOT NULL"), 36477)


if __name__ == "__main__":
    unittest.main(verbosity=2)
