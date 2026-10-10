"""检查数据和预期的完整性，不对尚未实现的 AI 行为宣称通过。"""

import copy
import unittest

from acceptance.reference_probe import read, scoped, check_golden, dataset, dt
from tools.check_stage2_assets import verify
from runtime.validation import ContractViolation, validate_response, validate_session
from test_contracts import example


class AssetTests(unittest.TestCase):
    def test_dataset_and_case_references_are_consistent(self):
        summary = verify()
        cases=read('cases.json','acceptance')
        self.assertEqual(summary['service_or_model_cases_executed'],sum(c['executed'] for c in cases))

    def test_each_manual_pricing_expectation(self):
        for case in read("pricing_goldens.json", "acceptance"):
            with self.subTest(case=case["id"]):
                check_golden(case)

    def test_scope_with_both_filters_uses_intersection(self):
        product = dataset()["products"]["P002"]
        self.assertFalse(scoped(product, {"categories": ["seaweed"], "sku_ids": ["P019"]}))
        self.assertTrue(scoped(product, {"categories": ["seaweed"], "sku_ids": ["P002"]}))

    def test_near_expiry_day_boundary_is_explicit(self):
        now = dt(read("manifest.json")["reference_time"])
        for fixture_id, remaining in (("F_NEAR_30", 30), ("F_NEAR_31", 31)):
            with self.subTest(fixture=fixture_id):
                expiry = dt(dataset(fixture_id)["products"]["P001"]["expires_at"])
                self.assertEqual((expiry.date() - now.date()).days, remaining)


class ProtocolExtensionTests(unittest.TestCase):
    def setUp(self):
        self.session = example("session-state")
        self.state = self.session["module_states"]["product_recommendation"]
        self.group = self.state["need_groups"][0]

    def inferred(self):
        source = {"constraint_id": "c_gift", "field": "purpose", "operator": "equals", "value": "gift",
                  "strength": "hard", "status": "active", "source_turn_id": "turn_demo_001",
                  "source_text": "送朋友", "origin": "explicit", "derived_from": []}
        target = {"constraint_id": "c_box", "field": "package", "operator": "equals", "value": "gift_box",
                  "strength": "soft", "status": "active", "source_turn_id": "turn_demo_001",
                  "source_text": "送朋友", "origin": "inferred", "derived_from": ["c_gift"]}
        self.group["constraints"].extend([source, target])
        return source, target

    def test_gift_is_separate_from_paid_lines(self):
        response = example("response-completed")
        response["message"]["cards"][0]["gifts"] = [{"sku_id": "P009", "product_name": "协议赠品", "quantity": 1, "image_url": None}]
        validate_response(response)
        response["message"]["cards"][0]["gifts"][0]["unit_price_fen"] = 0
        with self.assertRaises(ContractViolation):
            validate_response(response)

    def test_duplicate_gift_rows_must_be_aggregated(self):
        response = example("response-completed")
        gift = {"sku_id": "P009", "product_name": "协议赠品", "quantity": 1, "image_url": None}
        response["message"]["cards"][0]["gifts"] = [gift, copy.deepcopy(gift)]
        with self.assertRaises(ContractViolation):
            validate_response(response)

    def test_shared_budget_supported_but_not_per_item(self):
        self.state["shared_budget"] = copy.deepcopy(self.group["budget"])
        validate_session(self.session)
        self.state["shared_budget"]["scope"] = "per_unit"
        with self.assertRaises(ContractViolation):
            validate_session(self.session)

    def test_hard_minimum_requires_amount(self):
        self.group["budget"]["min_is_hard"] = True
        with self.assertRaises(ContractViolation):
            validate_session(self.session)

    def test_inferred_soft_constraint_preserves_source(self):
        self.inferred()
        validate_session(self.session)

    def test_inference_cannot_be_hard_requirement(self):
        _, target = self.inferred()
        target["strength"] = "hard"
        with self.assertRaises(ContractViolation):
            validate_session(self.session)

    def test_inference_without_source_is_invalid(self):
        _, target = self.inferred()
        target["derived_from"] = []
        with self.assertRaises(ContractViolation):
            validate_session(self.session)

    def test_removed_source_cannot_leave_active_inference(self):
        source, _ = self.inferred()
        source["status"] = "removed"
        with self.assertRaises(ContractViolation):
            validate_session(self.session)

    def test_unknown_dependency_is_invalid(self):
        _, target = self.inferred()
        target["derived_from"] = ["nonexistent"]
        with self.assertRaises(ContractViolation):
            validate_session(self.session)

    def test_dependency_cycle_is_invalid(self):
        source, target = self.inferred()
        source["origin"] = "inferred"
        source["strength"] = "soft"
        source["derived_from"] = [target["constraint_id"]]
        with self.assertRaises(ContractViolation):
            validate_session(self.session)


if __name__ == "__main__":
    unittest.main()
