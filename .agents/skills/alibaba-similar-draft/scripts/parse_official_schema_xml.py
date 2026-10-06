#!/usr/bin/env python3
"""Parse one read-only Alibaba schema.get XML response into a hash-bound tree.

The parser has no network or browser capability. It rejects DTD/entity input,
preserves nested field/rule/option/dependency semantics, records manual-only
business/tip rules, and fails closed on unsupported XML constructs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parent.parent
SCHEMA_PATH = SKILL_ROOT / "references" / "schemas" / "official-schema-xml-normalized.schema.json"

FIELD_TYPES = {"input", "multiInput", "singleCheck", "multiCheck", "complex", "multiComplex", "label"}
MACHINE_RULES = {
    "valueTypeRule",
    "requiredRule",
    "disableRule",
    "maxLengthRule",
    "minLengthRule",
    "maxValueRule",
    "minValueRule",
    "maxInputNumRule",
    "minInputNumRule",
    "maxTargetSizeRule",
    "minTargetSizeRule",
    "readOnlyRule",
    "regexRule",
    "regxRule",
    "maxImageSizeRule",
    "minImageSizeRule",
    "valueAttributeRule",
}
MANUAL_RULES = {"tipRule", "devTipRule"}
DEPEND_OPERATORS = {"and", "or"}
DEPEND_SYMBOL_ALIASES = {
    "is null": "is null",
    "==": "==",
    "!=": "!=",
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "contains": "contains",
    "not contains": "not contains",
    "this field’s value in fieldOptions": "this field's value in fieldOptions",
    "this field's value in fieldOptions": "this field's value in fieldOptions",
    "this field’s value not in fieldOptions": "this field's value not in fieldOptions",
    "this field's value not in fieldOptions": "this field's value not in fieldOptions",
}
ALLOWED_FIELD_CHILDREN = {
    "rules", "options", "fields", "label-group", "default-value",
    "value", "values", "complex-value", "complex-values",
}


class ParseError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ParseError("captured_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _tag(element: ET.Element) -> str:
    if "}" in element.tag:
        return element.tag.rsplit("}", 1)[-1]
    return element.tag


def _attributes(element: ET.Element, *, omit: set[str] | None = None) -> dict[str, str]:
    omitted = omit or set()
    return {key: value for key, value in sorted(element.attrib.items()) if key not in omitted}


def _parse_depend_groups(rule: ET.Element, path: str, unsupported: list[str]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for child in rule:
        child_tag = _tag(child)
        if child_tag != "depend-group":
            unsupported.append(f"{path}: unsupported rule child <{child_tag}>")
            continue
        operator = child.attrib.get("operator")
        if operator not in DEPEND_OPERATORS:
            unsupported.append(f"{path}: unsupported depend-group operator {operator!r}")
            continue
        expressions: list[dict[str, Any]] = []
        for expression in child:
            if _tag(expression) != "depend-express":
                unsupported.append(f"{path}: unsupported depend-group child <{_tag(expression)}>")
                continue
            raw_symbol = expression.attrib.get("symbol")
            symbol = DEPEND_SYMBOL_ALIASES.get(raw_symbol or "")
            field_id = expression.attrib.get("fieldId")
            if symbol is None or not field_id:
                unsupported.append(f"{path}: invalid dependency field/symbol {field_id!r}/{raw_symbol!r}")
                continue
            expressions.append({
                "field_id": field_id,
                "symbol": symbol,
                "value": expression.attrib.get("value"),
            })
        if not expressions:
            unsupported.append(f"{path}: dependency group has no supported expressions")
            continue
        groups.append({"operator": operator, "expressions": expressions})
    return groups


def _parse_rule(
    element: ET.Element,
    field_path: str,
    manual: list[dict[str, Any]],
    unsupported: list[str],
) -> dict[str, Any]:
    name = element.attrib.get("name") or ""
    if not name:
        unsupported.append(f"{field_path}: rule without name")
        name = "MISSING_RULE_NAME"
    groups = _parse_depend_groups(element, f"{field_path}.rule[{name}]", unsupported)
    machine = name in MACHINE_RULES
    material = {
        "name": name,
        "value": element.attrib.get("value"),
        "attributes": _attributes(element, omit={"name", "value"}),
        "depend_groups": groups,
        "machine_enforceable": machine,
    }
    rule_sha = _sha256_bytes(_canonical(material))
    material["rule_sha256"] = rule_sha
    if not machine:
        reason = "human-facing tip/developer rule" if name in MANUAL_RULES else "unrecognized business rule"
        manual.append({
            "field_path": field_path,
            "rule_name": name,
            "rule_sha256": rule_sha,
            "reason": reason,
        })
    return material


def _field_bool_rule(rules: list[dict[str, Any]], name: str) -> bool:
    return any(rule["name"] == name and str(rule.get("value") or "").lower() == "true" for rule in rules)


def _parse_fields(
    parents: list[ET.Element],
    parent_path: str,
    manual: list[dict[str, Any]],
    unsupported: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    id_counts: dict[str, int] = {}
    for ordinal, element in enumerate(parents):
        if _tag(element) != "field":
            unsupported.append(f"{parent_path}: unsupported field-list child <{_tag(element)}>")
            continue
        field_id = element.attrib.get("id") or ""
        field_type = element.attrib.get("type") or ""
        if not field_id:
            unsupported.append(f"{parent_path}: field without id")
            field_id = f"MISSING_FIELD_ID_{ordinal}"
        id_counts[field_id] = id_counts.get(field_id, 0) + 1
        suffix = f"[{id_counts[field_id] - 1}]" if id_counts[field_id] > 1 else ""
        path = f"{parent_path}.{field_id}{suffix}" if parent_path else f"{field_id}{suffix}"
        if field_type not in FIELD_TYPES:
            unsupported.append(f"{path}: unsupported field type {field_type!r}")
            field_type = "input"

        rules: list[dict[str, Any]] = []
        options: list[dict[str, Any]] = []
        children: list[dict[str, Any]] = []
        default_value: str | None = None
        for child in element:
            child_tag = _tag(child)
            if child_tag not in ALLOWED_FIELD_CHILDREN:
                unsupported.append(f"{path}: unsupported field child <{child_tag}>")
                continue
            if child_tag == "rules":
                for rule in child:
                    if _tag(rule) != "rule":
                        unsupported.append(f"{path}: unsupported rules child <{_tag(rule)}>")
                    else:
                        rules.append(_parse_rule(rule, path, manual, unsupported))
            elif child_tag == "options":
                for option in child:
                    if _tag(option) != "option":
                        unsupported.append(f"{path}: unsupported options child <{_tag(option)}>")
                        continue
                    options.append({
                        "display_name": option.attrib.get("displayName"),
                        "value": option.attrib.get("value"),
                        "attributes": _attributes(option, omit={"displayName", "value"}),
                    })
            elif child_tag == "fields":
                children.extend(_parse_fields(list(child), path, manual, unsupported))
            elif child_tag == "default-value":
                default_value = child.text or ""
            elif child_tag in {"value", "values", "complex-value", "complex-values"}:
                # schema.get may include defaults. Values are not treated as rules;
                # preserve only their presence so product data cannot leak into JSON.
                if (child.text or "").strip() or list(child) or child.attrib:
                    manual.append({
                        "field_path": path,
                        "rule_name": f"runtime-{child_tag}",
                        "rule_sha256": _sha256_bytes(_canonical({"tag": child_tag, "present": True})),
                        "reason": "runtime/default value container requires explicit review and is not copied",
                    })
            elif child_tag == "label-group":
                manual.append({
                    "field_path": path,
                    "rule_name": "label-group",
                    "rule_sha256": _sha256_bytes(_canonical({"tag": "label-group", "attributes": _attributes(child)})),
                    "reason": "display-only label group",
                })

        material = {
            "path": path,
            "ordinal": ordinal,
            "id": field_id,
            "name": element.attrib.get("name"),
            "type": field_type,
            "rules": rules,
            "options": options,
            "children": children,
            "required": _field_bool_rule(rules, "requiredRule"),
            "read_only": _field_bool_rule(rules, "readOnlyRule"),
            "default_value": default_value,
        }
        material["rule_sha256"] = _sha256_bytes(_canonical({
            "path": path,
            "type": field_type,
            "rules": rules,
            "options": options,
            "children_rule_sha256": [child["rule_sha256"] for child in children],
        }))
        result.append(material)
    return result


def _flatten(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for field in fields:
        result.append(field)
        result.extend(_flatten(field["children"]))
    return result


def parse_schema_xml(
    *,
    xml_path: Path,
    output: Path,
    capture_id: str,
    run_id: str,
    target_model: str,
    template_product_id: str,
    category_id: str,
    product_type: str,
    captured_at: str | None = None,
    valid_minutes: int = 15,
) -> dict[str, Any]:
    xml_path = xml_path.resolve()
    output = output.resolve()
    if output.exists():
        raise ParseError(f"refusing to overwrite output: {output}")
    if not xml_path.is_file() or xml_path.stat().st_size < 1:
        raise ParseError("XML input is missing or empty")
    if not str(template_product_id).isdigit():
        raise ParseError("template_product_id must be numeric")
    if not 1 <= valid_minutes <= 15:
        raise ParseError("valid_minutes must be between 1 and 15")
    raw = xml_path.read_bytes()
    # XML declarations are ASCII-compatible in every encoding supported by
    # ElementTree.  Removing NUL code units before the comparison also covers
    # UTF-16/UTF-32 LE/BE documents; the old raw-byte comparison silently
    # missed those encodings and allowed an internal DTD through.
    declaration_scan = re.sub(br"\s+", b"", raw.upper().replace(b"\x00", b""))
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise ParseError("DTD/entity declarations are forbidden")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ParseError(f"invalid schema XML: {exc}") from exc
    if _tag(root) != "itemSchema":
        raise ParseError(f"root element must be itemSchema, got {_tag(root)!r}")

    unsupported: list[str] = []
    manual: list[dict[str, Any]] = []
    fields = _parse_fields(list(root), "", manual, unsupported)
    if not fields:
        raise ParseError("schema XML contains no fields")
    flattened = _flatten(fields)
    paths = [field["path"] for field in flattened]
    if len(paths) != len(set(paths)):
        unsupported.append("field paths are not unique")
    captured_at_explicit = isinstance(captured_at, str) and bool(captured_at.strip())
    status = (
        "REPLAY_REVIEW_ONLY"
        if not captured_at_explicit
        else "BLOCKED_UNSUPPORTED_SCHEMA"
        if unsupported
        else "PARSED_REVIEW_REQUIRED"
        if manual
        else "PARSED_MACHINE_RULES_COMPLETE"
    )
    # Never manufacture freshness from the local clock.  A raw XML file can
    # be replayed at any time, so only an explicit capture timestamp may make
    # the normalized result eligible for the downstream freshness gate.  The
    # raw XML bytes/path/hash remain bound in ``raw_xml`` either way.
    observed_at = _utc(captured_at.strip()) if captured_at_explicit else None
    document = {
        "schema_version": "sellerkit.alibaba-official-schema-xml.v1",
        "capture_id": capture_id,
        "run_id": run_id,
        "target_model": target_model.strip().upper(),
        "template_product_id": str(template_product_id),
        "category_id": str(category_id),
        "product_type": str(product_type),
        "captured_at": observed_at.isoformat().replace("+00:00", "Z") if observed_at else None,
        "expires_at": (
            (observed_at + timedelta(minutes=valid_minutes)).isoformat().replace("+00:00", "Z")
            if observed_at else None
        ),
        "freshness_eligible": bool(observed_at),
        "source": {"api_method": "alibaba.icbu.product.schema.get", "read_only": True},
        "raw_xml": {"path": str(xml_path), "bytes": xml_path.stat().st_size, "sha256": _sha256_file(xml_path)},
        "parser": {
            "tool_sha256": _sha256_file(SCRIPT_PATH),
            "python_xml_parser": "xml.etree.ElementTree",
            "external_entities_allowed": False,
        },
        "fields": fields,
        "field_paths_sha256": _sha256_bytes(_canonical(paths)),
        "rule_graph_sha256": _sha256_bytes(_canonical([
            {"path": field["path"], "rule_sha256": field["rule_sha256"]} for field in flattened
        ])),
        "manual_review_rules": manual,
        "unsupported_constructs": sorted(set(unsupported)),
        "status": status,
        "write_authority": "NONE_READ_ONLY_EVIDENCE",
        "final_write_enabled": False,
    }
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            detail = "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors[:10])
            raise ParseError(f"normalized document failed schema validation: {detail}")
    except ImportError as exc:
        raise ParseError("jsonschema runtime is required; use the bundled wheelhouse") from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return document


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse a read-only Alibaba schema.get XML response")
    parser.add_argument("--xml-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-model", required=True)
    parser.add_argument("--template-product-id", required=True)
    parser.add_argument("--category-id", required=True)
    parser.add_argument("--product-type", required=True)
    parser.add_argument("--captured-at")
    parser.add_argument("--valid-minutes", type=int, default=15)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        document = parse_schema_xml(**vars(arguments))
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3
    print(json.dumps({
        "status": document["status"],
        "output_sha256": _sha256_file(arguments.output.resolve()),
        "field_paths_sha256": document["field_paths_sha256"],
        "rule_graph_sha256": document["rule_graph_sha256"],
        "manual_review_rule_count": len(document["manual_review_rules"]),
        "unsupported_construct_count": len(document["unsupported_constructs"]),
        "freshness_eligible": document["freshness_eligible"],
        "write_authority": "NONE_READ_ONLY_EVIDENCE",
    }, ensure_ascii=False, indent=2, sort_keys=True))
    if document["status"] == "PARSED_MACHINE_RULES_COMPLETE":
        return 0
    if document["status"] in {"PARSED_REVIEW_REQUIRED", "REPLAY_REVIEW_ONLY"}:
        return 4
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
