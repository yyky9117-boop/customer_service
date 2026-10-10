import http.cookiejar
import io
import json
import threading
import unittest
import urllib.error
import urllib.request

from test_service import test_database
from app import create_server
from database.demo_db import connect
from human_simulator import connect as human_connect
from human_client import HumanClient
from model_adapter import DeepSeekExtractor


class HumanApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=test_database()
        cls.path=cls.temp.__enter__()
        cls.server=create_server(0,cls.path,model=DeepSeekExtractor({'MODEL_ENABLED':'false'}))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
        cls.human_path=str(cls.path)+'.human.sqlite'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.__exit__(None,None,None)

    def setUp(self):
        self.client=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call('/api/demo/login',{'account':'demo_user_a'})

    def call(self,path,body=None):
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(self.base+path,data=data,headers={'Content-Type':'application/json'} if body is not None else {})
        try:
            with self.client.open(req,timeout=5) as response:return response.status,json.loads(response.read())
        except urllib.error.HTTPError as exc:return exc.code,json.loads(exc.read())

    def start(self,fault='',question='SN-DEMO-0005 物流异常，帮我联系人工'):
        _,chat=self.call('/api/chat',{'message':question})
        status,result=self.call('/api/human/request',{'conversation_id':chat['conversation_id'],'failure_mode':fault})
        self.assertEqual(status,200)
        return chat,result

    def test_real_http_create_and_context_forwarding(self):
        _,result=self.start()
        self.assertEqual(result['status'],'queued')
        self.assertGreaterEqual(result['queue_position'],1)
        call=result['human_calls'][0]
        self.assertEqual(call['http_status'],200)
        self.assertEqual(call['transport'],'http')
        con=human_connect(self.human_path)
        ticket=con.execute('SELECT * FROM tickets WHERE ticket_id=?',(result['ticket_id'],)).fetchone()
        received=con.execute('SELECT * FROM provider_requests WHERE request_id=?',(call['request_id'],)).fetchone()
        con.close()
        self.assertIsNotNone(received)
        context=json.loads(ticket['context_json'])
        self.assertEqual(context['orders'][0]['order_id'],'SN-DEMO-0005')
        self.assertIn('联系人工',context['customer_question'])
        self.assertEqual(len(context['messages']),2)
        self.assertNotIn(self.server.service.human_client.token,json.dumps(call))
        con=connect(self.path)
        row=con.execute('SELECT * FROM human_api_calls WHERE call_id=?',(call['request_id'],)).fetchone()
        con.close()
        self.assertEqual(row['human_request_id'],result['request_id'])

    def test_repeated_submit_is_idempotent(self):
        chat,first=self.start()
        _,second=self.call('/api/human/request',{'conversation_id':chat['conversation_id']})
        self.assertEqual(first['request_id'],second['request_id'])
        self.assertEqual(first['ticket_id'],second['ticket_id'])
        self.assertNotEqual(first['human_calls'][0]['request_id'],second['human_calls'][0]['request_id'])
        con=human_connect(self.human_path)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM tickets WHERE human_request_id=?',(first['request_id'],)).fetchone()[0],1)
        con.close()

    def test_queue_connect_complete_and_refresh(self):
        _,result=self.start()
        request=result['request_id']
        for state in ['connected','closed']:
            status,updated=self.call('/api/demo/human-state',{'request_id':request,'status':state})
            self.assertEqual(status,200)
            self.assertEqual(updated['status'],state)
            self.assertTrue(updated['status_verified'])
            self.assertIn('模拟坐席',updated['agent_name'])
            self.assertIsNone(updated['queue_position'])
        _,refreshed=self.call('/api/human/query',{'request_id':request})
        self.assertEqual(refreshed['status'],'closed')
        _,history=self.call('/api/human/requests')
        item=next(r for r in history['requests'] if r['request_id']==request)
        self.assertEqual(item['status'],'closed')

    def test_invalid_transition_keeps_previous_state(self):
        _,result=self.start()
        _,invalid=self.call('/api/demo/human-state',{'request_id':result['request_id'],'status':'closed'})
        self.assertEqual(invalid['status'],'queued')
        self.assertFalse(invalid['operation_succeeded'])
        self.assertEqual(invalid['human_calls'][0]['http_status'],409)

    def test_timeout_and_safe_recovery_do_not_duplicate_tickets(self):
        _,unknown=self.start('timeout')
        self.assertEqual(unknown['status'],'unknown')
        self.assertIsNone(unknown['ticket_id'])
        self.assertFalse(unknown['status_verified'])
        self.assertEqual(unknown['human_calls'][0]['status'],'timeout')
        _,recovered=self.call('/api/human/query',{'request_id':unknown['request_id']})
        self.assertEqual(recovered['status'],'queued')
        con=human_connect(self.human_path)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM tickets WHERE human_request_id=?',(unknown['request_id'],)).fetchone()[0],1)
        con.close()

    def test_http_error_fails_then_retry_creates_ticket(self):
        chat,failed=self.start('http_error')
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed['human_calls'][0]['http_status'],503)
        _,retried=self.call('/api/human/request',{'conversation_id':chat['conversation_id']})
        self.assertEqual(retried['status'],'queued')
        self.assertEqual(retried['request_id'],failed['request_id'])

    def test_malformed_result_is_not_claimed_success(self):
        _,result=self.start('malformed')
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(result['human_calls'][0]['status'],'invalid_response')
        self.assertIsNone(result['ticket_id'])

    def test_no_agents_still_queues_without_claiming_connected(self):
        _,result=self.start('no_agents')
        self.assertEqual(result['status'],'queued')
        self.assertIn('无人在线',result['message'])
        self.assertIsNone(result['agent_name'])

    def test_user_cannot_query_or_modify_another_users_ticket(self):
        _,result=self.start()
        self.call('/api/demo/login',{'account':'demo_user_b'})
        for path,body in [('/api/human/query',{'request_id':result['request_id']}),('/api/demo/human-state',{'request_id':result['request_id'],'status':'connected'})]:
            status,_=self.call(path,body)
            self.assertEqual(status,404)
        _,calls=self.call('/api/human/calls')
        self.assertNotIn(result['request_id'],[r['human_request_id'] for r in calls['calls']])

    def test_missing_login_is_rejected(self):
        self.client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path,body in [('/api/human/calls',None),('/api/human/query',{'request_id':'none'}),('/api/demo/human-state',{'request_id':'none','status':'connected'})]:
            status,_=self.call(path,body)
            self.assertEqual(status,401)

    def test_provider_requires_token(self):
        direct=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req=urllib.request.Request(self.server.service.human_client.base_url+'/v1/handoffs/create',data=b'{}',headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as caught:direct.open(req,timeout=3)
        self.assertEqual(caught.exception.code,401)

    def test_forwarded_context_is_frozen_for_the_requested_order(self):
        chat,result=self.start()
        self.call('/api/chat',{'message':'SN-DEMO-0002','conversation_id':chat['conversation_id']})
        self.call('/api/human/query',{'request_id':result['request_id']})
        con=human_connect(self.human_path)
        context=json.loads(con.execute('SELECT context_json FROM tickets WHERE ticket_id=?',(result['ticket_id'],)).fetchone()[0])
        con.close()
        self.assertEqual(context['orders'][0]['order_id'],'SN-DEMO-0005')

    def test_response_owner_mismatch_is_rejected(self):
        payload={'user_id':'demo_user_a','human_request_id':'HR-test'}
        output={'code':'ok','request_id':'HC-test','is_simulated':True,
                'data':{'status':'queued','is_simulated':True,'human_request_id':'HR-test','user_id':'demo_user_b'}}
        with self.assertRaises(ValueError):HumanClient.validate(output,payload,'HC-test')

    def test_valid_json_without_ticket_timestamps_is_rejected(self):
        payload={'user_id':'demo_user_a','human_request_id':'HR-test'}
        output={'code':'ok','request_id':'HC-test','is_simulated':True,
                'data':{'status':'queued','is_simulated':True,'human_request_id':'HR-test','user_id':'demo_user_a',
                        'ticket_id':'HT-test','queue_position':1,'agent_name':None,'forwarded_orders':0,'forwarded_messages':0}}
        with self.assertRaisesRegex(ValueError,'invalid timestamp'):HumanClient.validate(output,payload,'HC-test')
