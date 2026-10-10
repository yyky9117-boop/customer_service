"""服务端分流：明确号码优先识别，混合意图及跨模块指代先澄清。"""
import re
from runtime.module_interface import ModuleResult
from .identifiers import phone_numbers

REC = "product_recommendation"
ORDER = "order_query"
CLARIFY = "conversation_router"


class ConversationRouter:
    def route(self, message, previous, reference=None):
        text = message.strip()
        active = (previous or {}).get("active_module")
        states = (previous or {}).get("module_states", {})
        rec = bool(re.search(r"推荐|选购|买点|买些|想买|再买|零食方案|预算|低糖|无糖|不辣|独立包装|凑单|优惠券|换一款", text))
        order = bool(re.search(r"SN-DEMO-|DEMO-TRACK-|订单|运单|物流|快递|包裹|发货|发了没|发了吗|什么时候到|啥时候.*(?:到|送)|几号到|签收|退货|退款|售后|人工|催单|查.*(?:买的|下的)|(?:之前|我)?买的.*(?:到|发|查)|还没(?:到|收到)|手机号|手机号码", text, re.I))
        order = order or bool(phone_numbers(text)) or bool(re.search(r"(?:电话|手机)\s*[：:]?\s*\d+", text))
        if rec and re.search(r"不(?:用|要)?查(?:询)?订单|先不查(?:询)?订单|没有订单", text):
            return REC
        if order and re.search(r"不(?:用|要)?推荐|先不推荐", text):
            return ORDER
        if rec and order:
            return CLARIFY
        if order:
            return ORDER
        if rec:
            return REC
        if reference:
            return REC
        if re.fullmatch(r"1\d{10}", text) or re.search(r"第?[一二三四五六\d]+(?:笔|单)", text):
            return ORDER
        if re.search(r"这个|那个|第二个|这款|那款", text) and REC in states and ORDER in states and not states[ORDER].get("candidate_order_ids"):
            return CLARIFY
        if active == ORDER and (re.search(r"第?[一二三四五1-5](?:个|条)|那(?:笔|单)|这(?:笔|单)|到哪|催一下|另一笔|换一笔|换个订单|重新查询|手机号|饼干|薯片|牛肉干|坚果", text)
                                or states.get(ORDER, {}).get("pending_clarification")):
            return ORDER
        if re.search(r"这个|那个|第二个|这款|那款", text) and REC in states and ORDER in states:
            return CLARIFY
        if active == ORDER:
            return CLARIFY
        return REC


class RoutingClarification:
    module_id = CLARIFY

    async def handle(self, context):
        ask = {"question": "你是想先查询订单，还是继续商品推荐？我会分别处理，保留两边的咨询进度。",
               "asked_fields": ["consultation_order"], "group_id": None}
        return ModuleResult("needs_clarification", ask["question"], (), ask,
                            {"pending_clarification": ask}, {"routing": "clarification", "live_model_used": False})

    def verify(self, result, context):
        if result.cards or result.status != "needs_clarification" or result.clarification != result.next_module_state["pending_clarification"]:
            raise ValueError("路由澄清不能返回业务事实")
