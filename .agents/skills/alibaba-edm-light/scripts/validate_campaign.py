#!/usr/bin/env python3
# Validate locally; this validator never sends, schedules, or activates a campaign.
# 只做本地校验；本校验器不会发送、排期或启用活动。
"""Validate a privacy-minimal Alibaba EDM canary plan."""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit


ALLOWED_SEGMENTS = {
    "近30天询盘买家",
    "近30天TM买家",
    "活跃RFQ客户",
    "近30天访问买家",
    "店铺兴趣客户",
}
ALLOWED_CHANNELS = {"ALIYUN_DIRECTMAIL", "OKKI_EDM"}
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
EMAIL_CANDIDATE_RE = re.compile(r"(?i)[A-Z0-9._%+-]+")
EMAIL_START_RE = re.compile(r"(?i)\b[A-Z0-9_]")
STRONG_CLAIM_RE = re.compile(
    r"(?i)\b(best|cheapest|lowest\s+price|guaranteed|100%|number\s*1|No\.\s*1)\b"
)
SENSITIVE_KEYS = {
    "email", "emailaddress", "customeremail", "recipientemail", "staffemail",
    "replyemail", "replyaddress", "smtpuser", "smtpusername", "smtppassword",
    "password", "passwd", "pwd", "token", "accesstoken", "refreshtoken",
    "idtoken", "authtoken", "sessiontoken", "bearertoken", "csrftoken",
    "apikey", "secret", "secretkey", "privatekey", "clientsecret", "authorization", "cookie",
    "cookies", "sessionid", "customername", "recipientname", "staffname",
    "customerid", "recipientid", "contactid", "customerrows", "recipientrows",
    "rawcrmrows", "recipientdata", "customerdata", "recipient", "customer",
    "contact", "recipients", "contacts", "credentials",
}
ASSIGNMENT_KEY_PATTERN = (
    r'''(?:["'][^"'=\r\n]+["']|[A-Za-z_][^\s=;,&<>"'{}]*'''
    r'''(?:[ \t]+[A-Za-z_][^\s=;,&<>"'{}]*){0,2})'''
)
# Match a lexical key token once, without a trailing delimiter that could make
# a failed '=' search retry every overlapping colon suffix.
ASSIGNMENT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(?P<key>" + ASSIGNMENT_KEY_PATTERN + r")")
ASSIGNMENT_VALUE_RE = re.compile(
    r"(?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)|(?P<bare>[^\s&;,<>\"'\]}]+)", re.S
)
URI_SCHEME_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+.-]+")
URI_SCHEME_START_RE = re.compile(r"\b[A-Za-z]")
URL_ENCLOSURES = {'"': '"', "'": "'", "`": "`", "“": "”", "‘": "’",
                  "«": "»", "‹": "›", "「": "」", "『": "』"}
URL_ENCLOSURE_BOUNDARIES = frozenset(URL_ENCLOSURES) | frozenset(URL_ENCLOSURES.values())
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
HOST_LABEL_RE = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")
KEY_CONTRACT_LIMIT = max(len(prefix) + len(sensitive)
                         for prefix in ("", "raw", "smtp", "crm")
                         for sensitive in SENSITIVE_KEYS) + len("configured")
NORMALIZED_SENSITIVE_KEYS = SENSITIVE_KEYS | {
    prefix + sensitive for prefix in ("raw", "smtp", "crm") for sensitive in SENSITIVE_KEYS
}


def key_words(key: str) -> list[str]:
    """Use the same separators and CamelCase normalization for every key."""
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", key)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", separated)
    return re.findall(r"[a-z0-9]+", separated.casefold())


def key_suffixes(key: str) -> list[str]:
    """Only finite-vocabulary-sized suffixes can classify any key.

    Preserve longest-to-shortest precedence without constructing every growing
    dictionary namespace suffix. Normalization visits the original key once.
    """
    suffixes: list[str] = []
    candidate = ""
    for word in reversed(key_words(key)):
        if len(word) + len(candidate) > KEY_CONTRACT_LIMIT:
            break
        candidate = word + candidate
        suffixes.append(candidate)
    return suffixes[::-1]


def sensitive_normalized_key(key: str) -> bool:
    return key in NORMALIZED_SENSITIVE_KEYS


def normalized_metadata_kind(candidate: str) -> str | None:
    """Classify only the entire normalized key, never a trailing word alias."""
    if candidate.startswith("has") and sensitive_normalized_key(candidate[3:]):
        return "boolean"
    for suffix in ("present", "configured", "included", "count", "sha256", "hash"):
        if candidate.endswith(suffix) and sensitive_normalized_key(candidate[:-len(suffix)]):
            return {"count": "count", "sha256": "sha256", "hash": "sha256"}.get(suffix, "boolean")
    return None


def sensitive_metadata_kind(key: str) -> str | None:
    for candidate in key_suffixes(key):
        kind = normalized_metadata_kind(candidate)
        if kind is not None:
            return kind
    return None


def safe_metadata_value(kind: str | None, value: Any) -> bool:
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "count":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if kind == "sha256":
        return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None
    return False


def safe_sensitive_metadata(key: str, value: Any) -> bool:
    """Allow availability/count/hash metadata, never the underlying identifier."""
    return safe_metadata_value(sensitive_metadata_kind(key), value)


def sensitive_key(key: str) -> bool:
    return any(sensitive_normalized_key(candidate) for candidate in key_suffixes(key))


def sensitive_metadata_key(key: str) -> bool:
    return sensitive_metadata_kind(key) is not None


def exact_assignment_kind(key: str) -> str | None:
    """Bind a complete lexical key, not a suffix borrowed from later prose."""
    normalized = "".join(key_words(key))
    kind = normalized_metadata_kind(normalized)
    if kind is not None:
        return kind
    if normalized.endswith("policy") and sensitive_normalized_key(normalized[:-6]):
        return "policy"
    if sensitive_normalized_key(normalized):
        return "credential"
    return None


def complete_assignment_kind(key: str) -> str | None:
    """Typed whole keys may have explicit, non-sensitive UI namespaces.

    A namespace is a lexical sequence, not whitespace-separated copy. Any
    credential/metadata prefix retains its own delimiter and cannot be hidden
    by a later typed suffix (password:VALUE:has_token=false).
    """
    kind = exact_assignment_kind(key)
    if kind is not None:
        return kind
    if re.search(r"\s", key):
        return None
    parts = [part for part in re.split(r"[./:\[\]]+", key) if part]
    if len(parts) < 2 or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_+|\-]*", part) is None for part in parts):
        return None
    # Known contracts have a finite maximum normalized length. Normalize each
    # part once; no growing namespace substring is rejoined or re-normalized.
    limit = KEY_CONTRACT_LIMIT
    words_by_part = [key_words(part) for part in parts]

    def normalized_kind(candidate: str) -> str | None:
        result = normalized_metadata_kind(candidate)
        if result is not None:
            return result
        if candidate.endswith("policy") and sensitive_normalized_key(candidate[:-6]):
            return "policy"
        return "credential" if sensitive_normalized_key(candidate) else None

    suffix = ""
    selected = None
    for index in range(len(parts) - 1, 0, -1):
        suffix = "".join(words_by_part[index]) + suffix
        if len(suffix) > limit:
            break
        result = normalized_kind(suffix)
        if result is not None:
            selected = (index, result)
    if selected is None:
        return None
    index, kind = selected
    # Match only bounded word suffixes at each namespace part boundary. The
    # credential/metadata suffix rule includes CamelCase/underscore words;
    # policy contracts retain their original complete-part start requirement.
    window: list[tuple[str, bool]] = []
    length = 0
    for words in words_by_part[:index]:
        for position, word in enumerate(words):
            window.append((word, position == 0))
            length += len(word)
            while length > limit:
                length -= len(window.pop(0)[0])
        for start, (_, part_start) in enumerate(window):
            result = normalized_kind("".join(word for word, _ in window[start:]))
            if result is not None and (result != "policy" or part_start):
                return None
    return kind


def decoded_text(text: str) -> str:
    # Detect encoded addresses/credentials in CTA queries as well as plain text.
    for _ in range(2):
        decoded = html.unescape(unquote(text))
        if decoded == text:
            break
        text = decoded
    return text


def parsed_metadata_value(kind: str | None, value: Any, *, quoted: bool = False) -> Any:
    if isinstance(value, str) and not quoted:
        if kind == "boolean" and value in {"true", "false"}:
            return value == "true"
        if kind == "count" and re.fullmatch(r"\d+", value):
            return int(value)
    return value


def assignment_errors(key: str, value: Any, *, quoted: bool = False) -> set[str]:
    """One key/type contract for complete query and prose assignment keys."""
    errors: set[str] = set()
    # Only the finite credential/metadata vocabulary can match a word suffix.
    # Walk backwards once and never materialize growing namespace suffixes.
    limit = KEY_CONTRACT_LIMIT
    metadata_kind = None
    credential_key = False
    candidate = ""
    for word in reversed(key_words(key)):
        candidate = word + candidate
        if len(candidate) > limit:
            break
        kind = normalized_metadata_kind(candidate)
        if kind is not None:
            # Preserve the original longest-to-shortest suffix precedence.
            metadata_kind = kind
        credential_key = credential_key or sensitive_normalized_key(candidate)
    value = parsed_metadata_value(metadata_kind, value, quoted=quoted)
    if metadata_kind is not None:
        if not safe_metadata_value(metadata_kind, value):
            errors.add("contact or credential metadata must be a boolean, count or SHA-256 digest")
    elif credential_key and value not in (None, ""):
        errors.add("plan must not contain credential or contact assignment values")
    return errors


def plain_assignment_errors(text: str) -> set[str]:
    """Equals binds a complete compound key; colon remains a value delimiter."""
    errors: set[str] = set()
    for raw_key, value_start, colon in lexical_assignments(text):
        raw_key = raw_key.strip()
        quoted_key = raw_key.startswith(("\"", "'"))
        key = raw_key[1:-1] if quoted_key else raw_key
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        value_match = ASSIGNMENT_VALUE_RE.match(text, value_start)
        value: Any = None
        quoted = False
        if value_match:
            value = value_match.group("quoted")
            quoted = value is not None
            if value is None:
                value = value_match.group("bare")
        elif text[value_start:].startswith(("\"", "'")):
            # An unterminated quote is still a nonempty supplied value, not
            # an empty credential field; errors never echo this value.
            value = text[value_start:]
        errors.update(assignment_errors(key, value, quoted=quoted))
        # Retain sensitive prefix delimiters inside ambiguous quoted/unquoted
        # keys too; full metadata or an explicit nonempty policy takes precedence.
        kind = complete_assignment_kind(key)
        whole_typed_key = kind in {"boolean", "count", "sha256"} or (kind == "policy" and value not in (None, ""))
        if colon and not whole_typed_key:
            # Internal colon assignments always have a nonempty colon-bearing
            # value, which cannot be a typed boolean/count/SHA-256. Keep only
            # the finite alias-sized word tail at each delimiter, rather than
            # re-normalizing every growing prefix and its remaining value.
            limit = KEY_CONTRACT_LIMIT
            window: list[str] = []
            length = 0
            for part in key.split(":")[:-1]:
                for word in key_words(part):
                    window.append(word)
                    length += len(word)
                    while length > limit:
                        length -= len(window.pop(0))
                errors.update(assignment_errors(" ".join(window), ":"))
    return errors


def lexical_assignments(text: str):
    """Lex non-overlapping complete tokens, retaining ambiguous colon values.

    A complete typed '=' key suppresses only its own internal colons. Otherwise
    keep the rightmost colon delimiter and let the bounded prefix classifier
    retain earlier credential delimiters. Quoted keys can have an external
    colon delimiter or expose an ambiguous colon inside their quoted token.
    """
    # The lexer consumes at most three prose words per token. Keep only the
    # finite alias-sized tail across adjacent whitespace-separated chunks so
    # an alias such as 'api key' cannot be split by benign leading copy.
    # 保留有限词尾，防止前面的普通文案把凭据字段名拆散后漏检。
    carry: list[str] = []
    previous_end = 0

    def contextual_key(raw_key: str) -> str:
        if not carry or raw_key.startswith(("\"", "'")):
            return raw_key
        # Only an alias crossing the first colon needs earlier words. Later
        # colons remain part of this same token and its original value span.
        candidate = "".join(key_words(raw_key.split(":", 1)[0]))
        selected = None
        for start in range(len(carry) - 1, -1, -1):
            candidate = carry[start] + candidate
            if len(candidate) > KEY_CONTRACT_LIMIT:
                break
            if (normalized_metadata_kind(candidate) is not None
                    or sensitive_normalized_key(candidate)
                    or (candidate.endswith("policy") and sensitive_normalized_key(candidate[:-6]))):
                selected = start
        return raw_key if selected is None else " ".join(carry[selected:]) + " " + raw_key

    for match in ASSIGNMENT_TOKEN_RE.finditer(text):
        raw_key = match.group("key")
        gap = text[previous_end:match.start()]
        if not gap or not gap.isspace() or raw_key.startswith(("\"", "'")):
            carry = []
        previous_end = match.end()
        end = match.end()
        while end < len(text) and text[end] in " \t":
            end += 1
        external = text[end:end + 1]
        if external == "=":
            key = contextual_key(raw_key)
            yield key, end + 1, False
            if complete_assignment_kind(key.strip().strip("\"'")) is not None:
                carry = []
                continue
        if external == ":":
            yield contextual_key(raw_key), end + 1, True
            carry = []
            continue
        # The un-delimited token includes internal colons because ':' is also
        # a valid namespace separator. Do not search again at their suffixes.
        quoted_key = raw_key.startswith(("\"", "'"))
        content = raw_key[1:-1] if quoted_key else raw_key
        colon = content.rfind(":")
        if colon >= 0:
            key = content[:colon] if quoted_key else contextual_key(content[:colon])
            yield key, match.start() + int(quoted_key) + colon + 1, True
        if external == "=" or colon >= 0 or quoted_key:
            carry = []
        else:
            carry = (carry + key_words(raw_key))[-KEY_CONTRACT_LIMIT:]
            length = sum(map(len, carry))
            while carry and length > KEY_CONTRACT_LIMIT:
                length -= len(carry.pop(0))


def email_address_present(text: str) -> bool:
    """Consume each maximal local-part candidate once before anchored matching.

    A dotted namespace without '@' never enters the greedy email grammar. The
    first legacy word-boundary start owns the candidate; retrying later dotted
    suffixes cannot change its shared domain's validity.
    """
    for candidate in EMAIL_CANDIDATE_RE.finditer(text):
        if text[candidate.end():candidate.end() + 1] != "@":
            continue
        start = EMAIL_START_RE.search(text, candidate.start(), candidate.end())
        if start is not None and EMAIL_RE.match(text, start.start()) is not None:
            return True
    return False


def uri_scheme_spans(text: str):
    """Consume maximal scheme-character runs; never retry dotted suffixes."""
    for candidate in URI_SCHEME_CANDIDATE_RE.finditer(text):
        if text[candidate.end():candidate.end() + 3] != "://":
            continue
        start = URI_SCHEME_START_RE.search(text, candidate.start(), candidate.end())
        if start is not None:
            yield start.start(), candidate.end() + 3


def url_text_spans(text: str):
    """Keep genuine URL quote characters, trim only a surrounding enclosure.

    Escaped quotes and quoted assignment-value openers are URI content. The
    independent raw/intermediate authority scan remains unchanged.
    """
    position = 0
    for start, scheme_end in uri_scheme_spans(text):
        if start < position:
            continue
        enclosure = URL_ENCLOSURES.get(text[start - 1]) if start else None
        end = scheme_end
        inner_quote = False
        while end < len(text) and not (text[end].isspace() or text[end] in "<>"):
            if enclosure is not None and text[end] == enclosure:
                backslash = end - 1
                while backslash >= start and text[backslash] == "\\":
                    backslash -= 1
                escaped = (end - 1 - backslash) % 2 == 1
                if not escaped:
                    if inner_quote:
                        inner_quote = False
                    elif (enclosure in "\"'" and text[end - 1] in "=:"
                          and end + 1 < len(text) and not (text[end + 1].isspace() or text[end + 1] in "<>")):
                        inner_quote = True
                    else:
                        break
            end += 1
        yield start, end
        position = max(end, start + 1)


def decoded_query_pairs(query: str) -> list[tuple[str, str]]:
    """Form decoding once: '+' is space, whereas '%2B' stays literal '+' ."""
    return [(decoded_text(key), decoded_text(value))
            for key, value in parse_qsl(query, keep_blank_values=True, errors="strict")]


def text_assignment_errors(text: str) -> set[str]:
    """Separate raw URL form-query grammar from decoded prose assignments."""
    errors: set[str] = set()
    prose_parts: list[str] = []
    previous = 0
    for start, end in url_text_spans(text):
        prose_parts.extend((text[previous:start], "URL_VALUE"))
        previous = end
        try:
            parsed = urlsplit(html.unescape(text[start:end]))
            errors.update(plain_assignment_errors(decoded_text(parsed.path)))
            for key, value in decoded_query_pairs(parsed.query):
                errors.update(assignment_errors(key, value))
                # Every decoded key may itself carry an embedded assignment,
                # including credential-looking flags with no supplied value.
                # Only a full, correctly typed metadata contract or a nonempty
                # explicit policy key/value can keep its internal colons whole.
                kind = complete_assignment_kind(key)
                whole_metadata = safe_metadata_value(kind, parsed_metadata_value(kind, value))
                whole_policy = kind == "policy" and bool(value)
                if "=" in key or not (whole_metadata or whole_policy):
                    errors.update(plain_assignment_errors(key))
                errors.update(plain_assignment_errors(value))
            errors.update(plain_assignment_errors(decoded_text(parsed.fragment)))
        except (ValueError, UnicodeError):
            errors.add("URL query parsing failed")
    prose_parts.append(text[previous:])
    prose = "".join(prose_parts)
    errors.update(plain_assignment_errors(decoded_text(prose)))
    return errors


def url_userinfo_errors(text: str) -> set[str]:
    """Independently inspect URI authorities; never reparse decoded queries."""
    errors: set[str] = set()
    # Preserve raw lexical boundaries before decoding. Percent/HTML escapes in
    # an authority can introduce whitespace or '<'/'>'; re-tokenizing the final
    # decoded string would cut off '@' and silently discard supplied userinfo.
    for token in re.findall(r"[^\s<>]+", text):
        representations = [token]
        for _ in range(2):
            decoded = html.unescape(unquote(representations[-1]))
            if decoded == representations[-1]:
                break
            representations.append(decoded)
        for representation in representations:
            # Extract starts, retaining the whole original token's decoded tail.
            # Inspect raw and every intermediate stage, not just the final one.
            absolute = list(uri_scheme_spans(representation))
            spans = [(start, len(representation), representation[start:])
                     for start, _ in absolute]
            candidates = [span[2] for span in spans]
            for match in re.finditer(r"(?=//)", representation):
                start, uri = match.start(), representation[match.start():]
                # A network-path reference starts a token; double slashes in a
                # path or word are not authorities. Keep nested query/fragment
                # references, using the innermost containing URI as context.
                if start:
                    preceding = representation[start - 1]
                    # Text enclosures/field separators and URI query/fragment
                    # markers are reference boundaries, even when decoding
                    # introduced them. A slash or word/path continuation is not.
                    text_boundary = (preceding.isspace() or preceding in "\"'`([{<>:;,="
                                     or preceding in URL_ENCLOSURE_BOUNDARIES)
                    component_boundary = preceding in "?#&"
                    if not (text_boundary or component_boundary):
                        continue
                containers = [span for span in spans if span[0] < start < span[1]]
                if containers:
                    parent_start, _, parent_uri = max(containers, key=lambda span: span[0])
                    delimiters = [index for index in (parent_uri.find("?"), parent_uri.find("#")) if index >= 0]
                    if not delimiters or start < parent_start + min(delimiters):
                        continue
                candidates.append(uri)
                spans.append((start, len(representation), uri))
            # Authority checks do not reinterpret the raw form-query grammar.
            for uri in candidates:
                try:
                    parsed = urlsplit(uri)
                    if parsed.username is not None or parsed.password is not None:
                        errors.add("plan must not contain URL userinfo credentials")
                except (ValueError, UnicodeError):
                    errors.add("URL credential parsing failed")
    return errors


def privacy_errors(plan: Any) -> list[str]:
    errors: set[str] = set()
    # Preserve a dictionary's correctly typed whole-key contract when its key
    # is checked as text. A detached key has no supplied prose-assignment value.
    # 检查字典键时保留其真实值类型，合法布尔/计数元数据不能被拆键误报。
    pending = [(plan, False)]
    while pending:
        value, bound_metadata_key = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str):
                    suffixes = key_suffixes(key)
                    credential_key = any(sensitive_normalized_key(candidate) for candidate in suffixes)
                    metadata_kind = next((kind for candidate in suffixes
                                          if (kind := normalized_metadata_kind(candidate)) is not None), None)
                    safe_metadata = safe_metadata_value(metadata_kind, child)
                    whole_kind = complete_assignment_kind(key) if safe_metadata else None
                    bound_key = safe_metadata_value(whole_kind, child)
                    pending.append((key, bound_key))
                    # Empty values and booleans carry no contact/credential data.
                    if credential_key and child not in (None, "") and not isinstance(child, bool):
                        errors.add("plan must not contain raw contact or credential fields")
                    if metadata_kind is not None and not safe_metadata:
                        errors.add("contact or credential metadata must be a boolean, count or SHA-256 digest")
                    if not safe_metadata:
                        pending.append((child, False))
                else:
                    pending.append((child, False))
        elif isinstance(value, list):
            pending.extend((child, False) for child in value)
        elif isinstance(value, str):
            text = decoded_text(value)
            if email_address_present(text):
                errors.add("plan must not contain recipient or staff email addresses")
            errors.update(url_userinfo_errors(value))
            if not bound_metadata_key:
                errors.update(text_assignment_errors(value))
            if BEARER_RE.search(text) or JWT_RE.search(text):
                errors.add("plan must not contain credential values")
    # Error messages intentionally omit values, keys and paths that could be sensitive.
    # 错误消息不回显可能含敏感信息的值、字段名或路径。
    return sorted(errors)


def valid_cta_url(value: Any) -> bool:
    """Parse locally; HTTPS prefix alone is not a usable, credential-free URL."""
    if not isinstance(value, str) or not value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if parsed.scheme.lower() != "https" or not host or parsed.username is not None or parsed.password is not None:
            return False
        if port is not None and not (1 <= port <= 65535):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            ascii_host = host.rstrip(".").encode("idna").decode("ascii")
            if len(ascii_host) > 253 or not all(HOST_LABEL_RE.fullmatch(label) for label in ascii_host.split(".")):
                return False
        return True
    except (ValueError, UnicodeError):
        return False


def json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate(plan: dict[str, Any], max_recipients: int) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(plan, dict):
        return ["root must be an object"], warnings
    if not isinstance(max_recipients, int) or isinstance(max_recipients, bool) or max_recipients < 1:
        errors.append("max_recipients must be a positive integer")
        max_recipients = 0
    errors.extend(privacy_errors(plan))

    required = {
        "campaign_name",
        "segment",
        "channel",
        "channel_ready",
        "subject",
        "body_text",
        "claims_reviewed",
        "recipient_data_included",
        "final_send_allowed",
    }
    unknown = set(plan) - (required | {"cta", "schedule", "notes"})
    missing = required - set(plan)
    if missing:
        errors.append(f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        errors.append("unknown fields are not allowed")

    name = plan.get("campaign_name")
    if not isinstance(name, str) or not (4 <= len(name.strip()) <= 80):
        errors.append("campaign_name must be 4-80 characters")

    segment = plan.get("segment")
    if not isinstance(segment, dict):
        errors.append("segment must be an object")
    else:
        if set(segment) != {"source", "name", "live_count"}:
            errors.append("segment must contain exactly source, name, live_count")
        if segment.get("source") != "ALIBABA_SYSTEM_GROUP":
            errors.append("segment source must be ALIBABA_SYSTEM_GROUP")
        if not isinstance(segment.get("name"), str) or segment["name"] not in ALLOWED_SEGMENTS:
            errors.append("segment name is not an approved system audience")
        count = segment.get("live_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            errors.append("segment live_count must be a positive integer")
        elif count > max_recipients:
            errors.append(f"segment live_count exceeds canary maximum {max_recipients}")

    if not isinstance(plan.get("channel"), str) or plan["channel"] not in ALLOWED_CHANNELS:
        errors.append("channel must be ALIYUN_DIRECTMAIL or OKKI_EDM")
    if plan.get("channel_ready") is not True:
        errors.append("channel_ready must be true after live UI readback")

    subject = plan.get("subject")
    if not isinstance(subject, str) or not (6 <= len(subject.strip()) <= 120):
        errors.append("subject must be 6-120 characters")

    body = plan.get("body_text")
    if not isinstance(body, str) or not (40 <= len(body.strip()) <= 5000):
        errors.append("body_text must be 40-5000 characters")

    combined = "\n".join(value for value in (subject, body) if isinstance(value, str))
    if STRONG_CLAIM_RE.search(combined) and plan.get("claims_reviewed") is not True:
        errors.append("strong marketing claims require claims_reviewed=true")
    if plan.get("claims_reviewed") is not True:
        errors.append("claims_reviewed must be true")
    if plan.get("recipient_data_included") is not False:
        errors.append("recipient_data_included must be false")
    if plan.get("final_send_allowed") is not False:
        errors.append("final_send_allowed must be false")

    schedule = plan.get("schedule", "IMMEDIATE")
    if schedule != "IMMEDIATE":
        errors.append("light canary plan supports only IMMEDIATE review mode; scheduling requires separate approval")

    cta = plan.get("cta")
    if cta is not None:
        if not isinstance(cta, dict) or set(cta) != {"text", "url"}:
            errors.append("cta must contain exactly text and url")
        else:
            if not isinstance(cta.get("text"), str) or not cta["text"].strip():
                errors.append("cta text is required")
            if not valid_cta_url(cta.get("url")):
                errors.append("cta url must use HTTPS with a valid hostname and no credentials")
    else:
        warnings.append("no CTA supplied")

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--max-recipients", type=int, default=20)
    args = parser.parse_args()

    if args.receipt.exists():
        print("receipt already exists; refusing overwrite", file=sys.stderr)
        return 2

    try:
        raw = args.input.read_bytes()
        plan = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_json_constant, object_pairs_hook=json_object)
        if not isinstance(plan, dict):
            raise ValueError("root must be an object")
        errors, warnings = validate(plan, args.max_recipients)
        plan_hash = hashlib.sha256(canonical_json(plan)).hexdigest()
    except Exception as exc:
        receipt = {
            "schema_version": "sellerkit.alibaba.edm_plan_receipt.v1",
            "status": "BLOCKED_INVALID_INPUT",
            "errors": [f"invalid input or validation failure ({type(exc).__name__})"],
            "authority": "LOCAL_PLAN_VALIDATION_ONLY",
            "final_send_allowed": False,
        }
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_bytes(json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        return 3

    receipt = {
        "schema_version": "sellerkit.alibaba.edm_plan_receipt.v1",
        "status": "PASS_EDM_DRAFT_PLAN" if not errors else "BLOCKED_EDM_DRAFT_PLAN",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "plan_sha256": plan_hash,
        "max_recipients": args.max_recipients,
        "errors": errors,
        "warnings": warnings,
        "authority": "LOCAL_PLAN_VALIDATION_ONLY",
        "final_send_allowed": False,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_bytes(json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if not errors else 3


if __name__ == "__main__":
    raise SystemExit(main())
