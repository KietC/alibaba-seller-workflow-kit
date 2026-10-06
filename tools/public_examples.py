"""Generate and run offline examples containing only fictional product data.
生成并执行仅含虚构产品数据的离线示例。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(output: Path) -> dict:
    output = output.resolve()
    try:
        output.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise ValueError("Keep generated data outside the Git/source directory")
    output.mkdir(parents=True, exist_ok=True)
    # Explicit synthetic inputs prevent automatic discovery of private workspaces.
    # 显式指定虚构输入，避免自动发现私有工作区。
    product = {"canonical_model": "SYNTHETIC1", "name": "Stainless Steel Knee Operated Hand Wash Sink - Wall Mount",
               "category": "Wash Sinks", "attributes": [
                   {"name": "Material", "raw_value": "SUS304"},
                   {"name": "Installation", "raw_value": "Wall Mount"},
                   {"name": "Shape", "raw_value": "Single Bowl Rectangular"},
                   {"name": "Size", "raw_value": "500 x 400 x 395 mm"}]}
    official, catalog, disposition = (output / n for n in ("synthetic-products.jsonl", "synthetic-catalog.jsonl", "synthetic-disposition.jsonl"))
    official.write_text(json.dumps(product) + "\n", encoding="utf-8")
    catalog.write_text("", encoding="utf-8")
    disposition.write_text("", encoding="utf-8")
    plan = {"campaign_name": "Synthetic product inquiry demo", "segment": {"source": "ALIBABA_SYSTEM_GROUP", "name": "近30天询盘买家", "live_count": 1},
            "channel": "ALIYUN_DIRECTMAIL", "channel_ready": True,
            "subject": "Commercial stainless steel product options",
            "body_text": "Hello, here is a concise comparison of commercial stainless steel product options for your sourcing needs.",
            "claims_reviewed": True, "recipient_data_included": False,
            "final_send_allowed": False, "schedule": "IMMEDIATE",
            "cta": {"text": "View fictional collection", "url": "https://catalog.example.invalid/"}}
    plan_path = output / "synthetic-edm-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    for name in list(env):
        if name.endswith(("ALIBABA_CONFIG", "ALIBABA_WORKSPACE")):
            env.pop(name)
    commands = {
        "rfq": [sys.executable, str(ROOT / ".agents/skills/alibaba-rfq-light/scripts/rfq_match.py"),
                "--rfq-text", "SUS304 knee operated single bowl rectangular hand wash sink, wall mount 500 x 400 x 395 mm.",
                "--official-current", str(official), "--catalog-index", str(catalog), "--disposition", str(disposition)],
        "edm": [sys.executable, str(ROOT / ".agents/skills/alibaba-edm-light/scripts/validate_campaign.py"),
                "--input", str(plan_path), "--receipt", str(output / "synthetic-edm-receipt.json")],
    }
    results = []
    with tempfile.TemporaryDirectory(prefix="sellerkit-demo-") as cwd:
        for name, command in commands.items():
            process = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            (output / f"{name}-stdout.txt").write_text(process.stdout, encoding="utf-8")
            (output / f"{name}-stderr.txt").write_text(process.stderr, encoding="utf-8")
            results.append({"name": name, "exit_code": process.returncode})
    result = {"status": "PASS_OFFLINE_EXAMPLES" if all(r["exit_code"] == 0 for r in results) else "FAILED_OFFLINE_EXAMPLES",
              "synthetic_only": True, "network_actions": 0, "business_actions": 0, "commands": results}
    (output / "example-receipt.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.output)
    except (ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"].startswith("PASS") else 1)
