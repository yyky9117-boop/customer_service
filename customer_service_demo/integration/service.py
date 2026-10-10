"""初步统一入口：推荐为主体，订单仅由服务端显式分发，不接管现有网页。"""
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from runtime.errors import ServiceFailure
from runtime.module_interface import ModuleContext
from runtime.service import ChatService, new_id
from runtime.validation import ContractViolation, validate_request

from .contracts import validate_response, validate_transition
from .registry import ModuleRegistry


class UnifiedChatService:
    def __init__(self, recommendation, store, clock=None, reply_generator=None, router=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reply_generator = reply_generator
        self.router = router
        self.module = recommendation  # 保持已有开发工具访问推荐模块的兼容性。
        # 事实核验沿用推荐主体的当前实现，后续优化不用复制到接入层。
        verifier = ChatService(recommendation, store, clock=self.clock)
        self.modules = ModuleRegistry(recommendation, lambda result, context: verifier.verify_facts(result.cards, context))

    async def handle(self, request, trusted_context=None, *, target_module=None):
        trusted = copy.deepcopy(trusted_context or {"owner_id": "local_demo", "user_id": "U_GUEST", "channel": "web"})
        owner = trusted.get("owner_id", "local_demo")
        principal = trusted.get("user_id", "U_GUEST")
        previous = None
        error_module = "product_recommendation"
        try:
            validate_request(request)
        except ContractViolation:
            return self.error(request, None, "invalid_request", "输入格式未通过核验。", False, error_module)
        try:
            scope = request["conversation_id"] or "new"
            route_key = target_module or ("auto" if self.router else "product_recommendation")
            fingerprint = hashlib.sha256(json.dumps({"request": request, "identity": trusted, "module": route_key},
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cached = self.store.cached(owner, scope, request["request_id"])
            if cached:
                legacy_fingerprint = hashlib.sha256(json.dumps({"request": request, "identity": trusted}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                legacy_replay = (target_module in {None, "product_recommendation"} and cached[0] == legacy_fingerprint
                                 and json.loads(cached[1])["module"] == "product_recommendation")
                if cached[0] != fingerprint and not legacy_replay:
                    raise ServiceFailure("request_id_conflict", "相同请求编号对应不同内容或模块，请使用新的编号。")
                return json.loads(cached[1])
            previous, history = self.store.session(request["conversation_id"], owner)
            self.store.check_principal(request["conversation_id"], principal)
            revision = previous["revision"] if previous else 0
            if request["expected_revision"] != revision:
                raise ServiceFailure("revision_conflict", "会话已有新回复，请同步最新会话后重试。")
            target = target_module or (self.router.route(request["message"], previous, request["selected_reference"]) if self.router else None)
            entry = self.modules.resolve(target)
            module = entry.module
            error_module = module.module_id
            reference = request["selected_reference"]
            if self.router and target_module is None and module.module_id != "product_recommendation":
                reference = None  # 商品引用不能充当订单选择，也不阻碍显式切换模块。
            resolved = None
            if reference:
                if module.module_id != "product_recommendation":
                    raise ServiceFailure("invalid_request", "商品方案引用不能用于选择订单，请提供商城订单号。")
                for message in history:
                    if message.get("id") == reference["message_id"]:
                        resolved = next((c for c in message.get("cards", []) if c["plan_id"] == reference["plan_id"]), None)
                if resolved is None or reference["sku_id"] is not None and reference["sku_id"] not in [i["sku_id"] for i in resolved["items"]]:
                    raise ServiceFailure("invalid_request", "所选方案不属于当前会话，请重新选择。")
            now = self.clock()
            if now.tzinfo is None:
                raise ValueError("会话时钟必须包含时区")
            cid = previous["conversation_id"] if previous else new_id("conv")
            tid, mid = new_id("turn"), new_id("msg")
            module_state = copy.deepcopy(previous["module_states"].get(module.module_id, {})) if previous else {}
            model_history = copy.deepcopy(history)
            # 模型只看所属模块的历史；订单里的购买数量不能污染推荐需求。
            if self.router:
                relevant = {h["turn_id"] for h in history if h.get("module") == module.module_id}
                model_history = [h for h in model_history if h.get("turn_id") in relevant]
            if resolved:
                model_history.append({"role": "reference", "content": "本轮明确选中的历史方案", "cards": [copy.deepcopy(resolved)]})
            context = ModuleContext(cid, request["request_id"], tid, revision, request["message"], module_state,
                tuple(model_history), copy.deepcopy(reference), now, trusted)
            result = await module.handle(context)
            if result.status == "completed" and module.module_id == "product_recommendation" and self.reply_generator is not None:
                entry.verify(result, context)
                result = await self.reply_generator.enrich(result, context)
            module_id = None if result.status == "unsupported" else module.module_id
            response = {"schema_version": "1.0", "conversation_id": cid, "request_id": request["request_id"], "turn_id": tid,
                "base_revision": revision, "revision": revision + 1, "module": module_id, "status": result.status,
                "message": {"id": mid, "role": "assistant", "content": result.content, "cards": list(result.cards)},
                "clarification": result.clarification, "error": None}
            state = copy.deepcopy(previous) if previous else {"schema_version": "1.0", "conversation_id": cid,
                "revision": 0, "active_module": None, "updated_at": now.isoformat(), "module_states": {}}
            state.update(revision=revision + 1, active_module=module_id, updated_at=now.isoformat())
            if module_id:
                next_module = copy.deepcopy(dict(result.next_module_state))
                if result.status == "completed" and module_id == "product_recommendation":
                    next_module["last_presentation"] = {"message_id": mid, "turn_id": tid,
                        "plan_order": [{"plan_id": c["plan_id"], "sku_ids": [i["sku_id"] for i in c["items"]]} for c in result.cards]}
                state["module_states"][module_id] = next_module
                entry.verify(result, context)
            validate_transition(previous, request, response, state)
            history = history + [{"id": new_id("msg"), "role": "user", "content": request["message"], "turn_id": tid},
                {**response["message"], "turn_id": tid, "status": result.status, "module": module_id, "clarification": result.clarification}]
            return self.store.commit(owner, scope, request, fingerprint, previous, state, history, response, result.debug, principal=principal)
        except ContractViolation:
            return self.error(request, previous, "invalid_model_output", "客服结果格式未通过核验，本轮未保存。", True, error_module)
        except ServiceFailure as exc:
            if request["conversation_id"]:
                try:
                    previous, _ = self.store.session(request["conversation_id"], owner)
                except ServiceFailure:
                    pass
            return self.error(request, previous, exc.code, exc.public_message, exc.retryable, error_module)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            return self.error(request, previous, "internal_error", "客服处理发生异常，本轮未保存，请重试。", True, error_module)

    @staticmethod
    def error(request, previous, code, message, retryable, module="product_recommendation"):
        # 基础入口生成兼容的错误，再赋予已经核验过的模块名。
        response = ChatService.error(request, previous, code, message, retryable)
        response["module"] = module
        validate_response(response)
        return response
