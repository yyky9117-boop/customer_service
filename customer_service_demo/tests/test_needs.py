import copy
import json
import unittest

from runtime.errors import InvalidModelOutput
from runtime.needs import PATCH_SCHEMA, merge_patch, predicate, validate_predicate, quantity_range
from core_helpers import ROOT, NOW, budget, catalog, constraint, context, group, patch, state, update


class NeedTests(unittest.TestCase):
    def test_exported_model_schema_and_example_match_runtime(self):
        schema = json.loads((ROOT / "schemas/need-patch.schema.json").read_text(encoding="utf-8"))
        schema.pop("$id")
        schema.pop("$schema")
        self.assertEqual(schema, PATCH_SCHEMA)
        example = json.loads((ROOT / "examples/need-office.json").read_text(encoding="utf-8"))
        result = merge_patch({}, example["patch"], context(message=example["message"]))
        self.assertEqual(result["need_groups"][0]["budget"]["max_fen"], 6000)

    def test_budget_update_preserves_other_constraints(self):
        prior = state(group([constraint("taste", "spice:none"), constraint("package", "small")], budget(10000)))
        original = copy.deepcopy(prior)
        b = budget(6000)
        b.pop("source_turn_id")
        actual = merge_patch(prior, patch([update(budget=b)]), context(prior))
        self.assertEqual(actual["need_groups"][0]["budget"]["max_fen"], 6000)
        self.assertEqual(actual["need_groups"][0]["constraints"], prior["need_groups"][0]["constraints"])
        self.assertEqual(prior, original)

    def test_removal_withdraws_inference_but_retains_explicit_gift_box(self):
        parent = constraint("purpose", "gift", cid="purpose")
        inferred = constraint("package", "gift_box", strength="soft", cid="inferred", origin="inferred", derived_from=["purpose"])
        explicit = constraint("package", "gift_box", cid="explicit")
        prior = state(group([parent, inferred, explicit]))
        actual = merge_patch(prior, patch([update(remove=["purpose"])]), context(prior))
        by_id = {c["constraint_id"]: c for c in actual["need_groups"][0]["constraints"]}
        self.assertEqual(by_id["inferred"]["status"], "removed")
        self.assertEqual(by_id["explicit"]["status"], "active")

    def test_absent_shared_budget_preserved_and_null_clears(self):
        prior = state(group([constraint("scene", "office", strength="soft")]), shared=budget(10000))
        self.assertEqual(merge_patch(prior, patch(), context())["shared_budget"], prior["shared_budget"])
        self.assertIsNone(merge_patch(prior, patch(shared_budget=None), context())["shared_budget"])

    def test_no_source_or_fabricated_source_is_rejected(self):
        c = constraint("taste", "spice:none", source_text="原文不存在")
        with self.assertRaises(InvalidModelOutput):
            merge_patch({}, patch([update(constraints=[c])]), context())

    def test_unknown_predicate_and_mixed_taste_axes_rejected(self):
        for c in [constraint("taste", "healthy"), constraint("taste", ["spice:none", "sugar:sugar_free"], operator="include")]:
            with self.subTest(value=c["value"]), self.assertRaises(InvalidModelOutput):
                validate_predicate(c)

    def test_integer_quantities_not_bool_or_float(self):
        for value in [True, 1.0, 0, 1000]:
            with self.subTest(value=value), self.assertRaises(InvalidModelOutput):
                validate_predicate(constraint("quantity", value))

    def test_inferred_hard_or_cyclic_dependency_is_rejected(self):
        for constraints in [
            [constraint("scene", "office", cid="a"), constraint("package", "small", cid="b", origin="inferred", derived_from=["a"])],
            [constraint("scene", "office", cid="a", strength="soft", origin="inferred", derived_from=["b"]),
             constraint("package", "small", cid="b", strength="soft", origin="inferred", derived_from=["a"])]]:
            with self.assertRaises(InvalidModelOutput):
                merge_patch({}, patch([update(constraints=constraints)]), context())

    def test_reset_removes_old_groups_and_references(self):
        prior = state(group([constraint("taste", "spice:none")]))
        prior["last_presentation"] = {"message_id": "msg_old", "turn_id": "turn_old", "plan_order": []}
        actual = merge_patch(prior, patch(reset=True), context())
        self.assertEqual(actual["need_groups"], [])
        self.assertIsNone(actual["last_presentation"])

    def test_allergy_may_contain_and_unknown_differ_from_dislike(self):
        c = catalog()
        allergy = constraint("ingredient", "allergen:peanut", operator="exclude")
        dislike = constraint("ingredient", "ingredient:peanut", operator="exclude", strength="soft")
        self.assertFalse(predicate(c.products["P006"], allergy, NOW))
        self.assertTrue(predicate(c.products["P006"], dislike, NOW))
        self.assertIsNone(predicate(c.products["P023"], allergy, NOW))
        self.assertTrue(predicate(c.products["P028"], allergy, NOW))

    def test_nuts_exclusion_checks_gift_contents_but_not_seeds(self):
        c = catalog()
        rule = constraint("category", "nuts", operator="exclude")
        self.assertFalse(predicate(c.products["P014"], rule, NOW))
        self.assertTrue(predicate(c.products["P028"], rule, NOW))

    def test_sugar_and_sweetness_are_different(self):
        c = catalog()
        self.assertFalse(predicate(c.products["P008"], constraint("taste", "sugar:low_sugar"), NOW))
        self.assertTrue(predicate(c.products["P009"], constraint("taste", "sugar:sugar_free"), NOW))
        self.assertFalse(predicate(c.products["P009"], constraint("taste", ["sweetness:none", "sweetness:low"], operator="include"), NOW))

    def test_near_expiry_local_day_boundaries(self):
        rule = constraint("other", "not_near_expiry")
        for fixture, expected in [("F_NEAR_30", False), ("F_NEAR_31", True), ("F_EXPIRES_NOW", False)]:
            c = catalog(fixture)
            # fixture may modify an individual product; find modified expiry.
            sku = "P001"
            self.assertIs(predicate(c.products[sku], rule, NOW), expected, fixture)

    def test_small_inner_wrap_does_not_confuse_outer_gift_box(self):
        c = catalog()
        rule = constraint("package", "small")
        self.assertTrue(predicate(c.products["P015"], rule, NOW))
        self.assertFalse(predicate(c.products["P029"], rule, NOW))
        self.assertIsNone(predicate(c.products["P027"], rule, NOW))

    def test_quantity_contradictions(self):
        self.assertIsNone(quantity_range(group([constraint("quantity", 3, cid="a"), constraint("quantity", 5, cid="b")])))
        self.assertIsNone(quantity_range(group([constraint("quantity", 5, "at_least", cid="a"), constraint("quantity", 3, "at_most", cid="b")])))
