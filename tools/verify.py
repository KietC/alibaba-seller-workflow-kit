"""Run isolated, offline Python regression groups; report skips separately.
隔离执行离线 Python 回归测试，跳过项单独报告。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    "draft": ".agents/skills/alibaba-similar-draft/scripts",
    "rfq": ".agents/skills/alibaba-rfq-light/scripts",
    "edm": ".agents/skills/alibaba-edm-light/scripts",
    "legacy-product-chain": "src/legacy-product-chain/tests",
    "review-ready": "src/review-ready-workspace/tests",
    "release-tools": "tests",
}
BOOTSTRAP = r'''
import json, sys, unittest
from pathlib import Path
folder = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(folder))
suite = unittest.TestLoader().discover(str(folder), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=1).run(suite)
summary = {"tests_run": result.testsRun, "failures": len(result.failures),
           "errors": len(result.errors), "skips": len(result.skipped),
           "skipped_methods": sum(isinstance(test, unittest.TestCase) for test, _ in result.skipped),
           "skipped_setups": sum(not isinstance(test, unittest.TestCase) for test, _ in result.skipped),
           "skip_reasons": [str(reason) for _, reason in result.skipped]}
print("SELLERKIT_RESULT=" + json.dumps(summary))
sys.exit(0 if result.wasSuccessful() else 1)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--groups", nargs="+", choices=tuple(GROUPS), help="Run selected groups only; default is all / 仅运行指定组，默认全部")
    args = parser.parse_args()
    output = (args.output or Path(tempfile.mkdtemp(prefix="sellerkit-tests-"))).resolve()
    try:
        output.relative_to(ROOT)
    except ValueError:
        output.mkdir(parents=True, exist_ok=True)
    else:
        parser.error("test logs must be outside the public source tree")
    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    # Clear all workspace discovery overrides before synthetic tests run.
    # 虚构测试前清除所有工作区自动发现环境变量。
    for key in list(env):
        if key.endswith(("ALIBABA_CONFIG", "ALIBABA_WORKSPACE")):
            env.pop(key)

    def group(item):
        name, folder = item
        with tempfile.TemporaryDirectory(prefix=f"sellerkit-{name}-") as cwd:
            process = subprocess.run([sys.executable, "-c", BOOTSTRAP, str(ROOT / folder)],
                                     cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=900)
        (output / f"{name}.stdout.txt").write_text(process.stdout, encoding="utf-8")
        (output / f"{name}.stderr.txt").write_text(process.stderr, encoding="utf-8")
        lines = [line.removeprefix("SELLERKIT_RESULT=") for line in process.stdout.splitlines() if line.startswith("SELLERKIT_RESULT=")]
        result = json.loads(lines[-1]) if lines else {"tests_run": 0, "failures": 0, "errors": 1, "skips": 0}
        result.update(name=name, exit_code=process.returncode)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    selected = [(name, GROUPS[name]) for name in (args.groups or GROUPS)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 6))) as pool:
        results = list(pool.map(group, selected))
    result = {"status": "PASS_OFFLINE_REGRESSIONS" if all(r["exit_code"] == 0 for r in results) else "FAILED_OFFLINE_REGRESSIONS",
              "groups": results, "tests_run": sum(r["tests_run"] for r in results),
              "skips": sum(r["skips"] for r in results), "business_actions": 0,
              "final_write_enabled": False, "browser_integration_tested": False}
    (output / "test-receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "groups"}, indent=2))
    print(f"Local logs: {output}")
    return 0 if result["status"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
