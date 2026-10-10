"""统一协议校验；商品事实、用券资格和需求理解由各自运行模块处理。"""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    name: json.loads((ROOT / "schemas" / (name + ".schema.json")).read_text(encoding="utf-8"))
    for name in ("common", "chat-request", "chat-response", "session-state")
}
REGISTRY = Registry().with_resources((schema["$id"], Resource.from_contents(schema)) for schema in SCHEMAS.values())


class ContractViolation(ValueError):
    """协议或跨字段关系不满足。详情用于内部定位，不直接展示给用户。"""


def validate_schema(name: str, payload: Any) -> None:
    validator = Draft202012Validator(SCHEMAS[name], registry=REGISTRY, format_checker=FormatChecker())
    errors = list(validator.iter_errors(payload))
    if errors:
        first = errors[0]
        path = ".".join(map(str, first.absolute_path)) or "$"
        raise ContractViolation(f"{name}:{path}: {first.message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractViolation(message)


def unique(values: list[Any], message: str) -> None:
    require(len(values) == len(set(values)), message)


def strict_integers(payload: Any) -> None:
    """JSON Schema 允许 1.0 作为 integer；本协议要求传输真正的整数。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.endswith("_fen") or key in ("revision", "base_revision", "expected_revision", "quantity", "rank"):
                require(value is None or type(value) is int, f"{key} 必须是整数或允许的空值")
            strict_integers(value)
    elif isinstance(payload, list):
        for value in payload:
            strict_integers(value)


def validate_request(payload: Any) -> None:
    validate_schema("chat-request", payload)
    strict_integers(payload)


def validate_response(payload: Any) -> None:
    validate_schema("chat-response", payload)
    strict_integers(payload)
    delta = 0 if payload["status"] == "error" else 1
    require(payload["revision"] == payload["base_revision"] + delta, "回复版本增量不正确")
    cards = payload["message"]["cards"]
    unique([c["plan_id"] for c in cards], "方案 ID 重复")
    require([c["rank"] for c in cards] == list(range(1, len(cards) + 1)), "排名与展示顺序必须连续一致")
    for card in cards:
        items = card["items"]
        unique([i["sku_id"] for i in items], "同一方案中的 SKU 应合并数量，不得重复")
        unique([i["sku_id"] for i in card.get("gifts", [])], "重复赠品 SKU 应合并数量")
        kind = card["plan_type"]
        if kind == "single_product":
            require(len(items) == 1 and items[0]["quantity"] == 1, "单品方案必须为单个 SKU 一件")
        elif kind == "multi_quantity":
            require(len(items) == 1 and items[0]["quantity"] > 1, "同款多件方案必须为单个 SKU 多件")
        elif kind in ("bundle", "add_on"):
            require(len(items) >= 2, "组合或凑单方案至少包含两个不同 SKU")
        pricing = card["pricing"]
        subtotal = sum(i["unit_price_fen"] * i["quantity"] for i in items)
        require(pricing["subtotal_fen"] == subtotal, "商品原价合计与单价乘数量不一致")
        unique([(d["kind"], d["id"]) for d in pricing["discounts"]], "同类优惠 ID 不得重复扣减")
        discount = sum(d["amount_fen"] for d in pricing["discounts"])
        require(discount <= subtotal, "优惠金额超过商品原价合计")
        status = pricing["status"]
        merchandise = pricing["merchandise_payable_fen"]
        shipping = pricing["shipping_fen"]
        total = pricing["total_payable_fen"]
        if status == "unknown":
            require(merchandise is None and total is None, "无法计算的应付金额必须为空值")
        else:
            require(merchandise == subtotal - discount, "商品应付金额与优惠明细不一致")
            if shipping is None:
                require(total is None, "运费未知时不能给出整单总价")
                require(status == "conditional", "运费未知时整单价格不能标为已确认")
            else:
                require(total == merchandise + shipping, "整单总价与商品应付、运费不一致")
        if status != "confirmed":
            require(bool(pricing["conditions"]), "条件价格或未知价格必须给出条件说明")
        if status == "confirmed":
            require(all(d["eligibility"] == "eligible" for d in pricing["discounts"]), "未确认的优惠不能给出已确认价格")


def validate_session(payload: Any) -> None:
    validate_schema("session-state", payload)
    strict_integers(payload)
    active = payload["active_module"]
    require(active is None or active in payload["module_states"], "活动模块必须有对应的独立状态")
    state = payload["module_states"].get("product_recommendation")
    if state is None:
        return
    groups = state["need_groups"]
    unique([g["group_id"] for g in groups], "需求组 ID 重复")
    budgets = [g["budget"] for g in groups]
    if state.get("shared_budget") is not None:
        require(state["shared_budget"]["scope"] in ("goods_total", "order_total", "unknown"), "共同预算必须是订单层口径")
        budgets.append(state["shared_budget"])
    for budget in budgets:
        lower, upper = budget["min_fen"], budget["max_fen"]
        require(lower is None or upper is None or lower <= upper, "有效预算下限不能高于上限")
        has_amount = lower is not None or upper is not None
        require((budget["strictness"] == "unknown") != has_amount, "预算金额与已知程度不一致")
        if has_amount:
            require(budget["source_turn_id"] is not None and bool(budget["source_text"].strip()), "已知预算必须保留来源")
        require(not budget.get("min_is_hard", False) or lower is not None, "硬下界必须提供明确金额")
    for group in groups:
        constraints = group["constraints"]
        unique([c["constraint_id"] for c in constraints], "同一需求组内约束 ID 重复")
        by_id = {c["constraint_id"]: c for c in constraints}
        for constraint in constraints:
            sources = constraint.get("derived_from", [])
            if constraint.get("origin", "explicit") == "inferred":
                require(constraint["strength"] == "soft" and bool(sources), "推测条件必须是带来源的软偏好")
            else:
                require(not sources, "明确条件不能同时标为依赖推测")
            for source in sources:
                require(source in by_id and source != constraint["constraint_id"], "推测来源必须是同组其他条件")
                if constraint["status"] == "active":
                    require(by_id[source]["status"] == "active", "来源已撤销或待确认时推测条件不能继续生效")
        def visit(cid, path):
            require(cid not in path, "推测条件的来源不能循环依赖")
            for source in by_id[cid].get("derived_from", []):
                visit(source, path | {cid})
        for cid in by_id:
            visit(cid, set())
    clarification = state["pending_clarification"]
    group_ids = {g["group_id"] for g in groups}
    if clarification is not None:
        require(clarification["group_id"] is None or clarification["group_id"] in group_ids, "追问引用了不存在的需求组")
    last = state["last_presentation"]
    if last is not None:
        unique([p["plan_id"] for p in last["plan_order"]], "历史展示方案 ID 重复")


def validate_transition(previous: Any, request: Any, response: Any, next_state: Any) -> None:
    """检查一次新结果的提交关系；幂等重放不走新提交路径。"""
    validate_request(request)
    validate_response(response)
    if previous is not None:
        validate_session(previous)
        require(request["conversation_id"] == previous["conversation_id"], "请求不属于基础会话")
        if request["expected_revision"] != previous["revision"]:
            require(response["status"] == "error" and response["error"]["code"] == "revision_conflict", "过期请求只能返回版本冲突")
        base = previous["revision"]
        conversation_id = previous["conversation_id"]
    else:
        require(request["conversation_id"] is None, "已有会话不能作为新会话提交")
        base = 0
        conversation_id = response["conversation_id"]
    require(response["request_id"] == request["request_id"], "响应不属于本次请求")
    require(response["base_revision"] == base, "响应使用了错误的基础版本")
    if response["status"] == "error":
        require(response["conversation_id"] == (previous["conversation_id"] if previous else None), "错误不能创建或切换会话")
        require(next_state == previous, "错误不能写入候选状态")
        return
    validate_session(next_state)
    require(response["conversation_id"] == conversation_id == next_state["conversation_id"], "跨会话结果不得提交")
    require(next_state["revision"] == response["revision"], "会话版本与响应版本不一致")
    module = response["module"]
    require(next_state["active_module"] == module, "活动模块与本轮响应不一致")
    if module == "product_recommendation":
        state = next_state["module_states"][module]
        if response["status"] == "needs_clarification":
            require(state["pending_clarification"] == response["clarification"], "追问与待补充状态不一致")
        else:
            require(state["pending_clarification"] is None, "已结束的追问不能继续留在待补充状态")
        if response["status"] == "completed":
            cards = response["message"]["cards"]
            groups = {g["group_id"] for g in state["need_groups"]}
            require(all(c["group_id"] in groups for c in cards), "推荐方案引用了不存在的需求组")
            expected = {"message_id": response["message"]["id"], "turn_id": response["turn_id"],
                        "plan_order": [{"plan_id": c["plan_id"], "sku_ids": [i["sku_id"] for i in c["items"]]} for c in cards]}
            require(state["last_presentation"] == expected, "保存的展示顺序与实际回复不一致")
