"""增量需求协议及确定性谓词。模型只提交补丁，不覆盖服务端状态。"""
import copy
import re
from datetime import timezone, timedelta

from jsonschema import Draft202012Validator

from .catalog import instant
from .errors import InvalidModelOutput
from .validation import ContractViolation, validate_session

ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,80}$"}


def object_schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


# 兼容第一步通用约束：具体值的语义在本文件进一步限制。
CONSTRAINT = object_schema({
    "constraint_id": ID, "field": {"enum": ["category", "taste", "package", "ingredient", "scene", "purpose", "quantity", "gift_need", "other"]},
    "operator": {"enum": ["include", "exclude", "equals", "at_least", "at_most"]},
    "value": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "boolean"},
        {"type": "integer", "minimum": 0}, {"type": "array", "items": {"type": "string"}, "minItems": 1, "uniqueItems": True}]},
    "strength": {"enum": ["hard", "soft"]}, "source_text": {"type": "string", "maxLength": 2000},
    "origin": {"enum": ["explicit", "inferred"]}, "derived_from": {"type": "array", "items": ID, "uniqueItems": True}
}, ["constraint_id", "field", "operator", "value", "strength", "source_text", "origin", "derived_from"])
BUDGET_PATCH = object_schema({
    "min_fen": {"type": ["integer", "null"], "minimum": 0}, "max_fen": {"type": ["integer", "null"], "minimum": 0},
    "strictness": {"enum": ["strict", "approximate", "unknown"]},
    "scope": {"enum": ["per_unit", "per_recipient", "goods_total", "order_total", "unknown"]},
    "source_text": {"type": "string"}, "min_is_hard": {"type": "boolean"}
}, ["min_fen", "max_fen", "strictness", "scope", "source_text", "min_is_hard"])
GROUP_PATCH = object_schema({
    "group_id": ID, "label": {"type": "string", "minLength": 1, "maxLength": 100},
    "budget": BUDGET_PATCH, "upsert_constraints": {"type": "array", "items": CONSTRAINT, "maxItems": 30},
    "remove_constraint_ids": {"type": "array", "items": ID, "uniqueItems": True, "maxItems": 30}
}, ["group_id", "upsert_constraints", "remove_constraint_ids"])
PATCH_SCHEMA = object_schema({
    "intent": {"enum": ["recommend", "unsupported"]}, "reset": {"type": "boolean"},
    "groups": {"type": "array", "items": GROUP_PATCH, "maxItems": 3},
    "remove_group_ids": {"type": "array", "items": ID, "uniqueItems": True},
    "shared_budget": {"anyOf": [BUDGET_PATCH, {"type": "null"}]},
    "clarification": {"anyOf": [object_schema({"question": {"type": "string", "minLength": 1, "maxLength": 180},
        "asked_fields": {"type": "array", "items": {"enum": ["budget", "budget_scope", "quantity", "taste", "category", "allergy", "reference", "conflict", "purpose", "package"]},
            "minItems": 1, "maxItems": 2, "uniqueItems": True}, "group_id": {"anyOf": [ID, {"type": "null"}]}
        }, ["question", "asked_fields", "group_id"]), {"type": "null"}]},
    "retrieval_hints": {"type": "array", "items": ID, "maxItems": 30},
    "reference_action": {"enum": ["retain_selected", "replace_selected", "recommend_alternatives"]},
    "reference_source_text": {"type": "string", "minLength": 1, "maxLength": 2000}
}, ["intent", "reset", "groups", "remove_group_ids", "clarification", "retrieval_hints"])

CATEGORIES = {"biscuits", "seaweed", "beans", "meat", "nuts", "chocolate", "candy", "dried_fruit", "popcorn", "grain_bar", "gift_box", "mixed", "drinks", "seeds"}
INGREDIENTS = {"peanut", "tree_nuts", "sesame", "gluten", "soy", "milk", "egg", "fish", "shellfish", "beef", "pork", "wheat", "oats", "seaweed", "sugar"}
TASTES = {"spice:none", "spice:mild", "spice:hot", "sweetness:none", "sweetness:low", "sweetness:medium", "sweetness:high", "sugar:low_sugar", "sugar:sugar_free", "sugar:regular"}
PACKAGES = {"wrapped", "small", "gift_box", "bag", "bottle"}
SCENES = {"office", "breakfast", "watching_tv", "sharing", "outdoor", "travel", "gift", "self"}


def empty_budget():
    return {"min_fen": None, "max_fen": None, "strictness": "unknown", "scope": "unknown", "source_turn_id": None, "source_text": ""}


def empty_state():
    return {"need_groups": [], "pending_clarification": None, "last_presentation": None, "shared_budget": None}


def validate_predicate(c):
    values = c["value"] if isinstance(c["value"], list) else [c["value"]]
    field, op = c["field"], c["operator"]
    if field == "quantity":
        ok = type(c["value"]) is int and 1 <= c["value"] <= 999 and op in {"equals", "at_least", "at_most"} and c["strength"] == "hard"
    elif field == "gift_need":
        ok = type(c["value"]) is bool and op == "equals"
    else:
        allowed = {"category": CATEGORIES, "taste": TASTES, "package": PACKAGES,
            "ingredient": {prefix + x for prefix in ("ingredient:", "allergen:") for x in INGREDIENTS},
            "scene": SCENES, "purpose": {"gift", "self"}, "other": {"not_near_expiry", "no_add_on", "add_on_allowed", "verified_stock_price"}}
        ok = op in {"equals", "include", "exclude"} and all(isinstance(v, str) and (
            v in allowed.get(field, set()) or field == "other" and v.startswith("sku:") and len(v) <= 84) for v in values)
        if op == "equals" and len(values) != 1:
            ok = False
        if field == "taste" and len({v.split(":")[0] for v in values if isinstance(v, str)}) != 1:
            ok = False
    if not ok:
        if field == "taste":
            raise InvalidModelOutput(f"口味值必须使用完整前缀，允许值为 {sorted(TASTES)}；当前 {op} {c['value']}。一条约束只能包含一个维度。")
        if field == "ingredient":
            raise InvalidModelOutput("原料值必须带 ingredient: 或 allergen: 前缀，例如 ingredient:oats、ingredient:peanut、allergen:peanut；不存在 seafood，范围不明时不要建立该条件，改为澄清。")
        raise InvalidModelOutput(f"不支持的约束值/操作: {field} {op} {c['value']}")
    if field == "package" and op == "include" and {"small", "wrapped"} <= set(values):
        raise InvalidModelOutput("small 已包含独立包装与 <=30g 要求；include [small,wrapped] 是或关系，会放宽小包重量要求。小包只用 equals small，单独要求独立包装只用 equals wrapped。")
    if field == "other" and any(v in ("no_add_on", "add_on_allowed") for v in values):
        if not (len(values) == 1 and op == "equals" and c["strength"] == "hard" and c.get("origin", "explicit") == "explicit"):
            raise InvalidModelOutput("加购许可/禁止必须是用户明确表达的单项条件")


def merge_patch(previous, patch, context):
    errors = list(Draft202012Validator(PATCH_SCHEMA).iter_errors(patch))
    if errors:
        raise InvalidModelOutput("需求 JSON 结构错误: " + errors[0].message[:200])
    if patch['intent']=='recommend' and not patch['clarification'] and re.search(r'预算[^，。；]{0,24}\d',context.message):
        if not any('budget' in g for g in patch['groups']) and not patch.get('shared_budget'):
            raise InvalidModelOutput('当前原话包含明确预算数字，不能遗漏预算或置为null；把本轮金额与口径写入对应预算，口径不明时使用unknown并澄清。')
    state = empty_state() if patch["reset"] else copy.deepcopy(dict(previous) or empty_state())
    groups = {g["group_id"]: g for g in state["need_groups"]}
    if len({g["group_id"] for g in patch["groups"]}) != len(patch["groups"]):
        raise InvalidModelOutput("需求组 ID 重复")
    for gid in patch["remove_group_ids"]:
        if gid not in groups:
            raise InvalidModelOutput("移除不存在的需求组")
        del groups[gid]
    def budget(value):
        b = copy.deepcopy(value)
        if b["strictness"] != "unknown" and (not b["source_text"] or b["source_text"] not in context.message):
            raise InvalidModelOutput("预算没有当前用户原话依据")
        b["source_turn_id"] = context.turn_id if b["strictness"] != "unknown" else None
        return b
    for update in patch["groups"]:
        gid = update["group_id"]
        if gid not in groups:
            groups[gid] = {"group_id": gid, "label": update.get("label", "当前需求"), "budget": empty_budget(), "constraints": []}
        g = groups[gid]
        if "label" in update:
            g["label"] = update["label"]
        if "budget" in update:
            g["budget"] = budget(update["budget"])
        by_id = {c["constraint_id"]: c for c in g["constraints"]}
        for cid in update["remove_constraint_ids"]:
            if cid not in by_id:
                raise InvalidModelOutput("移除不存在的约束")
            by_id[cid]["status"] = "removed"
        if len({c["constraint_id"] for c in update["upsert_constraints"]}) != len(update["upsert_constraints"]):
            raise InvalidModelOutput("重复更新同一约束")
        for raw in update["upsert_constraints"]:
            validate_predicate(raw)
            if raw['field']=='package' and raw['strength']=='hard' and 'small' in (raw['value'] if isinstance(raw['value'],list) else [raw['value']]):
                source=raw['source_text']
                if '独立包装' in source and not re.search(r'小|\d+\s*(?:g|克)',context.message,re.I):
                    raise InvalidModelOutput('用户只要求独立包装，没有小包或重量要求。请用package equals wrapped，不能擅自添加small的30g限制。')
            c = copy.deepcopy(raw)
            if c["origin"] == "explicit" and (not c["source_text"] or c["source_text"] not in context.message):
                raise InvalidModelOutput("明确条件没有当前用户原话依据")
            c.update(status="active", source_turn_id=context.turn_id)
            by_id[c["constraint_id"]] = c
        # 递归撤销依赖推测；明确礼盒约束没有依赖，因此会保留。
        changed = True
        while changed:
            changed = False
            for c in by_id.values():
                if c["status"] == "active" and any(by_id.get(cid, {}).get("status") != "active" for cid in c.get("derived_from", [])):
                    c["status"], changed = "removed", True
        g["constraints"] = list(by_id.values())
    state["need_groups"] = list(groups.values())
    if "shared_budget" in patch:
        state["shared_budget"] = budget(patch["shared_budget"]) if patch["shared_budget"] is not None else None
    if not state["need_groups"] and state.get("shared_budget") and patch["clarification"] is None:
        state["need_groups"] = [{"group_id": "group_self", "label": "本次零食", "budget": empty_budget(), "constraints": []}]
    state["pending_clarification"] = copy.deepcopy(patch["clarification"])
    if state["pending_clarification"]:
        # 不将自由生成的追问原样展示，避免夹带商品事实或优惠承诺。
        questions = {"budget": "希望控制在多少预算内？", "budget_scope": "预算是每份、商品合计，还是含运费的整单金额？",
            "quantity": "这次需要购买多少件或多少份？", "taste": "有喜欢或必须避开的口味吗？",
            "category": "希望优先哪一种零食品类？", "allergy": "需要避开的原料或过敏原具体有哪些，是否也排除可能含有的信息？",
            "reference": "请明确选中哪一个方案或商品。", "conflict": "存在冲突的条件，最终应保留哪一项？",
            "purpose": "准备自己吃、分享，还是送人？", "package": "对内包装或外包装有什么具体要求？"}
        state["pending_clarification"]["question"] = "".join(questions[f] for f in state["pending_clarification"]["asked_fields"])
    try:
        validate_session({"schema_version": "1.0", "conversation_id": context.conversation_id,
            "revision": context.base_revision, "active_module": "product_recommendation",
            "updated_at": context.now.isoformat(), "module_states": {"product_recommendation": state}})
    except ContractViolation as exc:
        raise InvalidModelOutput(str(exc)) from exc
    return state


def predicate(row, c, now, near_days=30):
    """None 表示相关资料未知；未知不会通过硬排除。"""
    field, op = c["field"], c["operator"]
    values = c["value"] if isinstance(c["value"], list) else [c["value"]]
    if field == "quantity":
        return True  # 在方案层验证，而非商品层。
    if field == "other" and c["value"] in ("no_add_on", "add_on_allowed"):
        return True
    def atom(v):
        if field == "category":
            if row.get("category") is None:
                return None
            if row["category"] == v:
                return True
            # 礼盒/组合中的明确成分同样受品类排除约束。
            if v in {"nuts", "meat"}:
                ingredients = row.get("ingredients")
                allergens = row.get("declared_allergens")
                if ingredients is None or allergens is None:
                    return None
                evidence = set(ingredients) | set(allergens)
                return bool(evidence & ({"peanut", "tree_nuts", "cashew", "almond", "walnut"} if v == "nuts" else {"beef", "pork", "chicken"}))
            return False
        if field == "taste":
            prefix, expected = v.split(":")
            actual = row.get({"spice": "spice_level", "sweetness": "sweetness", "sugar": "sugar_label"}[prefix])
            return None if actual in (None, "unknown") else actual == expected
        if field == "package":
            p = row.get("packaging") or {}
            if v in {"wrapped", "small"}:
                flag = p.get("individually_wrapped")
                if flag is None:
                    return None
                if v == "wrapped" or flag is False:
                    return flag
                weight = p.get("inner_weight_g")
                return None if weight is None else weight <= 30
            return None if p.get("outer_type") is None else p["outer_type"] == ("box" if v == "gift_box" else v)
        if field == "ingredient":
            prefix, value = v.split(":")
            fields = ["ingredients"] if prefix == "ingredient" else ["ingredients", "declared_allergens", "may_contain_allergens"]
            rows = [row.get(f) for f in fields]
            if any(value in (x or []) for x in rows):
                return True
            return None if any(x is None for x in rows) else False
        if field in {"scene", "purpose"}:
            return v in row.get("scene_tags", []) if field == "scene" else (row.get("gift_suitable") if v == "gift" else True)
        if field == "gift_need":
            if v is False:
                return True  # 不送礼撤销适合送礼的要求，不等于禁止购买礼盒。
            return None if row.get("gift_suitable") is None else row["gift_suitable"] == v
        if field == "other":
            if v == "verified_stock_price":
                return row.get("price_fen") is not None and row.get("price_fen") >= 0 and row.get("stock_quantity") is not None and row.get("stock_quantity") > 0
            if v.startswith("sku:"):
                return row["sku_id"] == v[4:]
            if v == "not_near_expiry":
                if not row.get("expires_at"):
                    return None
                expiry = instant(row["expires_at"])
                local = timezone(timedelta(hours=8))
                return expiry > now and (expiry.astimezone(local).date() - now.astimezone(local).date()).days > near_days
        return None
    results = [atom(v) for v in values]
    if op == "exclude":
        return False if True in results else (None if None in results else True)
    return True if True in results else (None if None in results else False)


def active(group, strength=None):
    return [c for c in group["constraints"] if c["status"] == "active" and (strength is None or c["strength"] == strength)]


def quantity_range(group):
    lower, upper, exact = 1, 999, None
    for c in active(group):
        if c["field"] != "quantity":
            continue
        if c["operator"] == "equals":
            if exact is not None and exact != c["value"]:
                return None
            exact = c["value"]
        elif c["operator"] == "at_least":
            lower = max(lower, c["value"])
        else:
            upper = min(upper, c["value"])
    if lower > upper or exact is not None and not lower <= exact <= upper:
        return None
    return (exact, exact) if exact is not None else (lower, upper)
