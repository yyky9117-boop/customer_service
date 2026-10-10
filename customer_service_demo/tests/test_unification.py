"""初步统一的真实数据库和会话边界检查；不调用在线模型。"""
import copy
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

from integration.contracts import ContractViolation, validate_response, validate_transition
from integration.identity import IdentityBridge
from integration.order_adapter import LegacyOrderRepository, OrderQueryAdapter
from integration.registry import ModuleRegistry
from integration.service import UnifiedChatService
from integration.store import UnifiedSessionStore
from runtime.errors import ServiceFailure
from runtime.recommendation import RecommendationModule
from runtime.validation import validate_response as original_response
from core_helpers import NOW, ScriptedModel, WorkspaceTemp, catalog, constraint, patch, update
from test_service import request

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT.parent / "snack_customer_service"
TRUSTED = {"owner_id": "browser_a", "user_id": "U_NEW", "channel": "web"}


def create_orders(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE users(user_id TEXT PRIMARY KEY,is_demo INTEGER);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY,user_id TEXT,user_phonenumber TEXT,
                order_status TEXT,shipping_status TEXT,amount_fen INTEGER,created_at TEXT);
            CREATE TABLE order_items(item_id TEXT PRIMARY KEY,order_id TEXT,sku_id TEXT,item_title TEXT,quantity INTEGER);
            CREATE TABLE shipments(shipment_id TEXT PRIMARY KEY,order_id TEXT,tracking_number TEXT,
                carrier_name TEXT,logistics_status TEXT,latest_status_desc TEXT,estimated_delivery_at TEXT);
            INSERT INTO users VALUES('demo_user_a',1),('demo_user_b',1);
            INSERT INTO orders VALUES('SN-DEMO-0001','demo_user_a','13800000001','paid','unshipped',2990,'2026-10-01');
            INSERT INTO orders VALUES('SN-DEMO-0002','demo_user_a','13800000001','paid','shipped',4990,'2026-10-02');
            INSERT INTO orders VALUES('SN-DEMO-0010','demo_user_b','13800000002','completed','shipped',1990,'2026-10-03');
            INSERT INTO order_items VALUES('i1','SN-DEMO-0001','OLD-SKU-1','原味薯片',1),
                ('i2','SN-DEMO-0002','OLD-SKU-2','五香牛肉干',1),('i10','SN-DEMO-0010','OLD-SKU-10','曲奇',1);
            INSERT INTO shipments VALUES('s2','SN-DEMO-0002','DEMO-TRACK-1002','模拟快递','delivered','旧的已签收轨迹','2026-10-08');
        """)


class UnificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = UnifiedSessionStore(self.path / "sessions.sqlite3")
        self.identities = IdentityBridge(self.store)
        self.identities.bind("U_NEW", "demo_user_a", enabled=True)
        self.identities.bind("U_RETURN", "demo_user_b", enabled=True)
        self.order_db = self.path / "orders.sqlite3"
        create_orders(self.order_db)
        self.repository = LegacyOrderRepository(self.order_db)
        self.adapter = OrderQueryAdapter(LEGACY, self.repository, self.identities)
        self.model = ScriptedModel(patch([update(constraints=[constraint("package", "wrapped")])]), patch())
        self.rec = RecommendationModule(catalog(), self.model)
        self.service = UnifiedChatService(self.rec, self.store, clock=lambda: NOW)
        self.service.modules.register(self.adapter, self.adapter.verify)

    async def order(self, req=None, trusted=None):
        return await self.service.handle(req or request(message="查订单 SN-DEMO-0001"),
            trusted or TRUSTED, target_module="order_query")

    async def test_readonly_order_uses_common_response_and_shared_history(self):
        before = self.order_db.read_bytes()
        result = await self.order()
        self.assertEqual((result["module"], result["status"], result["revision"]), ("order_query", "completed", 1))
        self.assertEqual(result["message"]["cards"], [])
        self.assertIn("尚未发货", result["message"]["content"])
        self.assertEqual(self.order_db.read_bytes(), before)
        state, history = self.store.session(result["conversation_id"], "browser_a")
        self.assertEqual(len(history), 2)
        self.assertEqual(state["module_states"]["order_query"]["selected_order_id"], "SN-DEMO-0001")
        self.assertEqual(len(self.model.calls), 0)
        validate_response(result)
        with self.assertRaises(ContractViolation):
            original_response(result)

    async def test_default_stays_recommendation(self):
        result = await self.service.handle(request(), TRUSTED)
        self.assertEqual((result["module"], result["status"]), ("product_recommendation", "completed"))
        original_response(result)

    async def test_recommend_order_recommend_preserves_both_states(self):
        first = await self.service.handle(request(), TRUSTED)
        self.assertEqual(first["status"], "completed", first)
        before = self.store.session(first["conversation_id"], "browser_a")[0]
        second = await self.order(request("req_2", first["conversation_id"], 1, "查订单 SN-DEMO-0001"))
        self.assertEqual(second["status"], "completed", second)
        middle = self.store.session(first["conversation_id"], "browser_a")[0]
        self.assertEqual(before["module_states"]["product_recommendation"], middle["module_states"]["product_recommendation"])
        third = await self.service.handle(request("req_3", first["conversation_id"], 2, "继续刚才的推荐"), TRUSTED)
        self.assertEqual(third["status"], "completed", third)
        after, history = self.store.session(first["conversation_id"], "browser_a")
        self.assertEqual(after["module_states"]["order_query"], middle["module_states"]["order_query"])
        self.assertEqual(len(history), 6)
        self.assertEqual(len(self.model.calls), 2)

    async def test_replay_does_not_add_history(self):
        req = request(message="SN-DEMO-0001")
        first = await self.order(req)
        self.assertEqual(await self.order(req), first)
        self.assertEqual(len(self.store.session(first["conversation_id"], "browser_a")[1]), 2)

    async def test_same_request_id_other_module_is_conflict(self):
        req = request(message="SN-DEMO-0001")
        await self.order(req)
        result = await self.service.handle(req, TRUSTED)
        self.assertEqual(result["error"]["code"], "request_id_conflict")
        self.assertEqual(len(self.model.calls), 0)

    async def test_stale_revision_does_not_query_order(self):
        first = await self.order()
        self.repository.snapshot = lambda uid: self.fail("过期请求不能访问订单库")
        result = await self.order(request("req_2", first["conversation_id"], 0, "发货了吗"))
        self.assertEqual((result["error"]["code"], result["revision"]), ("revision_conflict", 1))

    async def test_followup_reuses_selected_order(self):
        first = await self.order()
        result = await self.order(request("req_2", first["conversation_id"], 1, "发货了吗"))
        self.assertIn("SN-DEMO-0001", result["message"]["content"])

    async def test_multiple_orders_then_explicit_selection(self):
        first = await self.order(request(message="查手机号13800000001的订单"))
        self.assertEqual(first["status"], "needs_clarification", first)
        self.assertIn("SN-DEMO-0002", first["clarification"]["question"])
        second = await self.order(request("req_2", first["conversation_id"], 1, "SN-DEMO-0002"))
        self.assertEqual(second["status"], "completed", second)

    async def test_guest_and_unbound_identity_do_not_access_orders(self):
        for uid in ("U_GUEST", "U_VIP"):
            result = await self.order(trusted={**TRUSTED, "user_id": uid})
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["conversation_id"])

    async def test_disabled_mapping_is_not_fallback_account(self):
        self.identities.bind("U_NEW", "demo_user_a", enabled=False)
        result = await self.order()
        self.assertEqual(result["status"], "error")
        self.assertIn("尚未启用", result["message"]["content"])

    async def test_order_and_phone_cannot_expand_ownership(self):
        for msg in ("SN-DEMO-0010", "手机号13800000002的订单"):
            result = await self.order(request("req_" + str(len(msg)), message=msg))
            self.assertEqual(result["status"], "no_match", result)
            self.assertNotIn("曲奇", result["message"]["content"])

    async def test_other_owner_cannot_reuse_conversation(self):
        first = await self.order()
        result = await self.order(request("req_2", first["conversation_id"], 1), {**TRUSTED, "owner_id": "browser_b"})
        self.assertEqual(result["status"], "error")

    async def test_same_owner_other_profile_cannot_reuse_conversation(self):
        first = await self.order()
        result = await self.order(request("req_2", first["conversation_id"], 1), {**TRUSTED, "user_id": "U_RETURN"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.store.session(first["conversation_id"], "browser_a")[1]), 2)

    async def test_cached_logistics_never_claimed_live(self):
        result = await self.order(request(message="快递 DEMO-TRACK-1002 到哪了"))
        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("旧的已签收轨迹", result["message"]["content"])
        self.assertIn("尚未调用实时物流接口", result["message"]["content"])
        debug = self.store.diagnostics(request(message="快递 DEMO-TRACK-1002 到哪了"), "browser_a")
        self.assertFalse(debug["live_logistics_queried"])
        shipment = debug["legacy_result"]["orders"][0]["shipments"][0]
        self.assertEqual((shipment["data_source"], shipment["tracking_events"]), ("not_queried", []))

    async def test_missing_db_is_error_and_not_created(self):
        self.repository.path = self.path / "missing.sqlite3"
        result = await self.order()
        self.assertEqual(result["error"]["code"], "data_unavailable")
        self.assertFalse(self.repository.path.exists())

    async def test_switch_order_does_not_reuse_previous_selection(self):
        first = await self.order()
        result = await self.order(request("req_2", first["conversation_id"], 1, "换个订单"))
        self.assertEqual(result["status"], "needs_clarification")
        state, _ = self.store.session(first["conversation_id"], "browser_a")
        self.assertIsNone(state["module_states"]["order_query"]["selected_order_id"])

    async def test_product_card_reference_is_rejected_by_order(self):
        first = await self.service.handle(request(), TRUSTED)
        card = first["message"]["cards"][0]
        result = await self.order(request("req_2", first["conversation_id"], 1, "查订单",
            {"message_id": first["message"]["id"], "plan_id": card["plan_id"], "sku_id": None}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["revision"], 1)

    async def test_reply_generator_only_runs_for_recommendation(self):
        class Enricher:
            calls = 0
            async def enrich(self, result, context):
                self.calls += 1
                return result
        enrich = Enricher()
        self.service.reply_generator = enrich
        await self.order()
        await self.service.handle(request("req_rec"), TRUSTED)
        self.assertEqual(enrich.calls, 1)

    async def test_other_module_state_cannot_be_removed_on_commit(self):
        first = await self.service.handle(request(), TRUSTED)
        before = self.store.session(first["conversation_id"], "browser_a")[0]
        req = request("req_2", first["conversation_id"], 1, "SN-DEMO-0001")
        second = await self.order(req)
        state = self.store.session(first["conversation_id"], "browser_a")[0]
        state["module_states"].pop("product_recommendation")
        with self.assertRaises(ContractViolation):
            validate_transition(before, req, second, state)

    def test_registration_requires_verifier_and_preserves_default(self):
        registry = ModuleRegistry(self.rec, lambda result, context: None)
        with self.assertRaises(ValueError):
            registry.register(self.adapter, None)
        with self.assertRaises(ServiceFailure):
            registry.resolve("order_query")
        registry.register(self.adapter, self.adapter.verify)
        self.assertIs(registry.resolve().module, self.rec)
        with self.assertRaises(ValueError):
            registry.register(self.adapter, self.adapter.verify)

    def test_no_sys_path_or_legacy_global_import_pollution(self):
        before = list(sys.path)
        OrderQueryAdapter(LEGACY, self.repository, self.identities)
        self.assertEqual(sys.path, before)
        self.assertNotIn("nodes.query", sys.modules)

    def test_mapping_cannot_silently_change_existing_user(self):
        with self.assertRaises(ValueError):
            self.identities.bind("U_NEW", "demo_user_b", enabled=True)
        with self.assertRaises(ValueError):
            self.identities.bind("U_GUEST", "demo_user_a")
        self.assertEqual(self.identities.resolve(TRUSTED), "demo_user_a")

    def test_mapping_schema_upgrade_preserves_existing_sessions(self):
        path = self.path / "old.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY,owner TEXT,state TEXT,history TEXT);"
                "INSERT INTO sessions VALUES('old','old-owner','{}','[]');")
        from runtime.service import SessionStore
        bridge = IdentityBridge(SessionStore(path))
        bridge.bind("U_NEW", "demo_user_a")
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT * FROM sessions").fetchone(), ("old", "old-owner", "{}", "[]"))
            self.assertEqual(db.execute("SELECT enabled FROM module_identity_bindings").fetchone()[0], 0)

    def test_recommendation_completed_still_requires_cards(self):
        payload = json.loads((ROOT / "examples/response-completed.json").read_text(encoding="utf-8"))
        payload["message"]["cards"] = []
        with self.assertRaises(ContractViolation):
            validate_response(payload)
