"""模拟商城坐席 HTTP 服务；队列与工单存储独立于订单库。"""
import hmac
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime,timezone,timedelta
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

DB_PATH=Path(__file__).resolve().parent/'database'/'mock_human.sqlite'
STATES=('queued','connected','closed','failed')
FAULTS=('', 'timeout', 'http_error', 'malformed', 'no_agents')


def connect(path=DB_PATH):
    con=sqlite3.connect(path)
    con.row_factory=sqlite3.Row
    return con


def initialize(path=DB_PATH):
    con=connect(path)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS tickets (
      ticket_id TEXT PRIMARY KEY,
      human_request_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      idempotency_key TEXT NOT NULL UNIQUE,
      status TEXT NOT NULL CHECK(status IN ('queued','connected','closed','failed')),
      queue_reason TEXT NOT NULL,
      agent_name TEXT,
      context_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS provider_requests (
      request_id TEXT PRIMARY KEY,
      human_request_id TEXT NOT NULL,
      action TEXT NOT NULL,
      request_json TEXT NOT NULL,
      response_status INTEGER NOT NULL,
      received_at TEXT NOT NULL
    );
    ''')
    con.close()


def now():return datetime.now(timezone(timedelta(hours=8))).isoformat()


def ticket_data(con,row):
    context=json.loads(row['context_json'])
    position=None
    if row['status']=='queued':
        position=con.execute("SELECT COUNT(*) FROM tickets WHERE status='queued' AND (created_at<? OR (created_at=? AND ticket_id<=?))",(row['created_at'],row['created_at'],row['ticket_id'])).fetchone()[0]
    return {'ticket_id':row['ticket_id'],'human_request_id':row['human_request_id'],'user_id':row['user_id'],
            'status':row['status'],'queue_position':position,'queue_reason':row['queue_reason'],
            'agent_name':row['agent_name'],'created_at':row['created_at'],'updated_at':row['updated_at'],
            'forwarded_orders':len(context.get('orders',[])),
            'forwarded_messages':len(context.get('messages',[])),
            'is_simulated':True}


def create_human_server(port=0,db_path=DB_PATH,token=None):
    initialize(db_path)
    token=token or uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def respond(self,value,status=200,malformed=False):
            body=b'{invalid json' if malformed else json.dumps(value,ensure_ascii=False).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(body)))
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                self.wfile.write(body)
            except OSError:pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.respond({'code':'unauthorized'},401)
                return
            call_id=self.headers.get('X-Request-ID','')
            if not re.fullmatch(r'[A-Za-z0-9-]{1,80}',call_id):call_id='HC-'+uuid.uuid4().hex
            try:
                length=int(self.headers.get('Content-Length','0'))
                if self.headers.get_content_type()!='application/json' or length<=0 or length>65536:raise ValueError()
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError()
                user=data.get('user_id')
                request=data.get('human_request_id')
                if not all(isinstance(v,str) and 1<=len(v)<=100 for v in [user,request]):raise ValueError()
                fault=data.get('demo_fault','')
                if fault not in FAULTS:raise ValueError()
                if self.path not in ('/v1/handoffs/create','/v1/handoffs/query','/v1/demo/handoff-state'):
                    self.respond({'code':'not_found','request_id':call_id},404)
                    return
                con=connect(db_path)
                try:
                    http_status=503 if fault=='http_error' else 200
                    row=None
                    error='temporarily_unavailable'
                    with con:
                        if http_status==200 and self.path=='/v1/handoffs/create':
                            context=data.get('context')
                            if not isinstance(context,dict) or not isinstance(context.get('orders'),list) or not isinstance(context.get('messages'),list):raise ValueError()
                            key=user+':'+request
                            timestamp=now()
                            con.execute('INSERT OR IGNORE INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?)',
                                        ('HT-'+uuid.uuid4().hex[:16].upper(),request,user,key,'queued',
                                         'no_agents' if fault=='no_agents' else 'waiting_agent',None,
                                         json.dumps(context,ensure_ascii=False),timestamp,timestamp))
                            row=con.execute('SELECT * FROM tickets WHERE idempotency_key=?',(key,)).fetchone()
                        elif http_status==200:
                            row=con.execute('SELECT * FROM tickets WHERE ticket_id=? AND human_request_id=? AND user_id=?',
                                            (data.get('ticket_id'),request,user)).fetchone()
                            if row is None:http_status,error=404,'ticket_not_found'
                            elif self.path=='/v1/demo/handoff-state':
                                target=data.get('status')
                                if target not in STATES:raise ValueError()
                                allowed={'queued':('connected','failed'),'connected':('closed','failed'),'closed':(),'failed':()}
                                if target!=row['status'] and target not in allowed[row['status']]:
                                    http_status,error=409,'invalid_transition'
                                else:
                                    agent='模拟坐席 小林' if target in ('connected','closed') else row['agent_name']
                                    con.execute('UPDATE tickets SET status=?,agent_name=?,queue_reason=?,updated_at=? WHERE ticket_id=?',
                                                (target,agent,'',now(),row['ticket_id']))
                                    row=con.execute('SELECT * FROM tickets WHERE ticket_id=?',(row['ticket_id'],)).fetchone()
                        con.execute('INSERT INTO provider_requests VALUES (?,?,?,?,?,?)',
                                    (call_id,request,self.path,json.dumps(data,ensure_ascii=False),http_status,now()))
                    if fault=='timeout':time.sleep(1.2)
                    if http_status!=200:
                        self.respond({'code':error,'request_id':call_id,'provider':'mock_human','is_simulated':True},http_status)
                    else:
                        self.respond({'code':'ok','request_id':call_id,'provider':'mock_human','is_simulated':True,
                                      'data':ticket_data(con,row)},malformed=fault=='malformed')
                finally:con.close()
            except (ValueError,TypeError):self.respond({'code':'invalid_request','request_id':call_id},400)
            except sqlite3.Error:self.respond({'code':'provider_error','request_id':call_id},503)

    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.token=token
    return server
