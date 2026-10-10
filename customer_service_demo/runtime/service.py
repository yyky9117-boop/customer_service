"""统一会话入口，SQLite 原子提交与幂等重放。等待模型时不持有数据库锁。"""
import copy
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from .errors import ServiceFailure
from .module_interface import ModuleContext
from .validation import ContractViolation, validate_request, validate_response, validate_transition


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


class SessionStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, history TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(
                    owner TEXT NOT NULL, scope TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, response TEXT NOT NULL, debug TEXT NOT NULL,
                    PRIMARY KEY(owner,scope,request_id));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self, cid, owner):
        if cid is None:
            return None, []
        with self.connect() as db:
            row = db.execute("SELECT state,history FROM sessions WHERE id=? AND owner=?", (cid, owner)).fetchone()
        if row is None:
            raise ServiceFailure("invalid_request", "找不到当前会话，请开始一个新对话。")
        return json.loads(row[0]), json.loads(row[1])

    def cached(self, owner, scope, rid):
        with self.connect() as db:
            return db.execute("SELECT fingerprint,response FROM requests WHERE owner=? AND scope=? AND request_id=?", (owner, scope, rid)).fetchone()

    def commit(self, owner, scope, request, fingerprint, previous, state, history, response, debug):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cached = db.execute("SELECT fingerprint,response FROM requests WHERE owner=? AND scope=? AND request_id=?",
                (owner, scope, request["request_id"])).fetchone()
            if cached:
                if cached[0] != fingerprint:
                    raise ServiceFailure("request_id_conflict", "相同请求编号对应了不同内容，请使用新的请求编号。")
                return json.loads(cached[1])
            if previous:
                row = db.execute("SELECT state FROM sessions WHERE id=? AND owner=?", (previous["conversation_id"], owner)).fetchone()
                if row is None or json.loads(row[0])["revision"] != previous["revision"]:
                    raise ServiceFailure("revision_conflict", "会话已有新回复，请同步最新会话后重新提交。")
            validate_transition(previous, request, response, state)
            encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            db.execute("INSERT INTO sessions(id,owner,state,history) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,history=excluded.history",
                (state["conversation_id"], owner, encoded(state), encoded(history)))
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)", (owner, scope, request["request_id"], fingerprint, encoded(response), encoded(debug)))
            return response

    def diagnostics(self, request, owner="local_demo"):
        with self.connect() as db:
            row = db.execute("SELECT debug FROM requests WHERE owner=? AND scope=? AND request_id=?",
                (owner, request["conversation_id"] or "new", request["request_id"])).fetchone()
        return json.loads(row[0]) if row else None


class ChatService:
    def __init__(self, module, store, clock=None, reply_generator=None):
        self.module, self.store = module, store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reply_generator = reply_generator

    async def handle(self, request, trusted_context=None):
        trusted = copy.deepcopy(trusted_context or {"owner_id": "local_demo", "user_id": "U_GUEST", "channel": "web"})
        owner = trusted.get("owner_id", "local_demo")
        previous = None
        try:
            validate_request(request)
        except ContractViolation:
            return self.error(request, None, "invalid_request", "输入格式未通过核验，请检查请求内容。", False)
        try:
            scope = request["conversation_id"] or "new"
            fingerprint = hashlib.sha256(json.dumps({"request": request, "identity": trusted}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cached = self.store.cached(owner, scope, request["request_id"])
            if cached:
                if cached[0] != fingerprint:
                    raise ServiceFailure("request_id_conflict", "相同请求编号对应了不同内容，请使用新的请求编号。")
                return json.loads(cached[1])
            previous, history = self.store.session(request["conversation_id"], owner)
            revision = previous["revision"] if previous else 0
            if request["expected_revision"] != revision:
                raise ServiceFailure("revision_conflict", "会话已有新回复，请同步最新会话后重新提交。")
            reference = request["selected_reference"]
            resolved = None
            if reference:
                for message in history:
                    if message.get("id") == reference["message_id"]:
                        resolved = next((c for c in message.get("cards", []) if c["plan_id"] == reference["plan_id"]), None)
                if resolved is None or reference["sku_id"] is not None and reference["sku_id"] not in [i["sku_id"] for i in resolved["items"]]:
                    raise ServiceFailure("invalid_request", "选中的商品或方案不属于当前会话，请重新选择。")
            now = self.clock()  # 每轮一个快照，包含模型等待期间。
            if now.tzinfo is None:
                raise ValueError("会话时钟必须包含时区")
            cid = previous["conversation_id"] if previous else new_id("conv")
            tid, mid = new_id("turn"), new_id("msg")
            module_state = copy.deepcopy(previous["module_states"].get(self.module.module_id, {})) if previous else {}
            model_history = copy.deepcopy(history)
            if resolved:
                model_history.append({"role": "reference", "content": "本轮明确选中的历史方案", "cards": [copy.deepcopy(resolved)]})
            context = ModuleContext(cid, request["request_id"], tid, revision, request["message"], module_state,
                tuple(model_history), copy.deepcopy(reference), now, trusted)
            result = await self.module.handle(context)
            if result.status == "completed" and self.reply_generator is not None:
                # 先核实事实，再交给理由与话术节点；最终统一校验后才保存。
                self.verify_facts(result.cards, context)
                result = await self.reply_generator.enrich(result, context)
            module_id = None if result.status == "unsupported" else self.module.module_id
            response = {"schema_version": "1.0", "conversation_id": cid, "request_id": request["request_id"], "turn_id": tid,
                "base_revision": revision, "revision": revision + 1, "module": module_id, "status": result.status,
                "message": {"id": mid, "role": "assistant", "content": result.content, "cards": list(result.cards)},
                "clarification": result.clarification, "error": None}
            next_state = copy.deepcopy(previous) if previous else {"schema_version": "1.0", "conversation_id": cid,
                "revision": 0, "active_module": None, "updated_at": now.isoformat(), "module_states": {}}
            next_state.update(revision=revision + 1, active_module=module_id, updated_at=now.isoformat())
            if module_id:
                next_module = copy.deepcopy(dict(result.next_module_state))
                if result.status == "completed":
                    next_module["last_presentation"] = {"message_id": mid, "turn_id": tid,
                        "plan_order": [{"plan_id": c["plan_id"], "sku_ids": [i["sku_id"] for i in c["items"]]} for c in result.cards]}
                next_state["module_states"][module_id] = next_module
                if result.status == "completed":
                    self.verify_facts(result.cards, context)
            # 先校验再提交，异常不追加历史或修改需求。
            validate_transition(previous, request, response, next_state)
            history = history + [{"id": new_id("msg"), "role": "user", "content": request["message"], "turn_id": tid},
                {**response["message"], "turn_id": tid, "status": result.status}]
            return self.store.commit(owner, scope, request, fingerprint, previous, next_state, history, response, result.debug)
        except ContractViolation:
            return self.error(request, previous, "invalid_model_output", "推荐结果格式未通过核验，本轮未保存。", True)
        except ServiceFailure as exc:
            # 并发冲突必须报告当前版本，而不是生成时使用的旧版本。
            if isinstance(request, dict) and request.get("conversation_id"):
                try:
                    previous, _ = self.store.session(request["conversation_id"], owner)
                except ServiceFailure:
                    pass
            return self.error(request, previous, exc.code, exc.public_message, exc.retryable)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            return self.error(request, previous, "internal_error", "推荐处理发生异常，本轮未保存，请重试。", True)

    def verify_facts(self, cards, context):
        for card in cards:
            for item in card["items"]:
                row = self.module.catalog.products.get(item["sku_id"])
                if not self.module.catalog.recommendable_expiry(row, context.now):
                    raise ServiceFailure("invalid_model_output", "商品保质期不符合店铺推荐规则，本轮未保存。", True)
                if not row or (item["product_name"], item["unit_price_fen"], item["image_url"]) != (row["product_name"], row["price_fen"], row["image_url"]):
                    raise ServiceFailure("invalid_model_output", "商品事实未通过核验，本轮未保存。", True)
            p = card["pricing"]
            aids = [d["id"] for d in p["discounts"] if d["kind"] == "activity"]
            cids = [d["id"] for d in p["discounts"] if d["kind"] == "coupon"]
            if len(aids) > 1 or len(cids) > 1:
                raise ServiceFailure("invalid_model_output", "优惠组合未通过核验，本轮未保存。", True)
            quote = self.module.pricing.quote(card["items"], self.module.catalog.user(context.trusted_context.get("user_id", "U_GUEST")),
                context.now, context.trusted_context.get("channel", "web"), aids[0] if aids else None, cids[0] if cids else None)
            if not quote["eligible"] or any(p[key] != quote[key] for key in ("merchandise_payable_fen", "shipping_fen", "total_payable_fen")):
                raise ServiceFailure("invalid_model_output", "推荐金额未通过核验，本轮未保存。", True)
            if p["status"] != quote["pricing_status"]:
                raise ServiceFailure("invalid_model_output", "价格确认状态未通过核验，本轮未保存。", True)
            for d in p["discounts"]:
                collection = "activities" if d["kind"] == "activity" else "coupons"
                key = "activity_discount_fen" if d["kind"] == "activity" else "coupon_discount_fen"
                if (d["label"], d["amount_fen"], d["eligibility"]) != (self.module.catalog.data[collection][d["id"]]["name"], quote[key], "eligible"):
                    raise ServiceFailure("invalid_model_output", "优惠明细未通过核验，本轮未保存。", True)
            expected_gifts = [(g["sku_id"], g["quantity"]) for g in quote["gifts"]]
            if [(g["sku_id"], g["quantity"]) for g in card.get("gifts", [])] != expected_gifts:
                raise ServiceFailure("invalid_model_output", "赠品未通过核验，本轮未保存。", True)
            for gift in card.get("gifts", []):
                row = self.module.catalog.products[gift["sku_id"]]
                if not self.module.catalog.recommendable_expiry(row, context.now):
                    raise ServiceFailure("invalid_model_output", "赠品保质期不符合店铺推荐规则，本轮未保存。", True)
                if gift["product_name"] != row["product_name"] or gift["image_url"] != row["image_url"]:
                    raise ServiceFailure("invalid_model_output", "赠品资料未通过核验，本轮未保存。", True)

    @staticmethod
    def error(request, previous, code, message, retryable):
        import re
        request = request if isinstance(request, dict) else {}
        valid_id = lambda value: value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) else None
        revision = previous["revision"] if previous else 0
        response = {"schema_version": "1.0", "conversation_id": previous["conversation_id"] if previous else valid_id(request.get("conversation_id")),
            "request_id": valid_id(request.get("request_id")) or "invalid_request", "turn_id": None,
            "base_revision": revision, "revision": revision, "module": "product_recommendation",
            "status": "error", "message": {"id": new_id("msg"), "role": "assistant", "content": message, "cards": []},
            "clarification": None, "error": {"code": code, "retryable": retryable}}
        validate_response(response)
        return response
