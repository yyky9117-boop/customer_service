"""独立 HTTP 模拟快递服务，使用自己的物流数据库。运行不需要外部快递账号。"""
import argparse
import hmac
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH=Path(__file__).resolve().parent/'database'/'mock_courier.sqlite'
SCENARIOS=('default','no_records','collected','in_transit','delivering','delivered','exception','stalled')
FAULTS=('', 'timeout', 'http_error', 'empty', 'malformed')
SEEDS={'DEMO-TRACK-1002':'in_transit','DEMO-TRACK-1003':'delivered',
       'DEMO-TRACK-1004':'no_records','DEMO-TRACK-1005':'exception',
       'DEMO-TRACK-1008A':'in_transit','DEMO-TRACK-1008B':'no_records','DEMO-TRACK-1010':'in_transit'}
TZ=timezone(timedelta(hours=8))


def connect(path=DB_PATH):
    con=sqlite3.connect(path)
    con.row_factory=sqlite3.Row
    return con


def initialize(path=DB_PATH):
    con=connect(path)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS parcels (
      tracking_number TEXT PRIMARY KEY,
      carrier_code TEXT NOT NULL,
      scenario TEXT NOT NULL,
      default_scenario TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS provider_requests (
      request_id TEXT PRIMARY KEY,
      tracking_number TEXT,
      action TEXT NOT NULL,
      fault TEXT NOT NULL,
      response_status INTEGER NOT NULL,
      received_at TEXT NOT NULL
    );
    ''')
    with con:
        for tracking,scenario in SEEDS.items():
            con.execute('INSERT OR IGNORE INTO parcels VALUES (?,?,?,?,?)',
                        (tracking,'mock_express',scenario,scenario,datetime.now(TZ).isoformat()))
    con.close()


def event(moment,status,description,location):
    return {'time':moment,'status':status,'description':description,'location':location}


def make_parcel(row):
    scenario=row['scenario']
    collected=event('2026-10-06T15:20:00+08:00','collected','包裹已由上海浦东营业部揽收','上海市浦东新区')
    departed=event('2026-10-06T20:10:00+08:00','in_transit','包裹已到达上海分拨中心','上海市')
    transport=event('2026-10-07T10:00:00+08:00','in_transit','包裹运输中，已离开上海分拨中心，发往杭州分拨中心','上海市')
    arrival=event('2026-10-08T06:45:00+08:00','in_transit','包裹已到达杭州分拨中心','浙江省杭州市')
    delivery=event('2026-10-08T09:15:00+08:00','delivering','杭州西湖营业部正在派送，请留意收件通知','浙江省杭州市西湖区')
    delivered=event('2026-10-08T13:40:00+08:00','delivered','包裹已签收，签收方式：本人签收（模拟记录）','浙江省杭州市西湖区')
    if scenario=='no_records':events=[]
    elif scenario=='collected':events=[collected]
    elif scenario=='in_transit':events=[collected,departed,transport]
    elif scenario=='delivering':events=[collected,departed,transport,arrival,delivery]
    elif scenario=='delivered':events=[collected,departed,transport,arrival,delivery,delivered]
    elif scenario=='exception':
        events=[collected,departed,transport,event('2026-10-07T11:25:00+08:00','exception','配送异常：收件地址需进一步核实，等待处理','浙江省杭州市')]
    else:events=[collected,departed,transport]
    clock={'no_records':'2026-10-07T12:00:00+08:00','collected':'2026-10-06T16:00:00+08:00',
           'in_transit':'2026-10-07T12:00:00+08:00','delivering':'2026-10-08T10:00:00+08:00',
           'delivered':'2026-10-08T14:00:00+08:00','exception':'2026-10-07T12:00:00+08:00',
           'stalled':'2026-10-09T12:00:00+08:00'}[scenario]
    status=events[-1]['status'] if events else 'no_records'
    if status=='collected':status='in_transit'
    latest=events[-1]['description'] if events else ''
    eta='2026-10-08 18:00' if scenario in ('in_transit','delivering') and row['tracking_number']=='DEMO-TRACK-1002' else None
    if scenario=='stalled':latest+='；超过48小时未更新（按模拟业务时间计算）'
    return {'tracking_number':row['tracking_number'],'carrier_code':row['carrier_code'],
            'carrier_name':'模拟速递','status':status,'scenario':scenario,
            'latest_description':latest,'estimated_delivery_at':eta,
            'eta_source':'mock_courier_estimate' if eta else None,
            'simulation_time':clock,'last_event_at':events[-1]['time'] if events else None,
            'events':list(reversed(events)), 'is_simulated':True}


def create_courier_server(port=0,db_path=DB_PATH,token=None):
    initialize(db_path)
    token=token or uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def respond(self,value,status=200,malformed=False):
            body=b'{invalid json' if malformed else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            try:self.wfile.write(body)
            except OSError:pass  # 超时演示中客户端可能已断开连接。

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.respond({'code':'unauthorized'},401)
                return
            request_id=self.headers.get('X-Request-ID','')
            if not re.fullmatch(r'[A-Za-z0-9-]{1,80}',request_id):request_id='CQ-'+uuid.uuid4().hex
            try:
                if self.headers.get_content_type()!='application/json':raise ValueError()
                length=int(self.headers.get('Content-Length','0'))
                if length<=0 or length>8192:raise ValueError()
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError()
                tracking=data.get('tracking_number')
                if not isinstance(tracking,str) or len(tracking)>100:raise ValueError()
                fault=data.get('demo_fault','')
                if fault not in FAULTS:raise ValueError()
                if self.path not in ('/v1/traces/query','/v1/demo/scenario'):
                    self.respond({'code':'not_found'},404)
                    return
                con=connect(db_path)
                try:
                    row=con.execute('SELECT * FROM parcels WHERE tracking_number=? AND carrier_code=?',(tracking,data.get('carrier_code'))).fetchone()
                    status=200 if row else 404
                    if fault=='http_error':status=503
                    if self.path=='/v1/demo/scenario':
                        scenario=data.get('scenario')
                        if scenario not in SCENARIOS:raise ValueError()
                        if row:
                            scenario=row['default_scenario'] if scenario=='default' else scenario
                            with con:con.execute('UPDATE parcels SET scenario=?,updated_at=? WHERE tracking_number=?',(scenario,datetime.now(TZ).isoformat(),tracking))
                            row=con.execute('SELECT * FROM parcels WHERE tracking_number=?',(tracking,)).fetchone()
                    with con:
                        con.execute('INSERT INTO provider_requests VALUES (?,?,?,?,?,?)',(request_id,tracking,self.path,fault,status,datetime.now(TZ).isoformat()))
                    if fault=='timeout':time.sleep(1.2)
                    if status!=200:
                        self.respond({'code':'temporarily_unavailable' if status==503 else 'tracking_not_found','request_id':request_id,'is_simulated':True},status)
                        return
                    parcel=make_parcel(row)
                    if fault=='empty':
                        parcel.update(status='no_records',events=[],latest_description='',last_event_at=None,estimated_delivery_at=None,eta_source=None)
                    self.respond({'code':'ok','request_id':request_id,'provider':'mock_courier','is_simulated':True,'data':parcel},malformed=fault=='malformed')
                finally:con.close()
            except (ValueError,TypeError):self.respond({'code':'invalid_request','request_id':request_id},400)
            except sqlite3.Error:self.respond({'code':'provider_error','request_id':request_id},503)

    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.token=token
    return server


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    server=create_courier_server(args.port)
    print(f'模拟快递服务：http://127.0.0.1:{server.server_port}；启动客服时通常由主服务自动管理。',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
