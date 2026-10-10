"""离线统一边界验收，加一次读取实际订单库的跨模块会话检查。"""
import asyncio
import hashlib
import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from core_helpers import NOW, ScriptedModel, WorkspaceTemp, catalog, constraint, patch, update
from integration.contracts import SCHEMAS
from integration.identity import DEMO_BINDINGS, IdentityBridge
from integration.order_adapter import LegacyOrderRepository, OrderQueryAdapter
from integration.service import UnifiedChatService
from integration.store import UnifiedSessionStore
from runtime.recommendation import RecommendationModule


async def smoke():
    with WorkspaceTemp() as folder:
        store = UnifiedSessionStore(Path(folder) / "unified.sqlite3")
        identities = IdentityBridge(store)
        for principal, external in DEMO_BINDINGS.items():
            identities.bind(principal, external, enabled=True)
        legacy = ROOT.parent / "snack_customer_service"
        adapter = OrderQueryAdapter(legacy, LegacyOrderRepository(legacy / "database/snack_orders_demo.sqlite"), identities)
        model = ScriptedModel(patch([update(constraints=[constraint("package", "wrapped", source_text="独立包装")])]), patch())
        service = UnifiedChatService(RecommendationModule(catalog(), model), store, clock=lambda: NOW)
        service.modules.register(adapter, adapter.verify)
        trusted = {"owner_id": "unification_check", "user_id": "U_NEW", "channel": "web"}
        cid, revision, steps = None, 0, []
        for message, target in (("独立包装的零食", None), ("查询 SN-DEMO-0001", "order_query"), ("继续之前的推荐", None)):
            request = {"schema_version": "1.0", "conversation_id": cid, "request_id": "check_" + uuid.uuid4().hex,
                "expected_revision": revision, "message": message, "selected_reference": None}
            result = await service.handle(request, trusted, target_module=target)
            if result["status"] != "completed":
                raise AssertionError("跨模块检查失败：" + json.dumps(result, ensure_ascii=False))
            cid, revision = result["conversation_id"], result["revision"]
            steps.append({"module": result["module"], "status": result["status"], "revision": revision})
        state, history = store.session(cid, trusted["owner_id"])
        if set(state["module_states"]) != {"product_recommendation", "order_query"} or len(history) != 6:
            raise AssertionError("统一状态或历史不一致")
        return {"passed": True, "steps": steps, "history_messages": len(history),
            "order_source": "existing_demo_database_readonly", "live_model_used": False,
            "model_extraction": "scripted_need_patch_for_boundary_check", "logistics_api_called": False, "human_api_called": False}


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    log = ROOT / ".local/unification-tests.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    integration_tests = [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("test_") and "test_unification." in line]
    try:
        smoke_result = asyncio.run(smoke()) if result.wasSuccessful() else {"passed": False, "reason": "tests_failed"}
    except (AssertionError, OSError, ValueError) as exc:
        smoke_result = {"passed": False, "reason": str(exc)}
    # 可读的扩展协议独立导出，推荐项目原 schemas/ 和生成器不改动。
    exports = ROOT / "integration/schemas"
    exports.mkdir(exist_ok=True)
    for name, schema in SCHEMAS.items():
        (exports / (name + ".schema.json")).write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tracked = ("runtime/service.py", "runtime/recommendation.py", "runtime/pricing.py", "runtime/model.py", "runtime/module_interface.py")
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "all_passed": result.wasSuccessful() and smoke_result["passed"],
        "automated_tests": {"run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "unification_tests": len(integration_tests)},
        "readonly_cross_module_smoke": smoke_result, "web_routing_enabled": False,
        "existing_core_sha256_at_verification": {rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() for rel in tracked},
        "live_model_accuracy_verified": False, "full_integration_completed": False}
    (ROOT / "acceptance/unification-execution.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["all_passed"]:
        print("详细失败信息见 .local/unification-tests.log", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
