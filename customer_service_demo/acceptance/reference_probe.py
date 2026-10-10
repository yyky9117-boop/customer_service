"""仅核对指定购物方案和指定优惠组合的参考算术。

不召回商品，不理解自然语言，不构造购物方案，不枚举最优优惠。
数值期望另存为人工确定的常量；生产计价实现不可调用此检查器。
"""

import copy
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"products": "sku_id", "users": "user_id", "activities": "activity_id", "coupons": "coupon_id"}


def read(name, folder="data"):
    return json.loads((ROOT / folder / name).read_text(encoding="utf-8"))


def dataset(fixture_id=None):
    result = {name: {row[KEYS[name]]: row for row in read(name + ".json")} for name in KEYS}
    result["manifest"] = read("manifest.json")
    if fixture_id:
        fixture = next(f for f in read("fixtures.json", "acceptance") if f["fixture_id"] == fixture_id)
        for patch in fixture["patches"]:
            result[patch["collection"]][patch["id"]].update(copy.deepcopy(patch["set"]))
    return result


def dt(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("测试时间必须包含时区")
    return value


def scoped(product, scope):
    return (not scope["categories"] or product["category"] in scope["categories"]) and (
        not scope["sku_ids"] or product["sku_id"] in scope["sku_ids"])


def audience_ok(user, allowed):
    return all(not values or user.get(field) in values for field, values in (
        ("member_level", allowed["member_levels"]), ("customer_type", allowed["customer_types"])))


def allocate(amount, bases):
    """只用于核对固定基准：比例下界 + 最大余数，SKU 破平局。"""
    total = sum(bases.values())
    result = {sku: 0 for sku in bases}
    if not amount:
        return result
    if total <= 0 or amount > total:
        raise ValueError("分摊基数不合法")
    remainders = {}
    for sku, base in bases.items():
        result[sku], remainders[sku] = divmod(amount * base, total)
    remaining = amount - sum(result.values())
    for sku in sorted(bases, key=lambda sku: (-remainders[sku], sku))[:remaining]:
        result[sku] += 1
    return result


def reduction(rule, basis):
    if rule["type"] == "amount_off":
        amount = rule["minus_fen"]
    elif rule["type"] == "percentage":
        amount = (basis * rule["discount_off_bps"] + 5000) // 10000
        if rule.get("cap_fen") is not None:
            amount = min(amount, rule["cap_fen"])
    else:
        raise ValueError("固定单价和赠品须单独核对")
    return min(amount, basis)


def probe(case):
    data = dataset(case["fixture_id"])
    context = case["context"]
    user = data["users"][context["user_id"]]
    now = dt(context["now"])
    channel = context["channel"]
    lines = {item["sku_id"]: item["quantity"] for item in case["items"]}
    products = data["products"]
    def reject(code, basis=None):
        result = {"eligible": False, "rejection_code": code}
        if basis is not None:
            result["eligible_basis_fen"] = basis
        return result
    bases = {}
    for sku, quantity in lines.items():
        p = products[sku]
        if p["price_fen"] is None:
            return reject("product_price_unknown")
        if type(p["price_fen"]) is not int or p["price_fen"] < 0:
            return reject("product_data_invalid")
        if p["stock_quantity"] is None:
            return reject("product_stock_unknown")
        if p["stock_quantity"] < quantity:
            return reject("product_stock_insufficient")
        if p["expires_at"] is None or now >= dt(p["expires_at"]):
            return reject("product_expired" if p["expires_at"] else "product_expiry_unknown")
        bases[sku] = p["price_fen"] * quantity
    activity = data["activities"].get(case["activity_id"])
    coupon = data["coupons"].get(case["coupon_id"])
    if activity and coupon and (not activity["stack_with_coupon"] or not coupon["stack_with_activity"]):
        return reject("stacking_not_allowed")
    activity_alloc = {sku: 0 for sku in lines}
    gifts = []
    activity_discount = 0
    if activity:
        if not activity["rule_complete"]:
            return reject("activity_rule_incomplete")
        if now < dt(activity["start_at"]):
            return reject("activity_not_started")
        if now >= dt(activity["end_at"]):
            return reject("activity_expired")
        if channel not in activity["channels"]:
            return reject("activity_channel")
        if not audience_ok(user, activity["audience"]):
            return reject("activity_audience")
        eligible = {sku: amount for sku, amount in bases.items() if scoped(products[sku], activity["scope"])}
        basis = sum(eligible.values())
        count = sum(lines[sku] for sku in eligible)
        if not eligible:
            return reject("activity_scope_empty")
        if count < activity["minimum_quantity"]:
            return reject("activity_quantity")
        if activity["maximum_quantity"] is not None and count > activity["maximum_quantity"]:
            return reject("activity_quantity_limit")
        if basis < activity["threshold_fen"]:
            return reject("activity_threshold", basis)
        if activity["type"] == "fixed_price":
            for sku in eligible:
                saved = (products[sku]["price_fen"] - activity["fixed_unit_price_fen"]) * lines[sku]
                if saved < 0:
                    return reject("activity_price_not_lower")
                activity_alloc[sku] = saved
            activity_discount = sum(activity_alloc.values())
        elif activity["type"] == "gift":
            gift = activity["gift"]
            gift_product = products[gift["sku_id"]]
            needed = lines.get(gift["sku_id"], 0) + gift["quantity"]
            if gift_product["stock_quantity"] is None or gift_product["stock_quantity"] < needed:
                return reject("gift_stock_insufficient")
            if gift_product["expires_at"] is None or now >= dt(gift_product["expires_at"]):
                return reject("gift_expired")
            gifts = [copy.deepcopy(gift)]
        else:
            activity_discount = reduction(activity, basis)
            activity_alloc.update(allocate(activity_discount, eligible))
    after_activity = {sku: bases[sku] - activity_alloc[sku] for sku in lines}
    coupon_alloc = {sku: 0 for sku in lines}
    coupon_discount = 0
    if coupon:
        if now < dt(coupon["start_at"]):
            return reject("coupon_not_started")
        if now >= dt(coupon["end_at"]):
            return reject("coupon_expired")
        if channel not in coupon["channels"]:
            return reject("coupon_channel")
        if not audience_ok(user, coupon["audience"]):
            return reject("coupon_audience")
        holdings = user["owned_coupons"]
        if holdings is None:
            return reject("coupon_ownership_unknown")
        hold = next((h for h in holdings if h["coupon_id"] == coupon["coupon_id"]), None)
        if hold is None:
            return reject("coupon_claim_required" if coupon["claimable"] else "coupon_not_owned")
        if hold["state"] != "unused":
            return reject("coupon_" + hold["state"])
        eligible = {sku: amount for sku, amount in after_activity.items() if scoped(products[sku], coupon["scope"])}
        basis = sum(eligible.values())
        if not eligible:
            return reject("coupon_scope_empty")
        if basis < coupon["threshold_fen"]:
            return reject("coupon_threshold", basis)
        coupon_discount = reduction(coupon, basis)
        coupon_alloc.update(allocate(coupon_discount, eligible))
    goods = sum(bases.values()) - activity_discount - coupon_discount
    shipping_policy = data["manifest"]["shipping"]
    shipping = shipping_policy["zones"][user["shipping_zone"]]
    # 未知区域仍保留未知；不假设该区域也享受标准包邮规则。
    if shipping is not None and goods >= shipping_policy["free_threshold_fen"]:
        shipping = 0
    return {
        "eligible": True, "activity_discount_fen": activity_discount, "coupon_discount_fen": coupon_discount,
        "merchandise_payable_fen": goods, "shipping_fen": shipping,
        "total_payable_fen": None if shipping is None else goods + shipping,
        "pricing_status": "conditional" if shipping is None else "confirmed", "gifts": gifts,
        "line_allocations": [{"sku_id": sku, "activity_discount_fen": activity_alloc[sku], "coupon_discount_fen": coupon_alloc[sku]} for sku in sorted(lines)],
    }


def check_golden(case):
    actual = probe(case)
    for key, expected in case["expected"].items():
        observed = actual.get(key)
        if key == "line_allocations":
            expected = sorted(expected, key=lambda row: row["sku_id"])
        if observed != expected:
            raise AssertionError(f"{case['id']} {key}: expected {expected!r}, got {observed!r}")
    return actual
