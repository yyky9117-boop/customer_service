import http.cookiejar
import json
import threading
import unittest
import urllib.error
import urllib.request

from test_service import test_database
from app import create_server
from model_adapter import DeepSeekExtractor


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=test_database()
        cls.path=cls.temp.__enter__()
        cls.server=create_server(0,cls.path,model=DeepSeekExtractor({'MODEL_ENABLED':'false'}))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.__exit__(None,None,None)

    def setUp(self):
        self.client=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self,path,body=None,headers=None):
        hdr=headers or {}
        payload=None
        if body is not None:
            payload=json.dumps(body).encode()
            hdr={'Content-Type':'application/json',**hdr}
        request=urllib.request.Request(self.base+path,data=payload,headers=hdr)
        try:
            with self.client.open(request,timeout=5) as response:
                return response.status,response.read(),response.headers
        except urllib.error.HTTPError as exc:return exc.code,exc.read(),exc.headers

    def login(self,user='demo_user_a'):
        status,_,headers=self.request('/api/demo/login',{'account':user})
        self.assertEqual(status,200)
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertIn('SameSite=Strict',headers['Set-Cookie'])

    def test_health_no_coze_dependency(self):
        status,body,_=self.request('/api/health')
        self.assertEqual(status,200)
        self.assertFalse(json.loads(body)['coze_required'])

    def test_page_and_assets(self):
        for path in ['/','/app.js','/style.css']:
            status,body,headers=self.request(path)
            self.assertEqual(status,200)
            self.assertTrue(body)
            self.assertIn("default-src 'self'",headers['Content-Security-Policy'])

    def test_no_session_requires_login(self):
        status,_,_=self.request('/api/chat',{'message':'SN-DEMO-0002'})
        self.assertEqual(status,401)

    def test_reply_runs_require_login(self):
        self.assertEqual(self.request('/api/reply/runs')[0],401)

    def test_reply_runs_are_scoped_to_account_and_match_current_reply(self):
        self.login()
        _,body,_=self.request('/api/chat',{'message':'SN-DEMO-0001发货了吗'})
        result=json.loads(body)
        self.assertEqual(result['reply_status']['mode'],'knowledge_fallback')
        status,body,_=self.request('/api/reply/runs')
        rows=json.loads(body)['runs']
        self.assertEqual(status,200)
        self.assertIn(result['reply_status']['run_id'],[row['run_id'] for row in rows])
        self.assertNotIn('user_id',rows[0])
        self.assertTrue(rows[0]['retrieval'])
        self.login('demo_user_b')
        _,body,_=self.request('/api/reply/runs')
        self.assertNotIn(result['reply_status']['run_id'],[row['run_id'] for row in json.loads(body)['runs']])

    def test_login_query_and_followup(self):
        self.login()
        status,body,_=self.request('/api/chat',{'message':'SN-DEMO-0002物流到哪了'})
        first=json.loads(body)
        self.assertEqual(status,200)
        self.assertEqual(first['orders'][0]['order_id'],'SN-DEMO-0002')
        status,body,_=self.request('/api/chat',{'message':'那它什么时候到','conversation_id':first['conversation_id']})
        self.assertEqual(status,200)
        self.assertIn('2026-10-08',json.loads(body)['reply'])

    def test_body_cannot_override_identity(self):
        self.login()
        status,body,_=self.request('/api/chat',{'message':'SN-DEMO-0010','user_id':'demo_user_b'})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['result_type'],'not_found')

    def test_owned_order_list(self):
        self.login('demo_user_b')
        status,body,_=self.request('/api/orders')
        self.assertEqual(status,200)
        self.assertEqual([o['order_id'] for o in json.loads(body)['orders']],['SN-DEMO-0010'])

    def test_multi_order_selection(self):
        self.login()
        _,body,_=self.request('/api/chat',{'message':'查一下牛肉干的订单'})
        first=json.loads(body)
        self.assertEqual(first['result_type'],'multiple_orders')
        status,body,_=self.request('/api/chat',{'message':'选择订单SN-DEMO-0002','selected_order_id':'SN-DEMO-0002','conversation_id':first['conversation_id']})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['orders'][0]['order_id'],'SN-DEMO-0002')

    def test_human_request_endpoint_and_listing(self):
        self.login()
        _,body,_=self.request('/api/chat',{'message':'SN-DEMO-0005物流异常'})
        conversation=json.loads(body)['conversation_id']
        status,body,_=self.request('/api/human/request',{'conversation_id':conversation})
        self.assertEqual(status,200)
        request=json.loads(body)
        self.assertEqual(request['status'],'queued')
        status,body,_=self.request('/api/human/requests')
        self.assertEqual(status,200)
        self.assertIn(request['request_id'],[r['request_id'] for r in json.loads(body)['requests']])

    def test_cross_account_conversation_denied(self):
        self.login()
        _,body,_=self.request('/api/chat',{'message':'SN-DEMO-0002'})
        conversation=json.loads(body)['conversation_id']
        self.login('demo_user_b')
        status,_,_=self.request('/api/chat',{'message':'继续查','conversation_id':conversation})
        self.assertEqual(status,404)

    def test_invalid_parameters(self):
        self.login()
        for message in [123,'','a'*2001]:
            status,_,_=self.request('/api/chat',{'message':message})
            self.assertEqual(status,400)

    def test_cross_origin_post_rejected(self):
        status,_,_=self.request('/api/demo/login',{'account':'demo_user_a'},{'Origin':'http://example.invalid'})
        self.assertEqual(status,403)

    def test_static_path_not_exposed(self):
        for path in ['/database/snack_orders_demo.sqlite','/.env.local','/../app.py']:
            status,_,_=self.request(path)
            self.assertEqual(status,404)

    def test_courier_calls_and_demo_control(self):
        self.login()
        status,body,_=self.request('/api/chat',{'message':'SN-DEMO-0002物流到哪了'})
        self.assertEqual(status,200)
        call=json.loads(body)['courier_calls'][0]
        status,body,_=self.request('/api/courier/calls')
        self.assertEqual(status,200)
        self.assertIn(call['request_id'],[c['request_id'] for c in json.loads(body)['calls']])
        status,_,_=self.request('/api/demo/logistics',{'tracking_number':'DEMO-TRACK-1002','scenario':'delivering'})
        self.assertEqual(status,200)
        _,body,_=self.request('/api/chat',{'message':'SN-DEMO-0002物流到哪了'})
        self.assertIn('正在派送',json.loads(body)['reply'])
        self.request('/api/demo/logistics',{'tracking_number':'DEMO-TRACK-1002','scenario':'default'})

    def test_courier_call_history_and_control_are_scoped(self):
        self.login('demo_user_b')
        status,body,_=self.request('/api/courier/calls')
        self.assertEqual(status,200)
        self.assertTrue(all(c['order_id']=='SN-DEMO-0010' for c in json.loads(body)['calls']))
        status,_,_=self.request('/api/demo/logistics',{'tracking_number':'DEMO-TRACK-1002','scenario':'delivered'})
        self.assertEqual(status,404)


if __name__=='__main__':unittest.main(verbosity=2)
