import json
import threading
import unittest
import urllib.error
import urllib.request
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from runtime.web import WebApp, make_server
from core_helpers import ScriptedModel, WorkspaceTemp, constraint, patch, update


def normal():
    return patch([update(constraints=[constraint('package','wrapped')])])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.model = ScriptedModel(normal(), normal())
        self.app = WebApp(Path(self.temp.name) / 'web.sqlite3', model=self.model)
        self.server = make_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = self.client_for()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        self.temp.cleanup()

    def client_for(self):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path, client=None):
        return (client or self.client).open(self.url+path,timeout=5)

    def request(self, rid='req_web', cid=None, revision=0):
        return {'schema_version':'1.0','conversation_id':cid,'request_id':rid,'expected_revision':revision,'message':'测试需求','selected_reference':None}

    def post(self, request, profile='U_GUEST', client=None, headers=None):
        req=urllib.request.Request(self.url+'/api/chat',data=json.dumps(request).encode(),headers={'Content-Type':'application/json','X-Demo-User':profile,**(headers or {})})
        return json.load((client or self.client).open(req,timeout=5))

    def test_registered_static_files_have_correct_headers(self):
        for path,mime in [('/','text/html'),('/app.js','text/javascript'),('/styles.css','text/css'),('/favicon.svg','image/svg+xml')]:
            with self.subTest(path=path),self.get(path) as response:
                self.assertTrue(response.headers['Content-Type'].startswith(mime))
                self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
                self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
                self.assertTrue(response.read())

    def test_occupied_port_cannot_be_shared_by_second_server(self):
        with self.assertRaises(OSError):
            make_server(self.app, port=self.server.server_port)

    def test_env_and_project_files_are_not_exposed(self):
        for path in ['/.env','/runtime/model.py','/../.env','/%2e%2e/.env','/acceptance/stage3_execution.json']:
            with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as failure:
                self.get(path)
            self.assertEqual(failure.exception.code,404)

    def test_bootstrap_only_exposes_public_configuration(self):
        data=json.load(self.get('/api/bootstrap'))
        self.assertEqual(data['application'],'snack-customer-service')
        self.assertNotIn('api_key',data)
        self.assertEqual(len(data['profiles']),6)

    def test_new_chat_and_history_roundtrip(self):
        result=self.post(self.request())
        history=json.load(self.get('/api/conversation?id='+result['conversation_id']))
        self.assertEqual(history['revision'],1)
        self.assertEqual(len(history['history']),2)
        self.assertEqual(history['history'][-1]['cards'],result['message']['cards'])
        listing=json.load(self.get('/api/conversations'))
        self.assertEqual(listing['conversations'][0]['id'],result['conversation_id'])

    def test_same_request_retry_returns_identical_response(self):
        request=self.request()
        self.assertEqual(self.post(request),self.post(request))
        self.assertEqual(len(self.model.calls),1)

    def test_cached_request_recovery_after_lost_response(self):
        original=self.post(self.request())
        cached=json.load(self.get('/api/request?scope=new&id=req_web'))
        self.assertEqual(cached['response'],original)
        self.assertIsNone(json.load(self.get('/api/request?scope=new&id=req_other'))['response'])

    def test_client_cookie_is_httponly_same_site(self):
        with self.get('/api/bootstrap') as response:
            cookie=response.headers['Set-Cookie']
            self.assertIn('HttpOnly',cookie)
            self.assertIn('SameSite=Strict',cookie)

    def test_other_browser_cannot_load_history_or_request_cache(self):
        original=self.post(self.request())
        other=self.client_for()
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.get('/api/conversation?id='+original['conversation_id'],other)
        self.assertEqual(failure.exception.code,404)
        self.assertEqual(json.load(self.get('/api/conversations',other))['conversations'],[])
        self.assertIsNone(json.load(self.get('/api/request?scope=new&id=req_web',other))['response'])

    def test_conversation_profile_cannot_change_silently(self):
        first=self.post(self.request(),profile='U_NEW')
        second=self.post(self.request('req_second',first['conversation_id'],1),profile='U_VIP')
        self.assertEqual(second['error']['code'],'invalid_request')
        history=json.load(self.get('/api/conversation?id='+first['conversation_id']))
        self.assertEqual(history['demo_user_id'],'U_NEW')
        self.assertEqual(history['revision'],1)

    def test_new_conversation_can_use_another_demo_profile(self):
        a=self.post(self.request(),profile='U_NEW')
        b=self.post(self.request('req_new'),profile='U_VIP')
        self.assertNotEqual(a['conversation_id'],b['conversation_id'])
        self.assertEqual(json.load(self.get('/api/conversation?id='+b['conversation_id']))['demo_user_id'],'U_VIP')

    def test_json_chat_cannot_override_identity(self):
        result=self.post({**self.request(),'user_id':'U_VIP'})
        self.assertEqual(result['error']['code'],'invalid_request')
        self.assertFalse(self.model.calls)

    def test_cross_site_origin_and_wrong_host_rejected(self):
        for headers in [{'Origin':'https://elsewhere.example'},{'Host':'elsewhere.example'}]:
            with self.subTest(headers=headers),self.assertRaises(urllib.error.HTTPError) as failure:
                self.post(self.request(),headers=headers)
            self.assertEqual(failure.exception.code,403)
        self.assertFalse(self.model.calls)

    def test_same_inflight_request_coalesces_model_work(self):
        started,release=threading.Event(),threading.Event()
        class SlowModel(ScriptedModel):
            async def extract(self,payload,repair=None):
                started.set(); release.wait(timeout=3)
                return await super().extract(payload,repair)
        model=SlowModel(normal())
        self.app.service.module.model=model
        req=self.request();owner='web_test_owner'
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.app.chat,req,owner,'U_GUEST')
            self.assertTrue(started.wait(timeout=2))
            second=pool.submit(self.app.chat,req,owner,'U_GUEST')
            release.set()
            self.assertEqual(first.result(timeout=5),second.result(timeout=5))
        self.assertEqual(len(model.calls),1)

    def test_bad_json_does_not_call_model(self):
        req=urllib.request.Request(self.url+'/api/chat',data=b'{bad',headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.client.open(req,timeout=5)
        self.assertEqual(failure.exception.code,400)
        self.assertFalse(self.model.calls)
