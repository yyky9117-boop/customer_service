"""本阶段只验证协议边界，不模拟或假装执行推荐模型。"""

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from runtime.validation import (
    SCHEMAS, ContractViolation, validate_request, validate_response,
    validate_session, validate_transition,
)

ROOT = Path(__file__).resolve().parents[1]


def example(name):
    return json.loads((ROOT / "examples" / (name + ".json")).read_text(encoding="utf-8"))


class SchemaTests(unittest.TestCase):
    def test_all_schemas_are_well_formed(self):
        for name, schema in SCHEMAS.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)

    def test_all_documented_examples_validate_offline(self):
        validate_request(example("request-new"))
        validate_session(example("session-state"))
        for name in ("completed", "clarification", "no-match", "unsupported", "error"):
            with self.subTest(example=name):
                validate_response(example("response-" + name))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.payload = example("request-new")

    def reject(self):
        with self.assertRaises(ContractViolation):
            validate_request(self.payload)

    def test_whitespace_message_rejected(self):
        self.payload["message"] = " \n\t"
        self.reject()

    def test_empty_message_rejected(self):
        self.payload["message"] = ""
        self.reject()

    def test_message_length_boundary(self):
        self.payload["message"] = "零" * 2000
        validate_request(self.payload)
        self.payload["message"] += "食"
        self.reject()

    def test_user_cannot_supply_privileged_fields(self):
        for field in ("system_prompt", "member_level", "user_id", "coupon_eligible"):
            with self.subTest(field=field):
                payload = {**self.payload, field: "override"}
                with self.assertRaises(ContractViolation):
                    validate_request(payload)

    def test_new_conversation_must_start_at_zero(self):
        self.payload["expected_revision"] = 1
        self.reject()

    def test_boolean_and_float_revisions_rejected(self):
        self.payload["conversation_id"] = "conv_existing"
        for revision in (True, 1.0):
            with self.subTest(revision=revision):
                self.payload["expected_revision"] = revision
                self.reject()

    def test_selected_reference_is_structured(self):
        self.payload["selected_reference"] = {"message_id": "msg_1", "plan_id": "plan_1", "sku_id": "sku_1"}
        validate_request(self.payload)
        self.payload["selected_reference"]["other"] = "untrusted"
        self.reject()


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.payload = example("response-completed")
        self.card = self.payload["message"]["cards"][0]
        self.pricing = self.card["pricing"]

    def reject(self):
        with self.assertRaises(ContractViolation):
            validate_response(self.payload)

    def test_completed_needs_cards(self):
        self.payload["message"]["cards"] = []
        self.reject()

    def test_noncompleted_cannot_smuggle_recommendations(self):
        for status in ("clarification", "no-match", "unsupported", "error"):
            with self.subTest(status=status):
                payload = example("response-" + status)
                payload["message"]["cards"] = [self.card]
                with self.assertRaises(ContractViolation):
                    validate_response(payload)

    def test_clarification_requires_details_and_at_most_two_fields(self):
        self.payload = example("response-clarification")
        self.payload["clarification"] = None
        self.reject()
        self.payload = example("response-clarification")
        self.payload["clarification"]["asked_fields"].append("taste")
        self.reject()

    def test_zero_quantity_rejected(self):
        self.card["items"][0]["quantity"] = 0
        self.reject()

    def test_money_must_be_nonnegative_integer_fen(self):
        for amount in (-1, 3500.5, 3500.0, True, "3500"):
            with self.subTest(amount=amount):
                payload = example("response-completed")
                payload["message"]["cards"][0]["items"][0]["unit_price_fen"] = amount
                with self.assertRaises(ContractViolation):
                    validate_response(payload)

    def test_subtotal_uses_quantity(self):
        self.pricing["subtotal_fen"] = 8000
        self.reject()

    def test_merchandise_payable_must_match_discounts(self):
        self.pricing["merchandise_payable_fen"] = 9999
        self.reject()

    def test_total_includes_shipping(self):
        self.pricing["shipping_fen"] = 500
        self.reject()
        self.pricing["total_payable_fen"] = 10500
        validate_response(self.payload)

    def test_discount_cannot_exceed_subtotal(self):
        self.pricing["discounts"][0]["amount_fen"] = 12000
        self.reject()

    def test_same_discount_cannot_be_deducted_twice(self):
        self.pricing["discounts"].append(copy.deepcopy(self.pricing["discounts"][0]))
        self.reject()

    def test_distinct_discount_kinds_can_have_same_local_id(self):
        self.pricing["discounts"][1]["id"] = self.pricing["discounts"][0]["id"]
        validate_response(self.payload)

    def test_sku_must_be_aggregated(self):
        self.card["items"].append(copy.deepcopy(self.card["items"][0]))
        self.reject()

    def test_plan_ids_are_unique(self):
        duplicate = copy.deepcopy(self.card)
        duplicate["rank"] = 2
        self.payload["message"]["cards"].append(duplicate)
        self.reject()

    def test_rank_matches_display_order(self):
        self.card["rank"] = 2
        self.reject()

    def test_more_than_three_plans_rejected(self):
        self.payload["message"]["cards"] *= 4
        self.reject()

    def test_plan_type_and_items_agree(self):
        for kind in ("single_product", "multi_quantity"):
            with self.subTest(kind=kind):
                self.card["plan_type"] = kind
                self.reject()

    def test_unknown_shipping_cannot_claim_confirmed_total(self):
        self.pricing["shipping_fen"] = None
        self.reject()
        self.pricing["status"] = "conditional"
        self.pricing["total_payable_fen"] = None
        self.pricing["conditions"] = ["运费尚未确认，100 元仅为商品应付金额。"]
        validate_response(self.payload)

    def test_conditional_coupon_cannot_claim_confirmed_price(self):
        self.pricing["discounts"][0]["eligibility"] = "conditional"
        self.reject()
        self.pricing["status"] = "conditional"
        self.pricing["conditions"] = ["活动资格待确认。"]
        validate_response(self.payload)

    def test_unknown_price_requires_null_and_explanation(self):
        self.pricing["status"] = "unknown"
        self.reject()
        self.pricing["merchandise_payable_fen"] = None
        self.pricing["total_payable_fen"] = None
        self.pricing["conditions"] = ["优惠计算规则缺失。"]
        validate_response(self.payload)

    def test_success_increments_revision_once(self):
        self.payload["revision"] = 2
        self.reject()

    def test_error_does_not_increment_revision(self):
        self.payload = example("response-error")
        self.payload["revision"] = 1
        self.reject()

    def test_raw_model_and_html_fields_not_in_contract(self):
        self.payload["message"]["raw_model_output"] = "untrusted"
        self.reject()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.payload = example("session-state")
        self.state = self.payload["module_states"]["product_recommendation"]

    def reject(self):
        with self.assertRaises(ContractViolation):
            validate_session(self.payload)

    def test_budget_lower_cannot_exceed_upper(self):
        self.state["need_groups"][0]["budget"]["min_fen"] = 10001
        self.reject()

    def test_budget_amount_and_known_status_agree(self):
        self.state["need_groups"][0]["budget"]["strictness"] = "unknown"
        self.reject()

    def test_budget_has_provenance(self):
        self.state["need_groups"][0]["budget"]["source_turn_id"] = None
        self.reject()

    def test_distinct_recipient_groups_are_supported(self):
        group = copy.deepcopy(self.state["need_groups"][0])
        group["group_id"] = "group_colleagues"
        group["label"] = "同事"
        self.state["need_groups"].append(group)
        validate_session(self.payload)

    def test_duplicate_groups_rejected(self):
        self.state["need_groups"] *= 2
        self.reject()

    def test_constraint_ids_unique_within_group(self):
        group = self.state["need_groups"][0]
        group["constraints"].append(copy.deepcopy(group["constraints"][0]))
        self.reject()

    def test_pending_and_removed_preserve_original_expression(self):
        constraints = self.state["need_groups"][0]["constraints"]
        constraints[0]["status"] = "pending"
        constraints[1]["status"] = "removed"
        validate_session(self.payload)

    def test_pending_question_refers_to_existing_group(self):
        self.state["pending_clarification"] = {"question": "预算是每人还是总共？", "asked_fields": ["budget.scope"], "group_id": "missing"}
        self.reject()

    def test_unregistered_module_state_rejected(self):
        self.payload["module_states"]["orders"] = {}
        self.reject()

    def test_active_module_has_state(self):
        self.payload["module_states"] = {}
        self.reject()

    def test_timestamp_requires_timezone(self):
        self.payload["updated_at"] = "2026-10-07T08:00:00"
        self.reject()


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.request = example("request-new")
        self.response = example("response-completed")
        self.next_state = example("session-state")

    def reject(self, previous=None):
        with self.assertRaises(ContractViolation):
            validate_transition(previous, self.request, self.response, self.next_state)

    def test_valid_first_commit(self):
        validate_transition(None, self.request, self.response, self.next_state)

    def test_cross_request_response_cannot_commit(self):
        self.response["request_id"] = "req_other"
        self.reject()

    def test_cross_conversation_state_cannot_commit(self):
        self.next_state["conversation_id"] = "conv_other"
        self.reject()

    def test_committed_revision_agrees_with_response(self):
        self.next_state["revision"] = 2
        self.reject()

    def test_reference_order_matches_displayed_cards(self):
        self.next_state["module_states"]["product_recommendation"]["last_presentation"]["plan_order"][0]["plan_id"] = "plan_other"
        self.reject()

    def test_plan_must_belong_to_existing_need_group(self):
        self.response["message"]["cards"][0]["group_id"] = "missing"
        self.reject()

    def test_stale_request_cannot_commit(self):
        previous = example("session-state")
        self.request["conversation_id"] = previous["conversation_id"]
        self.request["expected_revision"] = 0
        self.response["base_revision"] = 1
        self.response["revision"] = 2
        self.next_state["revision"] = 2
        self.reject(previous)

    def test_error_preserves_existing_state(self):
        previous = example("session-state")
        self.request["conversation_id"] = previous["conversation_id"]
        self.request["expected_revision"] = 1
        self.response = example("response-error")
        self.response["conversation_id"] = previous["conversation_id"]
        self.response["base_revision"] = self.response["revision"] = 1
        validate_transition(previous, self.request, self.response, previous)
        self.next_state["updated_at"] = "2026-10-07T00:00:01Z"
        self.reject(previous)

    def test_first_request_failure_does_not_create_state(self):
        self.response = example("response-error")
        validate_transition(None, self.request, self.response, None)

    def test_first_request_failure_cannot_claim_existing_conversation(self):
        self.response = example("response-error")
        self.response["conversation_id"] = "conv_nonexistent"
        with self.assertRaises(ContractViolation):
            validate_transition(None, self.request, self.response, None)

    def test_revision_conflict_reports_current_state_without_commit(self):
        previous = example("session-state")
        self.request["conversation_id"] = previous["conversation_id"]
        self.response = example("response-error")
        self.response["conversation_id"] = previous["conversation_id"]
        self.response["base_revision"] = self.response["revision"] = 1
        self.response["error"] = {"code": "revision_conflict", "retryable": False}
        validate_transition(previous, self.request, self.response, previous)


if __name__ == "__main__":
    unittest.main()
