import io
import json
import urllib.error
import urllib.request

from test_service import CourierFixture, test_database
from database.demo_db import initialize, connect
from courier_simulator import connect as provider_connect
from courier_client import CourierClient
from model_adapter import DeepSeekExtractor
from service import CustomerService, ServiceError


class CourierTests(CourierFixture):
    def setUp(self):
        super().setUp()
        self.temp=test_database()
        self.path=self.temp.__enter__()
        initialize(self.path)
        self.service=CustomerService(self.path,model=DeepSeekExtractor({'MODEL_ENABLED':'false'}),courier=self.courier)

    def tearDown(self):self.temp.__exit__(None,None,None)

    def chat(self,text,**kwargs):return self.service.chat('demo_user_a',text,**kwargs)

    def test_actual_http_call_matches_both_databases(self):
        result=self.chat('DEMO-TRACK-1002 物流到哪了')
        call=result['courier_calls'][0]
        self.assertEqual(call['transport'],'http')
        self.assertEqual(call['http_status'],200)
        self.assertEqual(call['response']['request_id'],call['request_id'])
        con=connect(self.path)
        stored=con.execute('SELECT * FROM courier_api_calls WHERE request_id=?',(call['request_id'],)).fetchone()
        con.close()
        self.assertEqual(stored['result_status'],'success')
        con=provider_connect(self.courier_path)
        received=con.execute('SELECT * FROM provider_requests WHERE request_id=?',(call['request_id'],)).fetchone()
        con.close()
        self.assertEqual(received['tracking_number'],'DEMO-TRACK-1002')
        self.assertNotIn(self.courier.token,json.dumps(call))
        self.assertNotIn('Authorization',json.dumps(call))

    def test_order_database_cannot_supply_fake_logistics(self):
        con=connect(self.path)
        with con:con.execute("UPDATE shipments SET logistics_status='delivered',latest_status_desc='伪造的本地轨迹',estimated_delivery_at='2099-01-01' WHERE tracking_number='DEMO-TRACK-1002'")
        con.close()
        result=self.chat('SN-DEMO-0002物流到哪了')
        shipment=result['orders'][0]['shipments'][0]
        self.assertEqual(shipment['logistics_status'],'in_transit')
        self.assertEqual(shipment['data_source'],'mock_courier_http')
        self.assertNotIn('伪造',json.dumps(result,ensure_ascii=False))

    def test_no_http_call_when_not_needed(self):
        for question in ['SN-DEMO-0001','SN-DEMO-0006','SN-DEMO-0007','SN-DEMO-0009','SN-DEMO-0002发货了吗','SN-DEMO-0010']:
            self.assertEqual(self.chat(question)['courier_calls'],[])
        con=connect(self.path)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM courier_api_calls').fetchone()[0],0)
        con.close()

    def test_candidate_selection_defers_courier_call(self):
        first=self.chat('查牛肉干物流')
        self.assertEqual(first['courier_calls'],[])
        second=self.chat('选SN-DEMO-0002',conversation_id=first['conversation_id'],selected_order_id='SN-DEMO-0002')
        self.assertEqual(len(second['courier_calls']),1)

    def test_progression_is_requeried_through_http(self):
        first=self.chat('SN-DEMO-0002 物流到哪了')
        self.service.set_logistics_scenario('demo_user_a','DEMO-TRACK-1002','delivering')
        second=self.chat('物流更新了吗',conversation_id=first['conversation_id'])
        self.assertIn('正在派送',second['reply'])
        self.service.set_logistics_scenario('demo_user_a','DEMO-TRACK-1002','delivered')
        third=self.chat('物流更新了吗',conversation_id=first['conversation_id'])
        self.assertIn('已签收',third['reply'])
        self.assertIsNone(third['orders'][0]['shipments'][0]['estimated_delivery_at'])
        self.assertEqual(len({x['courier_calls'][0]['request_id'] for x in [first,second,third]}),3)

    def test_two_packages_produce_two_independent_calls(self):
        result=self.chat('SN-DEMO-0008物流到哪了')
        self.assertEqual(len(result['courier_calls']),2)
        self.assertEqual([c['status'] for c in result['courier_calls']],['success','no_records'])
        self.assertEqual(len(result['orders'][0]['shipments'][0]['tracking_events']),3)

    def test_actual_timeout_clears_eta_without_losing_shipping_status(self):
        result=self.chat('SN-DEMO-0002什么时候到',failure_mode='logistics_timeout')
        call=result['courier_calls'][0]
        self.assertEqual(call['status'],'timeout')
        self.assertIsNone(call['http_status'])
        self.assertGreaterEqual(call['elapsed_ms'],250)
        self.assertEqual(result['result_type'],'partial_success')
        self.assertEqual(result['orders'][0]['shipping_status'],'shipped')
        self.assertIsNone(result['orders'][0]['shipments'][0]['estimated_delivery_at'])

    def test_actual_http_503_and_invalid_json_handled(self):
        for failure,status,http in [('logistics_error','http_error',503),('logistics_malformed','invalid_response',200)]:
            result=self.chat('SN-DEMO-0002物流到哪了',failure_mode=failure)
            self.assertEqual(result['courier_calls'][0]['status'],status)
            self.assertEqual(result['courier_calls'][0]['http_status'],http)
            self.assertEqual(result['result_type'],'partial_success')
            self.assertEqual(result['orders'][0]['shipments'][0]['tracking_events'],[])

    def test_actual_empty_response_has_no_eta(self):
        result=self.chat('SN-DEMO-0002什么时候到',failure_mode='logistics_empty')
        self.assertEqual(result['courier_calls'][0]['status'],'no_records')
        self.assertEqual(result['courier_calls'][0]['http_status'],200)
        self.assertIn('暂无预计',result['reply'])

    def test_stalled_uses_explicit_simulation_time(self):
        self.service.set_logistics_scenario('demo_user_a','DEMO-TRACK-1002','stalled')
        result=self.chat('SN-DEMO-0002物流到哪了')
        self.assertEqual(result['next_action'],'request_human')
        self.assertEqual(result['reason'],'tracking_stalled')
        self.assertIn('48小时',result['reply'])
        self.assertEqual(result['orders'][0]['shipments'][0]['simulation_time'],'2026-10-09T12:00:00+08:00')

    def test_scenario_change_is_scoped_to_owner(self):
        with self.assertRaises(ServiceError):
            self.service.set_logistics_scenario('demo_user_a','DEMO-TRACK-1010','delivered')
        parcel,_=self.courier.lookup('DEMO-TRACK-1010')
        self.assertEqual(parcel['status'],'in_transit')

    def test_provider_requires_internal_token(self):
        client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req=urllib.request.Request(self.courier.base_url+'/v1/traces/query',data=b'{}',headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as caught:client.open(req,timeout=3)
        self.assertEqual(caught.exception.code,401)

    def test_partial_package_failure_preserves_other_package(self):
        con=connect(self.path)
        with con:con.execute("UPDATE shipments SET tracking_number='DEMO-TRACK-MISSING' WHERE shipment_id='SHIP-8B'")
        con.close()
        result=self.chat('SN-DEMO-0008物流到哪了')
        self.assertEqual(result['result_type'],'partial_success')
        self.assertEqual([c['status'] for c in result['courier_calls']],['success','not_found'])
        self.assertTrue(result['orders'][0]['shipments'][0]['tracking_events'])
        self.assertEqual(result['orders'][0]['shipments'][1]['tracking_events'],[])

    def test_corrupted_response_cannot_replace_another_tracking_number(self):
        class BadResponse:
            def open(self,request,timeout):
                request_id=request.headers['X-request-id']
                output={'code':'ok','request_id':request_id,'data':{'tracking_number':'WRONG','status':'delivered','is_simulated':True}}
                class Response(io.BytesIO):status=200
                return Response(json.dumps(output).encode())
        parcel,trace=CourierClient(opener=BadResponse()).lookup('DEMO-TRACK-1002')
        self.assertIsNone(parcel)
        self.assertEqual(trace['status'],'invalid_response')

    def test_events_after_simulation_clock_are_rejected(self):
        parcel,trace=self.courier.lookup('DEMO-TRACK-1002')
        output=trace['response']
        output['data']['events'][0]['time']='2099-01-01T00:00:00+08:00'
        with self.assertRaises(ValueError):CourierClient.validate(output,'DEMO-TRACK-1002',trace['request_id'])

    def test_carrier_code_is_taken_from_order_shipment(self):
        con=connect(self.path)
        with con:con.execute("UPDATE shipments SET carrier_code='wrong_carrier' WHERE shipment_id='SHIP-2'")
        con.close()
        result=self.chat('SN-DEMO-0002物流到哪了')
        self.assertEqual(result['courier_calls'][0]['request']['carrier_code'],'wrong_carrier')
        self.assertEqual(result['courier_calls'][0]['http_status'],404)
        self.assertEqual(result['result_type'],'partial_success')
