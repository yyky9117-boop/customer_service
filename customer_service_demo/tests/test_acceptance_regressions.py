import unittest

from runtime.errors import InvalidModelOutput
from runtime.needs import merge_patch, predicate, validate_predicate
from runtime.recommendation import RecommendationModule
from core_helpers import budget, catalog, constraint, context, patch


class AcceptanceRegressions(unittest.TestCase):
    def test_small_or_wrapped_cannot_weaken_weight_requirement(self):
        with self.assertRaises(InvalidModelOutput):
            validate_predicate(constraint('package',['small','wrapped'],operator='include'))

    def test_budget_only_is_usable_and_preserves_shipping_scope(self):
        b=budget(15000);b['source_text']='商品最多150元'
        b.pop('source_turn_id')
        ctx=context(message='商品最多150元')
        needs=merge_patch({},patch(shared_budget=b),ctx)
        result=RecommendationModule(catalog(),None).recommend(needs,ctx)
        self.assertEqual(result.status,'completed')
        self.assertTrue(all(c['pricing']['merchandise_payable_fen']<=15000 for c in result.cards))

    def test_stock_price_requirement_does_not_filter_unknown_taste(self):
        c=catalog(subset=['P023','P026'])
        from core_helpers import group,state
        needs=state(group([constraint('other','verified_stock_price')]))
        result=RecommendationModule(c,None).recommend(needs,context(needs))
        self.assertEqual(result.status,'completed')
        self.assertEqual([i['sku_id'] for card in result.cards for i in card['items']],['P023'])

    def test_gift_box_matches_authoritative_outer_box_field(self):
        c=catalog()
        self.assertTrue(predicate(c.products['P014'],constraint('package','gift_box'),context().now))
        self.assertFalse(predicate(c.products['P001'],constraint('package','gift_box'),context().now))

    def test_not_gifting_does_not_exclude_gift_box_for_self(self):
        self.assertTrue(predicate(catalog().products['P014'],constraint('gift_need',False),context().now))

    def test_explicit_budget_cannot_disappear_from_model_patch(self):
        from core_helpers import update
        with self.assertRaises(InvalidModelOutput):
            merge_patch({},patch([update()]),context(message='商品预算50到100元'))

    def test_wrapping_cannot_gain_unrequested_weight_limit(self):
        from core_helpers import update
        c=constraint('package','small');c['source_text']='独立包装'
        with self.assertRaises(InvalidModelOutput):
            merge_patch({},patch([update(constraints=[c])]),context(message='必须独立包装'))
