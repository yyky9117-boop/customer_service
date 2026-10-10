"""客服与快递服务之间的 HTTP 边界；轨迹必须经过接口响应校验。"""
import json
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime,timezone,timedelta
from urllib.parse import urlsplit

STATUSES=('no_records','in_transit','delivering','delivered','exception','unknown')
FAULT_MAP={'logistics_timeout':'timeout','logistics_error':'http_error','logistics_empty':'empty','logistics_malformed':'malformed'}


class CourierClient:
    def __init__(self,base_url='http://127.0.0.1:8766',token='',timeout=2,opener=None):
        source=urlsplit(base_url)
        if source.scheme!='http' or source.hostname!='127.0.0.1' or source.username or source.password:
            raise ValueError('模拟快递服务只允许本机地址')
        self.base_url=base_url.rstrip('/')
        self.token=token
        self.timeout=timeout
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def status(self):
        return {'provider':'mock_courier','is_simulated':True,'transport':'http','endpoint':self.base_url+'/v1/traces/query'}

    def request(self,path,payload,timeout=None):
        request_id='CQ-'+uuid.uuid4().hex
        trace={'request_id':request_id,'provider':'mock_courier','is_simulated':True,'transport':'http',
               'method':'POST','endpoint':self.base_url+path,'request':payload,'response':None,
               'http_status':None,'status':'connection_failed','elapsed_ms':0,
               'called_at':datetime.now(timezone(timedelta(hours=8))).isoformat()}
        req=urllib.request.Request(self.base_url+path,data=json.dumps(payload).encode(),
                                   headers={'Content-Type':'application/json','Authorization':'Bearer '+self.token,'X-Request-ID':request_id})
        start=time.perf_counter()
        try:
            with self.opener.open(req,timeout=timeout or self.timeout) as response:
                trace['http_status']=response.status
                raw=response.read(65537)
            if len(raw)>65536:raise ValueError('oversized response')
            output=json.loads(raw)
            trace['response']=output
            self.validate(output,payload['tracking_number'],request_id)
            trace['status']='no_records' if output['data']['status']=='no_records' else 'success'
            return output['data'],trace
        except urllib.error.HTTPError as exc:
            trace['http_status']=exc.code
            trace['status']='not_found' if exc.code==404 else 'http_error'
            try:
                raw=exc.read(65537)
                if len(raw)<=65536:trace['response']=json.loads(raw)
            except (ValueError,OSError):pass
        except (TimeoutError,):trace['status']='timeout'
        except urllib.error.URLError as exc:
            trace['status']='timeout' if isinstance(exc.reason,TimeoutError) else 'connection_failed'
        except (ValueError,KeyError,IndexError,TypeError,UnicodeError):trace['status']='invalid_response'
        except OSError:trace['status']='connection_failed'
        finally:trace['elapsed_ms']=round((time.perf_counter()-start)*1000)
        return None,trace

    @staticmethod
    def validate(output,tracking,request_id):
        if not isinstance(output,dict) or output.get('code')!='ok' or output.get('request_id')!=request_id:
            raise ValueError('invalid response')
        data=output.get('data')
        if not isinstance(data,dict) or data.get('tracking_number')!=tracking or data.get('status') not in STATUSES or data.get('is_simulated') is not True:
            raise ValueError('invalid parcel')
        events=data.get('events')
        if not isinstance(events,list) or len(events)>100:raise ValueError('invalid events')
        previous=None
        clock=datetime.fromisoformat(data.get('simulation_time',''))
        if clock.tzinfo is None:raise ValueError('invalid simulation clock')
        for item in events:
            if not isinstance(item,dict) or not all(isinstance(item.get(k),str) for k in ('time','status','description','location')):raise ValueError('invalid event')
            moment=datetime.fromisoformat(item['time'])
            if moment.tzinfo is None or moment>clock or (previous is not None and moment>previous):raise ValueError('invalid timeline')
            previous=moment
        for key in ('carrier_name','latest_description','simulation_time','scenario'):
            if not isinstance(data.get(key),str):raise ValueError('invalid field')
        if data['status']=='no_records' and (events or data.get('estimated_delivery_at')):raise ValueError('inconsistent empty trace')
        if data['status']=='delivered' and data.get('estimated_delivery_at'):raise ValueError('inconsistent delivered parcel')

    def lookup(self,tracking,carrier_code='mock_express',failure_mode=''):
        fault=FAULT_MAP.get(failure_mode,'')
        payload={'carrier_code':carrier_code,'tracking_number':tracking,'demo_fault':fault}
        return self.request('/v1/traces/query',payload,timeout=0.35 if fault=='timeout' else None)

    def set_scenario(self,tracking,scenario):
        return self.request('/v1/demo/scenario',{'carrier_code':'mock_express','tracking_number':tracking,'scenario':scenario})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None
