"""第四步核验记录：离线测试与独立浏览器证据分别计数。不会调用真实模型。"""
import ast
import io
import json
import sys
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Evidence(unittest.TextTestResult):
    def __init__(self, *args):
        super().__init__(*args)
        self.executed = []

    def startTest(self, test):
        self.executed.append(test.id())
        super().startTest(test)


def verify():
    for file in (ROOT / "runtime").glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("acceptance"):
                raise AssertionError("生产运行路径不得导入验收探针")
            if isinstance(node, ast.Import) and any(n.name.startswith("acceptance") for n in node.names):
                raise AssertionError("生产运行路径不得导入验收探针")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=Evidence).run(suite)
    browser = {}
    for phase in ("welcome", "live", "faults"):
        source = ROOT / "acceptance" / f"stage4-browser-{phase}.json"
        if source.exists():
            data = json.loads(source.read_text(encoding="utf-8"))
            browser[phase] = {"source": str(source.relative_to(ROOT)).replace("\\", "/"),
                "verified_at": data["verified_at"], "passed": data["all_passed"],
                "check_count_including_screenshots": len(data["checks"]),
                "real_model_used": data["real_model_used"], "fault_injection_used": data["fault_injection_used"]}
        else:
            browser[phase] = {"passed": False, "status": "not_executed"}
    # 53 个基准由生产计价测试实际逐项核对，单独保留其数量口径。
    golden_tests = [name for name in result.executed if "test_production_pricing" in name and "golden" in name]
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "dataset_version": "demo-2026-10-07-v1",
        "all_passed": result.wasSuccessful() and all(item["passed"] for item in browser.values()),
        "automated_tests": {"run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "by_module": dict(sorted(Counter(n.split('.')[0] for n in result.executed).items()))},
        "production_pricing_goldens": {"count": 53, "passed": bool(golden_tests) and result.wasSuccessful(),
            "test_ids": golden_tests, "reference_probe_imported_by_runtime": False},
        "browser_evidence": browser,
        "execution_layers": {"protocol_data_core_session_and_http": "executed",
            "live_browser_to_deepseek_to_catalog_to_cards": "limited_scenarios_executed",
            "browser_faults": "injected_faults_executed", "98_full_business_scenarios": "not_fully_executed"},
        "natural_language_accuracy": None, "executed_test_ids": result.executed}
    (ROOT / "acceptance/stage4_execution.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "executed_test_ids"}, ensure_ascii=False, indent=2))
    if not result.wasSuccessful():
        print(stream.getvalue())
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(verify())
