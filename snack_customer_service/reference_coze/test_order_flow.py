import asyncio
import copy
import json
import sqlite3
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'database')]
from demo_db import initialize, snapshot, projection
from migrate_legacy import migrate
from build_workflow import build


@contextmanager
def workspace_database():
    root = (ROOT/'tests').resolve()
    path = root / ('test-db-' + uuid.uuid4().hex + '.sqlite')
    try:
        yield path
    finally:
        for suffix in ['', '-journal', '-wal', '-shm']:
            target = Path(str(path)+suffix).resolve()
            if target.parent != root:
                raise AssertionError('测试文件不在工作区')
            if target.exists():
                target.unlink()


def run_code(filename, params):
    scope = {}
    exec(compile((ROOT/'nodes'/filename).read_text(encoding='utf-8'), filename, 'exec'), scope)
    return asyncio.run(scope['main'](SimpleNamespace(params=params)))


def simulate(workflow, extracted, user='demo_user_a', question_answers=None, intent=0, text='查询订单', context='{}', failure=''):
    """运行导出配置中的真实连线与代码。只对模型和问答输出使用受控替身。"""
    nodes = {n['id']:n for n in workflow['nodes']}
    values = {}
    trace = []
    active = ['100001']
    question_answers = question_answers or {}
    while active:
        node_id = active.pop(0)
        if node_id in trace:
            raise AssertionError('不应重复执行节点')
        n = nodes[node_id]
        p = n['parameters']
        kind = n['type']
        inputs = {}
        for entry in p.get('node_inputs',[]):
            v = entry['input'].get('value')
            if isinstance(v, dict) and 'ref_node' in v:
                inputs[entry['name']] = values[v['ref_node']][v['path']]
            else:
                inputs[entry['name']] = v
        port = None
        if kind == 'start':
            output = {'USER_INPUT':text,'user_id':user,'CONTEXT_JSON':context,'DEMO_FAILURE_MODE':failure,'CONVERSATION_NAME':'test'}
        elif kind == 'intent':
            output = {'classificationId':intent}
            port = 'branch_' + str(intent)
        elif kind == 'llm':
            output = {key:extracted.get(key,'') for key in p['node_outputs']}
        elif kind == 'question':
            output = {key:'' for key in p['node_outputs']}
            output.update(question_answers.get(node_id,{}))
        elif kind == 'code':
            scope = {}
            exec(compile(p['code'],node_id,'exec'),scope)
            output = asyncio.run(scope['main'](SimpleNamespace(params=inputs)))
            if set(output) != set(p['node_outputs']):
                raise AssertionError(f'{node_id}代码与输出结构不一致')
        elif kind == 'condition':
            cond = p['branches'][0]['condition']['conditions'][0]
            left = cond['left']['input']['value']
            port = 'true' if values[left['ref_node']][left['path']] == cond['right']['input']['value'] else 'false'
            output = {}
        elif kind == 'variable_merge':
            output = {}
            for group in p['mergeGroups']:
                choices = [values.get(v['value']['ref_node'],{}).get(v['value']['path']) for v in group['variables']]
                output[group['name']] = next((value for value in choices if value is not None and value != ''),None)
        elif kind == 'output':
            output = {}
        elif kind == 'end':
            output = inputs
        else:
            raise AssertionError('尚未模拟节点类型：'+kind)
        values[node_id] = output
        trace.append(node_id)
        active += [e['target_node'] for e in workflow['edges'] if e['source_node']==node_id and (port is None or e.get('source_port')==port)]
    if '900001' not in values:
        raise AssertionError('执行未到达结束节点')
    return json.loads(values['900001']['result_json']),trace


class OrderFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow,_ = build()
        cls.data = snapshot()

    def result(self,extracted,**kwargs):
        return simulate(self.workflow,extracted,**kwargs)[0]

    def test_unshipped_source_status(self):
        r=self.result({'order_id':'SN-DEMO-0001'})
        self.assertIn('尚未发货',r['reply'])
        self.assertNotIn('通知',r['reply'])

    def test_commerce_order_resolves_tracking(self):
        r=self.result({'order_id':'SN-DEMO-0002'})
        self.assertEqual(r['orders'][0]['shipments'][0]['tracking_number'],'DEMO-TRACK-1002')

    def test_tracking_number_is_separate(self):
        r=self.result({'tracking_number':'DEMO-TRACK-1002'})
        self.assertEqual(r['orders'][0]['order_id'],'SN-DEMO-0002')

    def test_unknown_identifier_not_passed_as_tracking(self):
        r=self.result({'order_id':'DEMO-TRACK-1002'})
        self.assertEqual(r['result_type'],'not_found')

    def test_phone_multiple_orders(self):
        r=self.result({'phone_number':'13800000001'})
        self.assertEqual(r['result_type'],'multiple_orders')
        self.assertEqual(r['next_action'],'select_order')
        self.assertLessEqual(len(r['candidates']),5)

    def test_phone_order_selection_and_common_render(self):
        r,trace=simulate(self.workflow,{'phone_number':'13800000001'},question_answers={'201007':{'order_id':'SN-DEMO-0002','USER_RESPONSE':'SN-DEMO-0002'}})
        self.assertEqual(r['orders'][0]['order_id'],'SN-DEMO-0002')
        self.assertIn('201009',trace)
        self.assertIn('201011',trace)

    def test_followup_order_equals_direct(self):
        direct=self.result({'order_id':'SN-DEMO-0002','query_type':'delivery_time'})
        follow=self.result({'query_type':'delivery_time'},question_answers={'174327':{'order_id':'SN-DEMO-0002','USER_RESPONSE':'SN-DEMO-0002'}})
        self.assertEqual(direct['reply'],follow['reply'])
        self.assertEqual(direct['orders'],follow['orders'])

    def test_followup_phone_and_select(self):
        r=self.result({},question_answers={'174327':{'phone_number':'13800000001','USER_RESPONSE':'13800000001'},'201007':{'order_id':'SN-DEMO-0003'}})
        self.assertIn('已签收',r['reply'])

    def test_product_filter(self):
        r=self.result({'product_name':'辣条'})
        self.assertEqual(r['orders'][0]['order_id'],'SN-DEMO-0004')

    def test_product_alias(self):
        r=self.result({'product_name':'牛肉干'})
        self.assertEqual(r['result_type'],'multiple_orders')

    def test_cross_user_order_denied(self):
        r=self.result({'order_id':'SN-DEMO-0010'})
        self.assertEqual(r['result_type'],'not_found')

    def test_cross_user_tracking_denied(self):
        r=self.result({'tracking_number':'DEMO-TRACK-1010'})
        self.assertEqual(r['result_type'],'not_found')

    def test_cross_user_phone_denied(self):
        r=self.result({'phone_number':'13800000002'})
        self.assertEqual(r['result_type'],'not_found')

    def test_selection_cannot_change_owner(self):
        r=self.result({'phone_number':'13800000001'},question_answers={'201007':{'order_id':'SN-DEMO-0010'}})
        self.assertEqual(r['result_type'],'not_found')

    def test_context_order_revalidated(self):
        r=self.result({'query_type':'shipping'},context='{"selected_order_id":"SN-DEMO-0010"}')
        self.assertEqual(r['result_type'],'not_found')

    def test_context_followup(self):
        r=self.result({'query_type':'shipping'},context='{"selected_order_id":"SN-DEMO-0002"}',text='那它发货了吗')
        self.assertIn('已发货',r['reply'])

    def test_identity_cannot_be_model_overridden(self):
        r=run_code('normalize.py',{'extracted':{'order_id':'SN-DEMO-0010','user_id':'demo_user_b'},'user_id':'demo_user_a'})
        self.assertEqual(json.loads(r['params_json'])['user_id'],'demo_user_a')

    def test_missing_identity(self):
        r=self.result({'order_id':'SN-DEMO-0002'},user='',question_answers={'174327':{'order_id':'SN-DEMO-0002'}})
        self.assertEqual(r['result_type'],'auth_required')

    def test_invalid_phone(self):
        r=self.result({'phone_number':'12345678901'},question_answers={'174327':{'phone_number':'12345678901'}})
        self.assertEqual(r['result_type'],'need_more_info')

    def test_null_strings_normalized(self):
        r=run_code('normalize.py',{'extracted':{'order_id':'null','tracking_number':None,'phone_number':'None'},'user_id':'demo_user_a'})
        self.assertFalse(r['ready'])

    def test_database_error_not_order_not_found(self):
        for failure in ['database_timeout','database_error']:
            r=self.result({'order_id':'SN-DEMO-0002'},failure=failure)
            self.assertEqual(r['result_type'],'query_failed')
            self.assertNotIn('没有找到',r['reply'])

    def test_logistics_failure_preserves_shipping(self):
        r=self.result({'order_id':'SN-DEMO-0002'},failure='logistics_timeout')
        self.assertEqual(r['result_type'],'partial_success')
        self.assertIn('已发货',r['reply'])
        self.assertEqual(r['orders'][0]['shipments'][0]['tracking_events'],[])

    def test_shipping_query_does_not_require_logistics(self):
        r=self.result({'order_id':'SN-DEMO-0002','query_type':'shipping'},failure='logistics_error')
        self.assertEqual(r['result_type'],'success')
        self.assertNotIn('失败',r['reply'])

    def test_empty_tracking_not_unshipped(self):
        r=self.result({'order_id':'SN-DEMO-0004'})
        self.assertIn('已发货',r['reply'])
        self.assertIn('暂未获取',r['reply'])
        self.assertNotIn('尚未发货',r['reply'])

    def test_eta_only_from_data(self):
        r=self.result({'order_id':'SN-DEMO-0002','query_type':'delivery_time'})
        self.assertIn('2026-10-08 18:00',r['reply'])
        r=self.result({'order_id':'SN-DEMO-0004','query_type':'delivery_time'})
        self.assertIn('暂无预计送达时间',r['reply'])

    def test_empty_result_has_no_eta(self):
        r=self.result({'order_id':'SN-DEMO-0002','query_type':'delivery_time'},failure='logistics_empty')
        self.assertNotIn('2026-10-08',r['reply'])
        self.assertIsNone(r['orders'][0]['shipments'][0]['estimated_delivery_at'])

    def test_unknown_shipping_not_inferred(self):
        r=self.result({'order_id':'SN-DEMO-0007'})
        self.assertIn('暂未确认',r['reply'])
        self.assertEqual(r['next_action'],'request_human')

    def test_partial_shipping(self):
        r=self.result({'order_id':'SN-DEMO-0008'})
        self.assertIn('已部分发货',r['reply'])
        self.assertEqual(len(r['orders'][0]['shipments']),2)

    def test_cancelled_and_pending_payment(self):
        self.assertIn('已取消',self.result({'order_id':'SN-DEMO-0006'})['reply'])
        self.assertIn('待付款',self.result({'order_id':'SN-DEMO-0009'})['reply'])

    def test_exception_requests_human_without_false_confirmation(self):
        r=self.result({'order_id':'SN-DEMO-0005'})
        self.assertTrue(r['human_transfer']['requested'])
        self.assertEqual(r['human_transfer']['status'],'not_connected')
        self.assertNotIn('已转接',r['reply'])

    def test_dispute_also_requests_human(self):
        r=self.result({'order_id':'SN-DEMO-0003'},text='显示签收了但我没收到')
        self.assertEqual(r['next_action'],'request_human')

    def test_other_intent_uses_same_output_contract(self):
        r=self.result({},intent=1,text='我要退货')
        self.assertEqual(r['result_type'],'human_required')
        self.assertEqual(r['human_request']['user_message'],'我要退货')

    def test_query_failure_signal_from_database(self):
        read=run_code('read_records.py',{'records':[],'isSuccess':False})
        query=run_code('query.py',{'params_json':'{"user_id":"demo_user_a","order_id":"SN-DEMO-0002"}',**read})
        self.assertEqual(json.loads(query['query_result_json'])['result_type'],'query_failed')

    def test_truncated_source_not_false_not_found(self):
        query=run_code('query.py',{'params_json':'{"user_id":"demo_user_a","order_id":"UNKNOWN"}','data_json':json.dumps(projection(self.data)),'source_may_be_truncated':True})
        self.assertEqual(json.loads(query['query_result_json'])['result_type'],'narrow_search')

    def test_malformed_records_not_false_not_found(self):
        query=run_code('query.py',{'params_json':'{"user_id":"demo_user_a","order_id":"SN-DEMO-0002"}','data_json':'[null]'})
        self.assertEqual(json.loads(query['query_result_json'])['result_type'],'query_failed')

    def test_output_no_phone_or_owner(self):
        r=self.result({'order_id':'SN-DEMO-0002'})
        self.assertNotIn('user_id',r['orders'][0])
        self.assertNotIn('user_phonenumber',r['orders'][0])
        self.assertLessEqual(len(r['reply']),120)

    def test_export_graph_integrity(self):
        nodes={n['id']:n for n in self.workflow['nodes']}
        self.assertEqual(len(nodes),len(self.workflow['nodes']))
        for e in self.workflow['edges']:
            self.assertIn(e['source_node'],nodes)
            self.assertIn(e['target_node'],nodes)
        def check_refs(value):
            if isinstance(value,dict):
                if 'ref_node' in value:
                    self.assertIn(value['ref_node'],nodes)
                    self.assertIn(value['path'].split('.')[0],nodes[value['ref_node']]['parameters'].get('node_outputs',{}))
                for v in value.values():check_refs(v)
            elif isinstance(value,list):
                for v in value:check_refs(v)
        for node in nodes.values():check_refs(node['parameters'])
        self.assertFalse(any(n['type']=='plugin' for n in nodes.values()))


class DatabaseMigrationTests(unittest.TestCase):
    def test_schema_and_demo_integrity(self):
        with workspace_database() as path:
            initialize(path)
            con=sqlite3.connect(path)
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM orders').fetchone()[0],10)
            con.close()

    def test_legacy_tracking_preserved_and_no_shipping_guess(self):
        with workspace_database() as path:
            rows=[{'order_id':'TRACK-LEGACY-001','item_title':'牛肉干','user_phonenumber':'13800000001'}]
            before=copy.deepcopy(rows)
            self.assertEqual(migrate(rows,path),1)
            self.assertEqual(migrate(rows,path),0)
            self.assertEqual(rows,before)
            con=sqlite3.connect(path)
            order=con.execute('SELECT order_id,user_id,shipping_status,legacy_tracking_number,needs_review FROM orders').fetchone()
            self.assertTrue(order[0].startswith('LEGACY-DEMO-'))
            self.assertEqual(order[1:],('legacy_unassigned','unknown','TRACK-LEGACY-001',1))
            con.close()

    def test_empty_legacy_tracking_is_not_unshipped(self):
        with workspace_database() as path:
            migrate([{'order_id':'null','item_title':'薯片'}],path)
            con=sqlite3.connect(path)
            self.assertEqual(con.execute('SELECT shipping_status,legacy_tracking_number FROM orders').fetchone(),('unknown',None))
            con.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
