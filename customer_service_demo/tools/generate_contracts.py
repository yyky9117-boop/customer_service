"""生成本阶段协议和示例；无需网络、Coze 或模型。

协议源定义在本文件，生成后的 JSON 文件供其他语言的客户端消费。
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "urn:snack-customer-service:contracts:v1:"
ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,80}$"}
TEXT = {"type": "string", "minLength": 1, "pattern": r"\S"}
MONEY = {"type": "integer", "minimum": 0}
REVISION = {"type": "integer", "minimum": 0}
NULL = {"type": "null"}


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def arr(items, minimum=0, maximum=None):
    out = {"type": "array", "items": items, "minItems": minimum}
    if maximum is not None:
        out["maxItems"] = maximum
    return out


def nullable(schema):
    return {"anyOf": [schema, NULL]}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def ref(name):
    return {"$ref": BASE + "common#/$defs/" + name}


def document(name, title, body):
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": BASE + name, "title": title, **body}


def write(folder, name, payload):
    directory = ROOT / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate():
    clarification = obj({
        "question": TEXT,
        "asked_fields": {**arr(TEXT, 1, 2), "uniqueItems": True},
        "group_id": nullable(ID),
    })
    discount = obj({
        "kind": enum("activity", "coupon"), "id": ID, "label": TEXT,
        "amount_fen": MONEY, "eligibility": enum("eligible", "conditional"),
    })
    item = obj({
        "sku_id": ID, "product_name": TEXT,
        "quantity": {"type": "integer", "minimum": 1, "maximum": 999},
        "unit_price_fen": MONEY,
        "image_url": nullable({"type": "string", "format": "uri", "pattern": "^https?://"}),
    })
    pricing = obj({
        "currency": {"const": "CNY"},
        "status": enum("confirmed", "conditional", "unknown"),
        "subtotal_fen": MONEY, "discounts": arr(discount),
        "merchandise_payable_fen": nullable(MONEY),
        "shipping_fen": nullable(MONEY), "total_payable_fen": nullable(MONEY),
        "conditions": arr(TEXT),
    })
    card = obj({
        "type": {"const": "recommendation_plan"}, "plan_id": ID,
        "plan_type": enum("single_product", "multi_quantity", "bundle", "add_on", "gift_plan"),
        "title": TEXT, "rank": {"type": "integer", "minimum": 1, "maximum": 3},
        "group_id": ID, "items": arr(item, 1, 12), "pricing": pricing,
        "reasons": arr(TEXT, 1, 3), "notes": arr(TEXT),
    })
    card["properties"]["gifts"] = arr(obj({
        "sku_id": ID, "product_name": TEXT,
        "quantity": {"type": "integer", "minimum": 1, "maximum": 999},
        "image_url": nullable({"type": "string", "format": "uri", "pattern": "^https?://"}),
    }))
    budget = obj({
        "min_fen": nullable(MONEY), "max_fen": nullable(MONEY),
        "strictness": enum("strict", "approximate", "unknown"),
        "scope": enum("per_unit", "per_recipient", "goods_total", "order_total", "unknown"),
        "source_turn_id": nullable(ID), "source_text": {"type": "string", "maxLength": 2000},
    })
    budget["properties"]["min_is_hard"] = {"type": "boolean"}
    constraint = obj({
        "constraint_id": ID,
        "field": enum("category", "taste", "package", "ingredient", "scene", "purpose", "quantity", "gift_need", "other"),
        "operator": enum("include", "exclude", "equals", "at_least", "at_most"),
        "value": {"anyOf": [TEXT, {"type": "boolean"}, {"type": "integer", "minimum": 0}, arr(TEXT, 1)]},
        "strength": enum("hard", "soft"),
        "status": enum("active", "pending", "removed"),
        "source_turn_id": ID, "source_text": {**TEXT, "maxLength": 2000},
    })
    constraint["properties"]["origin"] = enum("explicit", "inferred")
    constraint["properties"]["derived_from"] = {**arr(ID), "uniqueItems": True}
    group = obj({
        "group_id": ID, "label": TEXT, "budget": budget,
        "constraints": arr(constraint),
    })
    presentation = obj({
        "message_id": ID, "turn_id": ID,
        "plan_order": arr(obj({"plan_id": ID, "sku_ids": {**arr(ID, 1), "uniqueItems": True}}), 0, 3),
    })
    module_state = obj({
        "need_groups": arr(group), "pending_clarification": nullable(clarification),
        "last_presentation": nullable(presentation),
    })
    module_state["properties"]["shared_budget"] = nullable(budget)
    common = document("common", "共用对象", {"$defs": {
        "id": ID, "clarification": clarification, "recommendation_card": card,
        "recommendation_state": module_state,
    }})

    request = document("chat-request", "客服请求", obj({
        "schema_version": {"const": "1.0"}, "conversation_id": nullable(ID),
        "request_id": ID, "expected_revision": REVISION,
        "message": {**TEXT, "maxLength": 2000},
        "selected_reference": nullable(obj({"message_id": ID, "plan_id": ID, "sku_id": nullable(ID)})),
    }))
    request["allOf"] = [{"if": {"properties": {"conversation_id": NULL}},
                         "then": {"properties": {"expected_revision": {"const": 0}}}}]

    response = document("chat-response", "客服统一回复", obj({
        "schema_version": {"const": "1.0"}, "conversation_id": nullable(ID),
        "request_id": ID, "turn_id": nullable(ID),
        "base_revision": REVISION, "revision": REVISION,
        "module": nullable(enum("product_recommendation")),
        "status": enum("completed", "needs_clarification", "no_match", "unsupported", "error"),
        "message": obj({"id": ID, "role": {"const": "assistant"},
                        "content": TEXT, "cards": arr(ref("recommendation_card"), 0, 3)}),
        "clarification": nullable(ref("clarification")),
        "error": nullable(obj({
            "code": enum("invalid_request", "revision_conflict", "request_id_conflict", "model_timeout", "invalid_model_output", "data_unavailable", "internal_error"),
            "retryable": {"type": "boolean"},
        })),
    }))
    response["allOf"] = []
    for status in ["completed", "needs_clarification", "no_match", "unsupported", "error"]:
        props = {
            "message": {"properties": {"cards": {"minItems": 1} if status == "completed" else {"maxItems": 0}}},
            "clarification": ref("clarification") if status == "needs_clarification" else NULL,
            "error": {"type": "object"} if status == "error" else NULL,
        }
        if status != "error":
            props["turn_id"] = ID
            props["conversation_id"] = ID
        if status in ["completed", "needs_clarification", "no_match"]:
            props["module"] = {"const": "product_recommendation"}
        if status == "unsupported":
            props["module"] = NULL
        response["allOf"].append({"if": {"properties": {"status": {"const": status}}}, "then": {"properties": props}})

    session = document("session-state", "服务端会话状态", obj({
        "schema_version": {"const": "1.0"}, "conversation_id": ID, "revision": REVISION,
        "active_module": nullable(enum("product_recommendation")),
        "updated_at": {"type": "string", "format": "date-time"},
        "module_states": obj({"product_recommendation": ref("recommendation_state")}, required=[]),
    }))
    for name, payload in [("common", common), ("chat-request", request), ("chat-response", response), ("session-state", session)]:
        write("schemas", name + ".schema.json", payload)

    write("examples", "request-new.json", {
        "schema_version": "1.0", "conversation_id": None, "request_id": "req_demo_001",
        "expected_revision": 0, "message": "100 元以内，办公室吃，独立小包，不要辣。", "selected_reference": None,
    })
    plan = {
        "type": "recommendation_plan", "plan_id": "plan_demo_001", "plan_type": "bundle",
        "title": "办公室分享组合（协议示例）", "rank": 1, "group_id": "group_self",
        "items": [
            {"sku_id": "demo_a", "product_name": "演示商品 A", "quantity": 2, "unit_price_fen": 3500, "image_url": None},
            {"sku_id": "demo_b", "product_name": "演示商品 B", "quantity": 1, "unit_price_fen": 4500, "image_url": None},
        ],
        "pricing": {
            "currency": "CNY", "status": "confirmed", "subtotal_fen": 11500,
            "discounts": [
                {"kind": "activity", "id": "demo_activity", "label": "演示活动减 10 元", "amount_fen": 1000, "eligibility": "eligible"},
                {"kind": "coupon", "id": "demo_coupon", "label": "演示券减 5 元", "amount_fen": 500, "eligibility": "eligible"},
            ],
            "merchandise_payable_fen": 10000, "shipping_fen": 0, "total_payable_fen": 10000, "conditions": [],
        },
        "reasons": ["本示例用于验证组合数量、优惠明细与总价关系。"],
        "notes": ["仅为协议示例，尚未查询商品或运行推荐。"],
    }
    def envelope(status, content, cards=None, clarification_value=None, error_value=None):
        return {
            "schema_version": "1.0", "conversation_id": None if status == "error" else "conv_demo_001", "request_id": "req_demo_001",
            "turn_id": None if status == "error" else "turn_demo_001", "base_revision": 0, "revision": 0 if status == "error" else 1,
            "module": None if status == "unsupported" else "product_recommendation", "status": status,
            "message": {"id": "msg_demo_001", "role": "assistant", "content": content, "cards": cards or []},
            "clarification": clarification_value, "error": error_value,
        }
    write("examples", "response-completed.json", envelope("completed", "这是协议演示组合：A 两件和 B 一件，原价 115 元，演示优惠合计 15 元，含演示运费的总价 100 元。", [plan]))
    write("examples", "response-clarification.json", envelope("needs_clarification", "主要是自己吃还是送人？预算大概多少？", clarification_value={"question": "主要是自己吃还是送人？预算大概多少？", "asked_fields": ["purpose", "budget"], "group_id": None}))
    write("examples", "response-no-match.json", envelope("no_match", "目前没有同时满足这些条件的演示商品。您愿意调整包装要求吗？"))
    write("examples", "response-unsupported.json", envelope("unsupported", "订单查询模块尚未接入这个客服界面。"))
    write("examples", "response-error.json", envelope("error", "这次请求暂时没有完成，可以重试。", error_value={"code": "model_timeout", "retryable": True}))
    write("examples", "session-state.json", {
        "schema_version": "1.0", "conversation_id": "conv_demo_001", "revision": 1,
        "active_module": "product_recommendation", "updated_at": "2026-10-07T00:00:00Z",
        "module_states": {"product_recommendation": {
            "need_groups": [{
                "group_id": "group_self", "label": "自己在办公室吃",
                "budget": {"min_fen": None, "max_fen": 10000, "strictness": "strict", "scope": "goods_total", "source_turn_id": "turn_demo_001", "source_text": "100 元以内"},
                "constraints": [
                    {"constraint_id": "c_package", "field": "package", "operator": "equals", "value": "独立小包", "strength": "hard", "status": "active", "source_turn_id": "turn_demo_001", "source_text": "独立小包"},
                    {"constraint_id": "c_taste", "field": "taste", "operator": "exclude", "value": "辣", "strength": "hard", "status": "active", "source_turn_id": "turn_demo_001", "source_text": "不要辣"},
                ],
            }],
            "pending_clarification": None,
            "last_presentation": {"message_id": "msg_demo_001", "turn_id": "turn_demo_001", "plan_order": [{"plan_id": "plan_demo_001", "sku_ids": ["demo_a", "demo_b"]}]},
        }},
    })


if __name__ == "__main__":
    generate()
