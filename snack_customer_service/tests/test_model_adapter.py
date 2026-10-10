import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from test_service import test_database, CourierFixture
from database.demo_db import initialize
from service import CustomerService
from model_adapter import DeepSeekExtractor, ModelFailure, read_config


class StubOpener:
    def __init__(self, output=None, error=None, finish='stop'):
        self.output=output
        self.error=error
        self.finish=finish
        self.request=None

    def open(self,request,timeout):
        self.request=request
        if self.error:raise self.error
        return io.BytesIO(json.dumps({'choices':[{'finish_reason':self.finish,'message':{'content':json.dumps(self.output)}}]}).encode())


def payload(**kwargs):
    return {'intent':'order_query','query_type':'general','order_id':'','tracking_number':'','phone_number':'','product_name':'',**kwargs}


class ModelAdapterTests(CourierFixture):
    def model(self,output=None,error=None,finish='stop'):
        return DeepSeekExtractor({'DEEPSEEK_API_KEY':'test-only-key'},StubOpener(output,error,finish))

    def test_missing_key_does_not_call_network(self):
        model=DeepSeekExtractor({},StubOpener(error=AssertionError('Network must not run')))
        self.assertFalse(model.configured)
        self.assertEqual(model.status()['mode'],'local_rules')
        with self.assertRaisesRegex(ModelFailure,'not_configured'):model.extract('查询订单')

    def test_model_request_json_and_no_database_data(self):
        model=self.model(payload(order_id='SN-DEMO-0002',query_type='delivery_time'))
        output=model.extract('SN-DEMO-0002 啥时候能送到？')
        self.assertEqual(output['query_type'],'delivery_time')
        body=json.loads(model.opener.request.data)
        self.assertEqual(body['response_format'],{'type':'json_object'})
        self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertEqual(len(body['messages']),2)
        self.assertNotIn('user_id',json.dumps(body))
        self.assertNotIn('test-only-key',json.dumps(body))

    def test_ungrounded_number_fails(self):
        with self.assertRaisesRegex(ModelFailure,'ungrounded_parameter'):
            self.model(payload(order_id='SN-DEMO-0002')).extract('查询我的订单')

    def test_extra_fields_cannot_override_identity_or_facts(self):
        result=self.model(payload(order_id='SN-DEMO-0010',user_id='demo_user_b',shipping_status='delivered')).extract('SN-DEMO-0010')
        self.assertNotIn('user_id',result)
        self.assertNotIn('shipping_status',result)

    def test_empty_truncated_or_invalid_output_fails(self):
        for model in [self.model(None),self.model(payload(),finish='length'),self.model(payload(query_type='refund'))]:
            with self.assertRaises(ModelFailure):model.extract('查询')

    def test_http_errors_are_redacted(self):
        for status,reason in [(401,'authentication_failed'),(429,'rate_limited'),(500,'provider_error')]:
            error=urllib.error.HTTPError('https://test.invalid',status,'sensitive-token',{},None)
            with self.assertRaisesRegex(ModelFailure,'^'+reason+'$'):
                self.model(error=error).extract('查询')

    def test_insecure_url_disabled(self):
        self.assertFalse(DeepSeekExtractor({'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'http://provider.invalid'}).configured)

    def test_env_takes_precedence_and_is_not_executed(self):
        with patch('pathlib.Path.exists',return_value=True),patch('pathlib.Path.read_text',return_value='DEEPSEEK_MODEL="file-model"\nDEEPSEEK_API_KEY=$(never_execute)\nUNRELATED=ignored\n'):
            config=read_config(environ={'DEEPSEEK_MODEL':'env-model'})
        self.assertEqual(config['DEEPSEEK_MODEL'],'env-model')
        self.assertEqual(config['DEEPSEEK_API_KEY'],'$(never_execute)')
        self.assertNotIn('UNRELATED',config)

    def test_semantic_extraction_queries_actual_database(self):
        with test_database() as path:
            initialize(path)
            model=self.model(payload(order_id='SN-DEMO-0002',query_type='delivery_time'))
            service=CustomerService(path,model=model,courier=self.courier)
            result=service.chat('demo_user_a','SN-DEMO-0002 啥时候能送到？')
            self.assertEqual(result['engine'],'deepseek')
            self.assertIn('2026-10-08',result['reply'])
            model.opener=StubOpener(payload(order_id='SN-DEMO-0010',user_id='demo_user_b'))
            result=service.chat('demo_user_a','SN-DEMO-0010')
            self.assertEqual(result['result_type'],'not_found')

    def test_provider_timeout_falls_back_to_local_rules(self):
        with test_database() as path:
            initialize(path)
            result=CustomerService(path,model=self.model(error=TimeoutError()),courier=self.courier).chat('demo_user_a','SN-DEMO-0002 物流到哪了')
            self.assertEqual(result['engine'],'local_rules')
            self.assertEqual(result['model_status']['fallback_reason'],'connection_failed')
            self.assertIn('运输中',result['reply'])

    def test_semantic_after_sales_keeps_verified_facts_and_human_request(self):
        with test_database() as path:
            initialize(path)
            service=CustomerService(path,model=self.model(payload(order_id='SN-DEMO-0002',intent='after_sales')),courier=self.courier)
            result=service.chat('demo_user_a','SN-DEMO-0002 我不想要了')
            self.assertEqual(result['next_action'],'request_human')
            self.assertEqual(result['human_transfer']['status'],'not_connected')
            self.assertEqual(result['orders'][0]['shipping_status'],'shipped')
