"""通过本机 HTTP 提交和查询模拟人工转接，校验工单归属与状态。"""
import json
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime,timezone,timedelta
from urllib.parse import urlsplit
from courier_client import NoRedirect

STATES=('queued','connected','closed','failed')
FAULTS=('', 'timeout', 'http_error', 'malformed', 'no_agents')


class HumanClient:
    def __init__(self,base_url='http://127.0.0.1:8767',token='',opener=None):
        source=urlsplit(base_url)
        if source.scheme!='http' or source.hostname!='127.0.0.1' or source.username or source.password:
            raise ValueError('模拟人工服务只允许本机地址')
        self.base_url=base_url.rstrip('/')
        self.token=token
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def status(self):
        return {'provider':'mock_human','is_simulated':True,'transport':'http','endpoint':self.base_url+'/v1/handoffs/create'}

    def request(self,path,payload):
        call_id='HC-'+uuid.uuid4().hex
        trace={'request_id':call_id,'provider':'mock_human','is_simulated':True,'transport':'http','method':'POST',
               'endpoint':self.base_url+path,'request':payload,'response':None,'http_status':None,
               'status':'connection_failed','elapsed_ms':0,'called_at':datetime.now(timezone(timedelta(hours=8))).isoformat()}
        req=urllib.request.Request(trace['endpoint'],data=json.dumps(payload,ensure_ascii=False).encode(),
                                   headers={'Content-Type':'application/json','Authorization':'Bearer '+self.token,'X-Request-ID':call_id})
        start=time.perf_counter()
        try:
            with self.opener.open(req,timeout=0.35 if payload.get('demo_fault')=='timeout' else 2) as response:
                trace['http_status']=response.status
                raw=response.read(65537)
            if len(raw)>65536:raise ValueError('oversized response')
            output=json.loads(raw)
            trace['response']=output
            self.validate(output,payload,call_id)
            trace['status']='success'
            return output['data'],trace
        except urllib.error.HTTPError as exc:
            trace['http_status']=exc.code
            trace['status']='http_error'
            try:
                raw=exc.read(65537)
                if len(raw)<=65536:trace['response']=json.loads(raw)
            except (ValueError,OSError):pass
        except TimeoutError:trace['status']='timeout'
        except urllib.error.URLError as exc:trace['status']='timeout' if isinstance(exc.reason,TimeoutError) else 'connection_failed'
        except (ValueError,KeyError,TypeError,UnicodeError):trace['status']='invalid_response'
        except OSError:trace['status']='connection_failed'
        finally:trace['elapsed_ms']=round((time.perf_counter()-start)*1000)
        return None,trace

    @staticmethod
    def validate(output,payload,call_id):
        if not isinstance(output,dict) or output.get('code')!='ok' or output.get('request_id')!=call_id or output.get('is_simulated') is not True:
            raise ValueError('invalid response')
        data=output.get('data')
        if not isinstance(data,dict) or data.get('status') not in STATES or data.get('is_simulated') is not True:
            raise ValueError('invalid ticket')
        if data.get('human_request_id')!=payload['human_request_id'] or data.get('user_id')!=payload['user_id']:
            raise ValueError('ticket owner mismatch')
        if not isinstance(data.get('ticket_id'),str) or not data['ticket_id'].startswith('HT-'):
            raise ValueError('invalid ticket id')
        if payload.get('ticket_id') and data['ticket_id']!=payload['ticket_id']:
            raise ValueError('ticket id mismatch')
        for key in ('forwarded_orders','forwarded_messages'):
            if type(data.get(key)) is not int or data[key]<0:raise ValueError('invalid count')
        for key in ('created_at','updated_at'):
            if not isinstance(data.get(key),str) or datetime.fromisoformat(data[key]).tzinfo is None:
                raise ValueError('invalid timestamp')
        if data['status']=='queued':
            if type(data.get('queue_position')) is not int or data['queue_position']<1 or data.get('agent_name') is not None:
                raise ValueError('invalid queue')
        elif data['status'] in ('connected','closed'):
            if not isinstance(data.get('agent_name'),str) or not data['agent_name'] or data.get('queue_position') is not None:
                raise ValueError('invalid agent')

    def submit(self,user,request,context,fault=''):
        return self.request('/v1/handoffs/create',{'user_id':user,'human_request_id':request,'context':context,'demo_fault':fault})

    def query(self,user,request,ticket):
        return self.request('/v1/handoffs/query',{'user_id':user,'human_request_id':request,'ticket_id':ticket})

    def transition(self,user,request,ticket,status):
        return self.request('/v1/demo/handoff-state',{'user_id':user,'human_request_id':request,'ticket_id':ticket,'status':status})
