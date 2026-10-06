"""Regression checks for audited RFQ/EDM issues using synthetic CLI inputs only.

Draft approval and overlay regressions belong to the draft Skill's discovered
suite. This entry point exercises actual RFQ/EDM CLI decisions rather than
duplicating their decision logic. It neither reads business data nor sends mail.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RFQ = ROOT / ".agents/skills/alibaba-rfq-light/scripts/rfq_match.py"
EDM = ROOT / ".agents/skills/alibaba-edm-light/scripts/validate_campaign.py"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def run_rfq(root: Path, requested_operation: str, candidate_operation: str,
            disposition: dict | None) -> dict:
    official = root / "official.jsonl"
    catalog = root / "catalog.jsonl"
    overlay = root / "disposition.jsonl"
    write_json(official, {
        "canonical_model": "SYNTHETICSINK",
        "name": f"SUS304 {candidate_operation} Operated Single Bowl Hand Wash Sink Wall Mount",
        "category": "Wash Sinks",
        "attributes": [{"name": "Dimensions", "raw_value": "500 x 400 x 395 mm"}],
    })
    write_json(catalog, {"product_id": "999900000001", "model": "SYNTHETICSINK",
                         "currency": "USD", "sku_min_price": 1, "sku_max_price": 2,
                         "min_order_quantity": 1})
    overlay.write_text("" if disposition is None else json.dumps(disposition) + "\n", encoding="utf-8")
    result = subprocess.run([
        sys.executable, "-B", str(RFQ), "--rfq-text",
        f"SUS304 {requested_operation} operated single bowl hand washing sink wall mount, 500 x 400 x 395 mm",
        "--official-current", str(official), "--catalog-index", str(catalog),
        "--disposition", str(overlay),
    ], capture_output=True, encoding="utf-8")
    if result.returncode:
        raise AssertionError(f"RFQ CLI failed: {result.stderr}")
    return json.loads(result.stdout)


def run_edm(root: Path, plan: object) -> tuple[int, dict]:
    source = root / "plan.json"
    receipt = root / "receipt.json"
    if receipt.exists():
        receipt.unlink()  # Only this newly generated synthetic tempfile receipt.
    write_json(source, plan)
    result = subprocess.run([sys.executable, "-B", str(EDM), "--input", str(source),
                             "--receipt", str(receipt)], capture_output=True, encoding="utf-8")
    if not receipt.is_file():
        raise AssertionError(f"EDM CLI did not produce receipt: {result.stderr}")
    return result.returncode, json.loads(receipt.read_text(encoding="utf-8-sig"))


def main() -> None:
    checks = {}
    with tempfile.TemporaryDirectory(prefix="alibaba-fixed-regression-") as directory:
        root = Path(directory)
        for requested, candidate in (("foot", "knee"), ("knee", "foot")):
            result = run_rfq(root, requested, candidate, None)
            best = result["candidates"][0]
            assert best["mismatches"] and best["mode"] == "REVIEW", result
            assert result["recommended_action"] != "FILL_TO_SUBMIT", result
            checks[f"rfq_{requested}_request_{candidate}_candidate"] = "BLOCKED_FROM_AUTO_FILL"

        for status in (None, "BLOCKED_UNCLASSIFIED", "HISTORICAL_REFERENCE_ONLY"):
            disposition = None if status is None else {
                "product_id": "999900000001", "record_disposition": status,
                **({"allow_rag": True} if status == "BLOCKED_UNCLASSIFIED" else {}),
            }
            result = run_rfq(root, "foot", "foot", disposition)
            assert result["candidates"][0]["historical_price_references"] == [], result
        checks["rfq_missing_unknown_or_unpermitted_disposition"] = "NO_REFERENCE"
        eligible = run_rfq(root, "foot", "foot", {
            "product_id": "999900000001", "record_disposition": "HISTORICAL_REFERENCE_ONLY",
            "allow_rag": True,
        })
        assert eligible["recommended_action"] == "FILL_TO_SUBMIT", eligible
        assert len(eligible["candidates"][0]["historical_price_references"]) == 1, eligible
        checks["rfq_exact_eligible_positive_control"] = "REFERENCE_ONLY_SHORTLIST"

        base = {
            "campaign_name": "Synthetic local regression",
            "segment": {"source": "ALIBABA_SYSTEM_GROUP", "name": "近30天询盘买家", "live_count": 1},
            "channel": "ALIYUN_DIRECTMAIL", "channel_ready": True,
            "subject": "Commercial faucet collection",
            "body_text": "Hello, this synthetic non-customer content verifies a local campaign plan without sending any email.",
            "claims_reviewed": True, "recipient_data_included": False, "final_send_allowed": False,
        }
        code, receipt = run_edm(root, base)
        assert code == 0, receipt
        checks["edm_valid_plan_positive_control"] = "LOCAL_PLAN_ONLY_PASS"
        for plan in (
            {**base, "notes": {"customer_email": "audit@example.invalid"}},
            {**base, "notes": {"smtp_password": "NOT_A_REAL_SECRET"}},
            {**base, "cta": {"text": "Synthetic", "url": "https://example.invalid/?email=audit@example.invalid"}},
        ):
            code, receipt = run_edm(root, plan)
            assert code != 0 and receipt.get("errors"), receipt
        checks["edm_entire_plan_privacy"] = "BLOCKED"
        for plan in ({**base, "channel": []}, {**base, "segment": {**base["segment"], "name": []}}):
            code, receipt = run_edm(root, plan)
            assert code != 0 and receipt.get("errors"), receipt
        checks["edm_malformed_enum_receipt"] = "BLOCKED_RECEIPT_CREATED"
        for url in ("https://", "https://audit-user:NOT_A_REAL_SECRET@example.invalid/path"):
            code, receipt = run_edm(root, {**base, "cta": {"text": "Synthetic", "url": url}})
            assert code != 0 and receipt.get("errors"), receipt
        checks["edm_invalid_or_credential_cta"] = "BLOCKED"

    print(json.dumps({
        "status": "PASS_FIXED_RFQ_EDM_REGRESSIONS_SYNTHETIC_ONLY",
        "private_data_read": False, "business_network_request_count": 0,
        "browser_action_count": 0, "final_write_enabled": False,
        "checks": checks,
        "scope_note": "Actual local CLI inputs only; no RFQ filled/submitted or EDM sent. Draft regressions run separately.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
