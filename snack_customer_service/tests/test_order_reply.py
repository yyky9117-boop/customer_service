"""验证实际话术检索、生成/质检边界及事实不变，不调用在线模型。"""
import asyncio
import copy
import io
import json
import sqlite3
import unittest

from test_service import test_database, CourierFixture
from test_model_adapter import StubOpener
from database.demo_db import initialize, connect
from model_adapter import DeepSeekExtractor, ModelFailure
from order_reply.generator import OrderReplyGenerator, fact_tokens, scene_for
from order_reply.knowledge import KnowledgeRetriever
from order_reply.model import ReplyModel
from service import CustomerService


def good_template(packet):
    parts=['亲～，我帮你核对了当前信息😊\n']
    tokens=packet['tokens']
    if 'order_ref' in tokens:parts.append('{{order_ref}}目前{{order_state}}。')
    parts.extend('{{'+key+'}}。' for key in tokens if key not in ('order_ref','order_state'))
    parts.append('\n有需要可以继续告诉我。')
    return {'template':''.join(parts)}


class ScriptedReplyModel:
    configured=True
    def __init__(self, invalid_first=False, review_reject=False, error=None):
        self.calls=[]
        self.invalid_first,self.review_reject,self.error=invalid_first,review_reject,error

    def call(self, prompt, packet, repair=None):
        self.calls.append((prompt,copy.deepcopy(packet),repair))
        if self.error:raise ModelFailure(self.error)
        if prompt=='compose':
            out=good_template(packet)
            if self.invalid_first and len(self.calls)==1:out['template']+='明天一定送到。'
            return out
        return {'pass':not self.review_reject,'issues':['捏造动作'] if self.review_reject else [],
            'rewrite_instruction':'只能说明本轮查到的信息' if self.review_reject else ''}


class OrderReplyTests(CourierFixture):
    def setUp(self):
        super().setUp()
        self.temp=test_database();self.path=self.temp.__enter__()
        self.addCleanup(lambda:self.temp.__exit__(None,None,None))
        initialize(self.path)
        self.knowledge=KnowledgeRetriever(self.path)
        self.model=ScriptedReplyModel()
        self.generator=OrderReplyGenerator(self.knowledge,self.model)
        self.service=CustomerService(self.path,model=DeepSeekExtractor({'MODEL_ENABLED':'false'}),courier=self.courier,
            reply_generator=self.generator)

    def test_retrieval_is_scene_scoped_and_ranks_urgent_phrase(self):
        hits=self.knowledge.retrieve('unshipped','我等了很久，很着急，帮我催一下发货')
        self.assertEqual(hits[0]['entry_id'],'waiting_shipment_urgent')
        self.assertTrue(all(h['scene']=='unshipped' for h in hits))
        self.assertIn('着急',hits[0]['matched_keywords'])
        self.assertEqual(hits[0]['source'],'reconstructed_demo')

    def test_seed_preserves_locally_edited_knowledge(self):
        con=connect(self.path)
        with con:con.execute("UPDATE order_reply_knowledge SET guide='人工调整的表达规范' WHERE entry_id='waiting_shipment'")
        con.close();initialize(self.path)
        self.assertIn('人工调整的表达规范',[r['guide'] for r in self.knowledge.retrieve('unshipped','发货')])

    def test_explicit_bank_sync_updates_only_knowledge(self):
        from order_reply.knowledge import seed_knowledge
        con=connect(self.path)
        before=[tuple(row) for row in con.execute('SELECT * FROM orders ORDER BY order_id')]
        with con:
            con.execute("UPDATE order_reply_knowledge SET guide='旧话术' WHERE entry_id='waiting_shipment'")
            seed_knowledge(con,replace=True)
        self.assertNotEqual(con.execute("SELECT guide FROM order_reply_knowledge WHERE entry_id='waiting_shipment'").fetchone()[0],'旧话术')
        self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM orders ORDER BY order_id')],before)
        con.close()

    def test_model_generation_and_independent_review_are_called(self):
        result=self.service.chat('demo_user_a','SN-DEMO-0001发货了吗')
        self.assertEqual([c[0] for c in self.model.calls],['compose','review'])
        self.assertEqual(result['reply_status']['mode'],'deepseek_grounded_reply')
        self.assertTrue(result['reply_status']['review_passed'])
        self.assertIn('尚未发货',result['reply'])
        self.assertTrue(result['reply'].startswith('亲～'))
        con=connect(self.path)
        self.assertEqual(con.execute('SELECT mode,review_passed FROM order_reply_runs').fetchone()[0],'deepseek_grounded_reply')
        self.assertEqual(con.execute('SELECT COUNT(*) FROM human_requests').fetchone()[0],0)
        con.close()

    def test_invalid_generated_claim_is_repaired_before_review(self):
        self.generator.model=ScriptedReplyModel(invalid_first=True)
        result=self.service.chat('demo_user_a','SN-DEMO-0001发货了吗')
        self.assertEqual([c[0] for c in self.generator.model.calls],['compose','compose','review'])
        self.assertEqual(result['reply_status']['attempts'],2)
        self.assertNotIn('明天',result['reply'])

    def test_rejected_review_falls_back_without_losing_facts(self):
        self.generator.model=ScriptedReplyModel(review_reject=True)
        result=self.service.chat('demo_user_a','SN-DEMO-0001发货了吗')
        self.assertEqual(result['reply_status']['mode'],'knowledge_fallback')
        self.assertEqual(result['reply_status']['fallback_reason'],'review_rejected')
        self.assertFalse(result['reply_status']['review_passed'])
        self.assertEqual(result['orders'][0]['shipping_status'],'unshipped')

    def test_provider_failure_is_redacted_and_falls_back(self):
        self.generator.model=ScriptedReplyModel(error='authentication_failed')
        result=self.service.chat('demo_user_a','SN-DEMO-0001发货了吗')
        self.assertEqual(result['reply_status']['fallback_reason'],'authentication_failed')
        self.assertIn('尚未发货',result['reply'])
        self.assertEqual(len(self.generator.model.calls),1)

    def test_mandatory_facts_cannot_be_removed_changed_or_duplicated(self):
        core={'result_type':'success','query_type':'shipping','orders':[{'order_id':'SN-DEMO-0001','order_status':'paid','shipping_status':'unshipped'}]}
        packet,_=self.generator.packet(core,'查订单',[])
        for template in ('亲～😊{{order_ref}}已发货。','亲～😊{{order_ref}}{{order_state}}{{order_state}}',
                '亲～😊{{order_ref}}{{order_state}}明天到', '亲～😊{{order_ref}}{{order_state}}退款30元',
                '亲～😊{{order_ref}}{{order_state}}{{unknown}}', '亲～😊{{order_ref}}{{order_state}}已通知仓库'):
            with self.subTest(template=template),self.assertRaises(ModelFailure):self.generator.render({'template':template},packet)

    def test_history_and_user_phone_are_not_sent_to_reply_provider(self):
        self.service.chat('demo_user_a','手机号13800000001查订单')
        payload=self.model.calls[0][1]
        serialized=json.dumps(payload,ensure_ascii=False)
        self.assertNotIn('13800000001',serialized)
        self.assertNotIn('demo_user_a',serialized)
        self.assertEqual(payload['actions_executed'],[])

    def test_split_parcels_keep_each_result(self):
        result=self.service.chat('demo_user_a','SN-DEMO-0008物流到哪了')
        self.assertIn('DEMO-TRACK-1008A',result['reply'])
        self.assertIn('DEMO-TRACK-1008B',result['reply'])
        self.assertIn('已部分发货',result['reply'])
        self.assertEqual(len(result['courier_calls']),2)

    def test_eta_only_uses_facts_with_source_and_signed_parcel_has_no_eta(self):
        core={'result_type':'success','query_type':'delivery_time','orders':[{'order_id':'SN-DEMO-0002',
            'order_status':'paid','shipping_status':'shipped','shipments':[{'logistics_status':'in_transit','estimated_delivery_at':'2026-10-10'}]}]}
        self.assertEqual(fact_tokens(core)['parcel_1'],'暂无预计送达时间')
        core['orders'][0]['shipments'][0].update(logistics_status='delivered',eta_source='fake')
        self.assertNotIn('2026-10-10',fact_tokens(core)['parcel_1'])

    def test_empty_order_and_interface_failure_are_different_scenes(self):
        self.assertEqual(scene_for({'result_type':'not_found'},'查订单'),'not_found')
        self.assertEqual(scene_for({'result_type':'query_failed'},'查订单'),'query_failed')

    def test_logistics_question_keeps_available_eta_and_rejects_contradiction(self):
        core={'result_type':'success','query_type':'logistics','orders':[{'order_id':'SN-DEMO-0002',
            'order_status':'paid','shipping_status':'shipped','shipments':[{'logistics_status':'in_transit',
                'latest_status_desc':'包裹运输中','estimated_delivery_at':'2026-10-08 18:00','eta_source':'mock_courier_estimate'}]}]}
        packet,_=self.generator.packet(core,'物流到哪，预计什么时候能到',[])
        self.assertIn('2026-10-08 18:00',packet['tokens']['eta_1'])
        out=good_template(packet)
        out['template']+='目前这单还没有显示可靠的预计时间。'
        with self.assertRaisesRegex(ModelFailure,'contradicted_eta'):self.generator.render(out,packet)

    def test_generation_never_modifies_business_result(self):
        core={'result_type':'success','query_type':'shipping','next_action':'none','orders':[{'order_id':'SN-DEMO-0001',
            'order_status':'paid','shipping_status':'unshipped'}]}
        before=copy.deepcopy(core)
        asyncio.run(self.generator.generate(core,'查订单'))
        self.assertEqual(core,before)

    def test_missing_or_corrupt_knowledge_does_not_lose_order_result(self):
        self.generator.model=None
        con=connect(self.path)
        with con:con.execute('DELETE FROM order_reply_knowledge')
        con.close()
        result=self.service.chat('demo_user_a','SN-DEMO-0001发货了吗')
        self.assertEqual(result['reply_status']['fallback_reason'],'knowledge_unavailable')
        self.assertIn('尚未发货',result['reply'])

    def test_knowledge_instruction_cannot_become_fake_action(self):
        self.generator.model=None
        con=connect(self.path)
        with con:con.execute("UPDATE order_reply_knowledge SET opening='亲～，已通知仓库😊' WHERE scene='unshipped'")
        con.close()
        result=self.service.chat('demo_user_a','SN-DEMO-0001发货了吗')
        self.assertNotIn('已通知',result['reply'])
        self.assertIn('尚未发货',result['reply'])


class ReplyTransportTests(unittest.TestCase):
    def test_http_model_body_has_retrieval_and_facts_but_no_secret(self):
        opener=StubOpener({'template':'亲～😊{{lookup}}'})
        extractor=DeepSeekExtractor({'DEEPSEEK_API_KEY':'test-only-key'})
        model=ReplyModel(extractor,opener)
        model.call('compose',{'knowledge':[{'guide':'不要承诺发货时间'}],'tokens':{'lookup':'请提供订单号'}})
        body=json.loads(opener.request.data)
        self.assertEqual(body['response_format'],{'type':'json_object'})
        self.assertIn('knowledge',body['messages'][1]['content'])
        self.assertNotIn('test-only-key',json.dumps(body))

    def test_invalid_or_truncated_response_cannot_be_used(self):
        extractor=DeepSeekExtractor({'DEEPSEEK_API_KEY':'test-only-key'})
        for opener in (StubOpener(None),StubOpener({'template':'x'},finish='length')):
            with self.assertRaises(ModelFailure):ReplyModel(extractor,opener).call('compose',{})

    def test_disabled_model_never_calls_network(self):
        model=ReplyModel(DeepSeekExtractor({'MODEL_ENABLED':'false'}),StubOpener(error=AssertionError('不应调用网络')))
        with self.assertRaisesRegex(ModelFailure,'not_configured'):model.call('compose',{})
