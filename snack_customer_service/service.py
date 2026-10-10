"""独立订单客服。无需 Coze、在线模型或第三方 Python 包。"""
import asyncio
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from database.demo_db import connect, snapshot, DB_PATH
from nodes.normalize import main as normalize
from nodes.query import main as query
from nodes.render import main as render
from nodes.human import main as human
from model_adapter import DeepSeekExtractor, ModelFailure
from courier_client import CourierClient
from courier_simulator import SCENARIOS
from human_client import FAULTS as HUMAN_FAULTS, STATES as HUMAN_STATES
from order_reply.generator import OrderReplyGenerator
from order_reply.knowledge import KnowledgeRetriever


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status=status


def extract_parameters(message):
    text=message.strip()
    upper=text.upper()
    output={k:'' for k in ['order_id','tracking_number','phone_number','product_name']}
    commerce=re.search(r'(?<![A-Z0-9-])SN-DEMO-\d{4}(?![A-Z0-9-])',upper)
    tracking=re.search(r'(?<![A-Z0-9-])DEMO-TRACK-[A-Z0-9]+(?![A-Z0-9-])',upper)
    if not commerce:
        commerce=re.search(r'(?:商城订单号|订单号|订单)\s*[：:#]?\s*((?:SN|ORDER|DD)[A-Z0-9-]+)',upper)
        if commerce:output['order_id']=commerce.group(1)
    else:output['order_id']=commerce.group(0)
    if tracking:output['tracking_number']=tracking.group(0)
    else:
        tracking=re.search(r'(?:快递运单号|快递单号|运单号|物流单号)\s*[：:#]?\s*([A-Z0-9-]+)',upper)
        if tracking:output['tracking_number']=tracking.group(1)
    phone=re.search(r'(?<!\d)(1\d{10})(?!\d)',text)
    if phone and phone.group(1)!=output['tracking_number']:output['phone_number']=phone.group(1)
    for product in ['牛肉干','牛肉粒','薯片','坚果','腰果','核桃','辣条','饼干','曲奇','零食组合装']:
        if product in text:
            output['product_name']='饼干' if product=='曲奇' else product
            break
    if any(term in text for term in ['什么时候到','何时到','几号到','多久能到','预计送达','到货时间','什么时候送到']):
        output['query_type']='delivery_time'
    elif any(term in text for term in ['发货','发了没','发出','发了吗']):
        output['query_type']='shipping'
    elif any(term in text for term in ['物流','到哪','签收','包裹','快递','收到了没']):
        output['query_type']='logistics'
    else:output['query_type']='general'
    return output


class CustomerService:
    def __init__(self, db_path=DB_PATH, model=None, courier=None, human_client=None, reply_generator=None):
        self.db_path=db_path
        self.lock=threading.RLock()
        self.model=model if model is not None else DeepSeekExtractor()
        self.courier=courier if courier is not None else CourierClient()
        self.human_client=human_client
        self.reply_generator=reply_generator or OrderReplyGenerator(KnowledgeRetriever(db_path))

    async def fetch_logistics(self,queried,conversation_id,user_id,con):
        """只有订单归属已校验且唯一匹配之后，才能使用订单中的运单调用快递接口。"""
        result=json.loads(queried['query_result_json'])
        result['courier_calls']=[]
        if result['result_type']!='success' or result['query_type']=='shipping':
            return result
        for order in result.get('orders',[]):
            if order['order_status'] in ('pending_payment','cancelled') or order['shipping_status'] not in ('shipped','partially_shipped'):
                continue
            for shipment in order.get('shipments',[]):
                tracking=shipment.get('tracking_number')
                if not tracking:continue
                parcel,trace=await asyncio.to_thread(self.courier.lookup,tracking,shipment.get('carrier_code','mock_express'),result.get('failure_mode',''))
                trace['order_id']=order['order_id']
                trace['shipment_id']=shipment['shipment_id']
                result['courier_calls'].append(trace)
                with con:
                    con.execute('INSERT INTO courier_api_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                (trace['request_id'],conversation_id,user_id,order['order_id'],shipment['shipment_id'],
                                 trace['endpoint'],json.dumps(trace['request'],ensure_ascii=False),
                                 json.dumps(trace['response'],ensure_ascii=False) if trace['response'] is not None else None,
                                 trace['status'],trace['http_status'],trace['elapsed_ms'],trace['called_at']))
                if parcel is None:
                    shipment.update(logistics_status='query_failed',latest_status_desc='',tracking_events=[],
                                    estimated_delivery_at=None,eta_source=None,data_source='mock_courier_http',
                                    lookup_error=trace['status'])
                else:
                    shipment.update(carrier_name=parcel['carrier_name'],logistics_status=parcel['status'],
                                    latest_status_desc=parcel['latest_description'],estimated_delivery_at=parcel.get('estimated_delivery_at'),
                                    eta_source=parcel.get('eta_source'),data_source='mock_courier_http',
                                    updated_at=trace['called_at'],simulation_time=parcel['simulation_time'],
                                    scenario=parcel['scenario'],lookup_error='',
                                    tracking_events=[{'status_time':e['time'],'status_desc':e['description'],'location':e['location']} for e in parcel['events']])
        # 物流故障已经由真实 HTTP 调用产生，回复按每个包裹的接口结果处理。
        result['failure_mode']=''
        return result

    def set_logistics_scenario(self,user_id,tracking,scenario):
        if not isinstance(tracking,str) or scenario not in SCENARIOS:
            raise ServiceError('请选择有效的运单和模拟物流状态。')
        con=connect(self.db_path)
        try:
            owned=con.execute('SELECT 1 FROM shipments s JOIN orders o ON s.order_id=o.order_id WHERE s.tracking_number=? AND o.user_id=?',(tracking,user_id)).fetchone()
        finally:con.close()
        if not owned:raise ServiceError('运单不存在或不属于当前账号。',404)
        parcel,trace=self.courier.set_scenario(tracking,scenario)
        if parcel is None:raise ServiceError('模拟快递服务更新未成功，请重试。',503)
        return {'tracking_number':tracking,'scenario':parcel['scenario'],'is_simulated':True,
                'message':'模拟物流状态已更新，再次查询将通过快递接口读取新轨迹。'}

    async def handle(self, user_id, message, conversation_id=None, selected_order_id=None, failure_mode=''):
        if not isinstance(message,str) or len(message)>2000:
            raise ServiceError('消息必须是2000字以内的文本。')
        message=message.strip()
        if not message and selected_order_id:
            message='查询订单 '+str(selected_order_id)
        if not message:raise ServiceError('请输入需要咨询的问题。')
        con=connect(self.db_path)
        try:
            if not con.execute('SELECT 1 FROM users WHERE user_id=? AND is_demo=1',(user_id,)).fetchone():
                raise ServiceError('请先选择有效的演示账号。',401)
            now=datetime.now(timezone.utc).isoformat()
            row=None
            if conversation_id:
                row=con.execute('SELECT * FROM conversations WHERE conversation_id=? AND user_id=?',(conversation_id,user_id)).fetchone()
                if row is None:raise ServiceError('会话不存在或不属于当前账号。',404)
            else:
                conversation_id=uuid.uuid4().hex
                with con:
                    con.execute('INSERT INTO conversations (conversation_id,user_id,created_at,updated_at) VALUES (?,?,?,?)',(conversation_id,user_id,now,now))
            previous=json.loads(row['result_json']) if row else {}
            previous_params=json.loads(row['params_json']) if row else {}
            fields=extract_parameters(message)
            engine='local_rules'
            model_reason='not_configured'
            model_intent='order_query'
            if self.model.configured and not selected_order_id:
                try:
                    extracted=await asyncio.to_thread(self.model.extract,message)
                    model_intent=extracted.pop('intent')
                    # 明确写出的号码及已命中的本地规则优先，避免模型交换号码含义。
                    for key,value in extracted.items():
                        if key=='query_type':
                            if fields[key]=='general':fields[key]=value
                        elif value and not fields[key]:fields[key]=value
                    engine='deepseek'
                    model_reason=''
                except ModelFailure as exc:
                    model_reason=str(exc)
            elif selected_order_id:
                model_reason='explicit_selection'
            explicit_query=fields['query_type']!='general'
            has_identifier=any(fields[k] for k in ['order_id','tracking_number','phone_number','product_name'])
            if selected_order_id:
                fields['order_id']=str(selected_order_id).strip()
                has_identifier=True
            elif previous.get('result_type')=='multiple_orders':
                choice=re.search(r'(?:第)?([一二三四五1-5])(?:笔|个|条)',message)
                if choice:
                    index={'一':0,'二':1,'三':2,'四':3,'五':4,'1':0,'2':1,'3':2,'4':3,'5':4}[choice.group(1)]
                    candidates=previous.get('candidates',[])
                    if index<len(candidates):
                        fields['order_id']=candidates[index]['order_id']
                        has_identifier=True
            is_selection=bool(fields['order_id'] and previous.get('result_type')=='multiple_orders')
            if previous.get('result_type') in ['need_more_info','multiple_orders','narrow_search']:
                merged=dict(previous_params)
                if fields.get('order_id') or fields.get('tracking_number'):
                    # 新的明确号码已经足以定位，不能被上轮商品或电话条件误挡。
                    for key in ['order_id','tracking_number','phone_number','product_name']:
                        merged[key]=''
                if previous_params.get('validation_error'):
                    merged['phone_number']=''
                merged.update({k:v for k,v in fields.items() if v and k!='query_type'})
                merged['query_type']=fields['query_type'] if explicit_query else previous_params.get('query_type','general')
                fields=merged
            context=previous.get('context',{}) if not has_identifier else {}
            if any(word in message for word in ['换个订单','换一笔','重新查询','查另一个']):
                context={}
            original=previous_params.get('original_question',message) if is_selection or previous.get('result_type')=='need_more_info' else message
            normalized=await normalize(SimpleNamespace(params={'extracted':fields,'user_id':user_id,'USER_INPUT':original,'context_json':json.dumps(context)}))
            params=json.loads(normalized['params_json'])
            if original!=message:params['follow_up_question']=message
            asks_human=model_intent in ['human','after_sales'] or any(word in message for word in ['人工','退款','退货','换货','赔偿','投诉'])
            other_consultation=model_intent=='other' or any(word in message for word in ['优惠券','多少钱','价格','配料','保质期','营养','过敏'])
            if other_consultation or (asks_human and not any(params.get(k) for k in ['order_id','tracking_number','phone_number','product_name'])):
                rendered=await human(SimpleNamespace(params={'USER_INPUT':message}))
            else:
                try:
                    data=snapshot(self.db_path,user_id=user_id,include_logistics=False)
                    source_success=True
                except sqlite3.Error:
                    data=[]
                    source_success=False
                queried=await query(SimpleNamespace(params={'params_json':json.dumps(params),'data_json':json.dumps(data,ensure_ascii=False),'failure_mode':failure_mode,'source_is_success':source_success}))
                enriched=await self.fetch_logistics(queried,conversation_id,user_id,con)
                rendered=await render(SimpleNamespace(params={'query_result_json':json.dumps(enriched,ensure_ascii=False)}))
            result=json.loads(rendered['result_json'])
            if asks_human and result.get('orders'):
                result['next_action']='request_human'
                result['reason']='after_sales_or_human_requested'
                result['human_transfer']={'requested':True,'status':'not_connected','ticket_id':None}
                result['human_request']={'user_message':message,'reason':result['reason'],'order_ids':[o['order_id'] for o in result['orders']]}
                if '人工' not in result['reply']:
                    result['reply']=(result['reply']+'此问题需要人工客服核实。')[:120]
            result['conversation_id']=conversation_id
            result['engine']=engine
            result['model_status']={'provider':'deepseek','fallback_reason':model_reason}
            history=[dict(r) for r in con.execute('SELECT role,content FROM conversation_messages WHERE conversation_id=? ORDER BY message_id DESC LIMIT 6',(conversation_id,))]
            history.reverse()
            reply,reply_trace=await self.reply_generator.generate(result,message,history)
            result['reply']=reply
            result['reply_status']=reply_trace
            # 只有已重新验证归属的订单才保留在跨轮上下文。
            if result['result_type'] in ['success','partial_success'] and result['orders']:
                params['order_id']=result['orders'][0]['order_id']
            with con:
                con.execute('INSERT INTO order_reply_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (reply_trace['run_id'],conversation_id,user_id,reply_trace['scene'],json.dumps(reply_trace['retrieval'],ensure_ascii=False),
                     reply_trace['mode'],reply_trace['attempts'],int(reply_trace['review_passed']),reply_trace['fallback_reason'],reply_trace['elapsed_ms'],reply_trace['created_at']))
                con.execute('UPDATE conversations SET params_json=?,result_json=?,updated_at=? WHERE conversation_id=? AND user_id=?',
                            (json.dumps(params,ensure_ascii=False),json.dumps(result,ensure_ascii=False),now,conversation_id,user_id))
                con.execute('INSERT INTO conversation_messages (conversation_id,role,content,created_at) VALUES (?,?,?,?)',(conversation_id,'user',message,now))
                con.execute('INSERT INTO conversation_messages (conversation_id,role,content,created_at) VALUES (?,?,?,?)',(conversation_id,'assistant',result['reply'],now))
            return result
        finally:
            con.close()

    def chat(self,*args,**kwargs):
        with self.lock:
            return asyncio.run(self.handle(*args,**kwargs))

    def request_human(self,user_id,conversation_id,fault=''):
        with self.lock:
            return self._request_human(user_id,conversation_id,fault)

    def _request_human(self,user_id,conversation_id,fault=''):
        if fault not in HUMAN_FAULTS:raise ServiceError('无效的模拟转接场景。')
        con=connect(self.db_path)
        try:
            row=con.execute('SELECT result_json FROM conversations WHERE conversation_id=? AND user_id=?',(conversation_id,user_id)).fetchone()
            if row is None:raise ServiceError('会话不存在或不属于当前账号。',404)
            result=json.loads(row['result_json'])
            order_id=(result.get('orders') or [{}])[0].get('order_id')
            reason=result.get('reason') or 'customer_requested'
            existing=con.execute('SELECT h.* FROM human_requests h JOIN human_request_conversations c ON h.request_id=c.request_id WHERE c.conversation_id=? AND h.user_id=? AND h.order_id IS ? AND h.reason=? ORDER BY h.created_at DESC LIMIT 1',(conversation_id,user_id,order_id,reason)).fetchone()
            if order_id and not con.execute('SELECT 1 FROM orders WHERE order_id=? AND user_id=?',(order_id,user_id)).fetchone():
                raise ServiceError('订单归属校验失败。',403)
            request_id=existing['request_id'] if existing else 'HR-'+uuid.uuid4().hex[:12].upper()
            now=datetime.now(timezone.utc).isoformat()
            if not existing:
                messages=[dict(r) for r in con.execute('SELECT role,content FROM conversation_messages WHERE conversation_id=? ORDER BY message_id DESC LIMIT 10',(conversation_id,))]
                messages.reverse()
                result['handoff_context']={'is_demo':True,'reason':reason,'orders':result.get('orders',[]),
                                           'customer_question':(result.get('human_request') or {}).get('user_message') or message_last_user(messages),
                                           'messages':messages}
                with con:
                    con.execute('INSERT INTO human_requests (request_id,user_id,order_id,reason,status,context_json,created_at) VALUES (?,?,?,?,?,?,?)',(request_id,user_id,order_id,reason,'not_connected',json.dumps(result,ensure_ascii=False),now))
                    con.execute('INSERT INTO human_request_conversations VALUES (?,?)',(request_id,conversation_id))
            record=con.execute('SELECT * FROM human_requests WHERE request_id=?',(request_id,)).fetchone()
            if self.human_client is None:return self.human_response(record)
            snapshot_result=json.loads(record['context_json'])
            context=snapshot_result.get('handoff_context') or {'orders':snapshot_result.get('orders',[]),'messages':[],'reason':reason,'is_demo':True}
            ticket,trace=self.human_client.submit(user_id,request_id,context,fault)
            return self.apply_human_response(con,user_id,record,ticket,trace,is_submission=True)
        finally:con.close()

    def human_response(self,record,calls=None,verified=False,message=None):
        state=record['transfer_state']
        messages={'not_connected':'人工请求已记录，模拟坐席服务尚未启用。',
                  'queued':f"已提交到模拟人工服务，当前排队第{record['queue_position']}位。",
                  'connected':f"{record['agent_name'] or '模拟坐席'}已接入，已收到你的咨询与订单信息。",
                  'closed':'模拟人工工单已处理完成。',
                  'failed':'本次模拟转接未成功，可以重试。',
                  'unknown':'暂未取得可靠的创建结果，请重试核实；同一请求不会重复创建工单。'}
        if state=='failed' and record['external_ticket_id']:
            messages['failed']='该模拟工单已终止，请开启新对话后重新提交。'
        return {'request_id':record['request_id'],'ticket_id':record['external_ticket_id'],
                'order_id':record['order_id'],'status':state,'queue_position':record['queue_position'],
                'agent_name':record['agent_name'],'is_simulated':True,'status_verified':verified,
                'updated_at':record['updated_at'],'message':message or messages.get(state,'状态待确认。'),
                'human_calls':calls or []}

    def apply_human_response(self,con,user_id,record,ticket,trace,is_submission=False):
        with con:
            con.execute('INSERT INTO human_api_calls VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (trace['request_id'],record['request_id'],user_id,trace['endpoint'],json.dumps(trace['request'],ensure_ascii=False),
                         json.dumps(trace['response'],ensure_ascii=False) if trace['response'] is not None else None,
                         trace['status'],trace['http_status'],trace['elapsed_ms'],trace['called_at']))
            if ticket:
                state=ticket['status']
                storage='requested' if state=='queued' else 'accepted' if state in ('connected','closed') else 'failed'
                con.execute('UPDATE human_requests SET status=?,transfer_state=?,external_ticket_id=?,queue_position=?,agent_name=?,updated_at=? WHERE request_id=?',
                            (storage,state,ticket['ticket_id'],ticket['queue_position'],ticket['agent_name'],ticket['updated_at'],record['request_id']))
            elif is_submission and not record['external_ticket_id']:
                state='unknown' if trace['status'] in ('timeout','invalid_response') else 'failed'
                con.execute('UPDATE human_requests SET status=?,transfer_state=?,updated_at=? WHERE request_id=?',
                            ('failed',state,trace['called_at'],record['request_id']))
        updated=con.execute('SELECT * FROM human_requests WHERE request_id=?',(record['request_id'],)).fetchone()
        message=None
        if ticket and ticket.get('queue_reason')=='no_agents':message=f"模拟坐席当前无人在线，工单已进入队列，排队第{ticket['queue_position']}位。"
        elif ticket is None and record['external_ticket_id']:message='本次接口调用未成功，显示上次确认的工单状态，请刷新核实。'
        return self.human_response(updated,[trace],verified=ticket is not None,message=message)

    def human_ticket(self,user_id,request_id,target_state=None):
        if target_state is not None and target_state not in HUMAN_STATES:raise ServiceError('无效的工单状态。')
        with self.lock:
            con=connect(self.db_path)
            try:
                record=con.execute('SELECT * FROM human_requests WHERE request_id=? AND user_id=?',(request_id,user_id)).fetchone()
                if record is None:raise ServiceError('人工请求不存在或不属于当前账号。',404)
                if not self.human_client or not record['external_ticket_id']:
                    if target_state:raise ServiceError('尚未取得模拟工单号，请先重试转接。',409)
                    if self.human_client:
                        stored=json.loads(record['context_json'])
                        context=stored.get('handoff_context') or {'orders':stored.get('orders',[]),'messages':[],'is_demo':True}
                        ticket,trace=self.human_client.submit(user_id,request_id,context)
                        return self.apply_human_response(con,user_id,record,ticket,trace,is_submission=True)
                    return self.human_response(record)
                if target_state:
                    ticket,trace=self.human_client.transition(user_id,request_id,record['external_ticket_id'],target_state)
                else:ticket,trace=self.human_client.query(user_id,request_id,record['external_ticket_id'])
                response=self.apply_human_response(con,user_id,record,ticket,trace)
                if target_state and ticket is None:
                    response['operation_succeeded']=False
                    response['message']='模拟状态更新未成功。请先接入坐席，再完成工单；已结束的工单不能重新进入排队。'
                else:response['operation_succeeded']=ticket is not None
                return response
            finally:con.close()


def message_last_user(messages):
    return next((m['content'] for m in reversed(messages) if m['role']=='user'),'用户申请人工服务')
