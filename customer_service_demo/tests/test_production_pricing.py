import json
import unittest
from itertools import combinations

from runtime.pricing import Pricing, distribute, in_scope
from core_helpers import ROOT, NOW, catalog


class ProductionPricingTests(unittest.TestCase):
    def test_all_53_independent_goldens(self):
        cases = json.loads((ROOT / "acceptance/pricing_goldens.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 53)
        for case in cases:
            with self.subTest(golden=case["id"]):
                c = catalog(case["fixture_id"])
                ctx = case["context"]
                from runtime.catalog import instant
                result = Pricing(c).quote(case["items"], c.user(ctx["user_id"]), instant(ctx["now"]),
                    ctx["channel"], case["activity_id"], case["coupon_id"])
                for key, expected in case["expected"].items():
                    observed = result.get(key)
                    if key == "line_allocations":
                        observed = [{k: row[k] for k in expected[0]} for row in observed]
                        expected = sorted(expected, key=lambda row: row["sku_id"])
                    self.assertEqual(observed, expected, key)

    def test_optimum_compares_all_legal_combinations(self):
        c = catalog()
        p = Pricing(c)
        items = [{"sku_id": "P004", "quantity": 4}]
        choices = list(p.alternatives(items, c.user("U_NEW"), NOW))
        best = min(choices, key=lambda q: q["total_payable_fen"])
        self.assertEqual(best["total_payable_fen"], 9424)
        self.assertEqual((best["activity_id"], best["coupon_id"]), ("A_MEAT", "C_ALL"))
        self.assertTrue(any(q["activity_id"] is None and q["coupon_id"] is None for q in choices))
        self.assertFalse(any(q["activity_id"] and q["coupon_id"] == "C_NO_STACK" for q in choices))

    def test_more_discount_can_cost_more_including_shipping(self):
        c = catalog("F_ACTIVITY_10000")
        p = Pricing(c)
        items = [{"sku_id": "P001", "quantity": 1}]
        original = p.quote(items, c.user("U_NEW"), NOW)
        reduced = p.quote(items, c.user("U_NEW"), NOW, coupon_id="C_WEB")
        self.assertLess(reduced["merchandise_payable_fen"], original["merchandise_payable_fen"])
        self.assertGreater(reduced["total_payable_fen"], original["total_payable_fen"])

    def test_allocation_properties_many_integer_values(self):
        for a, b in combinations(range(1, 18), 2):
            for amount in {0, 1, a + b - 1, a + b}:
                result = distribute(amount, {"B": b, "A": a})
                self.assertEqual(sum(result.values()), amount)
                self.assertTrue(0 <= result["A"] <= a and 0 <= result["B"] <= b)
                self.assertEqual(result, distribute(amount, {"A": a, "B": b}))

    def test_scope_intersection(self):
        row = catalog().products["P001"]
        self.assertFalse(in_scope(row, {"scope": {"categories": ["nuts"], "sku_ids": ["P001"]}}))
        self.assertTrue(in_scope(row, {"scope": {"categories": ["biscuits"], "sku_ids": ["P001"]}}))

    def test_claimable_coupon_is_conditional_only(self):
        c = catalog()
        items = [{"sku_id": "P001", "quantity": 3}]
        result = Pricing(c).quote(items, c.user("U_GUEST"), NOW, coupon_id="C_CLAIM", allow_claim=True)
        self.assertEqual(result["pricing_status"], "conditional")
        self.assertTrue(result["coupon_conditional"])
        options = list(Pricing(c).alternatives(items, c.user("U_GUEST"), NOW))
        self.assertFalse(any(q["coupon_id"] == "C_CLAIM" for q in options))

    def test_gift_does_not_bypass_hard_exclusions(self):
        c = catalog("F_GIFT_ALLERGEN")
        q = Pricing(c).quote([{"sku_id": "P002", "quantity": 3}], c.user("U_GUEST"), NOW,
            activity_id="A_SEA_GIFT", gift_filter=lambda row: "peanut" not in row["declared_allergens"])
        self.assertEqual(q["rejection_code"], "gift_hard_constraint")

    def test_duplicate_lines_aggregate_stock_and_discounts(self):
        c = catalog()
        p = Pricing(c)
        user = c.user("U_NEW")
        self.assertEqual(p.quote([{"sku_id": "P019", "quantity": 1}] * 2, user, NOW, activity_id="A_FIXED"),
            p.quote([{"sku_id": "P019", "quantity": 2}], user, NOW, activity_id="A_FIXED"))

    def test_invalid_promotion_is_ignored_without_losing_baseline(self):
        c = catalog()
        c.data["activities"]["A_ORDER"].pop("threshold_fen")
        choices = list(Pricing(c).alternatives([{"sku_id": "P001", "quantity": 10}], c.user("U_GUEST"), NOW))
        self.assertTrue(choices)
        self.assertFalse(any(q["activity_id"] == "A_ORDER" for q in choices))

    def test_unknown_user_is_never_treated_as_new_customer(self):
        c = catalog()
        q = Pricing(c).quote([{"sku_id": "P001", "quantity": 3}], c.user("not_a_user"), NOW, coupon_id="C_NEW")
        self.assertEqual(q["rejection_code"], "coupon_audience")
