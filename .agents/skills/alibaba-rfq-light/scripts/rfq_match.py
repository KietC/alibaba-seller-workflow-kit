#!/usr/bin/env python3
# Match evidence supports a reviewed quotation, never an automatic submission.
# 匹配证据仅支持人工审核报价，不能自动提交。
"""Create a small, evidence-labelled SellerKit shortlist for one Alibaba RFQ."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


STOPWORDS = {
    "and", "the", "for", "with", "this", "that", "from", "into", "item",
    "product", "looking", "supplier", "reliable", "good", "price", "style",
    "design", "modern", "usage", "application", "customization", "welcomed",
}
HISTORICAL_REFERENCE_DISPOSITION = "HISTORICAL_REFERENCE_ONLY"
CONCEPT_PATTERNS = {
    "HAND_WASH_SINK": r"hand\s*(?:wash(?:ing)?|washington)\s*(?:sink|basin)|wash\s*sink",
    "FOOT_OPERATED": r"foot(?:ball)?[-\s]*(?:pedal[-\s]*)?operated|foot\s*pedal",
    "KNEE_OPERATED": r"knee[-\s]*operated",
    "HANDS_FREE": r"hands?[-\s]*free|touchless|non[-\s]*contact",
    "SUS304": r"sus\s*304|304\s*stainless|stainless\s*steel\s*304",
    "SUS201": r"sus\s*201|201\s*stainless|stainless\s*steel\s*201",
    "WALL_MOUNT": r"wall[-\s]*mount",
    "FLOOR_STAND": r"floor[-\s]*(?:standing|stand|mount)|free[-\s]*standing|freestanding",
    "DECK_MOUNT": r"deck[-\s]*mount|counter[-\s]*(?:top|mount)",
    "SINGLE_BOWL": r"\bsingle[-\s]*(?:bowls?|basins?)\b|\b(?:one|1)(?!\d)\s*[-\s]?(?:bowls?|basins?)\b",
    "DOUBLE_BOWL": r"\bdouble[-\s]*(?:bowls?|basins?)\b|\b(?:two|2)(?!\d)\s*[-\s]?(?:bowls?|basins?)\b",
    "RECTANGULAR": r"rectang(?:le|ular)|square",
    "FAUCET": r"faucet|tap|spout",
    "SOAP_DISPENSER": r"soap\s*dispenser",
    "WASTE_BIN": r"waste\s*bin|dust\s*bin",
    "TOWEL_BOX": r"towel\s*box|dowel\s*box|paper[-\s]*towel",
    "HOSPITAL": r"hospital|medical|healthcare",
    "COMMERCIAL_KITCHEN": r"commercial\s*kitchen|restaurant|canteen|catering",
}
NEGATION_RE = re.compile(r"\b(?:not|no|without|never|avoid|reject|exclude|excluding|except|unacceptable|unsuitable|unwanted|disallowed|excluded|prohibited|forbidden|don't|doesn't|isn't|aren't|wasn't|weren't|shouldn't|mustn't|won't|cannot|can't)\b", re.I)
CUSTOM_SCOPES = {"MATERIAL", "ACTUATION", "INSTALLATION", "DIMENSIONS", "BOWL_COUNT"}
SCOPE_PATTERNS = {
    "MATERIAL": r"\b(?:material|grade|steel|sus\s*\d+)\b",
    "ACTUATION": r"\b(?:actuation|operation|operated|pedal|knee|foot)\b",
    "INSTALLATION": r"\b(?:installation|install|mount|mounting|mounted)\b",
    "DIMENSIONS": r"\b(?:dimensions?|sizes?|sized|width|height|depth)\b",
    "BOWL_COUNT": r"\b(?:bowls?|basins?)\b",
}
DIMENSION_PATTERN = (
    r"(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)"
    r"(?:\s*[x×*]\s*(\d+(?:\.\d+)?))?\s*(mm|cm)?"
)


def known_shared_actuation_conjunctions(value: str) -> list[tuple[re.Match[str], list[tuple[int, int, str]]]]:
    """Consume only two different known actuations with one shared suffix."""
    matches = []
    for match in re.finditer(r"\b(?P<left>foot|knee)(?:\s+and\s+|\s*&\s*)(?P<right>foot|knee)[-\s]*operated\b", value, flags=re.I):
        if match.group("left").lower() == match.group("right").lower():
            continue
        matches.append((match, [
            (match.start("left"), match.end("left"), match.group("left").upper() + "_OPERATED"),
            (match.start("right"), match.end(), match.group("right").upper() + "_OPERATED"),
        ]))
    return matches


def canonical_model(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def text_blob(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "name", "category", "subcategory", "description_text", "details_text",
        "short_description_text", "materials",
    ):
        value = record.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    for attr in record.get("attributes") or []:
        if isinstance(attr, dict):
            parts.extend((str(attr.get("name") or ""), str(attr.get("raw_value") or "")))
    return " ".join(parts)


def structured_blob(record: dict[str, Any]) -> str:
    parts = [
        str(record.get("name") or ""),
        str(record.get("category") or ""),
        str(record.get("subcategory") or ""),
    ]
    for attr in record.get("attributes") or []:
        if isinstance(attr, dict):
            parts.extend((str(attr.get("name") or ""), str(attr.get("raw_value") or "")))
    return " ".join(parts)


def customization_allowed(text: str) -> bool:
    """Recognize explicit buyer permission, not a bare customization keyword."""
    value = text.lower()
    customization = r"(?:customi[sz](?:ation|e|ed|ing)|custom[-\s]*(?:made|sizes?|sized|dimensions?))"
    negative_patterns = (
        rf"\b(?:no|not|without|cannot|can't|do\s+not|don't)\b[^.!?;\n]{{0,45}}\b{customization}\b",
        rf"\b{customization}\b[^.!?;\n]{{0,25}}\b(?:not|never|prohibited|disallowed|unavailable)\b",
    )
    if any(re.search(pattern, value) for pattern in negative_patterns):
        return False
    positive_patterns = (
        rf"\b{customization}\b\s*(?:(?:is|are)\s+)?(?:welcome(?:d)?|accept(?:ed|able)?|allowed|permitted|supported|required|requested|needed)\b",
        rf"\b(?:welcome|accept|allow|permit|support|require|request|need)\s+(?:the\s+)?{customization}\b",
        r"\bcustom[-\s]*(?:made|sizes?|sized|dimensions?)\b",
        r"\bcustomi[sz]ed\s+(?:design|dimensions?|size|product|sink|basin|faucet)\b",
    )
    return any(re.search(pattern, value) for pattern in positive_patterns)


def explicit_requirement_branch(value: str) -> bool:
    """Recognize a finite known-fact requirement, not approval conditions."""
    if not re.search(r"\b(?:require(?:d|s)?|mandatory|must|shall|need(?:s|ed)?)\b", value):
        return False
    if not any(re.search(pattern, value) for pattern in SCOPE_PATTERNS.values()):
        return False
    if unrecognized_hard_value_scopes(value.lower()):
        return False
    known_words = {
        "we", "buyer", "the", "is", "are", "be", "and", "only", "for", "on",
        "require", "required", "requires", "mandatory", "must", "shall",
        "need", "needs", "needed", "remain", "stay", "foot", "knee", "pedal",
        "operated", "actuation", "operation", "material", "grade", "sus",
        "stainless", "steel", "wall", "mount", "mounted", "mounting", "install",
        "installation", "floor", "stand", "standing", "free", "freestanding",
        "deck", "counter", "top", "dimension", "dimensions", "size", "sizes",
        "width", "height", "depth", "bowl", "bowls", "basin", "basins", "count",
        "type", "single", "double", "one", "two", "mm", "cm", "inch", "inches",
    }
    words_only = re.sub(DIMENSION_PATTERN, " ", value.lower(), flags=re.I)
    return set(re.findall(r"[a-z]+", words_only)) <= known_words


def split_permission_requirement(value: str) -> tuple[str, str]:
    """Separate only a complete, known requirement from permission text."""
    boundaries = re.finditer(
        r"(?:,\s*|(?:^|\s+)and\s+|\s*&\s*)(?=[^,]*\b(?:require(?:d|s)?|mandatory|must|shall|need(?:s|ed)?)\b)",
        value,
    )
    for boundary in boundaries:
        if explicit_requirement_branch(value[boundary.end():]):
            return value[:boundary.start()], value[boundary.end():]
    return value, ""


def parse_permission_scope_list(value: str) -> set[str] | None:
    """Accept a finite known scope list; unknown qualifications are not global."""
    value = re.sub(r"^\s*(?:only\s+)?(?:(?:for|on)\s+)?(?:the\s+)?", "", value).strip()
    value = re.sub(r"\s+only\s*$", "", value).strip()
    spans = sorted(
        (match.start(), match.end(), scope)
        for scope, pattern in SCOPE_PATTERNS.items()
        for match in re.finditer(pattern, value)
    )
    if not spans:
        return None
    scopes: set[str] = set()
    previous_end, previous_scope = 0, None
    for start, end, scope in spans:
        between = value[previous_end:start]
        modifier_free = re.sub(r"\b(?:count|type|changes?|modifications?)\b", "", between).strip()
        if previous_scope is None:
            if modifier_free:
                return None
        elif previous_scope != scope:
            if modifier_free not in {"and", ",", "&"}:
                return None
        elif re.search(r"[^\s,&-]|\b(?:or|only)\b", re.sub(r"\band\b", "", modifier_free)):
            return None
        scopes.add(scope)
        previous_end, previous_scope = end, scope
    remainder = re.sub(r"\b(?:count|type|changes?|modifications?)\b", "", value[previous_end:]).strip()
    return scopes if not remainder else None


def customization_exception_scopes(value: str) -> set[str] | None:
    """Consume the complete exception tail, preserving unknown conditions."""
    subject, _ = split_permission_requirement(value.strip())
    return parse_permission_scope_list(subject.strip())


def product_fact_prefix(value: str) -> bool:
    """Permit only a complete finite product-fact preface, never qualifiers."""
    if not re.search(CONCEPT_PATTERNS["HAND_WASH_SINK"], value):
        return False
    if NEGATION_RE.search(value) or re.search(
        r"\b(?:only|dimensions?|sizes?|width|height|depth|actuation|operation|installation|install|material|grade|count|type|changes?|modifications?)\b",
        value,
    ):
        return False
    facts = re.sub(CONCEPT_PATTERNS["HAND_WASH_SINK"], "", value)
    return explicit_requirement_branch(facts + " required")


def customization_prefix_scope(prefix: str) -> tuple[set[str] | None, bool]:
    """Consume a whole prefix subject; None alone means a known generic preface."""
    value = re.sub(r"[,:]\s*$", "", prefix.strip())
    if not value:
        return None, False
    scopes = parse_permission_scope_list(value)
    if scopes is not None:
        return scopes, bool(re.search(r"\bonly\b", value))
    # An earlier product-fact branch can be separated only after it is fully
    # recognized. Never skip an arbitrary prefix or take just its last scope.
    for delimiter in re.finditer(",", value):
        if product_fact_prefix(value[:delimiter.start()]):
            subject = value[delimiter.end():].strip()
            scopes = parse_permission_scope_list(subject)
            if scopes is not None:
                return scopes, bool(re.search(r"\bonly\b", subject))
    generic_words = {"any", "all", "full", "general", "product", "accept", "allow", "permit", "support", "require", "request", "need", "welcome"}
    words = re.findall(r"[a-z]+", value)
    if (words and set(words) <= generic_words and re.fullmatch(r"[a-z\s]+", value)) or product_fact_prefix(value):
        return None, False
    # This includes a scope/only mixed with any unconsumed approval condition.
    return set(), True


def customization_permission(clause: str, custom: re.Match[str]) -> tuple[set[str], bool]:
    """Parse one permission's fully consumed subject and qualification."""
    prefix_scopes, prefix_only = customization_prefix_scope(clause[:custom.start()])
    if prefix_scopes == set():
        return set(), True

    tail_parts = re.split(r"\bexcept\b", clause[custom.end():], maxsplit=1)
    if len(tail_parts) == 2 and customization_exception_scopes(tail_parts[1]) is None:
        return set(), True
    tail = tail_parts[0]
    tail = re.sub(
        r"^\s*(?:(?:is|are)\s+)?(?:welcome(?:d)?|accept(?:ed|able)?|allowed|permitted|supported|required|requested|needed)\b",
        "", tail,
    ).strip()
    tail, _ = split_permission_requirement(tail)
    only = prefix_only or bool(re.search(r"\bonly\b", tail))
    own_scopes = {scope for scope, pattern in SCOPE_PATTERNS.items() if re.search(pattern, custom.group())}
    if re.fullmatch(r"(?:design|product|sink|basin|faucet)(?:\s+(?:required|requested|needed))?", tail):
        tail = ""
    if tail.strip():
        suffix_scopes = parse_permission_scope_list(tail)
        if suffix_scopes is None or (prefix_scopes and prefix_scopes != suffix_scopes) or (own_scopes and own_scopes != suffix_scopes):
            return set(), True
        return suffix_scopes, only
    if prefix_scopes is not None:
        return prefix_scopes, only
    return own_scopes or set(CUSTOM_SCOPES), only


def independent_rfq_clause(clause: str) -> bool:
    """Fully consume finite facts/restrictions or unrelated RFQ requests."""
    if explicit_requirement_branch(clause) or product_fact_prefix(clause):
        return True
    restriction = re.fullmatch(r"(?:except|(?:no|not)\s+changes?\s+(?:to|in))\s+(.+)", clause)
    if restriction and parse_permission_scope_list(restriction.group(1)) is not None:
        return True
    restriction = re.fullmatch(r"(.+)\s+cannot\s+(?:be\s+)?changed", clause)
    if restriction and parse_permission_scope_list(restriction.group(1)) is not None:
        return True
    unrelated_requests = (
        r"(?:please\s+)?(?:send|share|provide)\s+(?:(?:the|your)\s+)?(?:catalog(?:ue)?|product\s+(?:details|specifications|specs)|technical\s+specifications)",
        r"(?:please\s+)?(?:quote|send|share|provide)\s+(?:your\s+)?(?:best\s+)?(?:price|prices|quotation)",
        r"thank(?:s|\s+you)(?:\s+for\s+your\s+(?:help|reply|time))?",
        r"we\s+(?:need|require|are\s+looking\s+for)\s+(?:a\s+)?(?:reliable\s+)?suppliers?",
        r"(?:please\s+)?(?:send|share|provide)\s+(?:your\s+)?lead\s+time",
        r"please\s+(?:contact|reply\s+to)\s+us",
    )
    if any(re.fullmatch(pattern, clause) for pattern in unrelated_requests):
        return True
    value = re.sub(r"[_/]+", " ", clause)
    value, count = re.subn(DIMENSION_PATTERN, " ", value, flags=re.I)
    fact_seen = count > 0
    value, count = re.subn(r"stainless\s*steel\s*201\s+304", " ", value)
    fact_seen |= count > 0
    for match, _ in reversed(known_shared_actuation_conjunctions(value)):
        value = value[:match.start()] + " " + value[match.end():]
        fact_seen = True
    for pattern in CONCEPT_PATTERNS.values():
        value, count = re.subn(pattern, " ", value, flags=re.I)
        fact_seen |= count > 0
    known_words = {
        "we", "need", "needs", "needed", "require", "requires", "required",
        "must", "shall", "the", "a", "an", "and", "with", "for", "of",
        "to", "in", "on", "is", "are", "be", "remain", "stay", "only",
        "not", "no", "without", "never", "avoid", "reject", "exclude",
        "excluding", "unacceptable", "unsuitable", "unwanted", "disallowed",
        "excluded", "prohibited", "forbidden", "acceptable", "accepted",
        "allowed", "permitted", "material", "grade", "stainless", "steel",
        "actuation", "operation", "installation", "mounting", "dimension",
        "dimensions", "size", "sizes", "width", "height", "depth", "shape",
        "bowl", "bowls", "basin", "basins", "count", "type", "sink",
        "sinks", "faucet", "product", "commercial", "use", "application", "or", "both",
    }
    return fact_seen and set(re.findall(r"[a-z]+", value)) <= known_words and re.fullmatch(r"[a-z\s,:&()\-]*", value) is not None


def unresolved_customization_context(clauses: list[str]) -> bool:
    """Retain pending qualifications before/after permissions across clauses."""
    permission_seen = False
    condition_pending = False
    for clause in clauses:
        if re.search(r"\bcustom(?:i[sz]\w*|[-\s]*(?:made|sizes?|sized|dimensions?))\b", clause) and customization_allowed(clause):
            if condition_pending:
                return True
            permission_seen = True
            continue
        # A known independent product fact/requirement is not a qualifier and
        # cannot silently fulfill a condition retained from another clause.
        if independent_rfq_clause(clause):
            continue
        # Unknown detached text is unresolved even when it uses no known
        # approval keyword. Never assume it is unrelated or already fulfilled.
        if permission_seen:
            return True
        condition_pending = True
    return False


def customization_scope_analysis(text: str) -> tuple[set[str], bool]:
    """Return permitted scopes and any unparsed permission qualification."""
    if not customization_allowed(text):
        return set(), False
    allowed: set[str] = set()
    limited: list[set[str]] = []
    forbidden: set[str] = set()
    clauses = [clause.strip() for clause in re.split(r"[;!?\n]|(?<!\d)\.|\.(?!\d)", text.lower()) if clause.strip()]
    unknown_qualification = unresolved_customization_context(clauses)
    if unknown_qualification:
        limited.append(set())
    for clause in clauses:
        custom = re.search(r"\bcustom(?:i[sz]\w*|[-\s]*(?:made|sizes?|sized|dimensions?))\b", clause)
        if custom and customization_allowed(clause):
            scopes, exclusive_or_unknown = customization_permission(clause, custom)
            allowed.update(scopes)
            if exclusive_or_unknown:
                limited.append(scopes)
                unknown_qualification |= not scopes
        _, following_requirement = split_permission_requirement(clause)
        restriction = following_requirement or clause
        if re.search(r"\b(?:must|shall)\s+(?:remain|stay)|\bcannot\s+(?:be\s+)?chang|\b(?:no|not)\s+(?:changes?|chang(?:e|ing))\b", restriction):
            forbidden.update(scope for scope, pattern in SCOPE_PATTERNS.items() if re.search(pattern, restriction))
        exception = re.search(r"\bexcept\b(.*)", clause)
        if exception:
            exception_scopes = customization_exception_scopes(exception.group(1))
            if exception_scopes is None:
                unknown_qualification = True
            else:
                forbidden.update(exception_scopes)
    for restriction in limited:
        allowed.intersection_update(restriction)
    return allowed - forbidden, unknown_qualification


def customization_scopes(text: str) -> set[str]:
    """Conservative English permission subjects, not general semantic parsing."""
    return customization_scope_analysis(text)[0]


def requirement_concepts(text: str) -> tuple[set[str], set[str], bool]:
    """Separate a few explicit English exclusions; unresolved negation stops filling."""
    value = re.sub(r"[_/]+", " ", text.lower())
    positive: set[str] = set()
    excluded: set[str] = set()
    covered: list[tuple[int, int]] = []
    ambiguous_polarity = False

    def classify(names: set[str], match: re.Match[str]) -> None:
        nonlocal ambiguous_polarity
        before = value[:match.start()]
        after = value[match.end():]
        prefix = re.split(r"[.,;!?\n]", before)[-1]
        suffix = re.split(r"[.,;!?\n]", after)[0]
        prefix_negative = re.search(
            r"\b(?:not|no|without|never|avoid|reject|exclude|excluding|except|don't|cannot|can't)\s+"
            r"(?:(?:want|accept|allow|permit|use|a|an|the|any|being|be)\s+){0,3}$", prefix,
        )
        suffix_negative = re.match(
            r"\s*(?:(?:is|are)\s+)?(?:not\s+(?:acceptable|accepted|allowed|permitted|wanted)|"
            r"unacceptable|unsuitable|unwanted|disallowed|prohibited|forbidden|excluded)\b", suffix,
        )
        if prefix_negative or suffix_negative:
            excluded.update(names)
            start = match.start() - len(prefix) + prefix_negative.start() if prefix_negative else match.start()
            end = match.end() + suffix_negative.end() if suffix_negative else match.end()
            covered.append((start, end))
        elif NEGATION_RE.search(prefix[-60:]) or NEGATION_RE.search(suffix[:25]):
            # Do not promote an ambiguous negated phrase into a required fact.
            ambiguous_polarity = True
            return
        else:
            positive.update(names)

    for name, pattern in CONCEPT_PATTERNS.items():
        for match in re.finditer(pattern, value, flags=re.I):
            classify({name}, match)
    for match in re.finditer(r"stainless\s*steel\s*201\s+304", value, flags=re.I):
        classify({"SUS201", "SUS304"}, match)
    for match, atoms in known_shared_actuation_conjunctions(value):
        classify({name for _, _, name in atoms}, match)
    # These are known permission restrictions rather than negative product
    # requirements.  Any other unconsumed negation still requires review.
    policy_negations = (
        r"\b(?:no|not|without)\s+customi[sz]ation\b",
        r"\bcustomi[sz]ation\s+(?:is\s+)?not\s+(?:allowed|permitted|accepted|supported|required|needed)\b",
        r"\b(?:no|not)\s+changes?\s+(?:to|in)\s+(?:actuation|operation|installation|mounting|material|dimensions?|size|bowl\s+count)\b",
        r"\bexcept\s+(?:actuation|operation|installation|mounting|material|dimensions?|size|bowl\s+count)\b",
    )
    for pattern in policy_negations:
        covered.extend((match.start(), match.end()) for match in re.finditer(pattern, value))
    unresolved = ambiguous_polarity or any(not any(start <= cue.start() < end for start, end in covered) for cue in NEGATION_RE.finditer(value))
    return positive - excluded, excluded, unresolved


def concepts(text: str) -> set[str]:
    value = re.sub(r"[_/]+", " ", text.lower())
    found, _, _ = requirement_concepts(text)
    if customization_allowed(text):
        found.add("CUSTOM_ALLOWED")

    for token in re.findall(r"[a-z0-9]+", value):
        if len(token) >= 4 and token not in STOPWORDS:
            found.add(f"T:{token}")
    return found


HARD_CONCEPT_GROUPS = {
    "MATERIAL": {"SUS304", "SUS201"},
    "ACTUATION": {"FOOT_OPERATED", "KNEE_OPERATED"},
    "INSTALLATION": {"WALL_MOUNT", "FLOOR_STAND", "DECK_MOUNT"},
    "BOWL_COUNT": {"SINGLE_BOWL", "DOUBLE_BOWL"},
}


def unrecognized_hard_value_scopes(value: str) -> set[str]:
    """Detected values outside the finite vocabulary retain their actual field."""
    patterns = {
        "MATERIAL": r"\bsus\s*\d+\b|\b\d+\s+(?:grade|stainless)\b|\bstainless\s*steel\s*\d+\b",
        "ACTUATION": r"\b\w+[-\s]+operated\b",
        "INSTALLATION": r"\b\w+mount\w*\b",
        "BOWL_COUNT": r"\b(?!(?:hand|wash|washing|commercial|steel)\b)\w+[-\s]+(?:bowls?|basins?)\b",
    }
    unresolved = set()
    for scope, pattern in patterns.items():
        known = [(match.start(), match.end()) for name in HARD_CONCEPT_GROUPS[scope] for match in re.finditer(CONCEPT_PATTERNS[name], value)]
        if scope == "ACTUATION":
            known.extend((match.start(), match.end()) for match, _ in known_shared_actuation_conjunctions(value))
        for match in re.finditer(pattern, value):
            if not any(start <= match.start() and match.end() <= end for start, end in known):
                unresolved.add(scope)
    return unresolved


def unsupported_mandatory_scopes(clause: str) -> set[str]:
    """A separate mandatory clause cannot vanish because it has no known atom."""
    if not re.search(r"\b(?:require(?:d|s)?|mandatory|must|shall|need(?:s|ed)?)\b", clause):
        return set()
    custom = re.search(r"\bcustom(?:i[sz]\w*|[-\s]*(?:made|sizes?|sized|dimensions?))\b", clause)
    if custom and customization_allowed(clause) and customization_permission(clause, custom)[0]:
        _, requirement = split_permission_requirement(clause)
        return unsupported_mandatory_scopes(requirement) if requirement else set()
    unresolved = unrecognized_hard_value_scopes(clause)
    labels = {
        "MATERIAL": r"\b(?:material|grade)\b",
        "ACTUATION": r"\b(?:actuation|operation)\b",
        "INSTALLATION": r"\b(?:installation|mounting)\b",
        "BOWL_COUNT": r"\bbowl\s+count\b",
        "DIMENSIONS": SCOPE_PATTERNS["DIMENSIONS"],
    }
    for scope, pattern in labels.items():
        known = bool(re.search(DIMENSION_PATTERN, clause)) if scope == "DIMENSIONS" else any(re.search(CONCEPT_PATTERNS[name], clause) for name in HARD_CONCEPT_GROUPS[scope])
        if re.search(pattern, clause) and not known:
            unresolved.add(scope)
    return unresolved


def unconsumed_conjunction_scopes(clause: str) -> set[str]:
    """A recognized singleton must not erase another mandatory AND operand."""
    choices = list(re.finditer(r"\band\b|&", clause))
    if not choices:
        return set()
    # Scope-only permission lists are not product values. Keep an attached
    # requirement separately instead of turning the permission into one.
    custom = re.search(r"\bcustom(?:i[sz]\w*|[-\s]*(?:made|sizes?|sized|dimensions?))\b", clause)
    if custom and customization_allowed(clause) and customization_permission(clause, custom)[0]:
        _, requirement = split_permission_requirement(clause)
        return unconsumed_conjunction_scopes(requirement) if requirement else set()
    spans = [(match.start(), match.end(), scope, True) for scope, names in HARD_CONCEPT_GROUPS.items() for name in names for match in re.finditer(CONCEPT_PATTERNS[name], clause)]
    spans.extend((start, end, "ACTUATION", True) for _, atoms in known_shared_actuation_conjunctions(clause) for start, end, _ in atoms)
    spans.extend((match.start(), match.end(), "DIMENSIONS", True) for match in re.finditer(DIMENSION_PATTERN, clause))
    spans.extend((match.start(), match.end(), scope, False) for scope, pattern in SCOPE_PATTERNS.items() for match in re.finditer(pattern, clause))
    value_patterns = {
        "MATERIAL": r"\bsus\s*\d+\b|\b\d+\s+grade\b",
        "ACTUATION": r"\b\w+[-\s]+operated\b",
        "INSTALLATION": r"\b\w*mount\w*\b|\b(?:wall|deck|floor)\b",
        "BOWL_COUNT": r"\b\w+[-\s]+(?:bowls?|basins?)\b|\b(?:single|double|triple)\b",
    }
    spans.extend((match.start(), match.end(), scope, False) for scope, pattern in value_patterns.items() for match in re.finditer(pattern, clause))
    unresolved = set()
    for index, choice in enumerate(choices):
        previous = choices[index - 1].end() if index else 0
        following = choices[index + 1].start() if index + 1 < len(choices) else len(clause)
        left = [span for span in spans if previous <= span[0] and span[1] <= choice.start()]
        right = [span for span in spans if choice.end() <= span[0] and span[1] <= following]
        left_end = max((span[1] for span in left), default=previous)
        right_start = min((span[0] for span in right), default=following)
        left_scopes = {scope for _, end, scope, _ in left if end == left_end}
        right_scopes = {scope for start, _, scope, _ in right if start == right_start}
        # Known field labels may follow/precede their complete value. A bare
        # shared-suffix token is not a complete value and stays unresolved.
        left_known = {scope for _, end, scope, known in left if known and scope in left_scopes and option_operand_glue(clause[end:choice.start()])}
        right_known = {scope for start, _, scope, known in right if known and scope in right_scopes and option_operand_glue(clause[choice.end():start])}
        before, after = clause[previous:choice.start()].strip(), clause[choice.end():following].strip()
        if unrelated_packaging_options(before, after):
            continue
        if not left_known and not right_known:
            # Shared suffix has a complete right value but an unknown prefix
            # on the left; otherwise no recognized requested value is lost.
            if not any(known for *_, known in left + right) and not re.search(r"\b(?:require(?:d|s)?|mandatory|must|shall|need(?:s|ed)?|both)\b", clause):
                continue
        if not right_scopes and independent_rfq_clause(after):
            continue
        if not left_scopes and independent_rfq_clause(before):
            continue
        if left_scopes and right_scopes and left_scopes != right_scopes and left_known and right_known:
            continue
        if left_scopes == right_scopes and left_scopes and left_known and right_known:
            continue
        unresolved.update((left_scopes - left_known) | (right_scopes - right_known))
        if not left_scopes or not right_scopes:
            unresolved.update(left_scopes | right_scopes)
    return unresolved


def hard_requirement_groups(text: str, positive: set[str], candidate_facts: bool = False) -> tuple[dict[str, list[set[str]]], set[str]]:
    """Keep conjunctive clauses; only complete explicit OR lists are alternatives."""
    clauses = [part.strip() for part in re.split(r"[;!?\n]|(?<!\d)\.|\.(?!\d)", text.lower().replace("_", " ")) if part.strip()]
    groups: dict[str, list[set[str]]] = {}
    ambiguous: set[str] = set().union(*(unconsumed_conjunction_scopes(clause) | unsupported_mandatory_scopes(clause) for clause in clauses)) if not candidate_facts else set()
    for scope, names in HARD_CONCEPT_GROUPS.items():
        requested = names & positive
        groups[scope] = []
        relevant_clauses = []
        for clause in clauses:
            atoms: list[tuple[int, int, set[str]]] = []
            shared_actuation = known_shared_actuation_conjunctions(clause) if scope == "ACTUATION" else []
            atoms.extend((start, end, {name}) for _, values in shared_actuation for start, end, name in values if name in requested)
            if scope == "MATERIAL" and {"SUS201", "SUS304"} <= requested:
                atoms.extend((match.start(), match.end(), {"SUS201", "SUS304"}) for match in re.finditer(r"stainless\s*steel\s*201(?:\s*/\s*|\s+)304", clause))
            for name in requested:
                for match in re.finditer(CONCEPT_PATTERNS[name], clause, flags=re.I):
                    if not any(start <= match.start() and match.end() <= end for start, end, _ in atoms):
                        atoms.append((match.start(), match.end(), {name}))
            atoms.sort(key=lambda atom: (atom[0], atom[1]))
            if not atoms:
                continue
            relevant_clauses.append(clause)
            mentioned = set().union(*(atom[2] for atom in atoms))
            if candidate_facts and scope in fact_option_scopes("", clause):
                # One recognized half does not consume the full qualification.
                ambiguous.add(scope)
            if len(mentioned) == 1:
                groups[scope].append(mentioned)
                continue
            connectors = [clause[left[1]:right[0]].strip() for left, right in zip(atoms, atoms[1:])]
            descriptors = {
                "MATERIAL": r"(?:material|grade)", "ACTUATION": r"(?:actuation|operation)",
                "INSTALLATION": r"(?:installation|mounting)", "BOWL_COUNT": r"(?:count|type)",
            }
            connectors = [re.sub(rf"^(?:{descriptors[scope]}\s+)+", "", connector) for connector in connectors]
            if len(atoms) == 1 or all(connector == "or" or scope == "MATERIAL" and connector == "/" for connector in connectors):
                groups[scope].append(mentioned)
            elif all(connector in {"and", "&", ","} for connector in connectors) and (candidate_facts or shared_actuation or re.search(r"\b(?:require(?:d|s)?|mandatory|must|shall|need(?:s|ed)?|both)\b", clause)):
                groups[scope].extend({name} for name in sorted(mentioned))
            else:
                groups[scope].extend({name} for name in sorted(mentioned))
                ambiguous.add(scope)
        if len(requested) > 1:
            for clause in relevant_clauses:
                known = independent_rfq_clause(clause.lower())
                custom = re.search(r"\bcustom(?:i[sz]\w*|[-\s]*(?:made|sizes?|sized|dimensions?))\b", clause)
                if custom and customization_allowed(clause):
                    known |= bool(customization_permission(clause, custom)[0])
                if not known:
                    ambiguous.add(scope)
        # Retain a recognized requirement even if its occurrence used a
        # supported shorthand not represented by a normal concept span.
        represented = set().union(*groups[scope]) if groups[scope] else set()
        groups[scope].extend({name} for name in sorted(requested - represented))
    return groups, ambiguous


def hard_scope_matches(groups: dict[str, list[set[str]]], ambiguous: set[str], scope: str, candidate: set[str]) -> bool:
    return scope not in ambiguous and all(group & candidate for group in groups[scope])


def unresolved_hard_requirements(groups: dict[str, list[set[str]]], ambiguous: set[str], candidate: set[str]) -> set[str]:
    unresolved = set(ambiguous)
    for scope, requirements in groups.items():
        names = set().union(*requirements) if requirements else set()
        if len(names) > 1 and len(requirements) > 1 and not all(group & candidate for group in requirements):
            unresolved.add(scope)
    return unresolved


def option_fact_clauses(value: str) -> list[str]:
    """Keep enclosed operand modifiers intact across ordinary punctuation."""
    clauses: list[str] = []
    start = 0
    enclosures: list[str] = []
    closing = {"(": ")", "[": "]"}
    for index, character in enumerate(value):
        if character in closing:
            enclosures.append(closing[character])
        elif enclosures and character == enclosures[-1]:
            enclosures.pop()
        elif not enclosures and (
            character in ",;!?\n"
            or character == "." and not (
                index > 0 and index + 1 < len(value)
                and value[index - 1].isdigit() and value[index + 1].isdigit()
            )
        ):
            # A leading conjunction is still the preceding operand's relation,
            # including comma/newline sequences and qualified bare modifiers.
            if re.match(r"[\s,;!?.]*(?:or\b|and\b|&|/)", value[index + 1:]):
                continue
            clauses.append(value[start:index])
            start = index + 1
    clauses.append(value[start:])
    return clauses


def option_operand_glue(value: str) -> bool:
    """Consume adjacent modifiers, never unrelated free text between OR facts."""
    # A qualifier is not evidence that an option was selected. Balanced
    # enclosures can contain unknown words/conditions, but must be consumed
    # whole; removing them must not leave an unrelated packaging operand.
    remainder: list[str] = []
    enclosures: list[str] = []
    closing = {"(": ")", "[": "]"}
    for character in value:
        if character in closing:
            enclosures.append(closing[character])
        elif enclosures:
            if character == enclosures[-1]:
                enclosures.pop()
        else:
            remainder.append(character)
    if enclosures:
        return False
    glue = (
        r"\s*(?:(?:type|count|material|grade|actuation|operation|installation|"
        r"mounting|sizes?|dimensions?|mm|cm|standard|optional|default|option|"
        r"available|as)\s*)*"
    )
    return re.fullmatch(glue, "".join(remainder)) is not None


def option_modifier_owners(value: str) -> list[int]:
    """Identify complete or unfinished modifiers without quadratic scans."""
    owners = [0] * len(value)
    enclosures: list[str] = []
    closing = {"(": ")", "[": "]"}
    start = 0
    owner = 0
    for index, character in enumerate(value):
        if character in closing:
            if not enclosures:
                start = index
            enclosures.append(closing[character])
        elif enclosures and character == enclosures[-1]:
            enclosures.pop()
            if not enclosures:
                owner += 1
                for position in range(start, index + 1):
                    owners[position] = owner
    if enclosures:
        owner += 1
        for position in range(start, len(value)):
            owners[position] = owner
    return owners


def unrelated_packaging_options(left: str, right: str) -> bool:
    """Only explicit nonstructural operands can exempt an unconsumed OR."""
    # This is positive field evidence, not a list of harmless connector words.
    # A structural-looking opposite operand keeps the uncertainty in scope.
    structural_right = any(re.search(pattern, right) for pattern in SCOPE_PATTERNS.values())
    structural_right |= any(re.search(CONCEPT_PATTERNS[name], right) for names in HARD_CONCEPT_GROUPS.values() for name in names)
    structural_right |= bool(re.search(DIMENSION_PATTERN, right) or re.search(r"\b\w*mount\w*\b", right))
    if structural_right:
        return False
    actual_left = [match.end() for names in HARD_CONCEPT_GROUPS.values() for name in names for match in re.finditer(CONCEPT_PATTERNS[name], left)]
    actual_left.extend(match.end() for match in re.finditer(DIMENSION_PATTERN, left))
    actual_left.extend(match.end() for match in re.finditer(r"\bsus\s*\d+\b|\b\w+[-\s]+operated\b|\b\w*mount\w*\b", left))
    last_fact = max(actual_left, default=0)
    packaging_operand = r"\b(?:cartons?|pallets?|crates?|box(?:es)?)\b"
    # The OR owner may be an open enclosure on the left fragment (the pair
    # itself is enclosed). Completed earlier modifiers have a different
    # owner and cannot supply positive packing-operand evidence.
    owners = option_modifier_owners(left)
    or_owner = owners[-1] if owners and left[-1:] not in {")", "]"} else 0
    left_operands = [match for match in re.finditer(packaging_operand, left) if not owners[match.start()] or owners[match.start()] == or_owner]
    if left_operands and left_operands[-1].start() >= last_fact and re.search(packaging_operand, right):
        return True
    return any(match.start() >= last_fact and not owners[match.start()] for match in re.finditer(r"\b(?:packaging|packing)\b", left))


def fact_option_scopes(label: str, value: str) -> set[str]:
    """Bind typed facts or adjacent OR operands, not every fact in a title."""
    unresolved = set()
    for clause in option_fact_clauses(value.lower()):
        choices = list(re.finditer(r"\bor\b|/", clause))
        if not choices:
            continue
        unresolved.update(scope for scope, pattern in SCOPE_PATTERNS.items() if re.search(pattern, label.lower()))
        spans = [(match.start(), match.end(), scope) for scope, pattern in SCOPE_PATTERNS.items() for match in re.finditer(pattern, clause)]
        actual_spans = [(match.start(), match.end(), scope) for scope, names in HARD_CONCEPT_GROUPS.items() for name in names for match in re.finditer(CONCEPT_PATTERNS[name], clause)]
        actual_spans.extend((match.start(), match.end(), "DIMENSIONS") for match in re.finditer(DIMENSION_PATTERN, clause))
        spans.extend(actual_spans)
        owners = option_modifier_owners(clause)
        # These are domain cues, not approved/known configurations. Retain
        # actual inner fact values separately from generic modifier labels.
        value_patterns = {
            "MATERIAL": r"\bsus\s*\d+\b",
            "ACTUATION": r"\b\w+[-\s]+operated\b",
            "INSTALLATION": r"\b\w*mount\w*\b",
            "BOWL_COUNT": r"\b\w+[-\s]+(?:bowls?|basins?)\b",
            "DIMENSIONS": DIMENSION_PATTERN,
        }
        actual_spans.extend((match.start(), match.end(), scope) for scope, pattern in value_patterns.items() for match in re.finditer(pattern, clause))
        spans.extend(actual_spans)
        inner_facts = {}
        for scope, pattern in value_patterns.items():
            for match in re.finditer(pattern, clause):
                owner = owners[match.start()]
                if owner:
                    inner_facts.setdefault(owner, set()).add(scope)
        for scope, names in HARD_CONCEPT_GROUPS.items():
            for name in names:
                for match in re.finditer(CONCEPT_PATTERNS[name], clause):
                    owner = owners[match.start()]
                    if owner:
                        inner_facts.setdefault(owner, set()).add(scope)
        modifier_starts = {}
        for position, owner in enumerate(owners):
            if owner:
                modifier_starts.setdefault(owner, position)
        for choice_index, choice in enumerate(choices):
            # A scope word inside a modifier is not the operand boundary.
            # Ignore only complete enclosures outside this OR's own branch;
            # known options inside the same enclosure remain visible.
            adjacent = [span for span in spans if not owners[span[0]] or owners[span[0]] == owners[choice.start()]]
            anchor = set()
            if owners[choice.start()]:
                start = modifier_starts[owners[choice.start()]]
                preceding = [span for span in spans if span[1] <= start and not owners[span[0]]]
                if preceding:
                    nearest = max(span[1] for span in preceding)
                    if option_operand_glue(clause[nearest:start]):
                        anchor.update(scope for _, end, scope in preceding if end == nearest)
                        # A field label inside its enclosing qualifier does
                        # not retype the operand, but actual inner values do
                        # retain their own field (material/actuation/size etc.).
                        actual_inner = inner_facts.get(owners[choice.start()], set())
                        if actual_inner:
                            anchor.clear()
                        inner_scopes = actual_inner or anchor
                        adjacent = [span for span in adjacent if not owners[span[0]] or span[2] in inner_scopes]
            left = [span for span in adjacent if span[1] <= choice.start()]
            right = [span for span in adjacent if span[0] >= choice.end()]
            bound = set()
            left_bound = set()
            if left:
                nearest = max(span[1] for span in left)
                if option_operand_glue(clause[nearest:choice.start()]):
                    left_bound.update(scope for _, end, scope in left if end == nearest)
                    bound.update(left_bound)
            if right:
                nearest = min(span[0] for span in right)
                if option_operand_glue(clause[choice.end():nearest]):
                    bound.update(scope for start, _, scope in right if start == nearest)
            # Glue is a sufficient binding, not permission to discard unknown
            # connector text or an unfinished enclosure. An unconsumed OR
            # retains its nearest actual field unless a separate packaging
            # field is positively identified. Do not collect every scope in
            # the title or bridge a previous/next OR operand.
            previous = choices[choice_index - 1].end() if choice_index else 0
            following = choices[choice_index + 1].start() if choice_index + 1 < len(choices) else len(clause)
            before, after = clause[previous:choice.start()], clause[choice.end():following]
            packaging = unrelated_packaging_options(before, after)
            if not packaging:
                bound.update(anchor)
            if not left_bound and not packaging:
                left = [span for span in left if span[0] >= previous]
                right = [span for span in right if span[1] <= following]
                if owners[choice.start()]:
                    # Generic labels in a structural modifier are not an
                    # independent operand. Concrete inner facts still are.
                    left = [span for span in left if span in actual_spans]
                # A title section delimiter is evidence of a new operand
                # subject when its opposite side has a known field. It is
                # not enough on its own to erase the earlier uncertainty.
                sections = list(re.finditer(r"\s+-\s+|:\s*", before))
                boundary = previous + sections[-1].end() if sections else previous
                section_left = [span for span in left if span[0] >= boundary]
                if section_left or not right:
                    selected = section_left or left
                    if selected:
                        nearest = max(span[1] for span in selected)
                        bound.update(scope for _, end, scope in selected if end == nearest)
                elif right:
                    nearest = min(span[0] for span in right)
                    bound.update(scope for start, _, scope in right if start == nearest)
            unresolved.update(bound)
    return unresolved


def candidate_option_scopes(record: dict[str, Any]) -> set[str]:
    """Inspect full typed fact text even when one or both options are unknown."""
    fields = [("", str(record.get("name") or "")), ("", str(record.get("subcategory") or ""))]
    fields.extend((str(attr.get("name") or ""), str(attr.get("raw_value") or "")) for attr in record.get("attributes") or [] if isinstance(attr, dict))
    return set().union(*(fact_option_scopes(label, value) for label, value in fields))


def mandatory_candidate_ambiguity(groups: dict[str, list[set[str]]], candidate: set[str], record: dict[str, Any]) -> set[str]:
    """Requested hard-domain facts must be definite, not unapproved options."""
    parts = [str(record.get("name") or ""), str(record.get("subcategory") or "")]
    parts.extend(str(attr.get("raw_value") or "") for attr in record.get("attributes") or [] if isinstance(attr, dict))
    candidate_groups, ambiguous = hard_requirement_groups("; ".join(parts), candidate, candidate_facts=True)
    options = candidate_option_scopes(record)
    unresolved = set()
    for scope, requirements in groups.items():
        names = set().union(*requirements) if requirements else set()
        if not requirements:
            continue
        if scope in options or scope in ambiguous or any(len(group) > 1 for group in candidate_groups[scope]):
            unresolved.add(scope)
        if len(names) < 2 or len(requirements) < 2:
            continue
        definite = set().union(*(group for group in candidate_groups[scope] if len(group) == 1)) if candidate_groups[scope] else set()
        if scope in ambiguous or not all(group & definite for group in requirements):
            unresolved.add(scope)
    return unresolved


def dimension_requirements(text: str) -> list[list[float]]:
    requirements = []
    for match in re.finditer(DIMENSION_PATTERN, text, flags=re.I):
        values = [float(match.group(1)), float(match.group(2))]
        if match.group(3):
            values.append(float(match.group(3)))
        if (match.group(4) or "").lower() == "cm":
            values = [item * 10 for item in values]
        requirements.append(values)
    return requirements


def dimensions_agree(requested: list[float], actual: list[float]) -> bool:
    return len(requested) == len(actual) and all(abs(a - b) <= max(20, a * 0.08) for a, b in zip(requested, actual))


def incompatible_dimension_requirements(requirements: list[list[float]]) -> bool:
    return any(not dimensions_agree(left, right) for index, left in enumerate(requirements) for right in requirements[index + 1:])


def dimensions(text: str) -> list[float]:
    requirements = dimension_requirements(text)
    return requirements[0] if requirements else []


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                value["_line_number"] = line_number
                rows.append(value)
    return rows


def latest_path(root: Path, pattern: str) -> Path | None:
    candidates = [path for path in root.glob(pattern) if path.is_file()]
    return max(candidates, key=lambda item: item.stat().st_mtime_ns) if candidates else None


def dispositions(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    return {str(row.get("product_id")): row for row in load_jsonl(path) if row.get("product_id")}


def historical_refs(
    catalog_rows: Iterable[dict[str, Any]],
    model: str,
    disposition_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for row in catalog_rows:
        if canonical_model(row.get("model")) != model:
            continue
        product_id = str(row.get("product_id") or "")
        overlay = disposition_map.get(product_id)
        if not isinstance(overlay, dict):
            continue
        record_disposition = overlay.get("record_disposition")
        if (
            record_disposition != HISTORICAL_REFERENCE_DISPOSITION
            or overlay.get("allow_rag") is not True
            or ("disposition" in overlay and overlay["disposition"] != record_disposition)
        ):
            continue
        refs.append(
            {
                "product_id": product_id,
                "subject": row.get("subject"),
                "currency": row.get("currency"),
                "min_price": row.get("sku_min_price"),
                "max_price": row.get("sku_max_price"),
                "moq": row.get("min_order_quantity"),
                "modified": row.get("gmt_modified"),
                "record_disposition": record_disposition,
                "commercial_authority": "REFERENCE_ONLY",
            }
        )
    return sorted(refs, key=lambda row: str(row.get("modified") or ""), reverse=True)[:5]


def score_candidate(rfq_text: str, record: dict[str, Any]) -> dict[str, Any]:
    rfq = concepts(rfq_text)
    _, excluded_requirements, unresolved_negation = requirement_concepts(rfq_text)
    candidate_structured_text = structured_blob(record)
    candidate = concepts(candidate_structured_text)
    _, _, unresolved_candidate_negation = requirement_concepts(candidate_structured_text)
    allowed_custom_scopes, unknown_custom_qualification = customization_scope_analysis(rfq_text)
    hard_groups, ambiguous_hard_scopes = hard_requirement_groups(rfq_text, rfq)
    ambiguous_hard_scopes.update(mandatory_candidate_ambiguity(hard_groups, candidate, record))
    unresolved_hard_scopes = unresolved_hard_requirements(hard_groups, ambiguous_hard_scopes, candidate)
    score = 0.0
    matched: list[str] = []
    mismatches: list[str] = []
    mismatch_scopes: list[str] = []
    family_match = "NOT_APPLICABLE"

    def mismatch(scope: str, description: str) -> None:
        mismatches.append(description)
        mismatch_scopes.append(scope)

    rejected = sorted(excluded_requirements & candidate)
    for concept in rejected:
        mismatch("EXPLICIT_EXCLUSION", f"candidate has explicitly excluded requirement: {concept}")
    if unresolved_negation:
        mismatch("UNRESOLVED_NEGATION", "unresolved RFQ negation requires manual review")
    if unresolved_candidate_negation:
        mismatch("UNRESOLVED_CANDIDATE_POLARITY", "unresolved candidate negation requires manual review")
    if unknown_custom_qualification:
        mismatch("UNRESOLVED_CUSTOMIZATION_SCOPE", "unresolved customization qualification requires manual review")

    if "HAND_WASH_SINK" in rfq:
        category = str(record.get("category") or "").strip().lower()
        name = str(record.get("name") or "")
        if category == "wash sinks" or re.search(r"hand\s*wash|wash\s*sink", name, flags=re.I):
            score += 4
            matched.append("product_family")
            family_match = "EXACT_OR_INTENDED_APPLICATION"
        elif re.search(r"\bsink\b|\bbasin\b", name, flags=re.I):
            score += 1
            matched.append("generic_sink_only")
            family_match = "GENERIC_ONLY"
        else:
            family_match = "NO_MATCH"
    else:
        shared_terms = {item for item in rfq & candidate if item.startswith("T:")}
        family_terms = {"T:faucet", "T:sink", "T:basin", "T:dispenser", "T:grill", "T:bubbler"}
        if shared_terms & family_terms:
            score += 2
            matched.append("partial_product_family")

    requested_materials = rfq & {"SUS304", "SUS201"}
    candidate_materials = candidate & {"SUS304", "SUS201"}
    if requested_materials and hard_scope_matches(hard_groups, ambiguous_hard_scopes, "MATERIAL", candidate_materials):
        score += 2
        matched.append("material")
    elif requested_materials:
        description = "material" if candidate_materials else "requested material not shown in base facts"
        if len(requested_materials) > 1:
            description = f"material requirements {sorted(requested_materials)}; base {sorted(candidate_materials) if candidate_materials else 'not shown'}"
        mismatch("MATERIAL", description)

    requested_actuation = rfq & {"FOOT_OPERATED", "KNEE_OPERATED"}
    candidate_actuation = candidate & {"FOOT_OPERATED", "KNEE_OPERATED"}
    if requested_actuation:
        if hard_scope_matches(hard_groups, ambiguous_hard_scopes, "ACTUATION", candidate_actuation):
            score += 2
            matched.append("actuation")
        elif candidate_actuation:
            score += 1.5
            matched.append("hands_free_actuation_family")
            requested_label = "foot-operated" if "FOOT_OPERATED" in rfq else "knee-operated"
            base_label = "foot-operated" if "FOOT_OPERATED" in candidate else "knee-operated"
            description = f"{requested_label} requested; {base_label} base"
            if len(requested_actuation) > 1:
                description = f"actuation requirements {sorted(requested_actuation)}; base {sorted(candidate_actuation)}"
            mismatch("ACTUATION", description)
        else:
            requested_label = "foot-operated" if "FOOT_OPERATED" in rfq else "knee-operated"
            description = f"{requested_label} actuation not shown in base facts"
            if len(requested_actuation) > 1:
                description = f"actuation requirements {sorted(requested_actuation)} not shown in base facts"
            mismatch("ACTUATION", description)

    requested_mounts = rfq & {"WALL_MOUNT", "FLOOR_STAND", "DECK_MOUNT"}
    candidate_mounts = candidate & {"WALL_MOUNT", "FLOOR_STAND", "DECK_MOUNT"}
    if requested_mounts and hard_scope_matches(hard_groups, ambiguous_hard_scopes, "INSTALLATION", candidate_mounts):
        score += 2
        matched.append("installation")
    elif requested_mounts:
        mismatch("INSTALLATION",
            f"installation requested {sorted(requested_mounts)}; base {sorted(candidate_mounts) if candidate_mounts else 'not shown'}"
        )
        if "INSTALLATION" not in allowed_custom_scopes:
            score -= 5

    requested_bowls = rfq & {"SINGLE_BOWL", "DOUBLE_BOWL"}
    candidate_bowls = candidate & {"SINGLE_BOWL", "DOUBLE_BOWL"}
    if requested_bowls:
        if not candidate_bowls:
            mismatch("BOWL_COUNT", f"bowl count requested {sorted(requested_bowls)}; base count not shown")
        elif not hard_scope_matches(hard_groups, ambiguous_hard_scopes, "BOWL_COUNT", candidate_bowls) or len(candidate_bowls) > 1:
            mismatch("BOWL_COUNT", f"bowl count requested {sorted(requested_bowls)}; base {sorted(candidate_bowls)}")

    accessory_concepts = {"SINGLE_BOWL", "DOUBLE_BOWL", "RECTANGULAR", "FAUCET", "SOAP_DISPENSER", "WASTE_BIN", "TOWEL_BOX"}
    accessory_hits = rfq & candidate & accessory_concepts
    if accessory_hits:
        score += 1
        matched.append("size_or_accessories")

    rfq_dimensions = dimensions(rfq_text)
    requested_dimensions = dimension_requirements(rfq_text)
    candidate_dimensions = dimensions(candidate_structured_text)
    candidate_dimension_options = bool(requested_dimensions and "DIMENSIONS" in candidate_option_scopes(record))
    if candidate_dimension_options:
        unresolved_hard_scopes.add("DIMENSIONS")
    if incompatible_dimension_requirements(requested_dimensions):
        unresolved_hard_scopes.add("DIMENSIONS")
    if rfq_dimensions and candidate_dimensions:
        if not candidate_dimension_options and all(dimensions_agree(size, candidate_dimensions) for size in requested_dimensions):
            score += 1
            matched.append("dimensions")
        else:
            description = f"dimensions requested {rfq_dimensions} mm; base {candidate_dimensions} mm"
            if len(requested_dimensions) > 1:
                description = f"dimensions requested {requested_dimensions} mm; base {candidate_dimensions} mm"
            mismatch("DIMENSIONS", description)
    elif "DIMENSIONS" in unresolved_hard_scopes:
        mismatch("DIMENSIONS", "multiple incompatible requested dimensions require manual review")

    if unresolved_hard_scopes:
        mismatch("UNRESOLVED_HARD_REQUIREMENTS", f"mandatory or ambiguous requirements require review: {sorted(unresolved_hard_scopes)}")

    if allowed_custom_scopes:
        score += 1
        matched.append("customization_allowed")

    mode = "EXACT_OR_NEAR"
    if mismatches:
        mode = "CUSTOM" if set(mismatch_scopes) <= allowed_custom_scopes else "REVIEW"
    if rejected:
        mode = "REJECTED"

    fact_values: dict[str, str] = {}
    for attr in record.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("name") and attr.get("raw_value"):
            fact_values.setdefault(str(attr["name"]), str(attr["raw_value"]))

    return {
        "model": canonical_model(record.get("canonical_model") or record.get("raw_model")),
        "name": record.get("name"),
        "category": record.get("category"),
        "subcategory": record.get("subcategory"),
        "score": round(score, 2),
        "mode": mode,
        "family_match": family_match,
        "matched": matched,
        "mismatches": mismatches,
        "mismatch_scopes": mismatch_scopes,
        "customization_scopes": sorted(allowed_custom_scopes),
        "candidate_concepts": sorted(candidate),
        "rejected_requirements": rejected,
        "negation_requires_review": unresolved_negation,
        "candidate_negation_requires_review": unresolved_candidate_negation,
        "customization_requires_review": unknown_custom_qualification,
        "hard_requirements_requires_review": bool(unresolved_hard_scopes),
        "unresolved_hard_requirement_scopes": sorted(unresolved_hard_scopes),
        "source_line": record.get("_line_number"),
        "facts": fact_values,
    }


def recommended_action(rfq_text: str, best: dict[str, Any] | None) -> str:
    """A score ranks candidates; it cannot authorize a structural conflict."""
    if not best:
        return "SKIP_RFQ"
    rfq_concepts = concepts(rfq_text)
    _, excluded_requirements, unresolved_negation = requirement_concepts(rfq_text)
    groups, ambiguous = hard_requirement_groups(rfq_text, rfq_concepts)
    candidate_record = {"name": best.get("name"), "subcategory": best.get("subcategory"), "attributes": [{"name": name, "raw_value": value} for name, value in (best.get("facts") or {}).items()]}
    ambiguous.update(mandatory_candidate_ambiguity(groups, set(best.get("candidate_concepts") or []), candidate_record))
    unresolved_requirements = unresolved_hard_requirements(groups, ambiguous, set(best.get("candidate_concepts") or []))
    if incompatible_dimension_requirements(dimension_requirements(rfq_text)):
        unresolved_requirements.add("DIMENSIONS")
    if dimension_requirements(rfq_text) and "DIMENSIONS" in candidate_option_scopes(candidate_record):
        unresolved_requirements.add("DIMENSIONS")
    if best.get("mode") == "REJECTED" or excluded_requirements & set(best.get("candidate_concepts") or []):
        return "SKIP_RFQ"
    if unresolved_negation or unresolved_requirements or best.get("candidate_negation_requires_review") or customization_scope_analysis(rfq_text)[1]:
        return "PREPARE_AND_REVIEW" if best["score"] >= 4 else "SKIP_RFQ"
    family_gate = not (
        "HAND_WASH_SINK" in rfq_concepts
        and best["family_match"] != "EXACT_OR_INTENDED_APPLICATION"
    )
    mode = best.get("mode")
    mismatches = best.get("mismatches") or []
    mismatch_scopes = best.get("mismatch_scopes") or []
    covered_conflicts = (
        bool(mismatches)
        and len(mismatches) == len(mismatch_scopes)
        and set(mismatch_scopes) <= customization_scopes(rfq_text)
    )
    conflict_gate = (
        mode == "EXACT_OR_NEAR" and not mismatches
    ) or (
        mode == "CUSTOM" and covered_conflicts
    )
    if best["score"] >= 6 and family_gate and conflict_gate:
        return "FILL_TO_SUBMIT"
    if best["score"] >= 4:
        return "PREPARE_AND_REVIEW"
    return "SKIP_RFQ"


def parse_args() -> argparse.Namespace:
    script_path = Path(__file__).resolve()
    workspace = script_path.parents[4]
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rfq-text")
    source.add_argument("--rfq-file", type=Path)
    parser.add_argument(
        "--official-current",
        type=Path,
        default=workspace / "sellerkit-alibaba-review-ready-v1" / "work" / "official_current.jsonl",
    )
    parser.add_argument("--catalog-index", type=Path)
    parser.add_argument("--disposition", type=Path)
    parser.add_argument("--top", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = Path(__file__).resolve().parents[4]
    rfq_text = args.rfq_text
    if args.rfq_file:
        rfq_text = args.rfq_file.read_text(encoding="utf-8-sig")
    assert rfq_text is not None

    if not args.official_current.is_file():
        raise SystemExit(f"official_current not found: {args.official_current}")
    official_rows = load_jsonl(args.official_current)

    catalog_path = args.catalog_index or latest_path(
        workspace, "已上架商品全量归档_*/01_商品索引/catalog_index.jsonl"
    )
    disposition_path = args.disposition or latest_path(
        workspace, "已上架商品全量归档_*__record_disposition_overlay__*/02_overlay/record_disposition.jsonl"
    )
    catalog_rows = load_jsonl(catalog_path) if catalog_path else []
    disposition_map = dispositions(disposition_path)

    scored = sorted(
        (score_candidate(rfq_text, row) for row in official_rows),
        key=lambda row: (-row["score"], row["model"]),
    )
    ranked = [candidate for candidate in scored if candidate["mode"] != "REJECTED"][: max(1, args.top)]
    rejected = [candidate for candidate in scored if candidate["mode"] == "REJECTED"]
    for candidate in ranked:
        candidate["historical_price_references"] = historical_refs(
            catalog_rows, candidate["model"], disposition_map
        )

    best = ranked[0] if ranked else None
    action = recommended_action(rfq_text, best)

    result = {
        "schema_version": "sellerkit.alibaba_rfq_shortlist.v1",
        "status": "HEURISTIC_SHORTLIST_ONLY",
        "rfq_concepts": sorted(concepts(rfq_text)),
        "recommended_action": action,
        "recommended_model": best["model"] if best else None,
        "official_source": str(args.official_current),
        "catalog_source": str(catalog_path) if catalog_path else None,
        "disposition_source": str(disposition_path) if disposition_path else None,
        "candidates": ranked,
        "rejected_candidate_count": len(rejected),
        "rejected_candidates": rejected[: max(1, args.top)],
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
