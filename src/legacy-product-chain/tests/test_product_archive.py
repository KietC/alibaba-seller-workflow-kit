from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "product_archive.py"
SPEC = importlib.util.spec_from_file_location("product_archive", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class ProductArchiveTests(unittest.TestCase):
    def test_safe_public_url_drops_query_and_fragment(self) -> None:
        self.assertEqual(
            module.safe_public_url("https://sc04.alicdn.com/kf/a.jpg?token=secret#x"),
            "https://sc04.alicdn.com/kf/a.jpg",
        )

    def test_host_allowlist(self) -> None:
        self.assertTrue(module.host_allowed("https://sc04.alicdn.com/kf/a.jpg"))
        self.assertFalse(module.host_allowed("https://example.com/a.jpg"))

    def test_extract_product_json_ld_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            product_id = "123"
            (run / "01_商品索引").mkdir(parents=True)
            (run / "01_商品索引" / "catalog_index.jsonl").write_text(
                json.dumps({"run_id": "r", "product_id": product_id, "detail_url": "https://www.alibaba.com/p/123.html"}) + "\n",
                encoding="utf-8",
            )
            target = run / "03_商品标准化" / product_id
            target.mkdir(parents=True)
            (target / "public_fullpage.png").write_bytes(b"png")
            original_html = """<!doctype html><html><head><title>T</title><link rel='canonical' href='https://www.alibaba.com/p/123.html'>
                <script type='application/ld+json'>{"@type":"Product","name":"P","image":["https://sc04.alicdn.com/kf/a.jpg?x=1"]}</script>
                </head><body><h1>P</h1><img src='https://sc04.alicdn.com/kf/a.jpg'></body></html>"""
            (target / "public_rendered.html").write_text(original_html, encoding="utf-8")
            result = module.extract_product(run, product_id)
            self.assertEqual(result["gallery_count"], 1)
            self.assertEqual((target / "public_rendered.html").read_text(encoding="utf-8"), original_html)
            sanitized = (target / "public_rendered_sanitized.html").read_text(encoding="utf-8")
            self.assertNotIn("<script", sanitized)
            manifest = (target / "asset_manifest.jsonl").read_text(encoding="utf-8")
            self.assertIn('"role":"gallery"', manifest)
            self.assertNotIn("?x=1", manifest)

    def test_secret_scanner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.json").write_text('{"url":"https://x/?X-XSRF-TOKEN=abcdefghijk"}', encoding="utf-8")
            findings = module.scan_secrets(root)
            self.assertEqual(findings[0]["pattern"], "sensitive_query")
            report = module.scan_secrets_report(root)
            self.assertEqual(report["finding_count"], 1)
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["files_scanned"], 1)

    def test_secret_scanner_ignores_javascript_identifier_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bundle.js").write_text(
                "e.setCookie=function(value){};e.assign=function(source){}",
                encoding="utf-8",
            )
            report = module.scan_secrets_report(root)
            self.assertEqual(report["finding_count"], 0)
            self.assertEqual(report["classified_non_secret_count"], 0)

    def test_public_favorite_guest_ctoken_is_classified_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.html").write_text(
                '<script src="//us-favorite.alibaba.com/favorite2/ajax/checkFavorites.htm?pids=1&amp;ctoken=abcdefghijkl&amp;callback=x"></script>',
                encoding="utf-8",
            )
            report = module.scan_secrets_report(root)
            self.assertEqual(report["finding_count"], 0)
            self.assertEqual(report["classified_non_secret_count"], 1)
            self.assertEqual(
                report["classified_non_secret"][0]["classification"],
                "alibaba_public_favorite_guest_ctoken",
            )

    def test_public_review_jsonp_signature_is_classified_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.html").write_text(
                '<script src="//acs.h.alibaba.com/h5/mtop.alibaba.icbu.review.new.shopreview.scatter/1.0/?jsv=2.7.5&amp;appKey=12574478&amp;t=123456789&amp;sign=0123456789abcdef0123456789abcdef&amp;api=mtop.alibaba.icbu.review.new.shopreview.scatter"></script>',
                encoding="utf-8",
            )
            report = module.scan_secrets_report(root)
            self.assertEqual(report["finding_count"], 0)
            self.assertEqual(report["classified_non_secret_count"], 1)
            self.assertEqual(
                report["classified_non_secret"][0]["classification"],
                "alibaba_public_review_jsonp_sign",
            )

    def test_public_aplus_analytics_signature_is_classified_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.html").write_text(
                '<script dmtrack_b="%7Bifm%3D0%7Clogin%3D1%7D&amp;xman_us_t=l_source%3Dalibaba&amp;sign=0123456789abcdef0123456789abcdef&amp;havana_lgc2_4=-" plugins="aplus_plugin_icbu/0.0.14/index.js"></script>',
                encoding="utf-8",
            )
            report = module.scan_secrets_report(root)
            self.assertEqual(report["finding_count"], 0)
            self.assertEqual(report["classified_non_secret_count"], 1)
            self.assertEqual(
                report["classified_non_secret"][0]["classification"],
                "alibaba_public_aplus_analytics_sign",
            )

    def test_checkpoint_stops_at_first_uncaptured_product(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            index_dir = run / "01_商品索引"
            index_dir.mkdir(parents=True)
            rows = [
                {"run_id": "r", "product_id": "1", "detail_url": "https://www.alibaba.com/product-detail/1.html"},
                {"run_id": "r", "product_id": "2", "detail_url": "https://www.alibaba.com/product-detail/2.html"},
            ]
            (index_dir / "catalog_index.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )
            (index_dir / "catalog_freeze.json").write_text(
                json.dumps({"run_id": "r", "captured_at": "2026-08-03T00:00:00Z"}),
                encoding="utf-8",
            )
            first = module.product_dir(run, "1")
            first.mkdir(parents=True)
            for name in module.CAPTURE_ARTIFACTS:
                (first / name).write_text("{}" if name.endswith(".json") else "x", encoding="utf-8")
            (first / "capture_receipt.json").write_text(
                json.dumps({"status": "captured"}), encoding="utf-8"
            )
            second = module.product_dir(run, "2")
            second.mkdir(parents=True)
            (second / "capture_receipt.json").write_text(
                json.dumps({"status": "failed", "error_code": "challenge_detected"}), encoding="utf-8"
            )
            checkpoint = module.build_resume_checkpoint(run)
            self.assertEqual(checkpoint["captured_count"], 1)
            self.assertEqual(checkpoint["remaining_count"], 1)
            self.assertEqual(checkpoint["next_ordinal"], 2)
            self.assertEqual(checkpoint["status"], "BLOCKED_HUMAN_VERIFICATION")
            self.assertEqual(checkpoint["challenge_receipt_count"], 1)
            self.assertEqual(checkpoint["not_attempted_count"], 0)
            self.assertRegex(checkpoint["catalog_bindings"]["catalog_index_sha256"], r"^[0-9a-f]{64}$")

    def test_complete_checkpoint_reconciles_browser_state_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            index_dir = run / "01_商品索引"
            index_dir.mkdir(parents=True)
            rows = [
                {"run_id": "r", "product_id": "1", "detail_url": "https://www.alibaba.com/product-detail/1.html"},
                {"run_id": "r", "product_id": "2", "detail_url": "https://www.alibaba.com/product-detail/2.html"},
            ]
            (index_dir / "catalog_index.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )
            (index_dir / "catalog_freeze.json").write_text(
                json.dumps({"run_id": "r", "captured_at": "2026-08-03T00:00:00Z"}),
                encoding="utf-8",
            )
            for row in rows:
                target = module.product_dir(run, row["product_id"])
                target.mkdir(parents=True)
                for name in module.CAPTURE_ARTIFACTS:
                    (target / name).write_text("{}" if name.endswith(".json") else "x", encoding="utf-8")
                (target / "capture_receipt.json").write_text(
                    json.dumps({"status": "captured"}), encoding="utf-8"
                )

            browser_path = run / module.BROWSER_CAPTURE_CHECKPOINT_PATH
            browser_path.parent.mkdir(parents=True)
            browser_path.write_text(
                json.dumps({"status": "PARTIAL", "next_ordinal": 2}), encoding="utf-8"
            )
            ledger_path = run / module.REQUEST_LEDGER_PATH
            ledger_path.parent.mkdir(parents=True)
            ledger_path.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "product_id": row["product_id"],
                            "method": "GET",
                            "http_status": 200,
                            "outcome": "captured",
                            "external_write": False,
                        }
                    )
                    for row in rows
                )
                + "\n",
                encoding="utf-8",
            )

            checkpoint = module.build_resume_checkpoint(run)
            browser = json.loads(browser_path.read_text(encoding="utf-8"))
            ledger = module.summarize_request_ledger(run)
            self.assertEqual(checkpoint["status"], "COMPLETE")
            self.assertEqual(browser["status"], "COMPLETE")
            self.assertEqual(browser["captured_count"], 2)
            self.assertEqual(browser["last_attempted_ordinal"], 2)
            self.assertIsNone(browser["next_ordinal"])
            self.assertTrue((run / browser["prior_checkpoint_archived_as"]).is_file())
            self.assertEqual(ledger["captured_product_id_count"], 2)
            self.assertEqual(ledger["external_write_rows"], 0)
            self.assertEqual(ledger["methods"], {"GET": 2})

    def test_evidence_manifest_has_sidecar_and_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            index_dir = run / "01_商品索引"
            index_dir.mkdir(parents=True)
            (index_dir / "catalog_index.jsonl").write_text(
                json.dumps({"run_id": "r", "product_id": "1"}) + "\n", encoding="utf-8"
            )
            (index_dir / "catalog_freeze.json").write_text(
                json.dumps({"run_id": "r", "captured_at": "2026-08-03T00:00:00Z"}),
                encoding="utf-8",
            )
            quarantine = run / "09_quarantine"
            quarantine.mkdir()
            (quarantine / "kept.partial").write_bytes(b"partial-evidence")
            result = module.build_evidence_manifest(run)
            self.assertEqual(result["file_count"], 3)
            manifest = run / module.EVIDENCE_MANIFEST_PATH
            sidecar = run / module.EVIDENCE_MANIFEST_SHA_PATH
            self.assertTrue(manifest.is_file())
            self.assertTrue(sidecar.is_file())
            self.assertEqual(sidecar.read_text(encoding="utf-8").split()[0], module.sha256_file(manifest))
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            rows = {row["relative_path"]: row for row in payload["files"]}
            self.assertIn("09_quarantine/kept.partial", rows)
            self.assertEqual(
                rows["01_商品索引/catalog_index.jsonl"]["timestamp_source"],
                "catalog_freeze.captured_at",
            )


if __name__ == "__main__":
    unittest.main()
