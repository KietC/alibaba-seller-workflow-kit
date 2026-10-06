#!/usr/bin/env python3

import importlib.util
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import quote


MODULE_PATH = Path(__file__).with_name("validate_campaign.py")
SPEC = importlib.util.spec_from_file_location("validate_campaign", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def valid_plan():
    return {
        "campaign_name": "MODEL-019 inquiry buyer canary",
        "segment": {
            "source": "ALIBABA_SYSTEM_GROUP",
            "name": "近30天询盘买家",
            "live_count": 1,
        },
        "channel": "ALIYUN_DIRECTMAIL",
        "channel_ready": True,
        "subject": "Commercial SUS304 pre-rinse faucet options",
        "body_text": "Hello, we prepared a concise comparison of our commercial SUS304 pre-rinse faucet options for your current sourcing needs.",
        "claims_reviewed": True,
        "recipient_data_included": False,
        "final_send_allowed": False,
        "schedule": "IMMEDIATE",
        "cta": {"text": "View the collection", "url": "https://catalog.example.invalid/"},
    }


def complete_key_residual_cases():
    """Synthetic overlap/flag cases; no fixtures, contacts or business files."""
    cases = []
    prefixes = ("password", "auth_token", "form.password", "api:key", "form:api:key")
    metadata = ("has_token=false", "session_token_count=2", "token_sha256=" + "1" * 64,
                "token_policy=not_copied")
    for prefix in prefixes:
        for suffix in metadata:
            key, value = suffix.split("=", 1)
            for text in (prefix + ": SYNTHETICSECRET " + suffix,
                         prefix + ":SYNTHETICSECRET " + suffix,
                         prefix + ":\nSYNTHETICSECRET " + suffix,
                         prefix + ":SYNTHETICSECRET:" + suffix,
                         '"' + prefix + ':SYNTHETICSECRET:' + key + '"=' + value,
                         '"' + prefix + ':SYNTHETICSECRET:' + key + '":' + value):
                cases.append(("notes", text, 3))
        for encoded in (":SYNTHETIC_secret", "%3ASYNTHETIC_secret", "%253ASYNTHETIC_secret"):
            cases.append(("url", "https://example.invalid/?" + prefix + encoded, 3))
        cases.append(("url", "https://example.invalid/?" + prefix + "%3ASYNTHETICSECRET+has_token=false", 3))
        cases.append(("url", "https://example.invalid/?" + prefix + "%3A%0ASYNTHETICSECRET+has_token=false", 3))
        cases.append(("url", "https://example.invalid/" + prefix + ":SYNTHETICSECRET:has_token=false", 3))
    for text in (
        "has_token=false", "auth_token_count=2", "password:sha256=" + "1" * 64,
        "form:api:key:present=false", "form:api:key:sha256=" + "0" * 64,
        '"form:password:sha256"=' + "1" * 64,
        "token_policy=not_copied", "form:api:key:policy=not_copied",
        "api:key:policy:No-copy", '"api:key:policy":No-copy',
        "form:api:key:present:\nfalse",
        "Product: commercial faucet. Material: SUS304.",
        "passwordless_operation=Supported; tokenization_notes=Commercial",
    ):
        cases.append(("notes", text, 0))
    for url in (
        "https://example.invalid/?password%3Asha256=" + "1" * 64,
        "https://example.invalid/?form%3Aapi%3Akey%3Apresent=false",
        "https://example.invalid/?form%3Aapi%3Akey%3Asha256=" + "0" * 64,
        "https://example.invalid/?token_policy=not_copied",
        "https://example.invalid/?form%3Aapi%3Akey%3Apolicy=No-copy",
        "https://example.invalid/?note=passwordless_operation%3Aapproved&label=secret-word",
        "https://example.invalid/?label=api%2Bkey&view=2",
    ):
        cases.append(("url", url, 0))
    for key in ("password:sha256", "form:api:key:present", "form:api:key:count"):
        cases.append(("url", "https://example.invalid/?" + key, 3))
    return cases


def url_userinfo_residual_cases():
    uri = "https://audit-user:SYNTHETIC_URI_ONLY@localhost/catalog"
    blocked = (
        uri, "http://audit-user:SYNTHETIC_URI_ONLY@127.0.0.1/catalog",
        "ftp://audit-user:SYNTHETIC_URI_ONLY@[2001:db8::1]/catalog",
        "https://audit-user@localhost/catalog", "https://:SYNTHETIC_URI_ONLY@localhost/catalog",
        "https://:@localhost/catalog",
        "https://%61udit-user:%53YNTHETIC_URI_ONLY@localhost/catalog",
        "https://audit-user%3ASYNTHETIC_URI_ONLY%40localhost/catalog",
        quote(uri, safe=""), quote(quote(uri, safe=""), safe=""),
        "https&#58;//audit-user:SYNTHETIC_URI_ONLY&#64;localhost/catalog",
        "https://example.invalid/?redirect=" + quote(uri, safe=""),
        "https://example.invalid/?redirect=" + uri,
        "https://example.invalid/#" + quote(uri, safe=""),
    )
    plain = "https://localhost/catalog?label=api%2Bkey&view=2"
    safe = (
        plain, "http://127.0.0.1/catalog", "ftp://localhost/catalog",
        "https://example.invalid/catalog?label=plain+space&text=literal%2Bplus",
        quote(plain, safe=""), quote(quote(plain, safe=""), safe=""),
        "https://localhost/catalog?label=audit-user%3APUBLIC%40localhost",
    )
    cases = [(surface, text, code) for surface in ("notes", "body", "nested")
             for values, code in ((blocked, 3), (safe, 0)) for text in values]
    cases += [("cta_url", text, 3) for text in (uri, blocked[6], blocked[7])]
    cases += [("cta_url", text, 0) for text in (plain, safe[3], safe[6])]
    cases += [(surface, text, code) for surface in ("cta_text", "notes_key")
              for text, code in ((uri, 3), (plain, 0))]
    return cases


def plan_with_url_surface(surface, text):
    plan = valid_plan()
    if surface == "body":
        plan["body_text"] += " " + text
    elif surface == "nested":
        plan["notes"] = [{"safe_label": {"link": text}}]
    elif surface == "notes_key":
        plan["notes"] = {text: "Commercial collection review."}
    elif surface == "cta_url":
        plan["cta"]["url"] = text
    elif surface == "cta_text":
        plan["cta"]["text"] = text
    else:
        plan["notes"] = text
    return plan


def protocol_relative_userinfo_cases():
    uri = "//audit-user:SYNTHETIC_URI_ONLY@localhost/catalog"
    blocked = (
        uri, quote(uri, safe=""), quote(quote(uri, safe=""), safe=""),
        "&#47;&#47;audit-user:SYNTHETIC_URI_ONLY&#64;localhost/catalog",
        "//audit-user%3ASYNTHETIC_URI_ONLY%40localhost/catalog",
        "//audit-user@localhost/catalog", "//:@localhost/catalog",
        "//audit-user:SYNTHETIC_URI_ONLY@127.0.0.1/catalog",
        "//audit-user:SYNTHETIC_URI_ONLY@[2001:db8::1]/catalog",
        '<a href="' + uri + '">Collection</a>',
        "https://example.invalid/?redirect=" + uri,
        "https://example.invalid/?redirect=" + quote(uri, safe=""),
        "https://example.invalid/?redirect=" + quote(quote(uri, safe=""), safe=""),
        "https://example.invalid/#" + uri,
        "//localhost/catalog?redirect=" + uri,
        "//localhost/catalog?next=https://example.invalid/assets?redirect=" + quote(uri, safe=""),
    )
    plain = "//localhost/catalog?label=plain+space&literal=plus%2Bplus"
    safe = (
        plain, "//127.0.0.1/catalog", "//[2001:db8::1]/catalog",
        quote(plain, safe=""), quote(quote(plain, safe=""), safe=""),
        "&#47;&#47;localhost/catalog", "https://example.invalid/?redirect=//localhost/catalog",
        "//localhost/catalog?redirect=//127.0.0.1/catalog",
        "https://localhost/catalog//public:topic@localhost",
        "https://localhost/catalog=//public:topic@localhost",
        "https://localhost/catalog.//public:topic@localhost",
        "//localhost/catalog=//public:topic@localhost",
        "//localhost/catalog//public:topic@localhost",
        "parts//public:topic@localhost", "/catalog//public:topic@localhost",
        "https://example.invalid/?redirect=https://localhost/catalog=//public:topic@localhost",
    )
    return [(surface, text, code) for surface in ("notes", "body", "nested", "cta_text", "notes_key")
            for values, code in ((blocked, 3), (safe, 0)) for text in values]


def encoded_authority_userinfo_cases():
    """Raw token lineage prevents decoding delimiters from hiding userinfo."""
    cases = []
    surfaces = ("notes", "body", "nested")
    payloads = ("SYNTHETIC%20PASSWORD!", "SYNTHETIC%3cPASSWORD!",
                "SYNTHETIC%3ePASSWORD!", "SYNTHETIC&#32;PASSWORD!",
                "SYNTHETIC&#x3c;PASSWORD!", "SYNTHETIC&#x3e;PASSWORD!",
                "SYNTHETIC%2509PASSWORD!", "SYNTHETIC%25%32%30PASSWORD!")
    for prefix in ("https:", ""):
        for payload in payloads:
            uri = prefix + "//audit-user:" + payload + "@example.invalid/catalog"
            for text in (uri, quote(uri, safe=""), quote(quote(uri, safe=""), safe="")):
                cases += [(surface, text, 3) for surface in surfaces]
    partial = (
        "https%3A//audit-user:SYNTHETIC%20PASSWORD!@example.invalid/catalog",
        "https:%2F/audit-user:SYNTHETIC%20PASSWORD!@example.invalid/catalog",
        "%2F/audit-user:SYNTHETIC%20PASSWORD!@example.invalid/catalog",
        "https://audit-user:SYNTHETIC%2520PASSWORD!%40example.invalid/catalog",
        "//audit-user:SYNTHETIC%253cPASSWORD!%2540example.invalid/catalog",
        "https&#58;&#47;&#47;audit-user:SYNTHETIC&#32;PASSWORD!&#64;localhost/catalog",
        "https%3A&#47;&#47;audit-user:SYNTHETIC%3ePASSWORD!&#64;localhost/catalog",
        "%25%32%46%25%32%46audit-user:SYNTHETIC%20PASSWORD!%40localhost/catalog",
    )
    cases += [(surface, text, 3) for text in partial for surface in surfaces]
    uri = "//audit-user:SYNTHETIC%20PASSWORD!@localhost/catalog"
    embedded = (
        '<a href="' + uri + '">Collection</a>',
        '<a href="' + quote(uri, safe="") + '">Collection</a>',
        "https://example.invalid/?redirect=" + uri,
        "https://example.invalid/?redirect=" + quote(uri, safe=""),
        "https://example.invalid/#" + quote(quote(uri, safe=""), safe=""),
        "//localhost/catalog?redirect=" + quote(uri, safe=""),
    )
    cases += [(surface, text, 3) for text in embedded for surface in surfaces]
    for prefix in ("https:", ""):
        for escaped in ("%20", "%3c", "%3e"):
            plain = prefix + "//localhost/catalog" + escaped + "options?label=api%2Bkey&view=plain+space"
            for text in (plain, quote(plain, safe=""), quote(quote(plain, safe=""), safe="")):
                cases += [(surface, text, 0) for surface in surfaces]
    safe = (
        "https://localhost/catalog%20//public:topic@localhost",
        "//localhost/catalog%3c//public:topic@localhost",
        "https://example.invalid/?redirect=//localhost/catalog%3eoptions",
        "https://example.invalid/catalog?literal=public%2Bword&word=plain+space",
        "https://localhost/catalog&#32;options",
        "https://localhost/catalog&#x3c;options",
        "https://localhost/catalog&#x3e;options",
        '<a href="https://localhost/catalog%20options">Collection</a>',
    )
    cases += [(surface, text, 0) for text in safe for surface in surfaces]
    return cases


def uri_reference_boundary_cases():
    """Reference delimiters must not become word/path-prefix false positives."""
    cases = []
    surfaces = ("notes", "body", "nested")
    uri = "//audit-user:SYNTHETIC_BOUNDARY@localhost/catalog"
    enclosures = (("<", ">"), (">", ""), ("`", "`"), ('"', '"'),
                  ("(", ")"), ("[", "]"), ("{", "}"), ("?", ""),
                  ("&", ""), (";", ""))
    for prefix, suffix in enclosures:
        for authority, code in ((uri, 3), ("//localhost/catalog?label=api%2Bkey&word=plain+space", 0)):
            wrapped = prefix + authority + suffix
            for text in (wrapped, quote(wrapped, safe=""), quote(quote(wrapped, safe=""), safe="")):
                cases += [(surface, text, code) for surface in surfaces]
    for delimiter in ("?", "?flag=public&", "#", "?next="):
        nested = "https://example.invalid/catalog" + delimiter + uri
        for text in (nested, quote(nested, safe=""), quote(quote(nested, safe=""), safe="")):
            cases += [(surface, text, 3) for surface in surfaces]
    for text in ("&#60;" + uri + "&#62;", "&lt;" + uri + "&gt;"):
        cases += [(surface, text, 3) for surface in surfaces]
    safe = (
        "parts//public:topic@localhost",
        "parts%2F%2Fpublic:topic@localhost",
        "parts.//public:topic@localhost",
        "prefix_//public:topic@localhost",
        "https://localhost/catalog%3c//public:topic@localhost",
        "//localhost/catalog%3e=//public:topic@localhost",
        "https://localhost/catalog?//localhost/public?label=api%2Bkey",
        "https://localhost/catalog?label=plain+space&literal=public%2Btopic",
    )
    cases += [(surface, text, 0) for text in safe for surface in surfaces]
    return cases


class ValidateCampaignTests(unittest.TestCase):
    def test_valid_canary_passes(self):
        errors, _ = MODULE.validate(valid_plan(), 20)
        self.assertEqual([], errors)

    def test_large_group_blocks(self):
        plan = valid_plan()
        plan["segment"]["live_count"] = 38
        errors, _ = MODULE.validate(plan, 20)
        self.assertTrue(any("exceeds" in error for error in errors))

    def test_raw_recipient_data_blocks(self):
        plan = valid_plan()
        plan["recipient_data_included"] = True
        errors, _ = MODULE.validate(plan, 20)
        self.assertTrue(any("recipient_data_included" in error for error in errors))

    def test_send_authority_blocks(self):
        plan = valid_plan()
        plan["final_send_allowed"] = True
        errors, _ = MODULE.validate(plan, 20)
        self.assertTrue(any("final_send_allowed" in error for error in errors))

    def test_unready_channel_blocks(self):
        plan = valid_plan()
        plan["channel_ready"] = False
        errors, _ = MODULE.validate(plan, 20)
        self.assertTrue(any("channel_ready" in error for error in errors))

    def test_email_in_copy_blocks(self):
        plan = valid_plan()
        plan["body_text"] += " Contact buyer@example.com for details."
        errors, _ = MODULE.validate(plan, 20)
        self.assertTrue(any("email addresses" in error for error in errors))

    def test_email_scan_covers_every_string_surface(self):
        plans = []
        for field in ("campaign_name", "subject", "body_text", "channel", "schedule"):
            plan = valid_plan()
            plan[field] = "Synthetic audit@example.invalid content"
            plans.append(plan)
        for field in ("source", "name"):
            plan = valid_plan()
            plan["segment"][field] = "audit@example.invalid"
            plans.append(plan)
        for field in ("text", "url"):
            plan = valid_plan()
            plan["cta"][field] = "https://example.invalid/?email=audit%40example.invalid"
            plans.append(plan)
        plans.append({**valid_plan(), "notes": [{"safe_label": {"deeper": "audit@example.invalid"}}]})
        plans.append({**valid_plan(), "audit@example.invalid": "key must not leak"})
        for plan in plans:
            with self.subTest(plan=plan):
                errors, _ = MODULE.validate(plan, 20)
                self.assertTrue(any("email addresses" in error for error in errors))
                self.assertNotIn("audit@example.invalid", " ".join(errors))

    def test_sensitive_keys_nested_in_notes_block(self):
        for key in ("password", "smtp_password", "accessToken", "api-key", "cookie", "customer_name", "recipientId", "raw_crm_rows", "credentials"):
            with self.subTest(key=key):
                plan = {**valid_plan(), "notes": [{"nested": {key: "SYNTHETIC_NOT_REAL"}}]}
                errors, _ = MODULE.validate(plan, 20)
                self.assertTrue(any("raw contact or credential" in error for error in errors))

    def test_credential_values_and_encoded_email_block(self):
        synthetic_jwt = "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8
        for value in ("password=SYNTHETIC_NOT_REAL", "Authorization: Bearer SYNTHETIC", "access_token: SYNTHETIC_NOT_REAL", synthetic_jwt, "audit%2540example.invalid", "audit&#64;example.invalid"):
            with self.subTest(value=value):
                errors, _ = MODULE.validate({**valid_plan(), "notes": [{"description": value}]}, 20)
                self.assertTrue(errors)

    def test_normal_commercial_copy_and_safe_metadata_pass(self):
        plan = {**valid_plan(), "notes": {
            "description": "Ceramic valve options; SMTP password is not configured. No cookie or token is copied.",
            "has_token": False, "cookie_present": True, "recipient_count": 4,
            "customer_count": 0, "token_sha256": "a" * 64,
            "password_hash": "b" * 64, "password": None,
            "token": False, "customer_name": "",
        }}
        errors, _ = MODULE.validate(plan, 20)
        self.assertEqual([], errors)

    def test_sensitive_metadata_cannot_hide_raw_values(self):
        for key, value in (("token_sha256", "SYNTHETIC_NOT_REAL"), ("has_token", "SYNTHETIC_NOT_REAL"), ("customer_count", "SYNTHETIC_CUSTOMER"), ("cookie_present", ["SYNTHETIC_NOT_REAL"])):
            with self.subTest(key=key):
                errors, _ = MODULE.validate({**valid_plan(), "notes": {key: value}}, 20)
                self.assertTrue(any("metadata must" in error for error in errors))

    def test_namespaced_and_auth_aliases_use_exact_word_suffixes(self):
        for key in ("form.password", "form[password]", "form/password", "form:Password", "formPassword", "smtp.form.password", "auth_token", "authToken", "authtoken", "session_token", "bearerToken", "api.private_key", "form.clientSecret", "response.refresh_token"):
            with self.subTest(key=key):
                errors, _ = MODULE.validate({**valid_plan(), "notes": {key: "SYNTHETIC_RANDOM_SECRET"}}, 20)
                self.assertTrue(any("raw contact or credential" in error for error in errors))
        safe_notes = {
            "form.password.sha256": "a" * 64,
            "auth_token_sha256": "b" * 64,
            "form.hasPassword": False,
            "api.auth_token_present": True,
            "form.password_count": 0,
            "tokenization_notes": "Commercial product grouping notes.",
            "passwordless_operation": "No browser credentials are used.",
            "auth_token_policy": "Configuration readiness only, no credentials copied.",
        }
        errors, _ = MODULE.validate({**valid_plan(), "notes": safe_notes}, 20)
        self.assertEqual([], errors)

    def test_unhashable_enum_values_block_without_exception(self):
        for field in ("channel", "segment_name"):
            for value in ([], {}, ["OKKI_EDM"], {"value": "OKKI_EDM"}, None, True, 4):
                with self.subTest(field=field, value=value):
                    plan = valid_plan()
                    if field == "channel":
                        plan[field] = value
                    else:
                        plan["segment"]["name"] = value
                    errors, _ = MODULE.validate(plan, 20)
                    self.assertTrue(errors)

    def test_cta_requires_parsed_https_hostname_without_credentials(self):
        for url in ("https://", "http://example.invalid/", "https://audit-user:SYNTHETIC_PASSWORD@example.invalid/path", "https://example.invalid:bad/", "https://example.invalid:0/", "https://example.invalid:65536/", "https://bad_host.invalid/", "https://-host.invalid/", "https://example..invalid/", "https://example.invalid/line\nbreak", "https://example.invalid/space here", "https://[invalid-ip]/", "https://%65xample.invalid/", "https://./"):
            with self.subTest(url=url):
                errors, _ = MODULE.validate({**valid_plan(), "cta": {"text": "View", "url": url}}, 20)
                self.assertTrue(any("valid hostname" in error for error in errors))

    def test_valid_cta_domains_and_ip_literals_are_offline(self):
        for url in ("https://example.invalid/catalog?product=MODEL-019", "HTTPS://example.invalid/", "https://example.invalid:443/", "https://例子.测试/catalog", "https://192.0.2.1/", "https://[2001:db8::1]/", "https://example.invalid/product%20options"):
            with self.subTest(url=url):
                errors, _ = MODULE.validate({**valid_plan(), "cta": {"text": "View", "url": url}}, 20)
                self.assertEqual([], errors)

    def test_decoded_query_and_text_assignment_keys_share_dictionary_aliases(self):
        for text in (
            "https://example.invalid/?auth_token=SYNTHETIC_ONLY",
            "session_token=SYNTHETIC_ONLY", "authToken: SYNTHETIC_ONLY",
            "form.password=SYNTHETIC_ONLY", "api.privateKey: SYNTHETIC_ONLY",
            "response.refresh_token=SYNTHETIC_ONLY", "form[password]=SYNTHETIC_ONLY",
            '{"auth_token":"SYNTHETIC_ONLY"}',
            "https://example.invalid/?auth%5Ftoken%3DSYNTHETIC_ONLY",
            "session%255Ftoken%253DSYNTHETIC_ONLY",
            "auth&#95;token&#61;SYNTHETIC_ONLY",
        ):
            with self.subTest(text=text):
                errors, _ = MODULE.validate({**valid_plan(), "notes": text}, 20)
                self.assertTrue(any("assignment values" in error for error in errors))
                self.assertNotIn("SYNTHETIC_ONLY", " ".join(errors))

    def test_assignment_metadata_and_benign_copy_still_pass(self):
        for text in (
            "auth_token_sha256=" + "a" * 64,
            'form.password.hash="' + "b" * 64 + '"',
            "form.hasPassword=false; auth_token_count=2; api.auth_token_present=true",
            "https://example.invalid/?auth_token_sha256=" + "a" * 64 + "&has_token=false",
            "tokenization_notes=Commercial; passwordless_operation=Supported",
            "auth_token_policy=Configuration-readiness; session_token_policy=No-copy",
            "SMTP password is not configured. No cookie or token is copied.",
            "Product: commercial faucet. Material: SUS304.",
        ):
            with self.subTest(text=text):
                errors, _ = MODULE.validate({**valid_plan(), "notes": text}, 20)
                self.assertEqual([], errors)


    def test_assignment_metadata_parsing_preserves_digest_strings(self):
        cases = [
            ("authTokenSha256=" + "1" * 64, False),
            ("form.password.hash=" + "0" * 64, False),
            ('auth_token_sha256="' + "1" * 64 + '"', False),
            ("session_token_count=2", False),
            ("form.hasPassword=true", False),
            ("form.hasPassword=false", False),
            ("auth_token_sha256=" + "1" * 63, True),
            ("auth_token_sha256=" + "1" * 65, True),
            ("auth_token_sha256=-" + "1" * 63, True),
            ("auth_token_sha256=" + "g" * 64, True),
            ("session_token_count=true", True),
            ("session_token_count=-1", True),
            ("form.hasPassword=0", True),
            ("auth_token_sha256=false", True),
            ("auth_token=true", True),
            ("auth_token=false", True),
            ("auth_token=0", True),
        ]
        for text, blocked in cases:
            with self.subTest(text=text, blocked=blocked):
                errors, _ = MODULE.validate({**valid_plan(), "notes": text}, 20)
                self.assertEqual(blocked, bool(errors))


    def test_complete_compound_assignment_keys_share_aliases(self):
        keys = (
            "api:key", "api/key", "api.key", "api-key", "api_key", "api+key",
            "api|key", "api[key]", "api key", "api::key", "form:api:key",
            "form/api/key", "form[api][key]", "form.apiKey",
        )
        for key in keys:
            with self.subTest(key=key):
                errors, _ = MODULE.validate({**valid_plan(), "notes": key + "=SYNTHETIC_ONLY"}, 20)
                self.assertTrue(any("assignment values" in error for error in errors))
                self.assertNotIn("SYNTHETIC_ONLY", " ".join(errors))
                errors, _ = MODULE.validate({**valid_plan(), "notes": key + ":sha256=" + "1" * 64}, 20)
                self.assertEqual([], errors)

    def test_form_query_plus_and_encoded_plus_are_not_double_decoded(self):
        self.assertEqual(
            [("api key", "plain space"), ("api+key", "literal+plus")],
            MODULE.decoded_query_pairs("api+key=plain+space&api%2Bkey=literal%2Bplus"),
        )


    def test_colon_assignments_are_not_hidden_by_following_equals(self):
        blocked = (
            "password: SYNTHETIC_ONLY mode=review",
            "password:SYNTHETIC_ONLY mode=review",
            "auth_token: SYNTHETIC_ONLY\tmode=review",
            "form.password: SYNTHETIC_ONLY; mode=review",
            "session_token:SYNTHETIC_ONLY, mode=review",
            "api:key: SYNTHETIC_ONLY mode=review",
            "password:SYNTHETIC_ONLY=review",
            "api:key:SYNTHETIC_ONLY=review",
            "form.password:SYNTHETIC_ONLY=review",
            '"password:SYNTHETIC_ONLY"=review',
        )
        safe = (
            "api:key:sha256=" + "1" * 64 + " mode=review",
            "form:api:key:present=false mode=review",
            "passwordless_operation: supported mode=review",
            "api:key:policy=No-copy mode=review",
            '"api:key:policy"=No-copy',
            '"section:label"=Commercial',
            '"api:key:sha256"=' + "1" * 64,
        )
        for text, expected in [(text, True) for text in blocked] + [(text, False) for text in safe]:
            with self.subTest(text=text, blocked=expected):
                errors, _ = MODULE.validate({**valid_plan(), "notes": text}, 20)
                self.assertEqual(expected, bool(errors))
                self.assertNotIn("SYNTHETIC_ONLY", " ".join(errors))


    def test_suffix_metadata_cannot_bind_a_preceding_credential_value(self):
        for surface, text, code in complete_key_residual_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                plan = valid_plan()
                if surface == "url":
                    plan["cta"]["url"] = text
                else:
                    plan["notes"] = text
                errors, _ = MODULE.validate(plan, 20)
                self.assertEqual(code != 0, bool(errors))
                for marker in ("SYNTHETICSECRET", "SYNTHETIC_secret"):
                    self.assertNotIn(marker, " ".join(errors))


    def test_all_string_url_userinfo_and_two_layer_encoding_block(self):
        for surface, text, code in url_userinfo_residual_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                errors, _ = MODULE.validate(plan_with_url_surface(surface, text), 20)
                self.assertEqual(code != 0, bool(errors))
                if code:
                    self.assertTrue(any("URL userinfo credentials" in error for error in errors))
                self.assertNotIn("SYNTHETIC_URI_ONLY", " ".join(errors))


    def test_protocol_relative_authorities_and_uri_path_controls(self):
        for surface, text, code in protocol_relative_userinfo_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                errors, _ = MODULE.validate(plan_with_url_surface(surface, text), 20)
                self.assertEqual(code != 0, bool(errors))
                if code:
                    self.assertTrue(any("URL userinfo credentials" in error for error in errors))
                self.assertNotIn("SYNTHETIC_URI_ONLY", " ".join(errors))


    def test_encoded_authority_delimiters_preserve_raw_uri_boundaries(self):
        for surface, text, code in encoded_authority_userinfo_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                errors, _ = MODULE.validate(plan_with_url_surface(surface, text), 20)
                self.assertEqual(code != 0, bool(errors))
                if code:
                    self.assertIn("plan must not contain URL userinfo credentials", errors)
                self.assertNotIn("SYNTHETIC", " ".join(errors))


    def test_uri_reference_boundaries_raw_decoded_and_parent_path_context(self):
        for surface, text, code in uri_reference_boundary_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                errors, _ = MODULE.validate(plan_with_url_surface(surface, text), 20)
                self.assertEqual(code != 0, bool(errors))
                if code:
                    self.assertIn("plan must not contain URL userinfo credentials", errors)
                self.assertNotIn("SYNTHETIC_BOUNDARY", " ".join(errors))


class ValidateCampaignCliTests(unittest.TestCase):
    """Real CLI invocations and disk receipts; all input is synthetic and local."""

    def run_cli(self, plan=None, raw=None, extra_args=()):
        with tempfile.TemporaryDirectory(prefix="sellerkit-edm-test-") as folder:
            root = Path(folder)
            source = root / "synthetic_plan.json"
            receipt_path = root / "receipt.json"
            source.write_bytes(raw if raw is not None else json.dumps(plan, ensure_ascii=False).encode("utf-8"))
            env = os.environ.copy()
            env.pop("SELLERKIT_ALIBABA_WORKSPACE", None)
            env.pop("SELLERKIT_ALIBABA_CONFIG", None)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONUTF8"] = "1"
            process = subprocess.run(
                [sys.executable, "-B", str(MODULE_PATH), "--input", str(source), "--receipt", str(receipt_path), *extra_args],
                cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            self.assertTrue(receipt_path.is_file(), process.stderr)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertIs(receipt["final_send_allowed"], False)
            self.assertEqual("LOCAL_PLAN_VALIDATION_ONLY", receipt["authority"])
            self.assertNotIn("Traceback", process.stderr)
            return process, receipt

    def test_real_cli_positive_and_prior_audit_regressions(self):
        cases = [
            (valid_plan(), 0, "PASS_EDM_DRAFT_PLAN"),
            ({**valid_plan(), "notes": {"email": "audit@example.invalid"}}, 3, "BLOCKED_EDM_DRAFT_PLAN"),
            ({**valid_plan(), "notes": {"token": "SYNTHETIC_NOT_REAL_TOKEN"}}, 3, "BLOCKED_EDM_DRAFT_PLAN"),
            ({**valid_plan(), "cta": {"text": "View", "url": "https://example.invalid/?email=audit%40example.invalid"}}, 3, "BLOCKED_EDM_DRAFT_PLAN"),
            ({**valid_plan(), "channel": []}, 3, "BLOCKED_EDM_DRAFT_PLAN"),
            ({**valid_plan(), "segment": {"source": "ALIBABA_SYSTEM_GROUP", "name": [], "live_count": 1}}, 3, "BLOCKED_EDM_DRAFT_PLAN"),
            ({**valid_plan(), "cta": {"text": "View", "url": "https://"}}, 3, "BLOCKED_EDM_DRAFT_PLAN"),
            ({**valid_plan(), "cta": {"text": "View", "url": "https://audit-user:SYNTHETIC_PASSWORD@example.invalid/path"}}, 3, "BLOCKED_EDM_DRAFT_PLAN"),
            ({**valid_plan(), "final_send_allowed": True}, 3, "BLOCKED_EDM_DRAFT_PLAN"),
        ]
        for plan, code, status in cases:
            with self.subTest(status=status, plan=plan):
                process, receipt = self.run_cli(plan)
                self.assertEqual(code, process.returncode)
                self.assertEqual(status, receipt["status"])
                output = process.stdout + process.stderr + json.dumps(receipt)
                for raw_value in ("audit@example.invalid", "SYNTHETIC_NOT_REAL_TOKEN", "SYNTHETIC_PASSWORD"):
                    self.assertNotIn(raw_value, output)

    def test_real_cli_all_malformed_required_field_types_have_receipts(self):
        for field in ("campaign_name", "segment", "channel", "channel_ready", "subject", "body_text", "claims_reviewed", "recipient_data_included", "final_send_allowed"):
            for value in ([], {}, None, 7):
                with self.subTest(field=field, value=value):
                    plan = copy.deepcopy(valid_plan())
                    plan[field] = value
                    process, receipt = self.run_cli(plan)
                    self.assertEqual(3, process.returncode)
                    self.assertTrue(receipt["status"].startswith("BLOCKED"))

    def test_real_cli_malformed_json_and_nonobject_roots_have_safe_receipts(self):
        for raw in (b"{", b"null", b"[]", b"NaN", b'{"notes": Infinity}', b'{"notes":{"token":"SYNTHETIC_SECRET","token":null}}', b"{" + b"[" * 1500):
            with self.subTest(raw=raw[:60]):
                process, receipt = self.run_cli(raw=raw)
                self.assertEqual(3, process.returncode)
                self.assertEqual("BLOCKED_INVALID_INPUT", receipt["status"])
                self.assertNotIn("SYNTHETIC_SECRET", process.stdout + process.stderr + json.dumps(receipt))

    def test_real_cli_invalid_recipient_limit_blocks(self):
        process, receipt = self.run_cli(valid_plan(), extra_args=("--max-recipients", "-1"))
        self.assertEqual(3, process.returncode)
        self.assertEqual("BLOCKED_EDM_DRAFT_PLAN", receipt["status"])

    def test_real_cli_namespaced_alias_regressions_and_metadata_controls(self):
        cases = [
            ({key: "SYNTHETIC_RANDOM_SECRET"}, 3)
            for key in ("form.password", "auth_token", "form[password]", "formPassword", "authtoken", "api.private_key", "form.clientSecret", "session_token")
        ]
        cases.extend([
            ({"form.password.sha256": "a" * 64, "auth_token_sha256": "b" * 64}, 0),
            ({"form.hasPassword": False, "api.auth_token_present": True, "auth_token_count": 2}, 0),
            ({"tokenization_notes": "Commercial product options.", "passwordless_operation": "Credentials are not collected."}, 0),
            ({"form.password.sha256": "SYNTHETIC_RANDOM_SECRET"}, 3),
            ({"api.auth_token_present": "SYNTHETIC_RANDOM_SECRET"}, 3),
        ])
        for notes, expected_code in cases:
            with self.subTest(notes=notes):
                process, receipt = self.run_cli({**valid_plan(), "notes": notes})
                self.assertEqual(expected_code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if expected_code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                self.assertNotIn("SYNTHETIC_RANDOM_SECRET", process.stdout + process.stderr + json.dumps(receipt))

    def test_real_cli_query_text_assignment_alias_and_safe_controls(self):
        blocked_notes = (
            "session_token=SYNTHETIC_ONLY", "authToken: SYNTHETIC_ONLY",
            "form.password=SYNTHETIC_ONLY", "api.privateKey=SYNTHETIC_ONLY",
            "form[password]=SYNTHETIC_ONLY", '{"session_token":"SYNTHETIC_ONLY"}',
            "session%255Ftoken%253DSYNTHETIC_ONLY", "auth&#95;token&#61;SYNTHETIC_ONLY",
            "auth_token_sha256=SYNTHETIC_ONLY", "auth_token_present=SYNTHETIC_ONLY",
        )
        blocked_urls = (
            "https://example.invalid/?auth_token=SYNTHETIC_ONLY",
            "https://example.invalid/?sessionToken=SYNTHETIC_ONLY",
            "https://example.invalid/?form.password=SYNTHETIC_ONLY",
            "https://example.invalid/?api.private_key=SYNTHETIC_ONLY",
            "https://example.invalid/?auth%5Ftoken%3DSYNTHETIC_ONLY",
            "https://example.invalid/?auth%255Ftoken%253DSYNTHETIC_ONLY",
        )
        safe_notes = (
            "auth_token_sha256=" + "a" * 64,
            "form.hasPassword=false; session_token_count=2; api.auth_token_present=true",
            "tokenization_notes=Commercial; passwordless_operation=Supported",
            "auth_token_policy=Readiness; session_token_policy=No-copy",
            "SMTP password is not configured. No cookie or token is copied.",
        )
        plans = [(dict(valid_plan(), notes=text), 3) for text in blocked_notes]
        plans += [(dict(valid_plan(), cta={"text": "View", "url": url}), 3) for url in blocked_urls]
        plans += [(dict(valid_plan(), notes=text), 0) for text in safe_notes]
        plans.append((dict(valid_plan(), cta={"text": "View", "url": "https://example.invalid/?session_token_sha256=" + "a" * 64 + "&has_token=false"}), 0))
        for plan, expected_code in plans:
            with self.subTest(plan=plan, expected_code=expected_code):
                process, receipt = self.run_cli(plan)
                self.assertEqual(expected_code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if expected_code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                self.assertNotIn("SYNTHETIC_ONLY", process.stdout + process.stderr + json.dumps(receipt))


    def test_real_cli_numeric_digest_and_kind_specific_metadata_controls(self):
        safe_notes = (
            "authTokenSha256=" + "1" * 64,
            "form.password.hash=" + "0" * 64,
            'auth_token_sha256="' + "1" * 64 + '"',
            "session_token_count=2; form.hasPassword=true",
            "session_token_count=0; form.hasPassword=false; auth_token_sha256=" + "0" * 64,
        )
        safe_urls = (
            "https://example.invalid/?authTokenSha256=" + "1" * 64,
            "https://example.invalid/?form.password.hash=" + "0" * 64,
            "https://example.invalid/?session_token_count=2&form.hasPassword=false",
        )
        plans = [(dict(valid_plan(), notes=text), 0) for text in safe_notes]
        plans += [(dict(valid_plan(), cta={"text": "View", "url": url}), 0) for url in safe_urls]
        for value in ("1" * 63, "1" * 65, "-" + "1" * 63, "g" * 64):
            plans.append((dict(valid_plan(), notes="authTokenSha256=" + value), 3))
            plans.append((dict(valid_plan(), cta={"text": "View", "url": "https://example.invalid/?authTokenSha256=" + value}), 3))
        plans += [(dict(valid_plan(), notes=text), 3) for text in (
            "session_token_count=true", "session_token_count=-1",
            "form.hasPassword=0", "auth_token_sha256=false",
        )]
        for value in ("true", "false", "0"):
            plans.append((dict(valid_plan(), notes="auth_token=" + value), 3))
            plans.append((dict(valid_plan(), cta={"text": "View", "url": "https://example.invalid/?auth_token=" + value}), 3))
        for plan, expected_code in plans:
            with self.subTest(plan=plan, expected_code=expected_code):
                process, receipt = self.run_cli(plan)
                self.assertEqual(expected_code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if expected_code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])


    def test_real_cli_compound_keys_query_values_fragments_and_controls(self):
        blocked_notes = [key + "=SYNTHETIC_ONLY" for key in (
            "api:key", "api/key", "api.key", "api-key", "api_key", "api+key",
            "api|key", "api[key]", "api key", "api::key", "form:api:key",
            "form/api/key", "form[api][key]", "form.apiKey",
        )]
        blocked_notes += [
            "password:SYNTHETIC_ONLY", "api:key:SYNTHETIC_ONLY",
            "smtp password:SYNTHETIC_ONLY", "password:SYNTHETIC_ONLY:TAIL",
            '{"api:key":"SYNTHETIC_ONLY"}',
        ]
        blocked_urls = ["https://example.invalid/?" + key + "=SYNTHETIC_ONLY" for key in (
            "api+key", "api%2Bkey", "api%20key", "api:key", "form%3Aapi%3Akey",
            "api%2Fkey", "api%5Bkey%5D", "api%7Ckey", "auth%3Atoken",
            "smtp+password", "api%253Akey",
        )]
        blocked_urls += [
            "https://example.invalid/?note=password%3DSYNTHETIC_ONLY",
            "https://example.invalid/?note=password%253DSYNTHETIC_ONLY",
            "https://example.invalid/#password%3ASYNTHETIC_ONLY",
            'https://example.invalid/?note=password="SYNTHETIC_ONLY"',
            'https://example.invalid/?note=password="SYNTHETIC_ONLY',
        ]
        safe_notes = (
            "api:key:sha256=" + "1" * 64,
            "api:key:sha256:" + "0" * 64,
            "form:api:key:count=2", "form:api:key:present=false",
            '"api:key:sha256"="' + "1" * 64 + '"',
            "Product: commercial faucet. Material: SUS304.",
            "api:key:policy=No-copy; tokenization_notes=Commercial",
            "SMTP password is not configured. No cookie or token is copied.",
        )
        safe_urls = (
            "https://example.invalid/?api+key+sha256=" + "1" * 64,
            "https://example.invalid/?api%2Bkey%2Bsha256=" + "0" * 64,
            "https://example.invalid/?form%3Aapi%3Akey%3Acount=2&has_token=false",
            "https://example.invalid/?note=tokenization+notes&label=api%2Bkey#product-options",
        )
        plans = [(dict(valid_plan(), notes=text), 3) for text in blocked_notes]
        plans += [(dict(valid_plan(), cta={"text": "View", "url": url}), 3) for url in blocked_urls]
        plans += [(dict(valid_plan(), notes=text), 0) for text in safe_notes]
        plans += [(dict(valid_plan(), cta={"text": "View", "url": url}), 0) for url in safe_urls]
        for plan, expected_code in plans:
            with self.subTest(plan=plan, expected_code=expected_code):
                process, receipt = self.run_cli(plan)
                self.assertEqual(expected_code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if expected_code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                self.assertNotIn("SYNTHETIC_ONLY", process.stdout + process.stderr + json.dumps(receipt))


    def test_real_cli_overlap_query_flags_paths_and_safe_metadata(self):
        blocked_notes = (
            "password: SYNTHETIC_ONLY mode=review",
            "password:SYNTHETIC_ONLY mode=review",
            "auth_token: SYNTHETIC_ONLY\tmode=review",
            "form.password: SYNTHETIC_ONLY; mode=review",
            "session_token:SYNTHETIC_ONLY, mode=review",
            "api:key: SYNTHETIC_ONLY mode=review",
            "password:SYNTHETIC_ONLY=review",
            "api:key:SYNTHETIC_ONLY=review",
            "form.password:SYNTHETIC_ONLY=review",
            '"password:SYNTHETIC_ONLY"=review',
        )
        safe_notes = (
            "api:key:sha256=" + "1" * 64 + " mode=review",
            "form:api:key:present=false mode=review",
            "passwordless_operation: supported mode=review",
            "api:key:policy=No-copy mode=review",
            '"api:key:policy"=No-copy',
            '"section:label"=Commercial',
            '"api:key:sha256"=' + "1" * 64,
        )
        blocked_urls = (
            "https://example.invalid/?auth_token%3ASYNTHETIC_ONLY",
            "https://example.invalid/?auth_token:SYNTHETIC_ONLY",
            "https://example.invalid/?form.password%3ASYNTHETIC_ONLY",
            "https://example.invalid/?api%3Akey%3ASYNTHETIC_ONLY",
            "https://example.invalid/?session_token%253ASYNTHETIC_ONLY",
            "https://example.invalid/auth_token=SYNTHETIC_ONLY",
            "https://example.invalid/auth_token:SYNTHETIC_ONLY",
            "https://example.invalid/%61uth_token%3DSYNTHETIC_ONLY",
            "https://example.invalid/%61uth%255Ftoken%253DSYNTHETIC_ONLY",
            "https://example.invalid/form%2Fpassword%3ASYNTHETIC_ONLY",
            "https://example.invalid/?note=password%3ASYNTHETIC_ONLY%20mode%3Dreview",
            "https://example.invalid/#password:SYNTHETIC_ONLY%20mode%3Dreview",
            "https://example.invalid/?auth_token%3ASYNTHETIC_ONLY%3Dreview",
            "https://example.invalid/password:SYNTHETIC_ONLY=review",
        )
        safe_urls = (
            "https://example.invalid/api:key:sha256=" + "1" * 64,
            "https://example.invalid/%61pi%3Akey%3Asha256%3D" + "0" * 64,
            "https://example.invalid/form.password.count=2",
            "https://example.invalid/form:hasPassword=false",
            "https://example.invalid/auth_token_policy=none",
            "https://example.invalid/?api:key:sha256=" + "1" * 64,
            "https://example.invalid/?api:key:present=false",
            "https://example.invalid/product-options#installation",
        )
        plans = [(dict(valid_plan(), notes=text), 3) for text in blocked_notes]
        plans += [(dict(valid_plan(), notes=text), 0) for text in safe_notes]
        for urls, expected_code in ((blocked_urls, 3), (safe_urls, 0)):
            for url in urls:
                plans.append((dict(valid_plan(), notes=url), expected_code))
                plans.append((dict(valid_plan(), cta={"text": "View", "url": url}), expected_code))
        for plan, expected_code in plans:
            with self.subTest(plan=plan, expected_code=expected_code):
                process, receipt = self.run_cli(plan)
                self.assertEqual(expected_code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if expected_code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                self.assertNotIn("SYNTHETIC_ONLY", process.stdout + process.stderr + json.dumps(receipt))


    def test_real_cli_complete_key_suffix_overlap_flags_and_controls(self):
        for surface, text, code in complete_key_residual_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                plan = valid_plan()
                if surface == "url":
                    plan["cta"]["url"] = text
                else:
                    plan["notes"] = text
                process, receipt = self.run_cli(plan)
                self.assertEqual(code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                for marker in ("SYNTHETICSECRET", "SYNTHETIC_secret"):
                    self.assertNotIn(marker, process.stdout + process.stderr + json.dumps(receipt))


    def test_real_cli_url_userinfo_encoded_authorities_and_safe_controls(self):
        for surface, text, code in url_userinfo_residual_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                process, receipt = self.run_cli(plan_with_url_surface(surface, text))
                self.assertEqual(code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                self.assertNotIn("SYNTHETIC_URI_ONLY", process.stdout + process.stderr + json.dumps(receipt))


    def test_real_cli_protocol_relative_urls_nested_authorities_and_paths(self):
        for surface, text, code in protocol_relative_userinfo_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                process, receipt = self.run_cli(plan_with_url_surface(surface, text))
                self.assertEqual(code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                self.assertNotIn("SYNTHETIC_URI_ONLY", process.stdout + process.stderr + json.dumps(receipt))


    def test_real_cli_encoded_authority_delimiters_and_safe_path_query_controls(self):
        for surface, text, code in encoded_authority_userinfo_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                process, receipt = self.run_cli(plan_with_url_surface(surface, text))
                self.assertEqual(code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                if code:
                    self.assertIn("plan must not contain URL userinfo credentials", receipt["errors"])
                self.assertNotIn("SYNTHETIC", process.stdout + process.stderr + json.dumps(receipt))


    def test_real_cli_uri_reference_boundaries_and_normal_word_path_query_controls(self):
        for surface, text, code in uri_reference_boundary_cases():
            with self.subTest(surface=surface, text=text, expected_code=code):
                process, receipt = self.run_cli(plan_with_url_surface(surface, text))
                self.assertEqual(code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                if code:
                    self.assertIn("plan must not contain URL userinfo credentials", receipt["errors"])
                self.assertNotIn("SYNTHETIC_BOUNDARY", process.stdout + process.stderr + json.dumps(receipt))


class AssignmentNamespaceBoundTests(unittest.TestCase):
    run_cli = ValidateCampaignCliTests.run_cli

    def test_bounded_namespace_contracts_preserve_full_key_and_prefix_semantics(self):
        cases = (
            ("form:api:key:present", "boolean"),
            ("form:api:key:count", "count"),
            ("form:api:key:sha256", "sha256"),
            ("form:api:key:policy", "policy"),
            ("form:session_token", "credential"),
            ("form:password:SYNTHETIC:has_token", None),
            ("form:api:key:SYNTHETIC:token_policy", None),
            ("form:hasToken:label:token_policy", None),
            ("form:token_policy:label:has_token", None),
            ("form:AuditPassword:has_token", None),
            ("form:auditTokenPolicy:has_token", "boolean"),
            ("form:passwordless:has_token", "boolean"),
            ("form:_:has_token", "boolean"),
        )
        for key, expected in cases:
            with self.subTest(key=key):
                self.assertEqual(expected, MODULE.complete_assignment_kind(key))

    def test_namespace_normalization_work_is_linear_in_lexical_parts(self):
        original = MODULE.key_words
        for count in (100, 200, 1000, 5000):
            key = "form:" + "x:" * count + "has_token"
            calls = []
            def observed(value):
                calls.append(len(value))
                return original(value)
            with self.subTest(parts=count), patch.object(MODULE, "key_words", side_effect=observed):
                self.assertEqual("boolean", MODULE.complete_assignment_kind(key))
                self.assertLessEqual(len(calls), count + 3)
                self.assertLessEqual(sum(calls), 2 * len(key))

    def test_long_namespace_sensitive_prefixes_never_borrow_typed_suffixes(self):
        for count in (100, 200, 1000):
            for prefix in ("password", "api:key", "hasToken", "token_policy"):
                key = "form:" + "x:" * count + prefix + ":SYNTHETIC:has_token"
                with self.subTest(parts=count, prefix=prefix):
                    self.assertIsNone(MODULE.complete_assignment_kind(key))

    def test_real_cli_long_namespace_metadata_and_invalid_values_are_bounded(self):
        for count in (100, 200, 500):
            prefix = "form:" + "x:" * count
            endings = (("has_token=false", 0), ("session_token_count=0", 0),
                       ("token_sha256=" + "0" * 64, 0), ("token_policy=not_copied", 0),
                       ("has_token=SYNTHETIC_NAMESPACE_ONLY", 3),
                       ("auth_token=SYNTHETIC_NAMESPACE_ONLY", 3))
            for tail, code in endings:
                with self.subTest(parts=count, tail=tail):
                    started = time.perf_counter()
                    process, receipt = self.run_cli({**valid_plan(), "notes": prefix + tail})
                    self.assertLess(time.perf_counter() - started, 3)
                    self.assertEqual(code, process.returncode)
                    self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                    self.assertNotIn("SYNTHETIC_NAMESPACE_ONLY", process.stdout + process.stderr + json.dumps(receipt))


class AmbiguousNamespaceBoundTests(unittest.TestCase):
    run_cli = ValidateCampaignCliTests.run_cli

    def test_bounded_assignment_suffixes_preserve_legacy_key_type_value_contracts(self):
        keys = ("token", "form:api:key", "form:has_token", "form:token_count",
                "form:sessionTokenConfigured", "form:token_sha256", "form:api:key:policy",
                "AuditPassword", "form:rawCustomerEmail", "form:passwordless",
                "form:token_count:has_token", "form:has_token:token_count",
                "form:" + "x:" * 64 + "has_token",
                "form:" + "x:" * 64 + "api:key", "_:has_token", "has__token",
                "form:recipientDataHash", "form:secretkeyincluded")
        values = (False, 0, 2, "false", "0" * 64, "1" * 64, "SYNTHETIC_NAMESPACE_ONLY", None)
        for key in keys:
            for value in values:
                for quoted in (False, True):
                    with self.subTest(key=key, value=value, quoted=quoted):
                        kind = MODULE.sensitive_metadata_kind(key)
                        parsed = MODULE.parsed_metadata_value(kind, value, quoted=quoted)
                        expected = set()
                        if kind is not None:
                            if not MODULE.safe_sensitive_metadata(key, parsed):
                                expected.add("contact or credential metadata must be a boolean, count or SHA-256 digest")
                        elif MODULE.sensitive_key(key) and parsed not in (None, ""):
                            expected.add("plan must not contain credential or contact assignment values")
                        self.assertEqual(expected, MODULE.assignment_errors(key, value, quoted=quoted))

    def test_ambiguous_colon_prefix_normalization_work_is_linear(self):
        original = MODULE.key_words
        for count in (256, 1024):
            for ending in ("api:key:SYNTHETIC_NAMESPACE_ONLY:has_token=false",
                           "cookie:SYNTHETIC_NAMESPACE_ONLY:token_policy=not_copied"):
                text = "form:" + "x:" * count + ending
                calls = []
                def observed(value):
                    calls.append(len(value))
                    return original(value)
                with self.subTest(parts=count, ending=ending), patch.object(MODULE, "key_words", side_effect=observed):
                    self.assertTrue(MODULE.plain_assignment_errors(text))
                    self.assertLessEqual(len(calls), 10 * count + 100)
                    self.assertLessEqual(sum(calls), 100 * len(text))

    def test_internal_sensitive_prefixes_keep_colon_value_fail_closed(self):
        sensitive = ("api:key", "cookie", "session_token", "AuditPassword",
                     "has_token", "token_count", "token_sha256", "smtpUser")
        for count in (1, 64):
            for key in sensitive:
                for quoted in (False, True):
                    compound = "form:" + "x:" * count + key + ":SYNTHETIC_NAMESPACE_ONLY:token_policy"
                    text = ('"' + compound + '"' if quoted else compound) + "=not_copied"
                    with self.subTest(parts=count, key=key, quoted=quoted):
                        self.assertTrue(MODULE.plain_assignment_errors(text))
                        self.assertNotIn("SYNTHETIC_NAMESPACE_ONLY", str(MODULE.plain_assignment_errors(text)))

    def test_real_cli_hidden_aliases_and_typed_metadata_under_three_seconds(self):
        endings = (("api:key:SYNTHETIC_NAMESPACE_ONLY:has_token=false", 3),
                   ("cookie:SYNTHETIC_NAMESPACE_ONLY:token_policy=not_copied", 3),
                   ("has_token:SYNTHETIC_NAMESPACE_ONLY:token_policy=not_copied", 3),
                   ('api:key:present="false"', 3), ("has_token=false", 0),
                   ("api:key:sha256=" + "1" * 64, 0))
        for count in (256, 1024):
            for tail, code in endings:
                with self.subTest(parts=count, tail=tail):
                    started = time.perf_counter()
                    process, receipt = self.run_cli({**valid_plan(), "notes": "form:" + "x:" * count + tail})
                    self.assertLess(time.perf_counter() - started, 3)
                    self.assertEqual(code, process.returncode)
                    self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])
                    self.assertNotIn("SYNTHETIC_NAMESPACE_ONLY", process.stdout + process.stderr + json.dumps(receipt))


def review_quote_boundary_cases():
    """New review cases; old fixtures/helpers/expected values remain untouched."""
    enclosures = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
                  ("«", "»"), ("`", "`"), ("「", "」"))
    tails = (("has_token=false", 0), ("token_count=0", 0),
             ("token_sha256=" + "1" * 64, 0),
             ("has_token=SYNTHETIC_QUOTE_ONLY", 3), ("token_count=-1", 3),
             ("token_sha256=" + "1" * 63, 3),
             ("auth_token=SYNTHETIC_QUOTE_ONLY", 3),
             ("api+key=SYNTHETIC_QUOTE_ONLY", 3),
             ("api%2Bkey=SYNTHETIC_QUOTE_ONLY", 3))
    return [(surface, opening + "https://example.invalid/?" + tail + closing, code)
            for opening, closing in enclosures for tail, code in tails
            for surface in ("notes", "nested", "body", "cta_text")]


class ReviewQuoteAndLexerTests(unittest.TestCase):
    def run_bounded_cli(self, plan):
        with tempfile.TemporaryDirectory(prefix="sellerkit-edm-review-") as folder:
            source = Path(folder) / "synthetic-plan.json"
            receipt_path = Path(folder) / "receipt.json"
            source.write_bytes(json.dumps(plan, ensure_ascii=False).encode("utf-8"))
            env = os.environ.copy()
            env.pop("SELLERKIT_ALIBABA_WORKSPACE", None)
            env.pop("SELLERKIT_ALIBABA_CONFIG", None)
            env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            started = time.perf_counter()
            process = subprocess.run(
                [sys.executable, "-B", str(MODULE_PATH), "--input", str(source), "--receipt", str(receipt_path)],
                cwd=r"C:\Windows\Temp", env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=3,
            )
            self.assertLess(time.perf_counter() - started, 3)
            self.assertTrue(receipt_path.is_file(), process.stderr)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertIs(receipt["final_send_allowed"], False)
            self.assertEqual("LOCAL_PLAN_VALIDATION_ONLY", receipt["authority"])
            self.assertNotIn("SYNTHETIC", process.stdout + process.stderr + json.dumps(receipt))
            return process, receipt

    def test_surrounding_quotes_preserve_typed_url_metadata_and_negatives(self):
        for surface, text, code in review_quote_boundary_cases():
            with self.subTest(surface=surface, text=text, code=code):
                errors, _ = MODULE.validate(plan_with_url_surface(surface, text), 20)
                self.assertEqual(code != 0, bool(errors))
                self.assertNotIn("SYNTHETIC", " ".join(errors))

    def test_real_cli_surrounding_quotes_preserve_types_and_raw_form_queries(self):
        for surface, text, code in review_quote_boundary_cases():
            with self.subTest(surface=surface, text=text, code=code):
                process, receipt = self.run_bounded_cli(plan_with_url_surface(surface, text))
                self.assertEqual(code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])

    def test_enclosures_do_not_truncate_internal_quoted_or_encoded_credentials(self):
        cases = []
        for opening, closing in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("«", "»")):
            for tail in ('note=password="SYNTHETIC_QUOTE_ONLY"',
                         "note=password='SYNTHETIC_QUOTE_ONLY'",
                         'note=password=\\"SYNTHETIC_QUOTE_ONLY\\"',
                         "note=password=%22SYNTHETIC_QUOTE_ONLY%22",
                         "note=password=%2522SYNTHETIC_QUOTE_ONLY%2522",
                         "token=\"SYNTHETIC_QUOTE_ONLY\""):
                cases.append(opening + "https://example.invalid/?" + tail + closing)
            for uri in ("https://audit-user:SYNTHETIC%20QUOTE_ONLY@localhost/catalog",
                        "https://audit-user:SYNTHETIC%253cQUOTE_ONLY%2540localhost/catalog",
                        "https://audit-user:SYNTHETIC&#32;QUOTE_ONLY&#64;localhost/catalog"):
                cases.append(opening + uri + closing)
        for text in cases:
            with self.subTest(text=text):
                process, receipt = self.run_bounded_cli(plan_with_url_surface("notes", text))
                self.assertEqual(3, process.returncode)
                self.assertEqual("BLOCKED_EDM_DRAFT_PLAN", receipt["status"])

    def test_dictionary_suffix_storage_is_bounded_and_normalizes_once(self):
        original = MODULE.key_words
        for count in (8192, 16384):
            key = "form:" + "x:" * count + "has_token"
            calls = []
            def observed(value):
                calls.append(len(value))
                return original(value)
            with self.subTest(parts=count), patch.object(MODULE, "key_words", side_effect=observed):
                suffixes = MODULE.key_suffixes(key)
                self.assertEqual([len(key)], calls)
                self.assertLessEqual(len(suffixes), MODULE.KEY_CONTRACT_LIMIT)
                self.assertLessEqual(sum(map(len, suffixes)), MODULE.KEY_CONTRACT_LIMIT ** 2)
                self.assertEqual("hastoken", suffixes[-2])

    def test_real_cli_long_dictionary_metadata_and_hidden_credentials_are_bounded(self):
        endings = (("has_token", False, 0), ("token_count", 0, 0),
                   ("token_sha256", "1" * 64, 0),
                   ("token_policy", "Configuration readiness only.", 0),
                   ("has_token", "SYNTHETIC_NAMESPACE_ONLY", 3),
                   ("auth_token", "SYNTHETIC_NAMESPACE_ONLY", 3),
                   ("api:key:SYNTHETIC_NAMESPACE_ONLY:has_token", False, 3),
                   ("has_token:SYNTHETIC_NAMESPACE_ONLY:token_count", 0, 3))
        for count in (8192, 16384):
            for tail, value, code in endings:
                with self.subTest(parts=count, tail=tail, code=code):
                    plan = dict(valid_plan(), notes={"form:" + "x:" * count + tail: value})
                    process, receipt = self.run_bounded_cli(plan)
                    self.assertEqual(code, process.returncode)
                    self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])

    def test_lexer_emits_no_overlapping_colon_suffixes(self):
        for count in (8192, 16384):
            for tail, expected in (("has_token:false", 1), ("has_token=false", 1),
                                   ("api:key:SYNTHETIC_NAMESPACE_ONLY:has_token=false", 2)):
                text = "form:" + "x:" * count + tail
                with self.subTest(parts=count, tail=tail):
                    assignments = list(MODULE.lexical_assignments(text))
                    self.assertEqual(expected, len(assignments))
                    self.assertEqual(text.split("=", 1)[0] if "=" in text else text.rsplit(":", 1)[0], assignments[0][0])

    def test_real_cli_long_colon_only_and_equals_tokens_under_three_seconds(self):
        endings = (("has_token:false", 0), ("token_count:0", 0),
                   ("token_sha256:" + "1" * 64, 0),
                   ("token_policy:Configuration-readiness", 0),
                   ("has_token:SYNTHETIC_NAMESPACE_ONLY", 3),
                   ("auth_token:SYNTHETIC_NAMESPACE_ONLY", 3),
                   ("api:key:SYNTHETIC_NAMESPACE_ONLY:has_token:false", 3),
                   ("has_token:SYNTHETIC_NAMESPACE_ONLY:token_count:0", 3),
                   ("has_token=false", 0),
                   ("api:key:SYNTHETIC_NAMESPACE_ONLY:has_token=false", 3))
        for count in (8192, 16384):
            for tail, code in endings:
                with self.subTest(parts=count, tail=tail, code=code):
                    process, receipt = self.run_bounded_cli(dict(valid_plan(), notes="form:" + "x:" * count + tail))
                    self.assertEqual(code, process.returncode)
                    self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])


def review_unicode_relative_cases():
    """Formal19 Unicode authority boundaries, with unchanged prior helpers."""
    rows = []
    for opening, closing in (("“", "”"), ("‘", "’"), ("«", "»"), ("‹", "›"), ("「", "」"), ("『", "』")):
        values = (("//user:SYNTHETIC_RELATIVE_ONLY@192.0.2.1/path", 3),
                  ("//192.0.2.1/path", 0),
                  ("https://localhost/catalog" + opening + "//public:topic@192.0.2.1/path", 0),
                  ("https://example.invalid/?has_token=false", 0),
                  ("https://example.invalid/?token_count=0", 0),
                  ("https://example.invalid/?token_sha256=" + "1" * 64, 0))
        for uri, code in values:
            text = opening + uri + closing
            for depth in range(3):
                # Fully encoded metadata URLs retain r14's fail-closed prose
                # handling; do not reinterpret their form-query grammar.
                expected = 3 if depth and uri.startswith("https://example.invalid/?") else code
                rows.extend((surface, text, expected) for surface in ("notes", "nested", "body", "cta_text"))
                text = quote(text, safe="")
        for uri, code in values[:2]:
            text = "&#" + str(ord(opening)) + ";" + uri + "&#" + str(ord(closing)) + ";"
            rows.extend((surface, text, code) for surface in ("notes", "nested", "body", "cta_text"))
    return rows


class ReviewUnicodeAndDottedPipelineTests(unittest.TestCase):
    run_bounded_cli = ReviewQuoteAndLexerTests.run_bounded_cli

    def test_unicode_relative_authorities_all_decode_stages_and_parent_controls(self):
        for surface, text, code in review_unicode_relative_cases():
            with self.subTest(surface=surface, text=text, code=code):
                errors, _ = MODULE.validate(plan_with_url_surface(surface, text), 20)
                self.assertEqual(code != 0, bool(errors))
                if code:
                    if "SYNTHETIC_RELATIVE_ONLY" in MODULE.decoded_text(text):
                        self.assertIn("plan must not contain URL userinfo credentials", errors)
                    else:
                        self.assertIn("contact or credential metadata must be a boolean, count or SHA-256 digest", errors)
                self.assertNotIn("SYNTHETIC", " ".join(errors))

    def test_real_cli_unicode_relative_references_metadata_and_parent_paths(self):
        for surface, text, code in review_unicode_relative_cases():
            with self.subTest(surface=surface, text=text, code=code):
                process, receipt = self.run_bounded_cli(plan_with_url_surface(surface, text))
                self.assertEqual(code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])

    def test_maximal_candidate_email_and_scheme_semantics_match_legacy_short_controls(self):
        email_cases = (("user@example.invalid", True), (".user@example.invalid", True),
                       ("+user@example.invalid", True), ("用户user@example.invalid", False),
                       ("用户.user@example.invalid", True), ("a@b@c.invalid", True),
                       ("a@bad_domain", False), ("form.x.x.has_token=false", False),
                       ("user@192.0.2.1", False), ("no @ address", False),
                       ("audit%40example.invalid", False), ("user@host.extra.invalid", True))
        for text, expected in email_cases:
            with self.subTest(email=text):
                self.assertEqual(expected, bool(MODULE.EMAIL_RE.search(text)))
                self.assertEqual(expected, MODULE.email_address_present(text))
        scheme_cases = (("https://localhost/path", [(0, 8)]),
                        (".https://localhost/path", [(1, 9)]),
                        ("1https://localhost/path", []),
                        ("1.https://localhost/path", [(2, 10)]),
                        ("prefix https://localhost/?redirect=http://127.0.0.1", [(7, 15), (35, 42)]),
                        ("form.x.x.has_token=false", []))
        for text, expected in scheme_cases:
            with self.subTest(scheme=text):
                self.assertEqual(expected, list(MODULE.uri_scheme_spans(text)))

    def test_long_dotted_candidates_do_not_enter_overlapping_search(self):
        original = MODULE.EMAIL_RE
        class AnchoredEmail:
            def __init__(self):
                self.starts = []
            def match(self, text, start):
                self.starts.append(start)
                return original.match(text, start)
            def search(self, *_args):
                raise AssertionError("overlapping email search must not run")
        for count in (1024, 8192, 32768):
            prefix = "form." + "x." * count
            for tail, expected, matches in (("has_token=false", False, 0),
                                             ("user@example.invalid", True, 1),
                                             ("user@bad_domain", False, 1)):
                observed = AnchoredEmail()
                with self.subTest(parts=count, tail=tail), patch.object(MODULE, "EMAIL_RE", observed):
                    self.assertEqual(expected, MODULE.email_address_present(prefix + tail))
                    self.assertEqual(matches, len(observed.starts))
                    self.assertEqual([], list(MODULE.uri_scheme_spans(prefix + tail)))

    def test_real_cli_1k_to_32k_dotted_and_colon_strings_and_dictionary_keys(self):
        for count in (1024, 4096, 8192, 16384, 32768):
            for separator in (".", ":"):
                prefix = "form" + separator + ("x" + separator) * count
                endings = (("has_token", False, "false", 0),
                           ("token_count", 0, "0", 0),
                           ("token_sha256", "1" * 64, "1" * 64, 0),
                           ("auth_token", "SYNTHETIC_DOTTED_ONLY", "SYNTHETIC_DOTTED_ONLY", 3),
                           ("password:SYNTHETIC_DOTTED_ONLY:has_token", False, "false", 3))
                for tail, child, value, code in endings:
                    for surface in ("notes", "dict"):
                        with self.subTest(parts=count, separator=separator, tail=tail, surface=surface):
                            notes = {prefix + tail: child} if surface == "dict" else prefix + tail + "=" + value
                            process, receipt = self.run_bounded_cli(dict(valid_plan(), notes=notes))
                            self.assertEqual(code, process.returncode)
                            self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])


def review_chunk_and_dictionary_cases():
    """Minimal formal20 controls, identical to the external fixed pre/post inputs."""
    rows = []
    for text in ("See the api key=SYNTHETIC_CHUNK_ONLY",
                 "This is private key:SYNTHETIC_CHUNK_ONLY",
                 "Before entering confirm client secret=SYNTHETIC_CHUNK_ONLY",
                 "Review note smtp user:SYNTHETIC_CHUNK_ONLY",
                 "Please check the reply email=SYNTHETIC_CHUNK_ONLY",
                 "Please review very carefully the crm customer id:SYNTHETIC_CHUNK_ONLY",
                 "See the api key:SYNTHETIC_CHUNK_ONLY:has_token=false"):
        rows.append(("chunk_" + str(len(rows)), text, 3))
    text = "See the api key=SYNTHETIC_CHUNK_ONLY"
    for depth in (1, 2):
        text = quote(text, safe="")
        rows.append(("decoded_" + str(depth), text, 3))
    for text, code in (("See the token count=0", 0),
                       ("This is has token=false", 0),
                       ("See the api key:present=false", 0),
                       ("See the token count=-1", 3),
                       ("See the api, key=SYNTHETIC_CHUNK_ONLY", 0)):
        rows.append(("chunk_control_" + str(len(rows)), text, code))
    for key, value, code in (("form:api:key:present", False, 0),
                             ("form:api:key:count", 0, 0),
                             ("form:api:key:sha256", "1" * 64, 0),
                             ("form:private:key:hash", "0" * 64, 0),
                             ("form:api:key:present", "false", 3),
                             ("form:api:key:count", False, 3),
                             ("form:api:key:count", -1, 3),
                             ("form:api:key:sha256", "SYNTHETIC_BAD_DIGEST", 3),
                             ("form:password:SYNTHETIC_CHUNK_ONLY:api:key:present", False, 3),
                             ("form:api:key:SYNTHETIC_CHUNK_ONLY:token_count", 0, 3)):
        rows.append(("dictionary_" + str(len(rows)), {key: value}, code))
    prefix = "ordinary " * 8192
    rows.extend((("long_words_safe", prefix + "See the token count=0", 0),
                 ("long_words_secret", prefix + "See the api key=SYNTHETIC_CHUNK_ONLY", 3),
                 ("long_dictionary_safe", {"form:" + "x:" * 8192 + "api:key:present": False}, 0),
                 ("long_dictionary_secret", {"form:" + "x:" * 8192 + "password:SYNTHETIC_CHUNK_ONLY:api:key:present": False}, 3)))
    return rows


class ReviewChunkAndBoundDictionaryTests(unittest.TestCase):
    run_bounded_cli = ReviewQuoteAndLexerTests.run_bounded_cli

    def test_compound_chunk_aliases_and_bound_dictionary_contracts(self):
        for name, notes, code in review_chunk_and_dictionary_cases():
            with self.subTest(case=name):
                errors, _ = MODULE.validate(dict(valid_plan(), notes=notes), 20)
                self.assertEqual(code != 0, bool(errors))
                self.assertNotIn("SYNTHETIC", " ".join(errors))

    def test_real_cli_fixed_compound_chunks_and_typed_dictionary_keys(self):
        for name, notes, code in review_chunk_and_dictionary_cases():
            with self.subTest(case=name):
                process, receipt = self.run_bounded_cli(dict(valid_plan(), notes=notes))
                self.assertEqual(code, process.returncode)
                self.assertEqual("PASS_EDM_DRAFT_PLAN" if code == 0 else "BLOCKED_EDM_DRAFT_PLAN", receipt["status"])

    def test_chunk_tail_does_not_restore_overlapping_namespace_scans(self):
        for count in (1024, 8192):
            with self.subTest(words=count):
                assignments = list(MODULE.lexical_assignments("ordinary " * count + "See the api key=SYNTHETIC_CHUNK_ONLY"))
                self.assertEqual(1, len(assignments))
                self.assertTrue(assignments[0][0].endswith("api key"))
                self.assertLessEqual(len(assignments[0][0]), MODULE.KEY_CONTRACT_LIMIT)
            with self.subTest(dotted_parts=count):
                self.assertEqual([], list(MODULE.lexical_assignments("form." + "x." * count)))


if __name__ == "__main__":
    unittest.main()
