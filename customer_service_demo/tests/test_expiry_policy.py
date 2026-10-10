"""店铺推荐保质期规则覆盖：默认排除、日期边界、凑单、赠品和最终核验。"""
import copy
import unittest

from core_helpers import NOW, catalog, constraint, context, group, state, budget
from runtime.recommendation import RecommendationModule
from runtime.service import ChatService
from runtime.errors import ServiceFailure


class ExpiryPolicyTests(unittest.TestCase):
    def recommend(self, c, needs):
        return RecommendationModule(c,None).recommend(needs,context(needs))

    def test_default_30_day_boundary_and_unknown_expiry(self):
        for fixture, expected in [('F_NEAR_30','no_match'),('F_NEAR_31','completed'),('F_EXPIRES_NOW','no_match')]:
            with self.subTest(fixture=fixture):
                c=catalog(fixture,subset=['P001'])
                out=self.recommend(c,state(group([constraint('other','sku:P001')])))
                self.assertEqual(out.status,expected)
        c=catalog(subset=['P001']);c.products['P001']['expires_at']=None
        self.assertEqual(self.recommend(c,state(group([constraint('other','sku:P001')]))).status,'no_match')

    def test_timezone_boundary_uses_beijing_date(self):
        c=catalog(subset=['P001'])
        c.products['P001']['expires_at']='2026-11-06T16:00:00+00:00'  # 北京时间11月7日，31天。
        self.assertTrue(c.recommendable_expiry(c.products['P001'],NOW))
        c.products['P001']['expires_at']='2026-11-06T15:59:59+00:00'
        self.assertFalse(c.recommendable_expiry(c.products['P001'],NOW))

    def test_explicit_near_sku_cannot_bypass_default_filter(self):
        c=catalog(subset=['P024'])
        out=self.recommend(c,state(group([constraint('other','sku:P024')])))
        self.assertEqual(out.status,'no_match')
        self.assertFalse(out.cards)

    def test_near_expiry_cannot_be_used_for_cheaper_add_on(self):
        c=catalog(subset=['P001','P024']);c.products['P001']['price_fen']=9800
        needs=state(group([constraint('other','sku:P001',cid='main'),constraint('other','add_on_allowed',cid='addon')],budget(10100,scope='order_total')))
        out=self.recommend(c,needs)
        self.assertEqual(out.status,'no_match')
        self.assertFalse(out.cards)

    def test_near_expiry_gift_disqualifies_gift_offer(self):
        c=catalog(subset=['P002','P024'])
        c.data['activities']={'A_SEA_GIFT':c.data['activities']['A_SEA_GIFT']}
        c.data['activities']['A_SEA_GIFT']['gift']['sku_id']='P024'
        needs=state(group([constraint('other','sku:P002'),constraint('quantity',3)]))
        module=RecommendationModule(c,None)
        original=module.pricing.quote
        observed=[]
        def trace(*args,**kwargs):
            result=original(*args,**kwargs)
            observed.append(result)
            return result
        module.pricing.quote=trace
        out=module.recommend(needs,context(needs))
        self.assertEqual(out.status,'completed')
        self.assertTrue(any(not q['eligible'] for q in observed))
        self.assertTrue(all(not card.get('gifts') for card in out.cards))
        self.assertTrue(all(not card['pricing']['discounts'] for card in out.cards))

    def test_final_guard_rejects_card_when_expiry_changes(self):
        c=catalog(subset=['P001'])
        needs=state(group([constraint('other','sku:P001')]))
        module=RecommendationModule(c,None)
        out=module.recommend(needs,context(needs))
        self.assertEqual(out.status,'completed')
        c.products['P001']['expires_at']='2026-10-20T23:59:59+08:00'
        with self.assertRaises(ServiceFailure):ChatService(module,None).verify_facts(out.cards,context(needs))

    def test_final_guard_rejects_gift_when_expiry_changes(self):
        c=catalog(subset=['P002']);needs=state(group([constraint('other','sku:P002'),constraint('quantity',3)]))
        module=RecommendationModule(c,None)
        quote=module.pricing.quote([{'sku_id':'P002','quantity':3}],c.user('U_GUEST'),NOW,'web','A_SEA_GIFT')
        self.assertTrue(quote['eligible'])
        card=module.card(needs['need_groups'],(('P002',3),),({'P002':3},),quote,1,context(needs),0)
        # 改为非临期的另一赠品，先验证事实一致，再让其跨过店铺保质期门槛。
        c.products['P024']=copy.deepcopy(c.products['P002']);c.products['P024']['sku_id']='P024'
        c.data['activities']['A_SEA_GIFT']['gift']['sku_id']='P024'
        card=copy.deepcopy(card);card['gifts'][0]['sku_id']='P024'
        ChatService(module,None).verify_facts([card],context(needs))
        c.products['P024']['expires_at']='2026-10-20T23:59:59+08:00'
        with self.assertRaises(ServiceFailure):ChatService(module,None).verify_facts([card],context(needs))
