"""检索话术 → 根据已核验事实生成 → 规则校验 → 独立模型质检。"""
import asyncio
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from model_adapter import ModelFailure

TOKEN = re.compile(r'\{\{([a-z0-9_]+)\}\}')
ASSERTION = re.compile(r'(?:已经|已|尚未|还没|未)(?:发货|签收|退款|催单|通知|联系|安排|转接|接入)|运输中|派送中|配送异常|待付款|已取消')
FORBIDDEN = ('保证','一定','明天','今天','后天','丢件','已帮','已经帮','帮你催','替你催','为你催',
    '帮你通知','马上安排','安排补发','退款成功','已转','已接通','已通知','立刻发','立即发','马上就到',
    '我会帮你联系','我帮你联系','已经联系','已为你','已给你','<script','```')


def scene_for(result, message):
    kind = result['result_type']
    if kind in ('need_more_info','multiple_orders','not_found','narrow_search','query_failed'):
        return kind
    if kind == 'human_required' or any(word in message for word in ('退款','退货','换货','赔偿','投诉','人工','没收到','未收到')):
        return 'after_sales'
    order = (result.get('orders') or [{}])[0]
    if order.get('order_status') in ('pending_payment','cancelled'):
        return order['order_status']
    shipping = order.get('shipping_status')
    if shipping == 'unshipped':return 'unshipped'
    if shipping == 'unknown':return 'unknown_shipping'
    parcels = order.get('shipments') or []
    if any(p.get('logistics_status')=='query_failed' for p in parcels):return 'logistics_failed'
    if shipping == 'partially_shipped':return 'partially_shipped'
    if result.get('query_type')=='shipping':return 'shipped'
    if any(p.get('logistics_status')=='exception' or p.get('scenario')=='stalled' for p in parcels):return 'exception'
    states = {p.get('logistics_status') for p in parcels}
    if states == {'delivered'}:return 'delivered'
    if 'delivering' in states:return 'delivering'
    if not parcels or states <= {'unknown','no_records'}:return 'no_records'
    return 'in_transit'


def fact_tokens(result):
    tokens = {}
    orders = result.get('orders') or []
    kind = result['result_type']
    if kind == 'multiple_orders':
        tokens['lookup'] = '匹配到多笔订单，请选择需要查询的商城订单号'
        tokens['candidates'] = '\n'.join(o['order_id']+'：'+'、'.join(i['item_title'] for i in o.get('items',[])) for o in result.get('candidates',[]))
    elif kind == 'need_more_info':
        tokens['lookup'] = '请补充有效的商城订单号、快递运单号或下单手机号'
    elif kind == 'not_found':
        tokens['lookup'] = '没有找到与你的账号及查询条件匹配的订单'
    elif kind == 'narrow_search':
        tokens['lookup'] = '匹配范围较大，请提供完整订单号或更具体的商品名称'
    elif kind == 'query_failed':
        tokens['lookup'] = '本次查询未成功，请稍后重试'
    elif not orders:
        tokens['lookup'] = '这个问题需要人工客服进一步核实'
    else:
        order = orders[0]
        tokens['order_ref'] = '订单 '+str(order['order_id'])
        order_state = order.get('order_status')
        shipping = order.get('shipping_status')
        tokens['order_state'] = '已取消' if order_state=='cancelled' else '待付款' if order_state=='pending_payment' else {
            'unshipped':'尚未发货','shipped':'已发货','partially_shipped':'已部分发货','unknown':'发货状态暂未确认'}.get(shipping,'订单状态暂未确认')
        if result.get('query_type')!='shipping' and shipping in ('shipped','partially_shipped') and order_state not in ('cancelled','pending_payment'):
            parcels = order.get('shipments') or []
            if not parcels:
                tokens['parcel_missing'] = '暂未取得运单信息，需要人工核实'
            for n, parcel in enumerate(parcels,1):
                state = parcel.get('logistics_status')
                if state=='query_failed':
                    text = '本次物流查询未成功，暂时无法确认最新物流状态'
                elif state=='delivered':
                    text = '包裹已签收，以快递记录为准'
                elif result.get('query_type')=='delivery_time':
                    eta=parcel.get('estimated_delivery_at')
                    text = ('包裹预计'+str(eta)+'送达，以后续物流更新为准') if eta and parcel.get('eta_source') else '暂无预计送达时间'
                elif state=='no_records':
                    text = '暂未获取到物流轨迹'
                else:
                    text = str(parcel.get('latest_status_desc') or '物流状态暂未确认')
                if len(parcels)>1:
                    text = '包裹 '+str(n)+'（'+str(parcel.get('tracking_number') or '运单待确认')+'）：'+text
                tokens['parcel_'+str(n)] = text
                if result.get('query_type')!='delivery_time' and state not in ('delivered','query_failed','no_records'):
                    eta=parcel.get('estimated_delivery_at')
                    if eta and parcel.get('eta_source'):
                        tokens['eta_'+str(n)] = '包裹'+(' '+str(n) if len(parcels)>1 else '')+'预计'+str(eta)+'送达，以后续物流更新为准'
    if result.get('next_action')=='request_human':
        tokens['human_boundary'] = '需要人工客服进一步核实；你可以点击“联系人工”查看或发起模拟转接'
    return tokens


def redact(text):
    return re.sub(r'(?<!\d)1\d{10}(?!\d)','[手机号已隐藏]',str(text))[:1000]


class OrderReplyGenerator:
    def __init__(self, knowledge, model=None, timeout_seconds=35):
        self.knowledge,self.model,self.timeout_seconds = knowledge,model,timeout_seconds

    def packet(self, result, message, history):
        scene=scene_for(result,message)
        try:
            retrieved=self.knowledge.retrieve(scene,message)
        except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):
            retrieved=[]
        tokens=fact_tokens(result)
        return {'scene':scene,'user_message':redact(message),'query_type':result.get('query_type','general'),
            'history':[{'role':h['role'],'content':redact(h['content'])} for h in history[-6:]],
            'knowledge':[{'id':r['entry_id'],'guide':r['guide'],'opening_example':r['opening'],'closing_example':r['closing']} for r in retrieved],
            'tokens':tokens,'required_tokens':list(tokens),'actions_executed':[],
            'is_demo':True}, retrieved

    def render(self, output, packet):
        if not isinstance(output,dict) or set(output)!={'template'} or not isinstance(output['template'],str):
            raise ModelFailure('invalid_template')
        template=output['template']
        keys=TOKEN.findall(template)
        required=packet['required_tokens']
        if set(keys)!=set(required) or any(keys.count(k)!=1 for k in required):
            raise ModelFailure('missing_or_unknown_facts')
        remainder=TOKEN.sub('',template)
        if '{' in remainder or '}' in remainder or re.search(r'\d',remainder):
            raise ModelFailure('ungrounded_numbers')
        if ASSERTION.search(remainder) or any(term in remainder for term in FORBIDDEN):
            raise ModelFailure('ungrounded_claim')
        has_eta=any('预计' in value and '送达' in value for value in packet['tokens'].values())
        if has_eta and re.search(r'(?:没有|暂无|还没有|未提供|未显示|没显示|无法获取).{0,10}(?:预计|送达|到达|准确).{0,6}时间',remainder):
            raise ModelFailure('contradicted_eta')
        if len(template)>1200 or not template.strip().startswith('亲～') or template.count('😊')!=1:
            raise ModelFailure('invalid_style')
        if re.search(r'[\U0001F300-\U0001FAFF\u2600-\u27BF]',template.replace('😊','')):
            raise ModelFailure('invalid_style')
        # 候选订单没有选定时，不允许生成“这笔已…”等未经提供的断言。
        rendered=TOKEN.sub(lambda match:packet['tokens'][match.group(1)],template).strip()
        if len(rendered)>max(360,sum(len(v) for v in packet['tokens'].values())+180):
            raise ModelFailure('reply_too_long')
        return rendered

    def fallback(self, packet, retrieved):
        opening=retrieved[0]['opening'] if retrieved else '亲～，我可以继续帮你核对😊'
        closing=retrieved[0]['closing'] if retrieved else '你可以核对信息后再试一次。'
        tokens=packet['tokens']
        parts=[]
        if 'order_ref' in tokens:
            parts.append('{{order_ref}}目前{{order_state}}。')
        for key in tokens:
            if key not in ('order_ref','order_state'):
                parts.append('{{'+key+'}}。')
        return opening+'\n'+''.join(parts)+'\n'+closing

    async def generate(self, result, message, history=()):
        started=time.monotonic()
        packet,retrieved=self.packet(result,message,history)
        try:
            fallback=self.render({'template':self.fallback(packet,retrieved)},packet)
        except ModelFailure:
            retrieved=[]
            fallback=self.render({'template':self.fallback(packet,[])},packet)
        trace={'run_id':'RR-'+uuid.uuid4().hex,'scene':packet['scene'],
            'retrieval':[{'id':r['entry_id'],'version':r['version'],'source':r['source'],'score':r['score'],
                'matched_keywords':r['matched_keywords'],'guide':r['guide']} for r in retrieved],
            'mode':'knowledge_fallback','attempts':0,'review_passed':False,'fallback_reason':'not_configured' if retrieved else 'knowledge_unavailable',
            'created_at':datetime.now(timezone.utc).isoformat()}
        reply=fallback
        if self.model and self.model.configured:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    repair=None
                    for attempt in range(2):
                        trace['attempts']=attempt+1
                        try:
                            draft=await asyncio.to_thread(self.model.call,'compose',packet,repair)
                            content=self.render(draft,packet)
                            review=await asyncio.to_thread(self.model.call,'review',{**packet,'reply':content})
                            if (not isinstance(review,dict) or set(review)!={'pass','issues','rewrite_instruction'} or
                                    type(review['pass']) is not bool or not isinstance(review['issues'],list) or
                                    not all(isinstance(x,str) for x in review['issues']) or not isinstance(review['rewrite_instruction'],str)):
                                raise ModelFailure('invalid_review')
                            if not review['pass'] or review['issues'] or review['rewrite_instruction']:
                                repair='质检问题：'+'；'.join(review['issues'])+'；'+review['rewrite_instruction']
                                raise ModelFailure('review_rejected')
                            reply=content
                            trace.update(mode='deepseek_grounded_reply',review_passed=True,fallback_reason='')
                            break
                        except ModelFailure as exc:
                            trace['fallback_reason']=str(exc)
                            if str(exc) in ('connection_failed','authentication_failed','rate_limited','provider_error','not_configured'):
                                break
                            repair=repair or '规则校验未通过：'+str(exc)+'。所有必需占位符必须且只能引用一次，不补写状态、数字或动作。'
            except TimeoutError:
                trace['fallback_reason']='generation_timeout'
        trace['elapsed_ms']=round((time.monotonic()-started)*1000)
        return reply,trace
