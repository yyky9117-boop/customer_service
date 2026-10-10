"""需求补丁 → 商品准入 → 候选购物方案 → 优惠枚举 → 预算 → 排序 → 事实回复。"""
import hashlib
import copy
import re
from collections import Counter
from fractions import Fraction
from itertools import combinations, product

from .catalog import instant
from .errors import InvalidModelOutput, ServiceFailure
from .module_interface import ModuleResult
from .needs import active, empty_state, merge_patch, predicate, quantity_range
from .pricing import Pricing, distribute


def yuan(fen):
    return f"{fen // 100}.{fen % 100:02d}"


def clarification(question, fields, gid=None):
    return {"question": question, "asked_fields": fields, "group_id": gid}


def budget_check(budget, amount, unit_lines=()):
    if budget["strictness"] == "unknown":
        return True, 0
    if amount is None:
        return False, 0
    lower, upper = budget["min_fen"], budget["max_fen"]
    if budget["scope"] in {"per_unit", "per_recipient"}:
        values = [(payable, qty) for payable, qty in unit_lines]
    else:
        values = [(amount, 1)]
    if not values:
        return False, 0
    penalty = 0
    for value, count in values:
        if upper is not None and value > upper * count:
            if budget["strictness"] == "strict":
                return False, 0
            penalty += value - upper * count
        if lower is not None and value < lower * count:
            if budget.get("min_is_hard", False):
                return False, 0
            penalty += lower * count - value
    return True, penalty


def contradictions(group):
    if quantity_range(group) is None:
        return True
    flags = {c["value"] for c in active(group) if c["field"] == "other" and isinstance(c["value"], str) and c["operator"] != "exclude"}
    if {"no_add_on", "add_on_allowed"} <= flags:
        return True
    if "add_on_allowed" in flags and any(c["field"] == "quantity" and c["operator"] in {"equals", "at_most"} for c in active(group)):
        return True
    # 按同一味觉维度验证显式集合交集，不把无库存误说成需求矛盾。
    for prefix, universe in (("spice:", {"none", "mild", "hot"}),
                             ("sweetness:", {"none", "low", "medium", "high"}),
                             ("sugar:", {"regular", "low_sugar", "sugar_free"})):
        allowed = {prefix + x for x in universe}
        for c in active(group, "hard"):
            values = c["value"] if isinstance(c["value"], list) else [c["value"]]
            if c["field"] == "taste" and all(isinstance(x, str) and x.startswith(prefix) for x in values):
                allowed = allowed - set(values) if c["operator"] == "exclude" else allowed & set(values)
        if not allowed:
            return True
    return False


class RecommendationModule:
    module_id = "product_recommendation"

    def __init__(self, catalog, model, max_carts=5000):
        self.catalog, self.model = catalog, model
        self.pricing = Pricing(catalog)
        self.max_carts = max_carts

    async def handle(self, context):
        # 原料资料缺失时明确解释“无法确认”，不让自由追问覆盖这一关键事实。
        query = re.search(r'\b(P\d{3})\b.*(?:含不含|是否含|有无).*花生',context.message,re.I)
        if query:
            row=self.catalog.products.get(query.group(1).upper())
            if row and any(row.get(k) is None for k in ('ingredients','declared_allergens','may_contain_allergens')):
                ask=clarification('这款商品的原料和过敏原资料尚未完整，暂时无法确认是否含花生。需要按花生过敏要求筛选其它商品吗？',['allergy'])
                state=copy.deepcopy(dict(context.module_state)) or empty_state()
                state['pending_clarification']=ask
                return ModuleResult('needs_clarification',ask['question'],(),ask,state,{'engine':'unknown_ingredient_guard','model_attempts':0})
        if re.search(r'给(?:孩子|宝宝|儿童).*推荐',context.message) and not re.search(r'\d+\s*(?:岁|个月)',context.message):
            ask=clarification('孩子多大了？有没有需要避开的原料或过敏原？',['purpose','allergy'])
            state=copy.deepcopy(dict(context.module_state)) or empty_state();state['pending_clarification']=ask
            return ModuleResult('needs_clarification',ask['question'],(),ask,state,{'engine':'child_need_guard','model_attempts':0})
        payload = {"message": context.message, "module_state": dict(context.module_state),
            "history": list(context.history[-12:]), "selected_reference": context.selected_reference,
            "catalog_identifiers": [{"sku_id": p["sku_id"], "product_name": p["product_name"]} for p in self.catalog.recall()]}
        repair = None
        for attempt in range(2):
            try:
                patch = await self.model.extract(payload, repair=repair)
                state = merge_patch(context.module_state, patch, context)
                self.apply_reference(state, patch, context)
                break
            except InvalidModelOutput as exc:
                repair = str(exc)[:600]
                if attempt:
                    raise ServiceFailure("invalid_model_output", "需求理解结果未通过核验，请重新描述；本轮未保存。", True) from None
        debug = {"model_attempts": attempt + 1, "engine": "structured_recommendation", "rejections": {},
            "quarantined_skus": sorted(self.catalog.quarantined)}
        if patch["intent"] == "unsupported":
            # 未接入模块不修改当前推荐状态。
            previous = dict(context.module_state)
            previous["pending_clarification"] = None
            return ModuleResult("unsupported", "目前可以帮你挑选零食。订单、物流和售后服务尚未接入这个演示。", (), None, previous, debug)
        return self.recommend(state, context, patch["retrieval_hints"], debug)

    @staticmethod
    def apply_reference(state, patch, context):
        if not context.selected_reference or patch["intent"] != "recommend" or patch["clarification"]:
            return
        action, source = patch.get("reference_action"), patch.get("reference_source_text", "")
        if action is None or not source.strip() or source not in context.message:
            raise InvalidModelOutput("选中引用必须给出 reference_action 和当前原话 reference_source_text")
        ref = context.selected_reference
        card = next((c for h in context.history for c in h.get("cards", [])
                     if c["plan_id"] == ref["plan_id"]), None)
        if card is None:
            raise InvalidModelOutput("无法解析所选历史方案")
        group = next((g for g in state["need_groups"] if g["group_id"] == card["group_id"]), None)
        if group is None:
            raise InvalidModelOutput("所选商品的需求组不能被无依据删除")
        cid = "c_selected_" + hashlib.sha256(group["group_id"].encode()).hexdigest()[:16]
        skus = [ref["sku_id"]] if ref["sku_id"] else [i["sku_id"] for i in card["items"]]
        group["constraints"] = [c for c in group["constraints"] if c["constraint_id"] != cid]
        if action == "recommend_alternatives":
            return
        group["constraints"].append({"constraint_id": cid, "field": "other", "operator": "include" if action == "retain_selected" else "exclude",
            "value": ["sku:" + s for s in skus], "strength": "hard", "status": "active", "origin": "explicit",
            "derived_from": [], "source_text": source, "source_turn_id": context.turn_id})

    def recommend(self, state, context, hints=(), debug=None):
        debug = debug or {"engine": "structured_input_offline", "rejections": {}, "quarantined_skus": sorted(self.catalog.quarantined)}
        groups = state["need_groups"]
        def finish(status, text, cards=(), ask=None):
            state["pending_clarification"] = ask
            return ModuleResult(status, text, tuple(cards), ask, state, debug)
        ask = state.get("pending_clarification")
        if ask:
            return finish("needs_clarification", ask["question"], ask=ask)
        if not groups or not (any(active(g) or g["budget"]["strictness"] != "unknown" for g in groups) or state.get("shared_budget")):
            ask = clarification("准备自己吃还是送人？有没有必须避开的口味或原料？", ["purpose", "taste"])
            return finish("needs_clarification", ask["question"], ask=ask)
        if len(groups) > 3:
            ask = clarification("请先选择这次要优先安排的三个对象。", ["purpose"])
            return finish("needs_clarification", ask["question"], ask=ask)
        for group in groups:
            if contradictions(group):
                ask = clarification("同一组要求之间存在冲突，请确认最终的口味或购买数量。", ["conflict"], group["group_id"])
                return finish("needs_clarification", ask["question"], ask=ask)
        budgets = [g["budget"] for g in groups] + ([state["shared_budget"]] if state.get("shared_budget") else [])
        if any(b["strictness"] != "unknown" and b["scope"] == "unknown" for b in budgets):
            ask = clarification("这个预算是每份、商品合计，还是包含运费的整单金额？", ["budget_scope"])
            return finish("needs_clarification", ask["question"], ask=ask)
        if len(groups) > 1 and any(g["budget"]["scope"] == "order_total" and g["budget"]["strictness"] != "unknown" for g in groups):
            ask = clarification("这些对象一起下单吗？含运费预算请给共同总额，或分别给每份预算。", ["budget_scope"])
            return finish("needs_clarification", ask["question"], ask=ask)
        user = self.catalog.user(context.trusted_context.get("user_id", "U_GUEST"))
        if user["shipping_zone"] == "unknown" and any(b["strictness"] == "strict" and b["scope"] == "order_total" for b in budgets):
            ask = clarification("配送区域还未确认，暂时无法核对含运费预算。请先选择演示配送区域。", ["shipping_zone"])
            return finish("needs_clarification", ask["question"], ask=ask)
        candidates, addon_baselines, shape_restricted = [], {}, False
        def limit_result(lower_bound):
            debug.update(candidate_count_lower_bound=lower_bound, search_limit_reached=True)
            question = clarification("可选组合较多，请先指定一种品类或希望优先购买的商品，再为这些对象核算方案。", ["category"])
            return finish("needs_clarification", question["question"], ask=question)
        addon_groups = [g for g in groups if any(c["field"] == "other" and c["value"] == "add_on_allowed" and c["operator"] != "exclude" for c in active(g))]
        if addon_groups and len(groups) > 1:
            ask = clarification("请先确定这次要凑单的一个对象，其他对象可以保持原方案。", ["purpose"])
            return finish("needs_clarification", ask["question"], ask=ask)
        for group in groups:
            admitted = []
            for row in self.catalog.recall(hints):
                sku = row["sku_id"]
                reasons = []
                if row["price_fen"] is None:
                    reasons.append("price_unknown")
                if row["stock_quantity"] is None or row["stock_quantity"] < 1:
                    reasons.append("stock_unknown_or_empty")
                if not row["expires_at"] or context.now >= instant(row["expires_at"]):
                    reasons.append("expired_or_unknown")
                elif not self.catalog.recommendable_expiry(row, context.now):
                    reasons.append("near_expiry_store_policy")
                for c in active(group, "hard"):
                    if predicate(row, c, context.now) is not True:
                        reasons.append("constraint:" + c["constraint_id"])
                if reasons:
                    debug["rejections"].setdefault(group["group_id"], {})[sku] = reasons
                else:
                    admitted.append(row)
            amount = quantity_range(group)[0]
            shape_restricted |= amount > 12 or amount >= 4 and len(admitted) >= 4
            selections = []
            for row in admitted:
                if row["stock_quantity"] >= amount:
                    selections.append({row["sku_id"]: amount})
            # 精确数量不增加；小批量混搭最多三个 SKU，枚举数量分割。
            if 2 <= amount <= 20:
                for a, b in combinations(admitted, 2):
                    for qty_a in range(1, amount):
                        if qty_a <= a["stock_quantity"] and amount - qty_a <= b["stock_quantity"]:
                            selections.append({a["sku_id"]: qty_a, b["sku_id"]: amount - qty_a})
                            if len(selections) > self.max_carts:
                                return limit_result(len(selections))
            if 3 <= amount <= 12:
                for a, b, c in combinations(admitted, 3):
                    for qty_a in range(1, amount - 1):
                        for qty_b in range(1, amount - qty_a):
                            qty_c = amount - qty_a - qty_b
                            if qty_a <= a["stock_quantity"] and qty_b <= b["stock_quantity"] and qty_c <= c["stock_quantity"]:
                                selections.append({a["sku_id"]: qty_a, b["sku_id"]: qty_b, c["sku_id"]: qty_c})
                                if len(selections) > self.max_carts:
                                    return limit_result(len(selections))
            if not selections:
                if admitted and sum(p["stock_quantity"] for p in admitted) >= amount:
                    ask = clarification("当前数量需要拆分多种商品，请先明确希望如何分配各商品数量。", ["quantity"], group["group_id"])
                    return finish("needs_clarification", ask["question"], ask=ask)
                return finish("no_match", "现有演示商品中没有能同时满足这组硬要求和购买数量的方案。可以修改其中一项要求后再找。")
            if addon_groups:
                add_on_pool = [p for p in self.catalog.recall() if p["price_fen"] is not None and p["stock_quantity"] is not None
                    and p["stock_quantity"] > 0 and self.catalog.recommendable_expiry(p, context.now)
                    and all(predicate(p, c, context.now) is True for c in active(group, "hard")
                        if not (c["field"] == "other" and isinstance(c["value"], str) and c["value"].startswith("sku:")))]
                additions = []
                for baseline in selections:
                    if len(baseline) >= 3:
                        continue
                    for row in add_on_pool:
                        sku = row["sku_id"]
                        if sku in baseline:
                            continue
                        for qty in (1, 2):
                            if row["stock_quantity"] >= qty:
                                enlarged = {**baseline, sku: qty}
                                signature = tuple(sorted(enlarged.items()))
                                if signature not in addon_baselines:
                                    additions.append(enlarged)
                                    addon_baselines[signature] = tuple(sorted(baseline.items()))
                                    if len(selections) + len(additions) > self.max_carts:
                                        return limit_result(len(selections) + len(additions))
                selections += additions
            candidates.append(selections)
        combination_count = 1
        for choices in candidates:
            combination_count *= len(choices)
        debug["candidate_count"] = combination_count
        if combination_count > self.max_carts:
            return limit_result(combination_count)
        options, cache = {}, {}
        for assignment in product(*candidates):
            cart = Counter()
            for choice in assignment:
                cart.update(choice)
            signature = tuple(sorted(cart.items()))
            items = [{"sku_id": s, "quantity": q} for s, q in signature]
            def gift_filter(row):
                # 赠品归属尚不确定时按各组硬排除的并集核验。
                return self.catalog.recommendable_expiry(row, context.now) and all(predicate(row, c, context.now) is True for g in groups for c in active(g, "hard")
                    if c["operator"] == "exclude" or c["field"] in {"taste", "ingredient"}
                    or c["field"] == "package" and c["value"] in ("small", "wrapped")
                    or c["field"] == "other" and c["value"] == "not_near_expiry")
            if signature not in cache:
                cache[signature] = list(self.pricing.alternatives(items, user, context.now,
                    context.trusted_context.get("channel", "web"), gift_filter))
            for quote in cache[signature]:
                group_lines = [dict() for _ in groups]
                for line in quote["line_allocations"]:
                    sku = line["sku_id"]
                    weights = {str(index): self.catalog.products[sku]["price_fen"] * choice[sku]
                               for index, choice in enumerate(assignment) if sku in choice}
                    for index, value in distribute(line["payable_fen"], weights).items():
                        group_lines[int(index)][sku] = value
                feasible, penalty = True, 0
                for index, group in enumerate(groups):
                    b = group["budget"]
                    measure = quote["total_payable_fen"] if b["scope"] == "order_total" else sum(group_lines[index].values())
                    ok, distance = budget_check(b, measure, [(v, assignment[index][s]) for s, v in group_lines[index].items()])
                    feasible &= ok
                    penalty += distance
                if state.get("shared_budget"):
                    b = state["shared_budget"]
                    measure = quote["total_payable_fen"] if b["scope"] == "order_total" else quote["merchandise_payable_fen"]
                    ok, distance = budget_check(b, measure)
                    feasible &= ok
                    penalty += distance
                if not feasible:
                    continue
                explicit, scene = Fraction(0), Fraction(0)
                for index, group in enumerate(groups):
                    denominator = sum(assignment[index].values())
                    for c in active(group, "soft"):
                        matches = sum(q for sku, q in assignment[index].items() if predicate(self.catalog.products[sku], c, context.now) is True)
                        if c.get("origin", "explicit") == "inferred" or c["field"] == "scene":
                            scene += Fraction(matches, denominator)
                        else:
                            explicit += Fraction(matches, denominator)
                spent = quote["total_payable_fen"]
                if spent is None:
                    spent = quote["merchandise_payable_fen"]
                # 未知总价不与已确认总价按相同口径竞争。
                history_match = sum(self.catalog.products[s].get("sugar_label") in user.get("preferred_tags", []) for s in cart)
                score = (-explicit, -scene, penalty, quote["pricing_status"] != "confirmed", spent,
                    sum(cart.values()), -history_match, len(cart), quote["activity_id"] is not None,
                    quote["coupon_id"] is not None, signature, quote["activity_id"] or "", quote["coupon_id"] or "")
                option = (score, signature, assignment, quote, penalty)
                if signature not in options or score < options[signature][0]:
                    options[signature] = option
        debug["priced_cart_count"] = len(cache)
        if not options:
            if shape_restricted:
                ask = clarification("在当前组合范围内还没有找到预算内方案，请先明确各商品的数量分配，再继续核算。", ["quantity"])
                debug["search_shape_restricted"] = True
                return finish("needs_clarification", ask["question"], ask=ask)
            return finish("no_match", "现有演示商品和已确认优惠中，没有同时满足购买数量及预算的方案。没有擅自放宽预算或减少数量。")
        cards, seen = [], set()
        for score, signature, assignment, quote, penalty in sorted(options.values(), key=lambda x: x[0]):
            dominant = min(signature, key=lambda item: (-item[1], item[0]))[0]
            if dominant in seen:
                continue
            seen.add(dominant)
            if signature in addon_baselines:
                baseline = addon_baselines[signature]
                baseline_items = [{"sku_id": s, "quantity": q} for s, q in baseline]
                baseline_quotes = list(self.pricing.alternatives(baseline_items, user, context.now,
                    context.trusted_context.get("channel", "web"), gift_filter))
                base_quote = min(baseline_quotes, key=lambda q: (q["total_payable_fen"] is None,
                    q["total_payable_fen"] if q["total_payable_fen"] is not None else q["merchandise_payable_fen"]))
                quote = {**quote, "baseline_quote": base_quote, "baseline_items": baseline}
            card = self.card(groups, signature, assignment, quote, len(cards) + 1, context, penalty)
            cards.append(card)
            if len(cards) == 3:
                break
        content = "根据这次需求，以下是可选方案；每张卡片是一种购买选择。\n" + "\n".join(self.describe(c) for c in cards)
        return finish("completed", content, cards)

    def card(self, groups, signature, assignment, quote, rank, context, penalty):
        items = [{"sku_id": s, "product_name": self.catalog.products[s]["product_name"], "quantity": q,
            "unit_price_fen": self.catalog.products[s]["price_fen"], "image_url": self.catalog.products[s]["image_url"]} for s, q in signature]
        discounts = []
        for kind, id_key, amount_key, collection in (
            ("activity", "activity_id", "activity_discount_fen", "activities"), ("coupon", "coupon_id", "coupon_discount_fen", "coupons")):
            rid = quote[id_key]
            if rid:
                discounts.append({"kind": kind, "id": rid, "label": self.catalog.data[collection][rid]["name"],
                    "amount_fen": quote[amount_key], "eligibility": "eligible"})
        conditions = ["配送区域未确认，运费及整单应付待确认。"] if quote["shipping_fen"] is None else []
        notes = ["商品、身份与优惠为演示资料。"]
        if penalty:
            notes.append("此备选偏离近似预算目标或软下界；请以展示金额判断是否接受。")
        if len(groups) > 1:
            for index, selection in enumerate(assignment, 1):
                notes.append(f"需求组 {index}：" + "、".join(f"{self.catalog.products[s]['product_name']} × {q}" for s, q in sorted(selection.items())))
            notes.append("以上对象合并为一个订单计价；优惠券只使用一次。")
        reasons = ["符合已确认的硬条件和基础购买数量。"]
        preference_names = {"sugar:low_sugar": "低糖", "sugar:sugar_free": "无糖", "wrapped": "独立包装", "small": "独立小包"}
        for group, selection in zip(groups, assignment):
            for c in active(group, "soft"):
                if c.get("origin", "explicit") == "inferred" or c["field"] == "scene":
                    continue
                value = c["value"][0] if isinstance(c["value"], list) and len(c["value"]) == 1 else c["value"]
                label = preference_names.get(value, "口味、包装等") if isinstance(value, str) else "口味、包装等"
                complete = all(predicate(self.catalog.products[s], c, context.now) is True for s in selection)
                if complete:
                    reason = f"匹配你提出的{label}偏好。"
                    if reason not in reasons and len(reasons) < 3:
                        reasons.append(reason)
                else:
                    note = f"这套未完全匹配{label}偏好。"
                    if note not in notes:
                        notes.append(note)
        if len(reasons) < 3:
            reasons.append("商品应付和运费分别列出，便于比较实际支出。")
        kind = "bundle" if len(items) > 1 else ("multi_quantity" if items[0]["quantity"] > 1 else "single_product")
        if "baseline_quote" in quote:
            kind = "add_on"
            base = quote["baseline_quote"]
            base_names = "、".join(f"{self.catalog.products[s]['product_name']} × {q}" for s, q in quote["baseline_items"])
            notes.append(f"不凑单基线：{base_names}，商品应付 {yuan(base['merchandise_payable_fen'])} 元。")
            if base["total_payable_fen"] is not None and quote["total_payable_fen"] is not None:
                delta = quote["total_payable_fen"] - base["total_payable_fen"]
                change = f"增加 {yuan(delta)} 元" if delta >= 0 else f"减少 {yuan(-delta)} 元"
                notes.append(f"凑单比较：基线含运费 {yuan(base['total_payable_fen'])} 元，加购后总支出{change}。")
            else:
                notes.append("凑单比较：配送区域未知，暂时不能判断含运费总支出的变化。")
            extra_quantity = sum(q for _, q in signature) - sum(q for _, q in quote["baseline_items"])
            notes.append(f"可选凑单额外购买 {extra_quantity} 件，不是必需购买量。")
        token = hashlib.sha256((context.turn_id + repr(signature) + str(quote["activity_id"]) + str(quote["coupon_id"])).encode()).hexdigest()[:20]
        card = {"type": "recommendation_plan", "plan_id": "plan_" + token, "plan_type": kind,
            "title": "首选方案" if rank == 1 else f"备选方案 {rank - 1}", "rank": rank, "group_id": groups[0]["group_id"], "items": items,
            "pricing": {"currency": "CNY", "status": quote["pricing_status"],
                "subtotal_fen": sum(i["quantity"] * i["unit_price_fen"] for i in items), "discounts": discounts,
                "merchandise_payable_fen": quote["merchandise_payable_fen"], "shipping_fen": quote["shipping_fen"],
                "total_payable_fen": quote["total_payable_fen"], "conditions": conditions},
            "reasons": reasons, "notes": notes}
        if quote["gifts"]:
            card["gifts"] = [{"sku_id": g["sku_id"], "product_name": self.catalog.products[g["sku_id"]]["product_name"],
                "quantity": g["quantity"], "image_url": self.catalog.products[g["sku_id"]]["image_url"]} for g in quote["gifts"]]
        return card

    @staticmethod
    def describe(card):
        p = card["pricing"]
        names = "、".join(f"{i['product_name']} × {i['quantity']}" for i in card["items"])
        text = f"{card['rank']}. {names}；商品应付 {yuan(p['merchandise_payable_fen'])} 元"
        if p["shipping_fen"] is None:
            text += "，运费和整单总价待确认。"
        else:
            text += f"，运费 {yuan(p['shipping_fen'])} 元，整单 {yuan(p['total_payable_fen'])} 元。"
        if p["discounts"]:
            text += " 使用：" + "、".join(d["label"] for d in p["discounts"]) + "。"
        if card.get("gifts"):
            text += " 另赠：" + "、".join(f"{g['product_name']} × {g['quantity']}" for g in card["gifts"]) + "。"
        text += " " + " ".join(n for n in card["notes"] if "临期" in n or "预算目标" in n or "凑单" in n or "偏好" in n)
        return text.strip()
