"""生产计价：独立实现，不依赖 acceptance 的参考探针。"""
from collections import Counter
from itertools import product

from .catalog import instant


def distribute(amount, weights):
    result = dict.fromkeys(weights, 0)
    denominator = sum(weights.values())
    if amount == 0:
        return result
    if not 0 <= amount <= denominator:
        raise ValueError("非法优惠分摊")
    fractions = []
    for sku, weight in weights.items():
        result[sku], remainder = divmod(weight * amount, denominator)
        fractions.append((-remainder, sku))
    for _, sku in sorted(fractions)[:amount - sum(result.values())]:
        result[sku] += 1
    return result


def in_scope(row, rule):
    scope = rule["scope"]
    return all(not allowed or value in allowed for allowed, value in (
        (scope["categories"], row["category"]), (scope["sku_ids"], row["sku_id"])))


def discount(rule, basis):
    if rule["type"] == "amount_off":
        value = rule["minus_fen"]
    elif rule["type"] == "percentage":
        value = (basis * rule["discount_off_bps"] + 5000) // 10000
        if rule.get("cap_fen") is not None:
            value = min(value, rule["cap_fen"])
    else:
        raise ValueError("不支持的优惠类型")
    if type(value) is not int or value < 0:
        raise ValueError("异常优惠金额")
    return min(basis, value)


class Pricing:
    def __init__(self, catalog):
        self.catalog = catalog

    def quote(self, items, user, now, channel="web", activity_id=None, coupon_id=None,
              gift_filter=None, allow_claim=False):
        data, products = self.catalog.data, self.catalog.products
        def no(code, basis=None):
            result = {"eligible": False, "rejection_code": code}
            if basis is not None:
                result["eligible_basis_fen"] = basis
            return result
        if now.tzinfo is None:
            raise ValueError("计价时间必须包含时区")
        quantities = Counter()
        for item in items:
            if type(item["quantity"]) is not int or not 1 <= item["quantity"] <= 999:
                return no("product_quantity_invalid")
            quantities[item["sku_id"]] += item["quantity"]
        if not quantities:
            return no("empty_cart")
        original = {}
        for sku, quantity in sorted(quantities.items()):
            row = products.get(sku)
            if not row:
                return no("product_data_invalid" if sku in self.catalog.quarantined else "product_not_found")
            if row["price_fen"] is None:
                return no("product_price_unknown")
            if row["stock_quantity"] is None:
                return no("product_stock_unknown")
            if row["stock_quantity"] < quantity:
                return no("product_stock_insufficient")
            if row["expires_at"] is None:
                return no("product_expiry_unknown")
            if now >= instant(row["expires_at"]):
                return no("product_expired")
            original[sku] = row["price_fen"] * quantity
        activity = data["activities"].get(activity_id)
        coupon = data["coupons"].get(coupon_id)
        if activity_id and not activity:
            return no("activity_not_found")
        if coupon_id and not coupon:
            return no("coupon_not_found")
        try:
            if activity and coupon and not (activity["stack_with_coupon"] and coupon["stack_with_activity"]):
                return no("stacking_not_allowed")
            def eligibility(rule, prefix):
                if prefix == "activity" and not rule["rule_complete"]:
                    return prefix + "_rule_incomplete"
                if now < instant(rule["start_at"]):
                    return prefix + "_not_started"
                if now >= instant(rule["end_at"]):
                    return prefix + "_expired"
                if channel not in rule["channels"]:
                    return prefix + "_channel"
                audience = rule["audience"]
                if any(allowed and user.get(field) not in allowed for field, allowed in (
                    ("member_level", audience["member_levels"]), ("customer_type", audience["customer_types"]))):
                    return prefix + "_audience"
                return None
            a_alloc, c_alloc = dict.fromkeys(original, 0), dict.fromkeys(original, 0)
            gifts = []
            if activity:
                failure = eligibility(activity, "activity")
                if failure:
                    return no(failure)
                bases = {s: v for s, v in original.items() if in_scope(products[s], activity)}
                basis, count = sum(bases.values()), sum(quantities[s] for s in bases)
                if not bases:
                    return no("activity_scope_empty")
                if count < activity["minimum_quantity"]:
                    return no("activity_quantity")
                if activity["maximum_quantity"] is not None and count > activity["maximum_quantity"]:
                    return no("activity_quantity_limit")
                if basis < activity["threshold_fen"]:
                    return no("activity_threshold", basis)
                if activity["type"] == "fixed_price":
                    fixed = activity["fixed_unit_price_fen"]
                    if type(fixed) is not int or fixed < 0:
                        return no("activity_rule_incomplete")
                    if any(products[s]["price_fen"] < fixed for s in bases):
                        return no("activity_price_not_lower")
                    a_alloc.update({s: (products[s]["price_fen"] - fixed) * quantities[s] for s in bases})
                elif activity["type"] == "gift":
                    gift = activity["gift"]
                    g = products.get(gift["sku_id"])
                    if not g or g["stock_quantity"] is None or g["stock_quantity"] < quantities[g["sku_id"]] + gift["quantity"]:
                        return no("gift_stock_insufficient")
                    if not g["expires_at"] or now >= instant(g["expires_at"]):
                        return no("gift_expired")
                    if gift_filter and not gift_filter(g):
                        return no("gift_hard_constraint")
                    gifts = [dict(gift)]
                else:
                    a_alloc.update(distribute(discount(activity, basis), bases))
            residual = {s: v - a_alloc[s] for s, v in original.items()}
            conditional = False
            if coupon:
                failure = eligibility(coupon, "coupon")
                if failure:
                    return no(failure)
                holds = user["owned_coupons"]
                if holds is None:
                    return no("coupon_ownership_unknown")
                hold = next((h for h in holds if h["coupon_id"] == coupon_id), None)
                if hold is None:
                    if not (allow_claim and coupon["claimable"]):
                        return no("coupon_claim_required" if coupon["claimable"] else "coupon_not_owned")
                    conditional = True
                elif hold["state"] != "unused":
                    return no("coupon_" + hold["state"])
                bases = {s: v for s, v in residual.items() if in_scope(products[s], coupon)}
                basis = sum(bases.values())
                if not bases:
                    return no("coupon_scope_empty")
                if basis < coupon["threshold_fen"]:
                    return no("coupon_threshold", basis)
                c_alloc.update(distribute(discount(coupon, basis), bases))
            a_total, c_total = sum(a_alloc.values()), sum(c_alloc.values())
            goods = sum(original.values()) - a_total - c_total
            policy = data["manifest"]["shipping"]
            shipping = policy["zones"].get(user.get("shipping_zone"))
            if shipping is not None and goods >= policy["free_threshold_fen"]:
                shipping = 0
            return {
                "eligible": True, "activity_id": activity_id, "coupon_id": coupon_id,
                "activity_discount_fen": a_total, "coupon_discount_fen": c_total,
                "merchandise_payable_fen": goods, "shipping_fen": shipping,
                "total_payable_fen": None if shipping is None else goods + shipping,
                "pricing_status": "conditional" if conditional or shipping is None else "confirmed",
                "coupon_conditional": conditional, "gifts": gifts,
                "line_allocations": [{"sku_id": s, "activity_discount_fen": a_alloc[s],
                    "coupon_discount_fen": c_alloc[s], "payable_fen": residual[s] - c_alloc[s]} for s in sorted(original)]}
        except (KeyError, TypeError, ValueError):
            return no("promotion_rule_invalid")

    def alternatives(self, items, user, now, channel="web", gift_filter=None):
        # 无优惠也参与比较；不是先分别找最大活动、最大券。
        def applicable(rule):
            try:
                return rule.get("rule_complete", True) is True and instant(rule["start_at"]) <= now < instant(rule["end_at"]) \
                    and channel in rule["channels"] and all(not allowed or user.get(field) in allowed for field, allowed in (
                        ("member_level", rule["audience"]["member_levels"]), ("customer_type", rule["audience"]["customer_types"])))
            except (KeyError, TypeError, ValueError):
                return False
        held = {h["coupon_id"] for h in (user.get("owned_coupons") or []) if h["state"] == "unused"}
        activities = [None, *(rid for rid, rule in sorted(self.catalog.data["activities"].items()) if applicable(rule))]
        coupons = [None, *(rid for rid, rule in sorted(self.catalog.data["coupons"].items()) if rid in held and applicable(rule))]
        for aid, cid in product(activities, coupons):
            result = self.quote(items, user, now, channel, aid, cid, gift_filter)
            if result["eligible"]:
                yield result
