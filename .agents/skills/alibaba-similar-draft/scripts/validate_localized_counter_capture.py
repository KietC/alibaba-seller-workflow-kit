from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from capture_derivation import object_sha256, validate_capture_integrity


SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
CAPTURE_SCHEMA = SCHEMA_DIR / "browser-observation-capture.schema.json"
EXPECTATION_SCHEMA = SCHEMA_DIR / "localized-counter-expectation.schema.json"
RECEIPT_SCHEMA = SCHEMA_DIR / "localized-counter-verification-receipt.schema.json"
MAX_CAPTURE_AGE = timedelta(minutes=15)
FUTURE_SKEW = timedelta(seconds=5)
RTL = {"ar", "arc", "ckb", "dv", "fa", "he", "ks", "ku", "nqo", "ps", "sd", "syr", "ug", "ur", "yi"}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes, list[str]]:
    try:
        raw = path.resolve(strict=True).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, b"", [f"cannot read {label}: {exc}"]
    if not isinstance(value, dict):
        return {}, raw, [f"{label} must be a JSON object"]
    return value, raw, []


def schema_errors(value: dict[str, Any], schema_path: Path, label: str) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    return [f"{label} schema {'.'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}" for error in errors]


def parse_time(value: Any, label: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(timezone.utc)
    except ValueError:
        errors.append(f"{label} is not an RFC3339 timestamp")
        return None


def expected_direction(locale: str) -> str:
    return "RTL" if locale.split("-", 1)[0].lower() in RTL else "LTR"


def locale_semantic_key(locale: str) -> str:
    """Return a comparison key only; raw locale spelling remains contract data.

    JSON Schema rejects underscores and non-BCP47 punctuation, while NFKC plus
    casefold catches otherwise-valid aliases such as ``en``/``EN`` and
    ``zh-CN``/``zh-cn``.  The verifier still requires the raw locale sets to
    match exactly, so normalization can never silently rewrite a target.
    """

    return unicodedata.normalize("NFKC", locale.strip()).casefold()


def field_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "locale": row.get("locale"), "language_label": row.get("language_label"),
        "direction": row.get("direction"), "value_sha256": row.get("value_sha256"),
        "observed_count": row.get("observed_count"),
        "raw_counter_text": row.get("raw_counter_text"),
        "maximum": row.get("maximum"), "unit": row.get("unit"),
        "render_sha256": row.get("render_sha256"),
        "control_sha256": row.get("control_sha256"),
    }


def verify_counter_capture(
    *, capture_path: Path, expectation_path: Path, now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    capture, capture_raw, errors = read_json(capture_path, "browser observation capture")
    expectation, expectation_raw, expectation_errors = read_json(expectation_path, "counter expectation")
    errors.extend(expectation_errors)
    if capture:
        errors.extend(schema_errors(capture, CAPTURE_SCHEMA, "browser observation capture"))
        errors.extend(validate_capture_integrity(capture))
    if expectation:
        errors.extend(schema_errors(expectation, EXPECTATION_SCHEMA, "counter expectation"))

    for key in ("run_id", "target_model", "template_product_id", "browser_session_sha256", "page_fingerprint_sha256"):
        if capture.get(key) != expectation.get(key):
            errors.append(f"capture/expectation binding mismatch: {key}")
    captured_at = parse_time(capture.get("captured_at"), "capture captured_at", errors)
    if captured_at and captured_at > now + FUTURE_SKEW:
        errors.append("counter capture time is in the future")
    if captured_at and now - captured_at > MAX_CAPTURE_AGE:
        errors.append("counter capture is older than 15 minutes")

    expected_fields = [row for row in expectation.get("fields", []) if isinstance(row, dict)]
    declared_identities = expectation.get("field_identities_sha256")
    computed_identities = object_sha256([field_identity(row) for row in expected_fields])
    if declared_identities != computed_identities:
        errors.append("expectation field identities hash mismatch")
    expected_by_locale: dict[str, dict[str, Any]] = {}
    expected_semantic_locales: dict[str, str] = {}
    for row in expected_fields:
        locale = str(row.get("locale") or "")
        if locale in expected_by_locale:
            errors.append(f"duplicate expected locale: {locale}")
        semantic_locale = locale_semantic_key(locale)
        previous_locale = expected_semantic_locales.get(semantic_locale)
        if previous_locale is not None:
            errors.append(f"semantic duplicate expected locale: {previous_locale} conflicts with {locale}")
        else:
            expected_semantic_locales[semantic_locale] = locale
        expected_by_locale[locale] = row
        if row.get("value_sha256") != object_sha256(row.get("value")):
            errors.append(f"expected localized value hash mismatch: {locale}")
        if row.get("direction") != expected_direction(locale):
            errors.append(f"expected direction mismatch: {locale}")

    observed_rows = [row for row in capture.get("observations", []) if isinstance(row, dict) and row.get("observation_type") == "LOCALIZED_COUNTER"]
    observed_by_locale: dict[str, dict[str, Any]] = {}
    observed_semantic_locales: dict[str, str] = {}
    render_hashes: set[str] = set()
    control_hashes: set[str] = set()
    for row in observed_rows:
        locale = str(row.get("locale") or "")
        if locale in observed_by_locale:
            errors.append(f"duplicate observed locale: {locale}")
        semantic_locale = locale_semantic_key(locale)
        previous_locale = observed_semantic_locales.get(semantic_locale)
        if previous_locale is not None:
            errors.append(f"semantic duplicate observed locale: {previous_locale} conflicts with {locale}")
        else:
            observed_semantic_locales[semantic_locale] = locale
        observed_by_locale[locale] = row
        render_hash = str(row.get("render_sha256") or "")
        control_hash = str(row.get("control_sha256") or "")
        if render_hash in render_hashes:
            errors.append(f"render artifact reused across locales: {locale}")
        if control_hash in control_hashes:
            errors.append(f"control artifact reused across locales: {locale}")
        render_hashes.add(render_hash); control_hashes.add(control_hash)
    if set(observed_by_locale) != set(expected_by_locale):
        errors.append("localized counter capture locale set does not exactly match expectations")
    if set(observed_semantic_locales) != set(expected_semantic_locales):
        errors.append("localized counter capture semantic locale set does not match expectations")
    else:
        for semantic_locale, expected_locale in expected_semantic_locales.items():
            observed_locale = observed_semantic_locales.get(semantic_locale)
            if observed_locale != expected_locale:
                errors.append(
                    f"localized counter locale normalization mismatch: expected {expected_locale}, observed {observed_locale}"
                )

    results: list[dict[str, Any]] = []
    unit_map = {"UNICODE_CODE_POINTS": "CHARACTER", "UTF8_BYTES": "UTF8_BYTE", "PLATFORM_COUNTER": "PLATFORM_REPORTED"}
    for locale in sorted(expected_by_locale):
        expected = expected_by_locale[locale]
        observed = observed_by_locale.get(locale)
        row_errors: list[str] = []
        if observed is None:
            continue
        for expected_key, observed_key in (
            ("language_label", "language_label"), ("direction", "direction"),
            ("value", "value"), ("observed_count", "observed_count"),
            ("raw_counter_text", "raw_counter_text"), ("maximum", "maximum"),
            ("render_sha256", "render_sha256"), ("control_sha256", "control_sha256"),
        ):
            if observed.get(observed_key) != expected.get(expected_key):
                row_errors.append(f"{locale} {expected_key} mismatch")
        if observed.get("direction") != expected_direction(locale):
            row_errors.append(f"{locale} observed direction mismatch")
        expected_unit = unit_map.get(str(expected.get("unit") or ""))
        if observed.get("unit") != expected_unit:
            row_errors.append(f"{locale} counter unit mismatch")
        value = str(observed.get("value") or "")
        count = int(observed.get("observed_count") or 0)
        maximum = int(observed.get("maximum") or 0)
        if count > maximum:
            row_errors.append(f"{locale} observed count exceeds maximum")
        if expected_unit == "CHARACTER" and count != len(value):
            row_errors.append(f"{locale} character count mismatch")
        if expected_unit == "UTF8_BYTE" and count != len(value.encode("utf-8")):
            row_errors.append(f"{locale} UTF-8 byte count mismatch")
        numbers = [int(token) for token in re.findall(r"(?<![0-9])[0-9]+(?![0-9])", str(observed.get("raw_counter_text") or ""))]
        if numbers != [count, maximum]:
            if expected_unit == "PLATFORM_REPORTED":
                row_errors.append(f"{locale} platform counter text does not uniquely encode current count and maximum")
            else:
                row_errors.append(f"{locale} raw counter text does not uniquely encode observed count and maximum")
        errors.extend(row_errors)
        results.append({
            "locale": locale, "observation_id": str(observed.get("observation_id") or ""),
            "observed_count": count, "maximum": maximum, "unit": str(observed.get("unit") or ""),
            "render_sha256": str(observed.get("render_sha256") or ""),
            "control_sha256": str(observed.get("control_sha256") or ""),
            "status": "FAIL" if row_errors else "PASS",
        })

    errors = sorted(set(errors))
    receipt = {
        "schema_version": "sellerkit.alibaba-localized-counter-verification.v1",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "status": "BLOCKED_COUNTER_CAPTURE" if errors else "PASS_LOCAL_CONTRACT",
        "trust_status": "LOCAL_CONTRACT",
        "result_semantics": "CURRENT_CONTROL_COUNTER_CONSISTENCY_NOT_LIVE_ATTESTATION",
        "run_id": expectation.get("run_id") or None, "target_model": expectation.get("target_model") or None,
        "template_product_id": expectation.get("template_product_id") or None,
        "bindings": {
            "capture_sha256": sha256_bytes(capture_raw),
            "capture_body_sha256": str(capture.get("capture_body_sha256") or ""),
            "observation_ids_sha256": str(capture.get("observation_ids_sha256") or ""),
            "expectation_sha256": sha256_bytes(expectation_raw),
            "field_identities_sha256": computed_identities,
        },
        "locale_count": len(results), "field_results": results, "errors": errors,
        "write_authority": "NONE_READ_ONLY_EVIDENCE", "final_write_enabled": False,
    }
    receipt_errors = schema_errors(receipt, RECEIPT_SCHEMA, "counter verification receipt")
    if receipt_errors:
        receipt["errors"] = sorted(set(receipt["errors"] + receipt_errors))
        receipt["status"] = "BLOCKED_COUNTER_CAPTURE"
    return receipt


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve(); path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite receipt: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try: os.unlink(temporary)
        except OSError: pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate current-control localized counter captures (local contract only)")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--expectation", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.receipt.resolve().exists():
        print(json.dumps({
            "status": "BLOCKED_RECEIPT_EXISTS",
            "error": f"refusing to overwrite receipt: {args.receipt.resolve()}",
            "receipt_written": False, "final_write_enabled": False,
        }, ensure_ascii=False, sort_keys=True))
        return 3
    receipt = verify_counter_capture(capture_path=args.capture, expectation_path=args.expectation)
    try: atomic_write(args.receipt, receipt)
    except (OSError, FileExistsError) as exc:
        print(json.dumps({
            "status": "BLOCKED_RECEIPT_WRITE", "error": str(exc),
            "receipt_written": False, "final_write_enabled": False,
        }, ensure_ascii=False, sort_keys=True)); return 3
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 4 if receipt["status"] == "PASS_LOCAL_CONTRACT" else 3


if __name__ == "__main__":
    raise SystemExit(main())
