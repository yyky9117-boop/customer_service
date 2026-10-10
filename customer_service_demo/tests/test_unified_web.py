"""真实 HTTP 入口、原订单 SQLite、模拟物流接口与跨模块边界。模型使用脚本，不消耗额度。"""
import copy
import json
import sqlite3
import unittest
from pathlib import Path
from contextlib import closing

from core_helpers import ScriptedModel, WorkspaceTemp, constraint, patch, update
from test_unification import create_orders, LEGACY
from runtime.web import WebApp
from integration.router import ConversationRouter
from integration.store import UnifiedSessionStore


class UnifiedWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.order_db = self.root / 'orders.sqlite3'
        create_orders(self.order_db)
        wrapped=constraint('package','wrapped');wrapped['source_text']='独立包装'
        self.model = ScriptedModel(patch([update(constraints=[wrapped])]), patch())
        self.app = WebApp(self.root / 'sessions.sqlite3', model=self.model, order_database=self.order_db)
        self.addCleanup(self.app.close)

    def ask(self, text, prior=None, rid=None, profile='U_NEW', reference=None):
        return self.app.chat({'schema_version':'1.0','request_id':rid or 'req_'+str(len(text))+'_'+str((prior or {}).get('revision',0)),
            'conversation_id':prior['conversation_id'] if prior else None, 'expected_revision':prior['revision'] if prior else 0,
            'message':text,'selected_reference':reference}, 'browser_a', profile)

    def test_automatic_route_and_context_round_trip(self):
        first = self.ask('推荐独立包装零食')
        before = copy.deepcopy(self.app.store.session(first['conversation_id'],'browser_a')[0]['module_states']['product_recommendation'])
        order = self.ask('查订单 SN-DEMO-0001 发货了吗',first)
        self.assertEqual((order['module'],order['status']),('order_query','completed'),order)
        last = self.ask('继续刚才的推荐',order)
        self.assertEqual(last['module'],'product_recommendation')
        state, history = self.app.store.session(last['conversation_id'],'browser_a')
        self.assertEqual(state['module_states']['order_query']['selected_order_id'],'SN-DEMO-0001')
        self.assertEqual(before['need_groups'],state['module_states']['product_recommendation']['need_groups'])
        self.assertEqual(len(history),6)
        self.assertEqual(len(self.model.calls),2)
        self.assertNotIn('SN-DEMO-0001',json.dumps(self.model.calls[-1]['payload']['history'],ensure_ascii=False))

    def test_mixed_question_asks_without_query_or_model(self):
        self.app.order_module.repository.snapshot = lambda uid: self.fail('混合问题不应读取订单')
        result = self.ask('查询订单 SN-DEMO-0001，同时推荐低糖零食')
        self.assertEqual((result['module'],result['status']),('conversation_router','needs_clarification'),result)
        self.assertFalse(self.model.calls)
        self.assertFalse(result['message']['cards'])
        self.assertEqual(result['clarification']['asked_fields'],['consultation_order'])

    def test_ordinal_selection_and_invalid_ordinal_preserve_candidates(self):
        first = self.ask('查手机号13800000001的订单')
        ids = self.app.store.session(first['conversation_id'],'browser_a')[0]['module_states']['order_query']['candidate_order_ids']
        bad = self.ask('第六笔',first)
        self.assertEqual(bad['status'],'needs_clarification',bad)
        self.assertEqual(self.app.store.session(first['conversation_id'],'browser_a')[0]['module_states']['order_query']['candidate_order_ids'],ids)
        selected = self.ask('第二笔',bad)
        self.assertEqual(selected['status'],'completed',selected)
        self.assertIn(ids[1],selected['message']['content'])

    def test_new_invalid_number_does_not_fall_back_to_old_order(self):
        for text, expected in [('订单号 SN-DEMO-000','no_match'),('手机号1380000000','needs_clarification'),('DEMO-TRACK-','no_match')]:
            with self.subTest(text=text):
                first = self.ask('SN-DEMO-0001',rid='req_start_'+str(len(text)))
                result = self.ask(text,first)
                self.assertEqual(result['status'],expected,result)
                self.assertNotIn('SN-DEMO-0001',result['message']['content'])

    def test_phone_in_sentence_and_bare_number_query_external_database(self):
        for text in ('13800000001', '这是13800000001，帮我看一下', '电话：13800000001'):
            with self.subTest(text=text):
                first = self.ask(text, rid='req_phone_'+str(len(text)))
                self.assertEqual((first['module'],first['status']),('order_query','needs_clarification'),first)
                state = self.app.store.session(first['conversation_id'],'browser_a')[0]['module_states']['order_query']
                self.assertEqual(state['params']['phone_number'],'13800000001')
                self.assertEqual(state['candidate_order_ids'],['SN-DEMO-0002','SN-DEMO-0001'])
        self.assertFalse(self.model.calls, '订单识别与数据库查询不需要推荐模型')

    def test_phone_single_match_and_no_match_do_not_reuse_selected_order(self):
        first = self.ask('13800000002',profile='U_RETURN')
        self.assertEqual(first['status'],'completed',first)
        self.assertIn('SN-DEMO-0010',first['message']['content'])
        for text in ('13800000003','13800000001'):
            with self.subTest(text=text):
                result = self.ask(text,first,profile='U_RETURN',rid='req_missing_'+text)
                self.assertEqual(result['status'],'no_match',result)
                self.assertNotIn('SN-DEMO-',result['message']['content'])
                first = result

    def test_invalid_phone_explains_format_and_can_be_corrected(self):
        first = self.ask('SN-DEMO-0001')
        bad = self.ask('电话：1380000000',first)
        self.assertEqual((bad['module'],bad['status']),('order_query','needs_clarification'),bad)
        self.assertIn('11位',bad['message']['content'])
        self.assertNotIn('SN-DEMO-0001',bad['message']['content'])
        fixed = self.ask('13800000001',bad)
        self.assertEqual(fixed['status'],'needs_clarification',fixed)
        selected = self.ask('第二笔',fixed)
        self.assertEqual(selected['status'],'completed',selected)
        self.assertIn('SN-DEMO-0001',selected['message']['content'])

    def test_two_distinct_phones_clarify_before_selecting_any_order(self):
        result = self.ask('13800000001 或 13800000002')
        self.assertEqual((result['module'],result['status']),('order_query','needs_clarification'),result)
        self.assertEqual(result['clarification']['asked_fields'],['phone_number'])
        self.assertIn('多个手机号',result['message']['content'])
        self.assertNotIn('SN-DEMO-',result['message']['content'])
        self.assertFalse(self.model.calls)
        corrected = self.ask('13800000001',result)
        self.assertIn('SN-DEMO-0001',corrected['message']['content'])

    def test_repeated_same_phone_is_not_treated_as_two_identifiers(self):
        result = self.ask('13800000001，对，就是13800000001')
        self.assertEqual(result['status'],'needs_clarification',result)
        self.assertIn('SN-DEMO-0001',result['message']['content'])
        self.assertNotIn('多个手机号',result['message']['content'])

    def test_phone_predicate_is_applied_by_readonly_repository(self):
        with closing(sqlite3.connect(self.order_db)) as db, db:
            db.execute("UPDATE orders SET user_phonenumber=? WHERE order_id=?",('13800000003','SN-DEMO-0002'))
        repository = self.app.order_module.repository
        self.assertEqual([o['order_id'] for o in repository.snapshot('demo_user_a',phone_number='13800000001')],['SN-DEMO-0001'])
        self.assertEqual(repository.snapshot('demo_user_a',phone_number='13800000002'),[])
        self.assertEqual(repository.snapshot('demo_user_a',phone_number="' OR 1=1 --"),[])
        result = self.ask('13800000001')
        self.assertEqual(result['status'],'completed',result)
        self.assertIn('SN-DEMO-0001',result['message']['content'])
        self.assertNotIn('SN-DEMO-0002',result['message']['content'])

    def test_real_demo_phone_returns_nine_matches_with_recent_five_and_selection(self):
        app = WebApp(self.root / 'real-source-sessions.sqlite3', model=self.model)
        self.addCleanup(app.close)
        req = {'schema_version':'1.0','request_id':'req_real_phone','conversation_id':None,
               'expected_revision':0,'message':'13800000001','selected_reference':None}
        first = app.chat(req,'phone_test_browser','U_NEW')
        self.assertEqual(first['status'],'needs_clarification',first)
        self.assertIn('共匹配到9笔',first['message']['content'])
        self.assertIn('最近5笔',first['message']['content'])
        self.assertEqual(set(first['message']['content'].split()).intersection({'13800000001'}),set())
        selected = app.chat({**req,'request_id':'req_real_selected','conversation_id':first['conversation_id'],
                            'expected_revision':first['revision'],'message':'查询订单 SN-DEMO-0002 是否发货'},
                           'phone_test_browser','U_NEW')
        self.assertEqual(selected['status'],'completed',selected)
        self.assertIn('SN-DEMO-0002',selected['message']['content'])
        self.assertIn('已发货',selected['message']['content'])

    def test_multiple_explicit_order_numbers_do_not_silently_choose_first(self):
        out=self.ask('查 SN-DEMO-0001 和 SN-DEMO-0002')
        self.assertEqual(out['status'],'needs_clarification',out)
        self.assertNotIn('尚未发货',out['message']['content'])
        self.assertFalse(self.model.calls)

    def test_order_by_product_name_and_selected_followup(self):
        first = self.ask('查我之前买的牛肉干')
        self.assertEqual(first['status'],'completed',first)
        self.assertIn('SN-DEMO-0002',first['message']['content'])
        follow = self.ask('发货了吗',first)
        self.assertIn('SN-DEMO-0002',follow['message']['content'])

    def test_guest_vip_and_cross_account_cannot_read_other_orders(self):
        for profile in ('U_GUEST','U_VIP'):
            out = self.ask('查询订单 SN-DEMO-0001',profile=profile)
            self.assertEqual(out['status'],'error')
        for text in ('SN-DEMO-0010','手机号13800000002的订单'):
            out = self.ask(text,rid='req_cross_'+str(len(text)))
            self.assertEqual(out['status'],'no_match',out)
            self.assertNotIn('曲奇',out['message']['content'])

    def test_selected_product_reference_does_not_block_order_switch(self):
        first = self.ask('推荐独立包装零食')
        card=first['message']['cards'][0]
        out=self.ask('查询订单 SN-DEMO-0001',first,reference={'message_id':first['message']['id'],'plan_id':card['plan_id'],'sku_id':None})
        self.assertEqual(out['status'],'completed',out)
        self.assertEqual(out['module'],'order_query')

    def test_order_facts_change_prevents_commit(self):
        repository=self.app.order_module.repository
        original=repository.snapshot
        count=0
        def changing(user):
            nonlocal count
            count+=1
            rows=original(user)
            if count>1:rows[0]['shipping_status']='shipped'
            return rows
        repository.snapshot=changing
        out=self.ask('SN-DEMO-0001')
        self.assertEqual(out['error']['code'],'invalid_model_output',out)
        self.assertIsNone(out['conversation_id'])

    def test_replay_after_module_switch_is_stable(self):
        first=self.ask('SN-DEMO-0001',rid='req_replay')
        self.ask('推荐独立包装零食',first)
        replay=self.ask('SN-DEMO-0001',rid='req_replay')
        self.assertEqual(replay,first)
        self.assertEqual(len(self.app.store.session(first['conversation_id'],'browser_a')[1]),4)

    def test_disabled_mapping_stays_disabled_after_restart(self):
        with self.app.store.connect() as db:
            db.execute("UPDATE module_identity_bindings SET enabled=0 WHERE principal_id='U_NEW'")
        other=WebApp(self.root/'sessions.sqlite3',model=self.model,order_database=self.order_db)
        self.addCleanup(other.close)
        result=other.chat({'schema_version':'1.0','conversation_id':None,'request_id':'req_disabled','expected_revision':0,'message':'SN-DEMO-0001','selected_reference':None},'browser_a','U_NEW')
        self.assertEqual(result['status'],'error')

    def test_unavailable_human_entry_rejected_and_knowledge_sanitized(self):
        generator=self.app.order_module.reply_generator
        result={'result_type':'success','orders':[{'order_id':'SN-DEMO-0001','order_status':'paid','shipping_status':'unshipped'}],
                'query_type':'shipping','next_action':'none'}
        packet,retrieved=generator.packet(result,'发货了吗',[])
        self.assertFalse(packet['service_boundaries']['human_entry_available'])
        self.assertTrue(all('点击联系人工' not in r['closing'] for r in retrieved))
        template='亲～😊{{order_ref}}目前{{order_state}}。你可以点击联系人工。'
        from integration.legacy_loader import load
        with self.assertRaises(load(LEGACY,'model_adapter').ModelFailure):
            generator.render({'template':template},packet)

    def test_real_mock_logistics_transport_and_failure_clears_old_trace(self):
        app=WebApp(self.root/'courier-sessions.sqlite3',model=self.model,order_database=self.order_db,start_mock_courier=True)
        self.addCleanup(app.close)
        request={'schema_version':'1.0','conversation_id':None,'request_id':'req_courier','expected_revision':0,'message':'SN-DEMO-0002 物流到哪了','selected_reference':None}
        out=app.chat(request,'browser_a','U_NEW')
        self.assertEqual(out['status'],'completed',out)
        diagnostic=app.store.diagnostics(request,'browser_a')
        self.assertTrue(diagnostic['live_logistics_queried'])
        self.assertEqual(diagnostic['courier_calls'][0]['http_status'],200)
        self.assertEqual(diagnostic['courier_calls'][0]['transport'],'http')
        self.assertIn('本机模拟',out['message']['content'])
        self.assertNotIn('旧的已签收轨迹',out['message']['content'])
        app.courier_server.shutdown();app.courier_server.server_close();app.courier_thread.join(timeout=3);app.courier_server=None
        request={**request,'request_id':'req_courier_failure','conversation_id':out['conversation_id'],'expected_revision':1}
        failed=app.chat(request,'browser_a','U_NEW')
        self.assertEqual(failed['status'],'completed',failed)
        self.assertIn('未成功',failed['message']['content'])
        parcel=app.store.diagnostics(request,'browser_a')['legacy_result']['orders'][0]['shipments'][0]
        self.assertEqual(parcel['tracking_events'],[])
        self.assertIsNone(parcel['estimated_delivery_at'])

    def test_old_web_history_migrates_without_rewriting_messages(self):
        path=self.root/'old.sqlite3'
        from runtime.service import SessionStore, ChatService
        from runtime.recommendation import RecommendationModule
        from core_helpers import NOW,catalog
        import asyncio
        store=SessionStore(path)
        old_request={'schema_version':'1.0','conversation_id':None,'request_id':'req_old','expected_revision':0,'message':'推荐独立包装零食','selected_reference':None}
        trusted={'owner_id':'old_browser','user_id':'U_NEW','channel':'web'}
        old=asyncio.run(ChatService(RecommendationModule(catalog(),self.model),store,clock=lambda:NOW).handle(old_request,trusted))
        with store.connect() as db:
            db.execute('CREATE TABLE web_contexts(id TEXT PRIMARY KEY,owner TEXT,profile TEXT,touched REAL)')
            db.execute('INSERT INTO web_contexts VALUES(?,?,?,1)',(old['conversation_id'],'old_browser','U_NEW'))
            before=list(db.execute('SELECT * FROM sessions'))
        migrated=WebApp(path,model=self.model,order_database=self.order_db)
        self.addCleanup(migrated.close)
        with migrated.store.connect() as db:self.assertEqual(before,list(db.execute('SELECT * FROM sessions')))
        self.assertEqual(migrated.chat(old_request,'old_browser','U_NEW'),old)
        self.assertTrue(list((self.root/'unified-backups').glob('*.sqlite3')))
        follow={**old_request,'request_id':'req_old_next','conversation_id':old['conversation_id'],'expected_revision':1,'message':'SN-DEMO-0001'}
        self.assertEqual(migrated.chat(follow,'old_browser','U_NEW')['status'],'completed')


class RouterTests(unittest.TestCase):
    def test_clear_order_and_recommendation_phrases(self):
        router=ConversationRouter()
        for text in ('发了没','我之前买的饼干什么时候到','查订单 SN-DEMO-0001','快递到哪了','退款'):
            with self.subTest(text=text):self.assertEqual(router.route(text,None),'order_query')
        for text in ('推荐低糖饼干','预算改为60元','买三件独立包装零食','继续刚才的推荐'):
            with self.subTest(text=text):self.assertEqual(router.route(text,None),'product_recommendation')

    def test_ambiguous_cross_module_reference_clarifies(self):
        previous={'active_module':'order_query','module_states':{'order_query':{},'product_recommendation':{}}}
        self.assertEqual(ConversationRouter().route('第二个',previous),'conversation_router')

    def test_negated_intent_does_not_become_mixed_question(self):
        router=ConversationRouter()
        self.assertEqual(router.route('不用查订单，只推荐低糖饼干',None),'product_recommendation')
        self.assertEqual(router.route('不要推荐，先查询订单',None),'order_query')

    def test_initial_bad_phone_still_uses_order_module(self):
        self.assertEqual(ConversationRouter().route('手机号1380000000',None),'order_query')

    def test_embedded_phone_routes_to_order_or_mixed_clarification(self):
        router=ConversationRouter()
        self.assertEqual(router.route('这是13800000001，帮我看一下',None),'order_query')
        self.assertEqual(router.route('电话：1380000000',None),'order_query')
        self.assertEqual(router.route('13800000001，也推荐低糖零食',None),'conversation_router')
