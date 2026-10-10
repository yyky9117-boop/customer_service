import json
import sqlite3
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import asyncio
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from database.demo_db import initialize, connect, snapshot
from database import migrate_legacy
from service import CustomerService,ServiceError,extract_parameters
from nodes.normalize import main as normalize
from nodes.query import main as query
from nodes.render import main as render
from courier_simulator import create_courier_server, connect as courier_connect
from courier_client import CourierClient
from model_adapter import DeepSeekExtractor


@contextmanager
def test_database():
    root=(ROOT/'tests').resolve()
    path=root/('test-db-'+uuid.uuid4().hex+'.sqlite')
    try:yield path
    finally:
        for suffix in ['', '-journal','-wal','-shm','.courier.sqlite','.courier.sqlite-journal','.courier.sqlite-wal','.courier.sqlite-shm','.human.sqlite','.human.sqlite-journal','.human.sqlite-wal','.human.sqlite-shm']:
            target=Path(str(path)+suffix).resolve()
            if target.parent!=root:raise AssertionError('测试清理路径错误')
            if target.exists():target.unlink()


class CourierFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.courier_temp=test_database()
        cls.courier_path=cls.courier_temp.__enter__()
        cls.courier_server=create_courier_server(0,cls.courier_path)
        cls.courier_thread=threading.Thread(target=cls.courier_server.serve_forever,daemon=True)
        cls.courier_thread.start()
        cls.courier=CourierClient('http://127.0.0.1:'+str(cls.courier_server.server_port),cls.courier_server.token)

    @classmethod
    def tearDownClass(cls):
        cls.courier_server.shutdown()
        cls.courier_server.server_close()
        cls.courier_thread.join(timeout=3)
        cls.courier_temp.__exit__(None,None,None)

    def setUp(self):
        con=courier_connect(self.courier_path)
        with con:con.execute('UPDATE parcels SET scenario=default_scenario')
        con.close()


class ServiceTests(CourierFixture):
    def setUp(self):
        super().setUp()
        self.temp=test_database()
        self.path=self.temp.__enter__()
        initialize(self.path)
        self.service=CustomerService(self.path,model=DeepSeekExtractor({'MODEL_ENABLED':'false'}),courier=self.courier)

    def tearDown(self):self.temp.__exit__(None,None,None)

    def chat(self,message,**kwargs):return self.service.chat('demo_user_a',message,**kwargs)

    def test_database_schema_and_integrity(self):
        con=connect(self.path)
        self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(con.execute('SELECT COUNT(*) FROM orders').fetchone()[0],10)
        con.close()

    def test_order_and_tracking_are_different(self):
        order=self.chat('查商城订单 SN-DEMO-0002')
        tracking=self.chat('快递运单号 DEMO-TRACK-1002 到哪了')
        self.assertEqual(order['orders'][0]['order_id'],tracking['orders'][0]['order_id'])
        self.assertEqual(order['orders'][0]['shipments'][0]['tracking_number'],'DEMO-TRACK-1002')

    def test_unknown_order(self):self.assertEqual(self.chat('查询订单 SN-DEMO-9999')['result_type'],'not_found')

    def test_unshipped_no_fake_notification(self):
        result=self.chat('SN-DEMO-0001 发货了吗')
        self.assertIn('尚未发货',result['reply'])
        self.assertNotIn('通知',result['reply'])

    def test_product_filter(self):self.assertEqual(self.chat('我的辣条物流到哪了')['orders'][0]['order_id'],'SN-DEMO-0004')

    def test_multiple_orders_then_click_selection(self):
        first=self.chat('我买的牛肉干物流到哪了')
        self.assertEqual(first['result_type'],'multiple_orders')
        second=self.chat('选择订单 SN-DEMO-0002',conversation_id=first['conversation_id'],selected_order_id='SN-DEMO-0002')
        self.assertEqual(second['result_type'],'success')
        self.assertIn('运输中',second['reply'])

    def test_multiple_orders_then_ordinal(self):
        first=self.chat('我买的牛肉干物流到哪了')
        second=self.chat('第二笔',conversation_id=first['conversation_id'])
        self.assertEqual(second['orders'][0]['order_id'],first['candidates'][1]['order_id'])

    def test_followup_information_preserves_query_type(self):
        first=self.chat('预计什么时候到')
        self.assertEqual(first['result_type'],'need_more_info')
        second=self.chat('SN-DEMO-0002',conversation_id=first['conversation_id'])
        self.assertEqual(second['query_type'],'delivery_time')
        self.assertIn('2026-10-08 18:00',second['reply'])

    def test_context_survives_service_restart(self):
        first=self.chat('SN-DEMO-0002 物流到哪了')
        restarted=CustomerService(self.path,model=self.service.model,courier=self.courier)
        second=restarted.chat('demo_user_a','那它什么时候到',first['conversation_id'])
        self.assertEqual(second['orders'][0]['order_id'],'SN-DEMO-0002')
        self.assertIn('2026-10-08',second['reply'])

    def test_new_explicit_number_clears_previous_candidate_filters(self):
        first=self.chat('我买的牛肉干物流到哪了')
        second=self.chat('SN-DEMO-0001 发货了吗',conversation_id=first['conversation_id'])
        self.assertEqual(second['orders'][0]['order_id'],'SN-DEMO-0001')
        self.assertIn('尚未发货',second['reply'])

    def test_human_requests_for_different_orders_keep_separate_snapshots(self):
        first=self.chat('SN-DEMO-0007')
        request_a=self.service.request_human('demo_user_a',first['conversation_id'])
        second=self.chat('SN-DEMO-0005',conversation_id=first['conversation_id'])
        request_b=self.service.request_human('demo_user_a',second['conversation_id'])
        self.assertNotEqual(request_a['request_id'],request_b['request_id'])
        con=connect(self.path)
        rows=con.execute('SELECT context_json FROM human_requests ORDER BY created_at').fetchall()
        con.close()
        self.assertEqual([json.loads(r['context_json'])['orders'][0]['order_id'] for r in rows],['SN-DEMO-0007','SN-DEMO-0005'])

    def test_new_query_does_not_use_previous_order(self):
        first=self.chat('SN-DEMO-0002 物流到哪了')
        second=self.chat('我的辣条怎么样',conversation_id=first['conversation_id'])
        self.assertEqual(second['orders'][0]['order_id'],'SN-DEMO-0004')

    def test_user_scope_for_order_tracking_phone(self):
        for message in ['SN-DEMO-0010','运单号 DEMO-TRACK-1010','手机号13800000002']:
            self.assertEqual(self.chat(message)['result_type'],'not_found')

    def test_cross_user_conversation_denied(self):
        first=self.chat('SN-DEMO-0002')
        with self.assertRaises(ServiceError) as caught:self.service.chat('demo_user_b','继续查询',first['conversation_id'])
        self.assertEqual(caught.exception.status,404)

    def test_cross_user_selection_denied(self):
        first=self.chat('手机号13800000001')
        second=self.chat('选择SN-DEMO-0010',conversation_id=first['conversation_id'],selected_order_id='SN-DEMO-0010')
        self.assertEqual(second['result_type'],'not_found')

    def test_bad_phone_then_corrected(self):
        first=self.chat('手机号12345678901')
        self.assertEqual(first['result_type'],'need_more_info')
        second=self.chat('手机号13800000001',conversation_id=first['conversation_id'])
        self.assertEqual(second['result_type'],'multiple_orders')

    def test_database_error_not_not_found(self):
        for failure in ['database_timeout','database_error']:
            result=self.chat('SN-DEMO-0002',failure_mode=failure)
            self.assertEqual(result['result_type'],'query_failed')
            self.assertNotIn('没有找到',result['reply'])

    def test_logistics_failure_preserves_order_facts(self):
        result=self.chat('SN-DEMO-0002 物流到哪了',failure_mode='logistics_timeout')
        self.assertEqual(result['result_type'],'partial_success')
        self.assertIn('已发货',result['reply'])
        self.assertEqual(result['orders'][0]['shipments'][0]['tracking_events'],[])

    def test_shipping_query_skips_logistics_failure(self):
        result=self.chat('SN-DEMO-0002 发货了吗',failure_mode='logistics_timeout')
        self.assertEqual(result['result_type'],'success')

    def test_no_tracking_not_unshipped(self):
        result=self.chat('SN-DEMO-0004 物流到哪了')
        self.assertIn('已发货',result['reply'])
        self.assertIn('暂未获取',result['reply'])

    def test_eta_not_fabricated(self):
        self.assertIn('暂无预计',self.chat('SN-DEMO-0004 什么时候到')['reply'])
        result=self.chat('SN-DEMO-0002 什么时候到',failure_mode='logistics_empty')
        self.assertNotIn('2026-10-08',result['reply'])

    def test_delivered_cancelled_unpaid_and_partial(self):
        for code,text in [('0003','已签收'),('0006','已取消'),('0009','待付款'),('0008','已部分发货')]:
            self.assertIn(text,self.chat('查询SN-DEMO-'+code)['reply'])

    def test_unknown_shipping_needs_human(self):
        result=self.chat('SN-DEMO-0007 发货了吗')
        self.assertIn('暂未确认',result['reply'])
        self.assertEqual(result['next_action'],'request_human')

    def test_refund_and_dispute_need_human(self):
        self.assertEqual(self.chat('我要退款')['next_action'],'request_human')
        self.assertEqual(self.chat('SN-DEMO-0003 签收但没收到')['next_action'],'request_human')

    def test_product_consultation_not_misread_as_order(self):
        self.assertEqual(self.chat('牛肉干多少钱')['result_type'],'human_required')

    def test_human_request_persisted_and_idempotent(self):
        first=self.chat('SN-DEMO-0005 物流到哪了')
        a=self.service.request_human('demo_user_a',first['conversation_id'])
        b=self.service.request_human('demo_user_a',first['conversation_id'])
        self.assertEqual(a['request_id'],b['request_id'])
        self.assertEqual(a['status'],'not_connected')
        self.assertNotIn('已转接',a['message'])
        con=connect(self.path)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM human_requests').fetchone()[0],1)
        con.close()

    def test_other_user_cannot_request_human_for_conversation(self):
        first=self.chat('SN-DEMO-0005')
        with self.assertRaises(ServiceError):self.service.request_human('demo_user_b',first['conversation_id'])

    def test_output_does_not_expose_identity(self):
        result=self.chat('SN-DEMO-0002')
        self.assertNotIn('user_id',result['orders'][0])
        self.assertNotIn('user_phonenumber',result['orders'][0])
        self.assertLessEqual(len(result['reply']),360)

    def test_source_truncation_and_malformed_data(self):
        base={'params_json':'{"user_id":"demo_user_a","order_id":"UNKNOWN"}'}
        truncated=asyncio.run(query(SimpleNamespace(params={**base,'data_json':'[]','source_may_be_truncated':True})))
        self.assertEqual(json.loads(truncated['query_result_json'])['result_type'],'narrow_search')
        malformed=asyncio.run(query(SimpleNamespace(params={**base,'data_json':'[null]'})))
        self.assertEqual(json.loads(malformed['query_result_json'])['result_type'],'query_failed')

    def test_extract_missing_numbers_not_invented(self):
        self.assertEqual(extract_parameters('我忘了订单号')['order_id'],'')
        normalized=asyncio.run(normalize(SimpleNamespace(params={'extracted':{'order_id':'null','user_id':'demo_user_b'},'user_id':'demo_user_a'})))
        fields=json.loads(normalized['params_json'])
        self.assertEqual(fields['user_id'],'demo_user_a')
        self.assertEqual(fields['order_id'],'')

    def test_legacy_migration_preserves_tracking_without_guess(self):
        # 新数据库单独迁移，旧记录不改变。
        with test_database() as path:
            records=[{'order_id':'LEGACY-TRACK-001','item_title':'牛肉干','user_phonenumber':'13800000001'}]
            before=json.dumps(records,sort_keys=True)
            self.assertEqual(migrate_legacy.migrate(records,path),1)
            self.assertEqual(migrate_legacy.migrate(records,path),0)
            self.assertEqual(json.dumps(records,sort_keys=True),before)
            con=connect(path)
            row=con.execute('SELECT shipping_status,legacy_tracking_number,user_id FROM orders').fetchone()
            self.assertEqual(tuple(row),('unknown','LEGACY-TRACK-001','legacy_unassigned'))
            con.close()


if __name__=='__main__':unittest.main(verbosity=2)
