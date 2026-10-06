"""Official-XML dynamic-schema fixtures shared by local unit tests only."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

from normalize_dynamic_publish_schema import normalize_capture
from parse_official_schema_xml import parse_schema_xml


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("fixture captured_at must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_official_dynamic_schema(
    root: Path,
    *,
    fields: list[dict[str, Any]],
    run_id: str,
    target_model: str,
    template_product_id: str,
    category_id: str,
    product_type: str,
    captured_at: datetime | None = None,
    valid_minutes: int = 15,
    stem: str = "dynamic",
) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    captured_at = captured_at or datetime.now(timezone.utc)
    rows: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>', "<itemSchema>"]
    for spec in fields:
        field_id = str(spec["logical_name"])
        label = str(spec.get("observed_label") or field_id.replace("_", " ").title())
        field_type = str(spec.get("official_field_type") or "input")
        rows.append(
            f"  <field id={quoteattr(field_id)} name={quoteattr(label)} type={quoteattr(field_type)}>"
        )
        rules: list[str] = []
        if spec.get("required") is True:
            rules.append('      <rule name="requiredRule" value="true"/>')
        maximum = spec.get("max_length")
        if maximum is not None:
            unit_map = {
                "UTF8_BYTES": "byte",
                "CHARACTERS": "character",
                "PLATFORM_COUNTER": None,
            }
            unit = unit_map[str(spec.get("max_length_unit") or "PLATFORM_COUNTER")]
            unit_text = "" if unit is None else f" unit={quoteattr(unit)}"
            rules.append(f'      <rule name="maxLengthRule" value={quoteattr(str(maximum))}{unit_text}/>')
        if rules:
            rows.append("    <rules>")
            rows.extend(rules)
            rows.append("    </rules>")
        rows.append("  </field>")
    rows.append("</itemSchema>")
    raw_xml = root / f"{stem}-schema.xml"
    official = root / f"{stem}-official-normalized.json"
    dynamic = root / f"{stem}-current-schema.json"
    # This helper is test-only. Some fixture tests deliberately rebuild their
    # entire evidence graph after mutating one input, so remove only these
    # three exact fixture artifacts before regenerating them.
    for artifact in (raw_xml, official, dynamic):
        artifact.unlink(missing_ok=True)
    raw_xml.write_text("\n".join(rows) + "\n", encoding="utf-8")
    parse_schema_xml(
        xml_path=raw_xml,
        output=official,
        capture_id=f"{stem}-official-capture",
        run_id=run_id,
        target_model=target_model,
        template_product_id=template_product_id,
        category_id=category_id,
        product_type=product_type,
        captured_at=_timestamp(captured_at),
        valid_minutes=valid_minutes,
    )
    normalize_capture(
        raw_payload=raw_xml,
        official_schema=official,
        output=dynamic,
        run_id=run_id,
        target_model=target_model,
        template_product_id=template_product_id,
        category_id=category_id,
        product_type=product_type,
        source_kind="OFFICIAL_SCHEMA_GET",
        source_endpoint="https://eco.taobao.com/router/rest",
        source_operation="alibaba.icbu.product.schema.get",
    )
    return dynamic, official, raw_xml
