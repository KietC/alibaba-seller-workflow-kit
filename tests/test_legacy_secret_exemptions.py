"""Real scanner AST extraction; no legacy collector import or execution."""
from __future__ import annotations

import ast
import hashlib
import html
import json
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE = Path(__file__).resolve().parents[1] / "src/legacy-product-chain/scripts/product_archive.py"


def load_scanner():
    constants = {
        "SCHEMA_VERSION", "SECRET_PATTERNS", "PUBLIC_TRANSIENT_QUERY_ALLOWLIST",
        "PUBLIC_QUERY_URL_RE", "SCRIPT_OPEN_TAG_RE", "QUOTED_ATTRIBUTE_RE",
        "PUBLIC_QUERY_REASONS", "TEXT_SCAN_SUFFIXES",
    }
    functions = {"sha256_file", "public_transient_query_spans", "scan_secrets", "scan_secrets_report"}
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"), filename=str(SOURCE))
    selected = [node for node in tree.body if (
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)
    ) or (isinstance(node, ast.FunctionDef) and node.name in functions)]
    scope = {
        "__file__": str(SOURCE), "re": re, "hashlib": hashlib, "html": html,
        "urllib": urllib, "Path": Path, "Any": Any, "datetime": datetime, "timezone": timezone,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope


SCANNER = load_scanner()
FAVORITE = '<script src="//us-favorite.alibaba.com/favorite2/ajax/checkFavorites.htm?pids=1&amp;ctoken=syntheticguest123&amp;callback=x"></script>'
REVIEW = '<script src="//acs.h.alibaba.com/h5/mtop.alibaba.icbu.review.new.shopreview.scatter/1.0/?jsv=2.7.5&amp;appKey=12574478&amp;t=123456789&amp;sign=syntheticreview123&amp;api=mtop.alibaba.icbu.review.new.shopreview.scatter"></script>'
APLUS = '<script dmtrack_b="%7Bifm%3D0%7Clogin%3D1%7D&amp;xman_us_t=l_source%3Dalibaba&amp;sign=syntheticaplus123&amp;havana_lgc2_4=-" plugins="aplus_plugin_icbu/0.0.14/index.js"></script>'
SECRET_URL = '<a href="https://example.invalid/?access_token=SYNTHETIC_SECRET_NOT_REAL">x</a>'


class LegacySecretExemptionTests(unittest.TestCase):
    def scan(self, text):
        with tempfile.TemporaryDirectory(prefix="sellerkit-scanner-test-") as folder:
            root = Path(folder)
            (root / "synthetic_page.html").write_text(text, encoding="utf-8")
            return SCANNER["scan_secrets_report"](root)

    def assert_scan(self, text, expected_status, expected_classified, expected_unclassified):
        before = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
        result = self.scan(text)
        self.assertEqual(expected_status, result["status"])
        self.assertEqual(expected_classified, result["classified_non_secret_count"])
        self.assertEqual(expected_unclassified, sum(row["match_count"] for row in result["findings"]))
        self.assertEqual(before, hashlib.sha256(SOURCE.read_bytes()).hexdigest())
        self.assertNotIn("SYNTHETIC_SECRET_NOT_REAL", str(result))
        return result

    def test_official_public_query_controls_still_classify(self):
        for page, classification in ((FAVORITE, "alibaba_public_favorite_guest_ctoken"), (REVIEW, "alibaba_public_review_jsonp_sign"), (APLUS, "alibaba_public_aplus_analytics_sign")):
            with self.subTest(classification=classification):
                result = self.assert_scan(page, "PASS", 1, 0)
                self.assertEqual(classification, result["classified_non_secret"][0]["classification"])

    def test_encoded_aplus_sign_in_exact_attribute_still_classifies(self):
        self.assert_scan(APLUS.replace("&amp;sign=", "%26sign%3D"), "PASS", 1, 0)

    def test_nearby_access_token_is_not_whitelisted_in_either_order(self):
        for page in (FAVORITE, REVIEW, APLUS):
            for text in (page + SECRET_URL, SECRET_URL + page):
                with self.subTest(page=page, reversed=text.startswith(SECRET_URL)):
                    self.assert_scan(text, "FAIL", 1, 1)

    def test_nonpublic_same_key_nearby_is_not_whitelisted(self):
        for page, key in ((FAVORITE, "ctoken"), (REVIEW, "sign"), (APLUS, "sign")):
            with self.subTest(key=key, page=page):
                other = f'<a href="https://example.invalid/?{key}=SYNTHETIC_SECRET_NOT_REAL">x</a>'
                self.assert_scan(page + other, "FAIL", 1, 1)

    def test_additional_sensitive_key_in_same_public_url_is_not_whitelisted(self):
        for page in (FAVORITE, REVIEW):
            with self.subTest(page=page):
                self.assert_scan(page.replace('&amp;callback=x', '&amp;callback=x&amp;access_token=SYNTHETIC_SECRET_NOT_REAL').replace('&amp;api=', '&amp;access_token=SYNTHETIC_SECRET_NOT_REAL&amp;api='), "FAIL", 1, 1)

    def test_wrong_hosts_paths_userinfo_and_embedded_urls_fail_closed(self):
        cases = (
            FAVORITE.replace("us-favorite.alibaba.com", "evil-us-favorite.alibaba.com"),
            FAVORITE.replace("checkFavorites.htm", "checkFavorites.htm/extra"),
            FAVORITE.replace("//us-favorite.alibaba.com/", "https://example.invalid/?next=//us-favorite.alibaba.com/"),
            FAVORITE.replace("//us-favorite.alibaba.com/", "//user:synthetic" + "@" + "us-favorite.alibaba.com/"),
            REVIEW.replace("acs.h.alibaba.com", "acs.h.alibaba.com.example.invalid"),
            REVIEW.replace("/1.0/", "/not-a-version/"),
        )
        for text in cases:
            with self.subTest(text=text):
                self.assert_scan(text, "FAIL", 0, 1)

    def test_sign_must_be_inside_same_dmtrack_b_value_not_nearby_tag(self):
        cases = (
            '<script dmtrack_b="x&amp;sign=SYNTHETIC_SECRET_NOT_REAL"></script><script plugins="aplus_plugin_icbu/0.0.14/index.js"></script>',
            '<script dmtrack_b="no-sensitive-data" plugins="aplus_plugin_icbu/0.0.14/index.js" src="https://example.invalid/?sign=SYNTHETIC_SECRET_NOT_REAL"></script>',
            '<script dmtrack_b="x&amp;sign=SYNTHETIC_SECRET_NOT_REAL" plugins="other_plugin/0.0.14/index.js"></script>',
            '<script dmtrack_b="x&amp;sign=SYNTHETIC_SECRET_NOT_REAL" dmtrack_b="no-sensitive-data" plugins="aplus_plugin_icbu/0.0.14/index.js"></script>',
        )
        for text in cases:
            with self.subTest(text=text):
                self.assert_scan(text, "FAIL", 0, 1)

    def test_wrong_sensitive_key_in_dmtrack_b_value_remains_blocked(self):
        self.assert_scan(APLUS.replace("sign=", "access_token="), "FAIL", 0, 1)

    def test_encoded_second_parameter_cannot_hide_inside_exempt_value(self):
        for text in (
            FAVORITE.replace("syntheticguest123", "syntheticguest123%26access_token%3DSYNTHETIC_SECRET_NOT_REAL"),
            REVIEW.replace("syntheticreview123", "syntheticreview123%26access_token%3DSYNTHETIC_SECRET_NOT_REAL"),
            APLUS.replace("syntheticaplus123", "syntheticaplus123%26access_token%3DSYNTHETIC_SECRET_NOT_REAL"),
        ):
            with self.subTest(text=text):
                self.assert_scan(text, "FAIL", 0, 1)

    def test_fragment_and_nested_second_question_mark_are_not_query_parameters(self):
        for page in (
            FAVORITE.replace("?pids=1&amp;ctoken=", "#?ctoken="),
            FAVORITE.replace("?pids=1&amp;ctoken=", "?redirect=?ctoken="),
        ):
            with self.subTest(page=page):
                self.assert_scan(page, "FAIL", 0, 1)

    def test_nonquery_cookie_and_bearer_patterns_never_get_public_exemptions(self):
        for text in ("Cookie: synthetic_cookie_value\n" + FAVORITE, "Bearer " + "synthetic" * 3 + "\n" + FAVORITE):
            with self.subTest(text=text[:20]):
                self.assert_scan(text, "FAIL", 1, 1)

    def test_multiple_public_spans_bind_individually(self):
        self.assert_scan(FAVORITE + REVIEW + APLUS + SECRET_URL, "FAIL", 3, 1)

    def test_literal_second_question_mark_cannot_hide_in_exempt_value(self):
        for page, public_value in ((FAVORITE, "syntheticguest123"), (REVIEW, "syntheticreview123"), (APLUS, "syntheticaplus123")):
            for key in ("access_token", "refresh_token", "sign"):
                with self.subTest(page=page, nested_key=key):
                    self.assert_scan(page.replace(public_value, public_value + "?" + key + "=SYNTHETIC_SECRET_NOT_REAL"), "FAIL", 0, 1)

    def test_encoded_aplus_key_cannot_exempt_literal_question_in_value(self):
        page = APLUS.replace("&amp;sign=", "%26sign%3D")
        self.assert_scan(page.replace("syntheticaplus123", "syntheticaplus123?access_token=SYNTHETIC_SECRET_NOT_REAL"), "FAIL", 0, 1)

    def test_normal_public_token_punctuation_still_classifies(self):
        for page, public_value in ((FAVORITE, "syntheticguest123"), (REVIEW, "syntheticreview123"), (APLUS, "syntheticaplus123")):
            with self.subTest(page=page):
                self.assert_scan(page.replace(public_value, "synthetic_guest-token.123"), "PASS", 1, 0)

    def test_repeated_percent_encoding_cannot_hide_nested_delimiter(self):
        for page, public_value in ((FAVORITE, "syntheticguest123"), (REVIEW, "syntheticreview123"), (APLUS, "syntheticaplus123")):
            for delimiter in ("%253F", "%25253f", "%2526", "%252526"):
                with self.subTest(page=page, delimiter=delimiter):
                    self.assert_scan(page.replace(public_value, public_value + delimiter + "access_token%253DSYNTHETIC_SECRET_NOT_REAL"), "FAIL", 0, 1)

    def test_encoded_hex_characters_cannot_hide_nested_delimiter(self):
        for page, public_value in ((FAVORITE, "syntheticguest123"), (REVIEW, "syntheticreview123"), (APLUS, "syntheticaplus123")):
            for delimiter in ("%25%33%66", "%25%33%46", "%25%32%36", "%253%66", "%25%253%2566"):
                with self.subTest(page=page, delimiter=delimiter):
                    self.assert_scan(page.replace(public_value, public_value + delimiter + "access_token%3DSYNTHETIC_SECRET_NOT_REAL"), "FAIL", 0, 1)

    def test_encoded_opaque_public_values_remain_classifiable(self):
        for page, public_value in ((FAVORITE, "syntheticguest123"), (REVIEW, "syntheticreview123"), (APLUS, "syntheticaplus123")):
            for suffix in ("%25%34%31", "%2541", "%252541", "%2B%2F%3D", "%2525"):
                with self.subTest(page=page, suffix=suffix):
                    self.assert_scan(page.replace(public_value, public_value + suffix), "PASS", 1, 0)

    def test_unquoted_or_bare_duplicate_aplus_markers_fail_closed(self):
        for prefix in ('plugins=other ', 'PLUGINS=other ', 'plugins ', 'dmtrack_b=other ', 'DMTRACK_B=other ', 'dmtrack_b '):
            for placement in ("before", "after"):
                with self.subTest(prefix=prefix, placement=placement):
                    page = APLUS.replace("syntheticaplus123", "SYNTHETIC_SECRET_NOT_REAL")
                    page = page.replace("<script ", "<script " + prefix) if placement == "before" else page.replace("></script>", " " + prefix + "></script>")
                    self.assert_scan(page, "FAIL", 0, 1)

    def test_marker_text_inside_unrelated_attribute_is_not_a_duplicate(self):
        for attribute in ('title="plugins=other dmtrack_b=other" ', 'title="x plugins=other y dmtrack_b=other" ', 'data-note=plugins=other ', 'data-note=dmtrack_b=other '):
            with self.subTest(attribute=attribute):
                self.assert_scan(APLUS.replace("<script ", "<script " + attribute), "PASS", 1, 0)

    def test_encoded_fragment_delimiter_cannot_hide_nested_value(self):
        for page, public_value in ((FAVORITE, "syntheticguest123"), (REVIEW, "syntheticreview123"), (APLUS, "syntheticaplus123")):
            with self.subTest(page=page):
                self.assert_scan(page.replace(public_value, public_value + "%25%32%33access_token%3DSYNTHETIC_SECRET_NOT_REAL"), "FAIL", 0, 1)

    def test_html_parser_duplicate_attribute_boundaries_fail_closed(self):
        for prefix in ('<script title="x"plugins=other ', '<script title="x"dmtrack_b=other ', '<script/plugins=other '):
            with self.subTest(prefix=prefix):
                self.assert_scan(APLUS.replace('<script ', prefix).replace('syntheticaplus123', 'SYNTHETIC_SECRET_NOT_REAL'), "FAIL", 0, 1)
        self.assert_scan(APLUS.replace('<script ', '<script title="x"data-note=other '), "PASS", 1, 0)

    def test_unterminated_script_matcher_has_bounded_runtime(self):
        program = (
            "import runpy,sys; "
            "scope=runpy.run_path(sys.argv[1],run_name='synthetic_scanner_performance')['SCANNER']; "
            "prefix=bytes.fromhex(sys.argv[2]); n=int(sys.argv[3]); "
            "assert scope['public_transient_query_spans'](prefix+b'a'*n)=={}"
        )
        for prefix in (b'<script ', b'<script title="', b"<script title='"):
            for size in (48, 4096, 131072):
                with self.subTest(prefix=prefix, size=size):
                    result = subprocess.run(
                        [sys.executable, '-B', '-c', program,
                         str(Path(__file__).resolve()), prefix.hex(), str(size)],
                        cwd=tempfile.gettempdir(), capture_output=True, timeout=2,
                    )
                    self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))

    def test_quoted_tag_delimiters_and_tag_case_keep_exact_offsets(self):
        for prefix in ('<script title="a>b" ', "<script title='a>b' ", '<SCRIPT \t\n title="a<b>c" '):
            with self.subTest(prefix=prefix):
                self.assert_scan(APLUS.replace('<script ', prefix), 'PASS', 1, 0)

    def test_unterminated_aplus_tag_never_exempts_sign(self):
        for page in (APLUS.replace('></script>', ''), APLUS.replace('></script>', ' title="unterminated')):
            with self.subTest(page=page):
                self.assert_scan(page, 'FAIL', 0, 1)

    def test_repeated_unterminated_script_starts_have_bounded_runtime(self):
        program = (
            "import runpy,sys; "
            "scope=runpy.run_path(sys.argv[1],run_name='synthetic_scanner_performance')['SCANNER']; "
            "data=bytes.fromhex(sys.argv[2])+b'<script '*16384; "
            "assert scope['public_transient_query_spans'](data)=={}"
        )
        for prefix in (b'', b'<script title="'):
            with self.subTest(prefix=prefix):
                result = subprocess.run(
                    [sys.executable, '-B', '-c', program, str(Path(__file__).resolve()), prefix.hex()],
                    cwd=tempfile.gettempdir(), capture_output=True, timeout=2,
                )
                self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))


    def test_full_html_context_rejects_inert_script_markers(self):
        opening = APLUS.replace('</script>', '')
        fake_pages = ["<div title='" + opening + "'></div>", '<!--' + opening + '-->']
        fake_pages += ['<' + tag + '>' + opening + '</' + tag + '>' for tag in (
            'script', 'style', 'textarea', 'title', 'xmp', 'iframe',
            'noembed', 'noframes', 'noscript', 'plaintext', 'svg', 'math', 'template',
        )]
        for page in fake_pages:
            with self.subTest(page=page):
                self.assert_scan(page, 'FAIL', 0, 1)
        for tag in ('script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript'):
            with self.subTest(rawtext_self_closing=tag):
                self.assert_scan('<' + tag + '/>' + opening + '</' + tag + '>', 'FAIL', 0, 1)
        self.assert_scan('<plaintext>' + opening + '</plaintext>' + APLUS, 'FAIL', 0, 2)
        self.assert_scan('<!--' + opening, 'FAIL', 0, 1)

    def test_full_context_offsets_and_real_tags_after_inert_content_remain_exact(self):
        opening = APLUS.replace('</script>', '')
        for prefix in ('中文é\n', '\n\r\n中文é\n', '<!doctype html>中文é\n', '<div title="a>b">中文</div>\n'):
            with self.subTest(prefix=prefix):
                self.assert_scan(prefix + APLUS, 'PASS', 1, 0)
        for tag in ('style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript', 'svg', 'math', 'template'):
            with self.subTest(tag=tag):
                inner_close = '</script>' if tag in ('svg', 'math', 'template') else ''
                self.assert_scan('<' + tag + '>' + opening + inner_close + '</' + tag + '>' + APLUS, 'FAIL', 1, 1)
        self.assert_scan('中文\n' + APLUS + '\né\n' + APLUS, 'PASS', 2, 0)
        self.assert_scan('<script title="a>b" dmtrack_b="x&amp;sign=syntheticaplus123" plugins="aplus_plugin_icbu/x"></script>', 'PASS', 1, 0)

    def test_actual_public_scanner_trailing_whitespace_stays_linear_and_classifies(self):
        program = (
            "import runpy,sys,tempfile; from pathlib import Path; "
            "scope=runpy.run_path(sys.argv[1],run_name='synthetic_scanner_performance'); "
            "data=scope['APLUS'].replace('></script>',bytes.fromhex(sys.argv[2]).decode()*int(sys.argv[3])+'></script>'); "
            "folder=tempfile.TemporaryDirectory(); root=Path(folder.name); "
            "(root/'synthetic.html').write_text(data,encoding='utf-8'); "
            "r=scope['SCANNER']['scan_secrets_report'](root); "
            "assert r['status']=='PASS' and r['classified_non_secret_count']==1 and not r['findings']; folder.cleanup()"
        )
        for whitespace in (b' ', b'\t', b'\r\n', b' \t\r\n'):
            for size in (4096, 32768, 131072):
                with self.subTest(whitespace=whitespace, size=size):
                    result = subprocess.run(
                        [sys.executable, '-B', '-c', program, str(Path(__file__).resolve()), whitespace.hex(), str(size)],
                        cwd=tempfile.gettempdir(), capture_output=True, timeout=2,
                    )
                    self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))


    def test_unsafe_or_unknown_html_parser_loses_only_aplus_exemption(self):
        from html.parser import HTMLParser
        from unittest.mock import patch
        def unsupported(self, index):
            raise AssertionError('unsupported parser must not execute')
        with patch.object(HTMLParser, 'check_for_whole_start_tag', unsupported):
            self.assert_scan(APLUS, 'FAIL', 0, 1)
            self.assert_scan(FAVORITE, 'PASS', 1, 0)
            self.assert_scan(REVIEW, 'PASS', 1, 0)
        with patch.object(HTMLParser, 'check_for_whole_start_tag', None):
            self.assert_scan(APLUS, 'FAIL', 0, 1)

    def test_cookie_headers_keep_horizontal_space_and_blank_line_controls(self):
        for header in ('Cookie: synthetic_cookie_value', '\t Cookie: synthetic_cookie_value',
                       ' \tSet-Cookie: synthetic_cookie_value', '\n\r\n\tCookie: synthetic_cookie_value'):
            with self.subTest(header=header):
                self.assert_scan(header, 'FAIL', 0, 1)
        for text in (' \t' * 131072, '\r\n' * 131072, ' \t\r\n' * 131072):
            with self.subTest(blank_bytes=len(text)):
                self.assert_scan(text, 'PASS', 0, 0)


    def test_actual_non_html_files_never_gain_aplus_script_semantics(self):
        for suffix in sorted(SCANNER['TEXT_SCAN_SUFFIXES'] - {'.html'}):
            with self.subTest(suffix=suffix):
                with tempfile.TemporaryDirectory(prefix='sellerkit-context-type-') as folder:
                    path = Path(folder) / ('synthetic' + suffix)
                    text = json.dumps({'sample': APLUS}) if suffix in {'.json', '.jsonl'} else APLUS
                    path.write_text(text, encoding='utf-8')
                    result = SCANNER['scan_secrets_report'](Path(folder))
                    self.assertEqual('FAIL', result['status'])
                    self.assertEqual(0, result['classified_non_secret_count'])
                    self.assertEqual(1, sum(row['match_count'] for row in result['findings']))
                    for public in (FAVORITE, REVIEW):
                        path.write_text(public, encoding='utf-8')
                        result = SCANNER['scan_secrets_report'](Path(folder))
                        self.assertEqual('PASS', result['status'])
                        self.assertEqual(1, result['classified_non_secret_count'])
                self.assertNotIn('SYNTHETIC_SECRET_NOT_REAL', str(result))
        self.assertTrue(SCANNER['public_transient_query_spans'](APLUS.encode()))
        self.assertEqual({}, SCANNER['public_transient_query_spans'](APLUS.encode(), html_context=False))


    def test_unsigned_and_wrong_key_marker_tags_have_bounded_actual_scanner_runtime(self):
        program = (
            "import runpy,sys,tempfile; from pathlib import Path; "
            "s=runpy.run_path(sys.argv[1],run_name='synthetic_scanner_performance'); "
            "base=s['APLUS'].replace('&amp;sign=syntheticaplus123','') if sys.argv[2]=='unsigned' else s['APLUS'].replace('sign=','access_token='); "
            "data=base.replace('></script>',bytes.fromhex(sys.argv[3]).decode()*int(sys.argv[4])+'></script>'); "
            "folder=tempfile.TemporaryDirectory(); root=Path(folder.name); "
            "(root/'synthetic.html').write_text(data,encoding='utf-8'); "
            "r=s['SCANNER']['scan_secrets_report'](root); "
            "assert r['status']==('PASS' if sys.argv[2]=='unsigned' else 'FAIL') and r['classified_non_secret_count']==0; folder.cleanup()"
        )
        for variant in ('unsigned', 'wrong_key'):
            for whitespace in (b' ', b'\r\n', b' \t\r\n'):
                for size in (4096, 32768, 131072):
                    with self.subTest(variant=variant, whitespace=whitespace, size=size):
                        result = subprocess.run(
                            [sys.executable, '-B', '-c', program, str(Path(__file__).resolve()), variant, whitespace.hex(), str(size)],
                            cwd=tempfile.gettempdir(), capture_output=True, timeout=2,
                        )
                        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))


    def test_script_double_escape_context_conservatively_denies_aplus(self):
        double = "<script>const sample='<!--<script></script>" + APLUS + "';</script>"
        negative = (
            (double, 0, 1),
            ("<SCRIPT>const sample='<!--<ScRiPt ></sCrIpT>" + APLUS + "';</SCRIPT>", 0, 1),
            ("<script/>const sample='<!--<script></script>" + APLUS + "';</script>", 0, 1),
            ("<script><!--\n<ScRiPt/\n></script>" + APLUS + "</script>", 0, 1),
            ("<script><!--<script\t></script>" + APLUS + "</script>", 0, 1),
            (FAVORITE + REVIEW + double, 2, 1),
            (APLUS + double, 0, 2),
        )
        for text, classified, unclassified in negative:
            with self.subTest(classified=classified, input_hash=hashlib.sha256(text.encode()).hexdigest()):
                self.assert_scan(text, 'FAIL', classified, unclassified)
        positive = (
            APLUS,
            "<script>const sample='plain';</script>" + APLUS,
            "<script><!-- ordinary comment -->\n</script>" + APLUS,
            "<script><!--<scriptx></script>" + APLUS,
            "<script><!-- comment --> <script></script>" + APLUS,
            "<script>&lt;!--<script></script>" + APLUS,
            '<div title="<!--<script>"></div>' + APLUS,
        )
        for text in positive:
            with self.subTest(input_hash=hashlib.sha256(text.encode()).hexdigest()):
                self.assert_scan(text, 'PASS', 1, 0)

    def test_template_self_closing_slash_does_not_remove_inert_context(self):
        cases = (
            ("<template/>" + APLUS + "</template>", 'FAIL', 0, 1),
            ("<TEMPLATE />" + APLUS + "</TEMPLATE>", 'FAIL', 0, 1),
            ('<template data-x="a>b"/>' + APLUS + "</template>", 'FAIL', 0, 1),
            ("<template/>" + APLUS, 'FAIL', 0, 1),
            ("<template/><template/>" + APLUS + "</template></template>", 'FAIL', 0, 1),
            ("<template/>" + APLUS + "</template>" + APLUS, 'FAIL', 1, 1),
            ("<template/>benign</template>" + APLUS, 'PASS', 1, 0),
            ("<svg/>" + APLUS, 'PASS', 1, 0),
            ("<math/>" + APLUS, 'PASS', 1, 0),
            ("<template>" + APLUS + "</template>" + APLUS, 'FAIL', 1, 1),
        )
        for text, status, classified, unclassified in cases:
            with self.subTest(input_hash=hashlib.sha256(text.encode()).hexdigest()):
                self.assert_scan(text, status, classified, unclassified)


if __name__ == "__main__":
    unittest.main()
