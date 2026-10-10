"""事实支持的推荐理由 → 客服话术 → 独立质检。自由生成不能修改方案事实。"""
import asyncio
import copy
import json
import re
from dataclasses import replace
from decimal import Decimal

from jsonschema import Draft202012Validator

from .errors import InvalidModelOutput, ServiceFailure
from .needs import active, predicate


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


REASONS_SCHEMA = obj({"plans": {"type": "array", "minItems": 1, "maxItems": 3, "items": obj({
    "plan_id": {"type": "string"}, "reasons": {"type": "array", "minItems": 2, "maxItems": 2,
        "items": obj({"text": {"type": "string", "minLength": 8, "maxLength": 100},
            "fact_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "uniqueItems": True}})}})}})
TEMPLATE_SCHEMA = obj({"template": {"type": "string", "minLength": 20, "maxLength": 1500}})
REVIEW_SCHEMA = obj({"pass": {"type": "boolean"}, "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
    "rewrite_instruction": {"type": "string", "maxLength": 1200}})
TOKEN = re.compile(r"\{\{([a-z0-9_]+)\}\}")
FORBIDDEN = ("全网最低", "绝对好吃", "一定满意", "闭眼入", "保证最划算", "现在不买就没了", "最后一件",
    "治愈", "治疗", "减肥", "降血糖", "算法", "硬条件", "系统匹配", "recommendation_rank", "场景标签", "自有标签", "直接对应", "已核验", "```", "<script")


def validate(schema, value):
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise InvalidModelOutput("输出结构未通过校验：" + errors[0].message[:200])


def language(text):
    if any(word in text for word in FORBIDDEN):
        raise InvalidModelOutput("回复包含夸大、无依据功效或内部术语")


def amount(fen):
    return f"{fen // 100}.{fen % 100:02d}"


def need_summary(state):
    parts = []
    labels = {"wrapped": "独立包装", "small": "独立小包", "gift_box": "礼盒包装", "spice:none": "不辣",
        "spice:mild": "微辣", "spice:hot": "辣味", "sugar:low_sugar": "低糖", "sugar:sugar_free": "无糖",
        "sweetness:low": "低甜度", "office": "办公室吃", "gift": "送人", "not_near_expiry": "不要临期",
        "nuts": "坚果", "biscuits": "饼干", "drinks": "饮料", "no_add_on": "不额外加购"}
    groups = state["need_groups"]
    if len(groups) > 1:
        parts.append("给不同对象分别搭配")
    else:
        for c in active(groups[0]):
            if c["origin"] == "inferred":
                continue
            v = c["value"]
            if c["field"] == "quantity":
                parts.append({"equals": "买", "at_least": "至少买", "at_most": "最多买"}[c["operator"]] + str(v) + "件")
                continue
            if isinstance(v, list) and len(v) == 1:
                v = v[0]
            label = labels.get(v) if isinstance(v, str) else None
            if label:
                label = ("不要" + label) if c["operator"] == "exclude" else label
                if c["strength"] == "soft" and c["field"] != "scene":
                    label = "优先" + label
                if label not in parts:
                    parts.append(label)
    if len(parts) > 3:
        preferences = [p for p in parts if p.startswith("优先")]
        parts = parts[:2] + (preferences[:1] or parts[2:3])
    budgets = [state.get("shared_budget")] + [g["budget"] for g in groups]
    for b in budgets:
        if not b or b["max_fen"] is None:
            continue
        prefix = {"order_total": "含运费", "goods_total": "商品合计", "per_unit": "每件", "per_recipient": "每份"}.get(b["scope"], "预算")
        parts.append(prefix + ("最多" if b["strictness"] == "strict" else "约") + amount(b["max_fen"]) + "元")
        break
    return "、".join(parts) or "你这次的零食需求"


class ReplyGenerator:
    def __init__(self, model, catalog, timeout_seconds=90):
        self.model, self.catalog, self.timeout_seconds = model, catalog, timeout_seconds

    def packet(self, result, context):
        tokens = {"need_summary": need_summary(result.next_module_state),
            "follow_up": "想改数量、预算或搭配，可以选中卡片上的商品继续聊。"}
        plans = []
        for n, card in enumerate(result.cards, 1):
            facts = []
            def fact(kind, text):
                facts.append({"id": f"p{n}_f{len(facts) + 1}", "kind": kind, "text": text})
            for item in card["items"]:
                row = self.catalog.products[item["sku_id"]]
                name = item["product_name"]
                fact("purchase", f"{name}，购买{item['quantity']}件。")
                properties = (("spice_level", {"none": "不辣", "mild": "微辣", "hot": "辣味"}),
                    ("sugar_label", {"low_sugar": "低糖标签", "sugar_free": "无糖标签", "regular": "普通糖标签"}),
                    ("sweetness", {"none": "无甜味标签", "low": "低甜度标签", "medium": "中甜度标签", "high": "高甜度标签"}))
                for field, names in properties:
                    if row.get(field) in names:
                        fact(field, name + "：" + names[row[field]] + "。")
                p = row.get("packaging") or {}
                if p.get("individually_wrapped") is not None:
                    fact("packaging", name + ("：独立内包装。" if p["individually_wrapped"] else "：不是独立内包装。"))
                if p.get("inner_weight_g") is not None:
                    fact("packaging", f"{name}：已知单个内包{p['inner_weight_g']}克。")
                if p.get("outer_type") == "gift_box":
                    fact("packaging", name + "：外包装为礼盒。")
                for field in ("ingredients", "declared_allergens", "may_contain_allergens", "selling_points", "scene_tags"):
                    if row.get(field) is not None:
                        fact(field, name + "：" + field + "=" + repr(row[field]))
                if row.get("expires_at"):
                    fact("expiry", name + "：标注到期时间 " + row["expires_at"])
            p = card["pricing"]
            price = ("含运费" + amount(p["total_payable_fen"]) + "元") if p["total_payable_fen"] is not None else (
                "商品应付" + amount(p["merchandise_payable_fen"]) + "元，运费和整单金额待确认")
            fact("pricing", price + "；商品应付" + amount(p["merchandise_payable_fen"]) + "元。")
            for group in result.next_module_state["need_groups"]:
                for c in active(group):
                    if c["field"] == "quantity":
                        continue
                    matched = [i["product_name"] for i in card["items"] if predicate(self.catalog.products[i["sku_id"]], c, context.now) is True]
                    if matched:
                        fact("need_match", f"对于需求 {c['field']} {c['operator']} {c['value']}，符合的商品仅为：{'、'.join(matched)}。")
            for note in card["notes"] + p["conditions"]:
                fact("boundary", note)
            tokens[f"p{n}_items"] = " + ".join(i["product_name"] + "（" + str(i["quantity"]) + "件）" for i in card["items"])
            tokens[f"p{n}_price"] = price
            if n > 1:
                first = result.cards[0]
                differences = []
                if len(card["items"]) > 1 and len(first["items"]) == 1:
                    differences.append("换成不同商品的组合")
                if p["total_payable_fen"] is not None and first["pricing"]["total_payable_fen"] is not None:
                    delta = p["total_payable_fen"] - first["pricing"]["total_payable_fen"]
                    differences.append("总价比首选" + ("更低" if delta < 0 else "高一些" if delta > 0 else "相同"))
                if differences:
                    tokens[f"p{n}_difference"] = "，".join(differences)
                    fact("comparison", tokens[f"p{n}_difference"] + "。")
            notices = [s for s in card["notes"] if any(t in s for t in ("临期", "预算目标", "偏好", "凑单", "额外购买"))] + p["conditions"]
            notices = [s.replace("配送区域未确认，运费及整单应付待确认。", "配送区域尚未确认。") for s in notices]
            if notices:
                tokens[f"p{n}_notice"] = " ".join(notices)
            offer = [d["label"] for d in p["discounts"]]
            if card.get("gifts"):
                offer.append("另赠" + "、".join(g["product_name"] + str(g["quantity"]) + "件" for g in card["gifts"]))
            if offer:
                tokens[f"p{n}_offer"] = "已计入" + "、".join(offer) + "。"
                fact("offer", tokens[f"p{n}_offer"])
            plans.append({"plan_id": card["plan_id"], "rank": card["rank"], "items": list(card["items"]),
                "pricing": dict(p), "facts": facts})
        return {"user_message": context.message, "need_summary": tokens["need_summary"],
            "needs": copy.deepcopy(dict(result.next_module_state)), "plans": plans, "tokens": tokens}

    def validate_reasons(self, output, packet):
        validate(REASONS_SCHEMA, output)
        if [p["plan_id"] for p in output["plans"]] != [p["plan_id"] for p in packet["plans"]]:
            raise InvalidModelOutput("理由方案编号和顺序必须与输入完全一致")
        for generated, plan in zip(output["plans"], packet["plans"]):
            ids = {f["id"] for f in plan["facts"] if f["kind"] not in {"expiry", "boundary"}}
            texts = []
            for reason in generated["reasons"]:
                if not set(reason["fact_ids"]) <= ids:
                    raise InvalidModelOutput("推荐理由引用了不存在或其他方案的事实")
                language(reason["text"])
                if any(word in reason["text"] for word in ("到期", "有效期", "保质期")):
                    raise InvalidModelOutput("推荐理由解释适配点即可，日期和风险提醒已单独展示，不能重复")
                counts = {"一":1, "两":2, "二":2, "三":3, "四":4, "五":5, "六":6, "七":7, "八":8, "九":9}
                for match in re.finditer(r"([一两二三四五六七八九]|\d+)件商品(?:都|均)", reason["text"]):
                    count = counts.get(match.group(1)) or int(match.group(1))
                    if count != sum(i["quantity"] for i in plan["items"]):
                        raise InvalidModelOutput("理由把商品种类数误说成购买件数，请用两款/两种或省略数量")
                if "元" in reason["text"] or "优惠券" in reason["text"]:
                    raise InvalidModelOutput("理由不要重复价格或编写优惠资格，使用专门的价格/优惠占位符")
                texts.append(reason["text"])
            if len(set(texts)) != len(texts):
                raise InvalidModelOutput("理由重复，请分别解释不同适配点")

    def render(self, output, packet):
        validate(TEMPLATE_SCHEMA, output)
        template = output["template"]
        tokens = packet["tokens"]
        keys = TOKEN.findall(template)
        if any(key not in tokens for key in keys):
            raise InvalidModelOutput("话术使用了未知事实占位符")
        remainder = TOKEN.sub("", template)
        if any(word in remainder for word in ("下单", "商品页", "商品详情页", "跟店铺确认")):
            raise InvalidModelOutput("演示没有商品页或下单功能，请用follow_up占位符引导继续对话")
        if keys.count("follow_up") != 1 or not template.rstrip().endswith("{{follow_up}}"):
            raise InvalidModelOutput("请以唯一的{{follow_up}}结束回复，引导选中商品继续对话")
        # 可以如实复述开头的预算；不把正确的“20元预算”误判为改写商品价格。
        budget_values = {b[key] for b in [packet["needs"].get("shared_budget")] + [g["budget"] for g in packet["needs"]["need_groups"]]
                         if b for key in ("min_fen", "max_fen") if b[key] is not None}
        opening_end = template.find("{{p1_items}}")
        opening = template[:opening_end] if opening_end >= 0 else ""
        allowed = []
        for match in re.finditer(r"\d+(?:\.\d{1,2})?\s*元", opening):
            fen = Decimal(match.group().replace("元", "").strip()) * 100
            if fen in budget_values:
                allowed.append(match.group())
        for value in allowed:
            remainder = remainder.replace(value, "", 1)
        if "{" in remainder or "}" in remainder or re.search(r"\d", remainder):
            raise InvalidModelOutput("商品、数量、价格、日期必须由已提供的占位符引用，不能自行填写数字")
        allowed_names = {i["product_name"] for p in packet["plans"] for i in p["items"]}
        for row in self.catalog.recall():
            if row["product_name"] in remainder and row["product_name"] not in allowed_names:
                raise InvalidModelOutput("不能提及本轮推荐方案之外的商品")
        positions = []
        for n in range(1, len(packet["plans"]) + 1):
            for suffix in ("items", "price"):
                if keys.count(f"p{n}_{suffix}") != 1:
                    raise InvalidModelOutput("必须恰好引用一次 {{p" + str(n) + "_" + suffix + "}}，不要把其中数字或名称手工抄入正文。")
            if not any(key.startswith(f"p{n}_reason_") for key in keys):
                raise InvalidModelOutput("每个方案至少使用一条具体推荐理由")
            for suffix in ("notice", "offer", "difference"):
                key = f"p{n}_{suffix}"
                if key in tokens and keys.count(key) != 1:
                    raise InvalidModelOutput("遗漏或重复必需占位符 {{" + key + "}}，请恰好引用一次；其它必需事实占位符也须保留。")
            positions.append(template.index("{{p" + str(n) + "_items}}"))
        if positions != sorted(positions):
            raise InvalidModelOutput("客服话术改变了推荐顺序")
        rendered = TOKEN.sub(lambda match: tokens[match.group(1)], template).strip()
        rendered = re.sub(r"[。！？]+(?=[，；])", "", rendered)
        language(rendered)
        if not rendered.startswith("亲～") or rendered.count("😊") != 1:
            raise InvalidModelOutput("沿用原客服风格：以亲～开头且恰好使用一次😊")
        other = rendered.replace("😊", "")
        if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", other):
            raise InvalidModelOutput("请只使用一个😊，不添加其他表情")
        required = sum(len(tokens[f"p{n}_items"]) + len(tokens[f"p{n}_price"]) +
            min(len(v) for k,v in tokens.items() if k.startswith(f"p{n}_reason_")) +
            sum(len(tokens.get(f"p{n}_{s}", "")) for s in ("notice", "offer", "difference"))
            for n in range(1, len(packet["plans"]) + 1))
        if len(rendered) > max(360, required + 100):
            raise InvalidModelOutput("话术过长，请压缩到约300字；每方案仅一句理由，不重复需求和差异，不漏必要提醒")
        return rendered

    async def enrich(self, result, context):
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._enrich(result, context)
        except TimeoutError:
            raise ServiceFailure("model_timeout", "推荐理由和客服回复生成超时，本轮未保存，请重试原消息。", True) from None

    async def _enrich(self, result, context):
        packet = self.packet(result, context)
        repair = None
        for attempt in range(2):
            reasons = None
            try:
                reason_packet = {"user_message": packet["user_message"], "need_summary": packet["need_summary"],
                    "plans": [{"plan_id":p["plan_id"], "rank":p["rank"], "items":p["items"],
                        "facts":[f for f in p["facts"] if f["kind"] not in {"expiry", "boundary"}]} for p in packet["plans"]]}
                reasons = await self.model.generate_reasons(reason_packet, repair=repair)
                self.validate_reasons(reasons, packet)
                break
            except InvalidModelOutput as exc:
                repair = str(exc)[:600] + "\n上次错误输出：" + json.dumps(reasons, ensure_ascii=False)
                if attempt:
                    raise ServiceFailure("invalid_model_output", "推荐理由未通过事实核验，本轮未保存，请重试。", True) from None
        packet["reason_candidates"] = reasons
        attempt_reasons = attempt
        cards = copy.deepcopy(list(result.cards))
        for n, (card, generated) in enumerate(zip(cards, reasons["plans"]), 1):
            card["reasons"] = [r["text"] for r in generated["reasons"]]
            for index, reason in enumerate(generated["reasons"], 1):
                packet["tokens"][f"p{n}_reason_{index}"] = reason["text"].rstrip("。")
        repair = None
        for attempt in range(2):
            draft = None
            try:
                draft = await self.model.compose_reply(packet, repair=repair)
                content = self.render(draft, packet)
                review = await self.model.review_reply({"user_message": context.message, "need_summary": packet["need_summary"],
                    "plans": packet["plans"], "protected_tokens": packet["tokens"], "generated_reasons": reasons,
                    "template": draft["template"], "reply": content})
                validate(REVIEW_SCHEMA, review)
                if review["pass"] and not review["issues"] and not review["rewrite_instruction"]:
                    return replace(result, content=content, cards=tuple(cards), debug={**result.debug,
                        "reply_mode": "deepseek_reasons_composition_review", "reason_attempts": attempt_reasons + 1,
                        "reply_attempts": attempt + 1, "reply_review_passed": True})
                raise InvalidModelOutput("独立质检未通过：" + "；".join(review["issues"]) + "；" + review["rewrite_instruction"])
            except InvalidModelOutput as exc:
                repair = str(exc)[:1000] + "\n上次错误草稿（不是事实）：" + json.dumps(draft, ensure_ascii=False)
                if attempt:
                    raise ServiceFailure("invalid_model_output", "客服回复未通过事实与表达核验，本轮未保存，请重试。", True) from None
