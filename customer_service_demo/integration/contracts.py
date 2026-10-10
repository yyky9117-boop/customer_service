"""从当前推荐协议派生接入协议，不修改推荐的协议源或生成文件。"""
import copy

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from runtime import validation as original
from runtime.validation import ContractViolation, require, strict_integers

BASE = "urn:snack-customer-service:contracts:v1:"
ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,80}$"}
NULL = {"type": "null"}
CLARIFICATION = {"$ref": BASE + "common#/$defs/clarification"}
ORDER_STATE = {
    "type": "object",
    "properties": {
        "params": {"type": "object", "properties": {key: {"type": "string", "maxLength": 2000}
            for key in ("order_id", "tracking_number", "phone_number", "product_name", "query_type")},
            "required": ["order_id", "tracking_number", "phone_number", "product_name", "query_type"],
            "additionalProperties": False},
        "selected_order_id": {"anyOf": [ID, NULL]},
        "candidate_order_ids": {"type": "array", "items": ID, "maxItems": 5, "uniqueItems": True},
        "pending_clarification": {"anyOf": [CLARIFICATION, NULL]},
        "last_result_type": {"type": "string", "enum": ["success", "not_found", "need_more_info", "multiple_orders", "narrow_search", "partial_success"]},
        "source": {"const": "legacy_readonly"},
    },
    "required": ["params", "selected_order_id", "candidate_order_ids", "pending_clarification", "last_result_type", "source"],
    "additionalProperties": False,
}


def extended_schemas():
    schemas = copy.deepcopy(original.SCHEMAS)
    schemas["common"]["$defs"]["order_state"] = copy.deepcopy(ORDER_STATE)
    schemas["common"]["$defs"]["routing_state"] = {"type": "object", "properties": {"pending_clarification": CLARIFICATION},
        "required": ["pending_clarification"], "additionalProperties": False}
    response = schemas["chat-response"]
    response["properties"]["module"]["anyOf"][0]["enum"].extend(["order_query", "conversation_router"])
    for rule in response["allOf"]:
        status = rule.get("if", {}).get("properties", {}).get("status", {}).get("const")
        props = rule.get("then", {}).get("properties", {})
        if status in {"completed", "needs_clarification", "no_match"}:
            props["module"] = {"enum": ["product_recommendation", "order_query", "conversation_router"]}
        if status == "completed":
            props["message"]["properties"]["cards"]["minItems"] = 0
    response["allOf"].extend([
        {"if": {"properties": {"module": {"const": "conversation_router"}}},
         "then": {"properties": {"status": {"enum": ["needs_clarification", "error"]}, "message": {"properties": {"cards": {"maxItems": 0}}}}}},
        {"if": {"properties": {"module": {"const": "product_recommendation"}, "status": {"const": "completed"}}},
         "then": {"properties": {"message": {"properties": {"cards": {"minItems": 1}}}}}},
        {"if": {"properties": {"module": {"const": "order_query"}}},
         "then": {"properties": {"message": {"properties": {"cards": {"maxItems": 0}}}}}},
    ])
    session = schemas["session-state"]
    session["properties"]["active_module"]["anyOf"][0]["enum"].extend(["order_query", "conversation_router"])
    session["properties"]["module_states"]["properties"]["order_query"] = {"$ref": BASE + "common#/$defs/order_state"}
    session["properties"]["module_states"]["properties"]["conversation_router"] = {"$ref": BASE + "common#/$defs/routing_state"}
    return schemas


SCHEMAS = extended_schemas()
REGISTRY = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in SCHEMAS.values())


def validate_schema(name, payload):
    errors = list(Draft202012Validator(SCHEMAS[name], registry=REGISTRY, format_checker=FormatChecker()).iter_errors(payload))
    if errors:
        raise ContractViolation(f"integration:{name}:{errors[0].message}")
    strict_integers(payload)


def recommendation_view(state):
    if state is None:
        return None
    view = copy.deepcopy(state)
    view["module_states"].pop("order_query", None)
    view["module_states"].pop("conversation_router", None)
    if view["active_module"] in {"order_query", "conversation_router"}:
        view["active_module"] = None
    return view


def validate_response(response):
    validate_schema("chat-response", response)
    if response["module"] not in {"order_query", "conversation_router"}:
        original.validate_response(response)
    else:
        require(response["revision"] == response["base_revision"] + (0 if response["status"] == "error" else 1), "回复版本不一致")


def validate_session(state):
    validate_schema("session-state", state)
    require(state["active_module"] is None or state["active_module"] in state["module_states"], "活动模块没有独立状态")
    original.validate_session(recommendation_view(state))


def validate_transition(previous, request, response, state):
    original.validate_request(request)
    validate_response(response)
    if previous:
        validate_session(previous)
        require(request["conversation_id"] == previous["conversation_id"], "跨会话提交")
        require(request["expected_revision"] == previous["revision"], "过期请求不能提交")
    else:
        require(request["conversation_id"] is None and request["expected_revision"] == 0, "新会话版本错误")
    require(response["request_id"] == request["request_id"], "请求编号不一致")
    require(response["base_revision"] == (previous["revision"] if previous else 0), "基础版本不一致")
    if response["status"] == "error":
        require(state == previous, "错误不能修改会话")
        return
    validate_session(state)
    require(response["conversation_id"] == state["conversation_id"] == (previous["conversation_id"] if previous else response["conversation_id"]), "会话编号不一致")
    require(state["revision"] == response["revision"] and state["active_module"] == response["module"], "提交状态与回复不一致")
    module_id = response["module"]
    # 一轮只能修改自己模块的状态；未选中的需求、订单选择不得丢失。
    before = previous["module_states"] if previous else {}
    for key in set(before) | set(state["module_states"]):
        if key != module_id:
            require(before.get(key) == state["module_states"].get(key), "跨模块修改独立状态")
    if module_id in {"order_query", "conversation_router"}:
        pending = state["module_states"][module_id]["pending_clarification"]
        require(pending == response["clarification"], "订单追问与状态不一致")
        require(pending is None or pending["group_id"] is None, "订单追问不能引用推荐需求组")
    else:
        original.validate_transition(recommendation_view(previous), request, response, recommendation_view(state))
