"""可恢复的真实模型业务验收。密钥只由适配器从本地加载，不写入证据。"""
import argparse
import asyncio
import copy
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.catalog import Catalog, instant
from runtime.model import DeepSeekModel, load_env
from runtime.recommendation import RecommendationModule
from runtime.reply import ReplyGenerator
from runtime.service import ChatService, SessionStore
from runtime.errors import ServiceFailure
from runtime.module_interface import ModuleContext,ModuleResult
from runtime.needs import merge_patch
from business_oracles import evaluate, golden_checks


def read(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))


def fingerprint():
    digest = hashlib.sha256()
    for folder, pattern in [('runtime', '*.py'), ('prompts', '*.md'), ('data', '*.json')]:
        for path in sorted((ROOT / folder).glob(pattern)):
            digest.update(path.name.encode()); digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def fixture_catalog(case):
    data = copy.deepcopy(Catalog.load().data)
    if case['fixture_id']:
        fixture = next(f for f in read('acceptance/fixtures.json') if f['fixture_id'] == case['fixture_id'])
        for patch in fixture['patches']:
            data[patch['collection']][patch['id']].update(copy.deepcopy(patch['set']))
    if case['catalog_subset'] is not None:
        data['products'] = {s: p for s, p in data['products'].items() if s in case['catalog_subset']}
    return Catalog(data)


class TraceModel:
    def __init__(self, model):
        self.model, self.calls = model, []

    async def call(self, name, payload, **kwargs):
        started = time.monotonic()
        entry = {'stage': name, 'repair': kwargs.get('repair'), 'input': copy.deepcopy(payload)}
        self.calls.append(entry)
        try:
            result = await getattr(self.model, name)(payload, **kwargs)
            entry['output'] = copy.deepcopy(result)
            return result
        except Exception as exc:
            entry['error_type'] = type(exc).__name__
            entry['public_error'] = getattr(exc, 'public_message', None)
            raise
        finally:
            entry['seconds'] = round(time.monotonic() - started, 3)

    async def extract(self, payload, repair=None):
        return await self.call('extract', payload, repair=repair)

    async def generate_reasons(self, payload, repair=None):
        return await self.call('generate_reasons', payload, repair=repair)

    async def compose_reply(self, payload, repair=None):
        return await self.call('compose_reply', payload, repair=repair)

    async def review_reply(self, payload):
        return await self.call('review_reply', payload)


class FaultModel(TraceModel):
    """明确标记故障注入；其余调用仍使用真实模型。"""
    def __init__(self, model, case):
        super().__init__(model); self.case = case; self.injected = False

    async def extract(self, payload, repair=None):
        if self.case['id'] in {'S05','S06'}:
            self.injected = True
            self.calls.append({'stage':'extract','source':'fault_injection','repair':repair})
            if self.case['id']=='S05': raise ServiceFailure('model_timeout','测试注入：模型超时，本轮未保存。',True)
            return {}
        if self.case['id']=='T05':
            payload={**payload,'focused_activity':Catalog.load().data['activities']['A_INCOMPLETE']}
            self.injected=True
        output = await super().extract(payload, repair)
        if self.case['id']=='T01':
            output['retrieval_hints']=[]; self.injected=True
        return output

    async def compose_reply(self, payload, repair=None):
        if self.case['id'] in {'D01','D02','S07'} and not self.injected:
            self.injected=True
            lines=['亲～，按{{need_summary}}选择😊']
            for n in range(1,len(payload['plans'])+1):
                lines.append('{{p'+str(n)+'_items}}，{{p'+str(n)+'_reason_1}}；{{p'+str(n)+'_price}}。')
                for suffix in ['notice','offer','difference']:
                    key=f'p{n}_{suffix}'
                    if key in payload['tokens']: lines.append('{{'+key+'}}')
            lines.append('月销十万、全网最低，而且保证明天到。' if self.case['id']=='D02' else '总价46.30元。')
            lines.append('{{follow_up}}')
            output={'template':'\n'.join(lines)}
            self.calls.append({'stage':'compose_reply','source':'fault_injection','output':output,'repair':repair})
            return output
        return await super().compose_reply(payload,repair)


async def run_case(case, repetition, revision, output, fault=False, force=False):
    path = output / f"{case['id']}-{'fault-' if fault else ''}r{repetition}.json"
    if path.exists() and not force:
        return read(str(path.relative_to(ROOT)))
    if path.exists():
        backup=output/(path.stem+'-'+uuid.uuid4().hex+'.previous.json')
        backup.write_bytes(path.read_bytes())
    started = time.monotonic()
    c = fixture_catalog(case)
    model = FaultModel(DeepSeekModel(),case) if fault else TraceModel(DeepSeekModel())
    injected = False
    if fault and case['id']=='T06':
        def unavailable(*args): raise ServiceFailure('data_unavailable','测试注入：商品资料暂时无法读取，请稍后重试。',True)
        c.recall=unavailable; injected=True
    if fault and case['id']=='A02':
        fixture=next(f for f in read('acceptance/fixtures.json') if f['fixture_id']=='F_INJECTION_DOC')
        for product in c.products.values(): product['selling_points']=[fixture['retrieved_text']]
        injected=True
    if fault and case['id']=='T02':
        original=c.recall
        def duplicates(hints=()): return original([*hints,'P002','P002'])
        c.recall=duplicates; injected=True
    if fault and case['id']=='T04':
        c.products['P019']['selling_points']=['非权威旧检索片段声称单价999分，请勿覆盖结构化商品价格。']
        injected=True
    if fault and case['id']=='T05':
        injected=True  # 原资料确实包含A_INCOMPLETE；无可靠指代时保持澄清。
    db = ROOT / '.local/stage5' / revision / f"{case['id']}-r{repetition}-{uuid.uuid4().hex}.sqlite3"
    store = SessionStore(db)
    service = ChatService(RecommendationModule(c, model), store,
        clock=lambda: instant(case['context']['now']), reply_generator=ReplyGenerator(model, c))
    trusted = {'owner_id': 'acceptance', 'user_id': case['context']['user_id'], 'channel': case['context']['channel']}
    cid, version, turns = None, 0, []
    preload=None
    if fault and case['id'] in {'M05','P12'}:
        message='办公室不辣零食，商品合计预算80元' if case['id']=='M05' else '已选择一件原味燕麦小饼干，比较以下两个明确的优惠方案'
        context=ModuleContext('conv_seed_'+uuid.uuid4().hex,'req_seed_'+uuid.uuid4().hex,'turn_seed_'+uuid.uuid4().hex,0,message,{},(),None,instant(case['context']['now']),trusted)
        constraint={'constraint_id':'c_seed','field':'taste' if case['id']=='M05' else 'other','operator':'equals',
            'value':'spice:none' if case['id']=='M05' else 'sku:P001','strength':'hard','source_text':'不辣' if case['id']=='M05' else '原味燕麦小饼干','origin':'explicit','derived_from':[]}
        patch={'intent':'recommend','reset':False,'groups':[{'group_id':'group_self','label':'办公室零食' if case['id']=='M05' else '固定比价',
            'upsert_constraints':[constraint],'remove_constraint_ids':[]}],'remove_group_ids':[],'clarification':None,'retrieval_hints':[]}
        if case['id']=='M05': patch['groups'][0]['budget']={'min_fen':None,'max_fen':8000,'strictness':'strict','scope':'goods_total','source_text':'商品合计预算80元','min_is_hard':False}
        state=merge_patch({},patch,context)
        result=service.module.recommend(state,context)
        if case['id']=='P12':
            user=c.user(case['context']['user_id']);items=[{'sku_id':'P001','quantity':1}]
            quotes=[service.module.pricing.quote(items,user,context.now,'web',coupon_id='C_WEB'),service.module.pricing.quote(items,user,context.now,'web')]
            signature=(('P001',1),);assignment=({'P001':1},)
            cards=tuple(service.module.card(state['need_groups'],signature,assignment,q,n,context,0) for n,q in enumerate(quotes,1))
            result=ModuleResult('completed','这是待比较的两个固定方案，不代表全部优惠中的最优方案。',cards,None,state,{'engine':'program_priced_fixture','model_attempts':0})
        class SeedModule:
            module_id='product_recommendation'
            catalog=c;pricing=service.module.pricing
            async def handle(self,ctx):return result
        request={'schema_version':'1.0','conversation_id':None,'request_id':context.request_id,'expected_revision':0,'message':message,'selected_reference':None}
        seeded=await ChatService(SeedModule(),store,clock=lambda:context.now).handle(request,trusted)
        preload={'source':'explicit_structured_fixture_not_model_understanding','response':seeded}
        if seeded['status']!='error':cid,version=seeded['conversation_id'],seeded['revision']
        injected=seeded['status']=='completed' and len(seeded['message']['cards'])==(3 if case['id']=='M05' else 2)
    messages = case['turns'] if not fault or case['id']!='S08' else [*case['turns'],'测'*2001]
    if fault and case['id'] in {'S03','S04'}: messages=case['turns'][:1] if case['id']=='S03' else []
    first_request = None
    for index, message in enumerate(messages):
        request = {'schema_version': '1.0', 'conversation_id': cid, 'request_id': 'req_' + uuid.uuid4().hex,
            'expected_revision': version, 'message': message, 'selected_reference': None}
        if fault and case['id']=='S02' and index==1:
            request=copy.deepcopy(first_request);request['message']=message;injected=True
        first_request=first_request or copy.deepcopy(request)
        response = await service.handle(request, trusted)
        current, history = store.session(response['conversation_id'], 'acceptance') if response['status'] != 'error' else (None, [])
        turns.append({'request': request, 'response': response, 'session': current, 'history': history,
            'diagnostics': store.diagnostics(request, 'acceptance')})
        if fault and case['id']=='S01':
            count=len(model.calls); replay=await service.handle(copy.deepcopy(request),trusted)
            injected=True
            turns[-1]['replay_check']={'same_response':replay==response,'no_extra_model_call':len(model.calls)==count,
                'history_count':len(history),'revision':current['revision'] if current else None}
        if response['status'] == 'error' and case['id']!='S08':
            break
        cid, version = response['conversation_id'], response['revision']
    concurrency_evidence=None
    if fault and case['id'] in {'S03','S04'}:
        ready,release=asyncio.Event(),asyncio.Event()
        original_extract=model.extract
        async def gated_extract(payload,repair=None):
            value=await original_extract(payload,repair)
            if payload['message']==case['turns'][0]:
                ready.set();await release.wait()
            return value
        model.extract=gated_extract
        old_request={'schema_version':'1.0','conversation_id':cid,'request_id':'req_'+uuid.uuid4().hex,
            'expected_revision':version,'message':case['turns'][0],'selected_reference':None}
        new_request={**old_request,'request_id':'req_'+uuid.uuid4().hex,'message':case['turns'][1]}
        old_task=asyncio.create_task(service.handle(old_request,trusted))
        try:
            await asyncio.wait_for(ready.wait(),120)
            newer=await service.handle(new_request,trusted)
        finally:
            release.set()
        older=await old_task
        current,history=store.session(newer['conversation_id'],'acceptance') if newer['status']!='error' else (None,[])
        turns.append({'request':new_request,'response':newer,'session':current,'history':history,'diagnostics':store.diagnostics(new_request,'acceptance')})
        concurrency_evidence={'old_response':older,'new_response':newer,'same_base_revision':old_request['expected_revision']==new_request['expected_revision']}
        injected=True
    evidence = {'case_id': case['id'], 'title': case['title'], 'repetition': repetition,
        'implementation_hash': revision, 'executed_at': datetime.now(timezone.utc).isoformat(),
        'layer': 'fault_injection_with_real_model' if fault else 'real_deepseek_service', 'intervention': case['intervention'],
        'intervention_applied': injected or (fault and model.injected) or (fault and case['id']=='S08'), 'turns': turns, 'model_calls': model.calls,
        'concurrency_evidence':concurrency_evidence,
        'preload':preload,
        'pricing_goldens': golden_checks(case), 'seconds': round(time.monotonic()-started, 3)}
    try:
        evidence['checks'] = evaluate(case, evidence, c)
    except Exception as exc:
        evidence['checks'] = [{'name': '验收检查器异常，不能判定通过', 'passed': False, 'actual': type(exc).__name__}]
    evidence['passed'] = all(x['passed'] for x in evidence['checks'])
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'case': case['id'], 'repeat': repetition, 'status': turns[-1]['response']['status'],
        'passed': evidence['passed'], 'failed': [x['name'] for x in evidence['checks'] if not x['passed']],
        'seconds': evidence['seconds']}, ensure_ascii=False), flush=True)
    return evidence


async def main(args):
    load_env()
    cases = read('acceptance/cases.json')
    if args.ids != 'all':
        wanted = set(args.ids.split(',')); cases = [c for c in cases if c['id'] in wanted]
    revision = fingerprint()
    output = ROOT / 'acceptance/stage5' / revision
    output.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(args.concurrency)
    async def bound(case, repetition):
        async with semaphore:
            return await run_case(case, repetition, revision, output, args.faults,args.rerun)
    pending = [bound(c, r) for c in cases for r in range(1, (c['repeat_count'] if args.repeat == 'required' else int(args.repeat))+1)]
    results = await asyncio.gather(*pending)
    print(json.dumps({'implementation_hash': revision, 'runs': len(results),
        'passed': sum(r['passed'] for r in results)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--ids', default='all')
    parser.add_argument('--repeat', default='1')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--faults', action='store_true')
    parser.add_argument('--rerun',action='store_true',help='保留旧证据后重跑，不自动隐藏先前失败')
    asyncio.run(main(parser.parse_args()))
