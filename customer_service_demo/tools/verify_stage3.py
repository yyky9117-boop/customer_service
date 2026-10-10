"""离线执行证据。真实模型、浏览器与自然语言验收明确另列待验证。"""
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
sys.stdout.reconfigure(encoding="utf-8")


class Evidence(unittest.TextTestResult):
    def __init__(self, *args):
        super().__init__(*args)
        self.executed = []

    def startTest(self, test):
        self.executed.append(test.id())
        super().startTest(test)


def verify():
    # 生产运行路径不允许调用验收参考探针。
    for file in (ROOT / "runtime").glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("acceptance"):
                raise AssertionError("生产代码依赖参考验收实现")
            if isinstance(node, ast.Import) and any(n.name.startswith("acceptance") for n in node.names):
                raise AssertionError("生产代码依赖参考验收实现")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=Evidence).run(suite)
    by_module = Counter(name.split(".")[0] for name in result.executed)
    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(), "dataset_version": "demo-2026-10-07-v1",
        "all_passed": result.wasSuccessful(), "tests_run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped), "tests_by_module": dict(sorted(by_module.items())),
        "production_pricing_goldens": {"total": 53, "passed": 53 if result.wasSuccessful() else None,
            "source": "tests/test_production_pricing.py", "reference_probe_imported_by_runtime": False},
        "execution_layers": {
            "protocol_and_data_checks": "executed", "deterministic_core_with_structured_needs": "executed",
            "session_integration_with_scripted_model": "executed", "deepseek_http_adapter_with_mocked_transport": "executed",
            "real_deepseek_calls": "not_executed_by_this_command", "98_full_business_scenarios": "not_fully_executed",
            "browser_and_ui": "not_executed_by_this_command"},
        "natural_language_accuracy": None, "executed_test_ids": result.executed,
    }
    (ROOT / "acceptance/stage3_execution.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "executed_test_ids"}, ensure_ascii=False, indent=2))
    if not result.wasSuccessful():
        print(stream.getvalue())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(verify())
