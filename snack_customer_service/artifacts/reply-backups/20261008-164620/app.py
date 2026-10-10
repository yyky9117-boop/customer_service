"""运行：python app.py。浏览器打开 http://127.0.0.1:8765。"""
import argparse
import hashlib
import json
import secrets
import sqlite3
import time
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from database.demo_db import initialize, connect, DB_PATH
from service import CustomerService, ServiceError
from courier_client import CourierClient
from courier_simulator import create_courier_server, DB_PATH as COURIER_DB_PATH
from human_client import HumanClient
from human_simulator import create_human_server, DB_PATH as HUMAN_DB_PATH

ROOT=Path(__file__).resolve().parent


class CustomerServiceServer(ThreadingHTTPServer):
    def server_close(self):
        super().server_close()
        for kind in ('courier','human'):
            provider=getattr(self,kind+'_server',None)
            thread=getattr(self,kind+'_thread',None)
            if provider:
                if thread and thread.is_alive():provider.shutdown()
                provider.server_close()
                if thread:thread.join(timeout=3)


def create_server(port=8765,db_path=DB_PATH,model=None,courier_port=0,human_port=0):
    initialize(db_path)

    class Handler(BaseHTTPRequestHandler):
        server_version='SnackCustomerService/2.0'

        def log_message(self,fmt,*args):
            # 不记录请求正文、Cookie 或个人令牌。
            pass

        def send_json(self,value,status=200,cookie=None):
            body=json.dumps(value,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            if cookie:self.send_header('Set-Cookie',cookie)
            self.end_headers()
            self.wfile.write(body)

        def get_user(self):
            cookies=SimpleCookie()
            try:cookies.load(self.headers.get('Cookie',''))
            except Exception:return None
            session=cookies.get('snack_session')
            if not session:return None
            digest=hashlib.sha256(session.value.encode()).hexdigest()
            con=connect(db_path)
            try:
                row=con.execute('SELECT user_id FROM demo_sessions WHERE token_hash=? AND expires_at>?',(digest,int(time.time()))).fetchone()
                return row['user_id'] if row else None
            finally:con.close()

        def require_user(self):
            user=self.get_user()
            if not user:raise ServiceError('请先选择演示账号。',401)
            return user

        def read_json(self):
            origin=self.headers.get('Origin')
            if origin:
                source=urlsplit(origin)
                if source.netloc!=self.headers.get('Host') or source.scheme!='http':
                    raise ServiceError('请求来源不匹配。',403)
            if self.headers.get_content_type()!='application/json':
                raise ServiceError('请求应使用 JSON。',415)
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:raise ServiceError('请求长度无效。')
            if length<0 or length>16384:raise ServiceError('请求内容过大。',413)
            try:data=json.loads(self.rfile.read(length))
            except (ValueError,UnicodeDecodeError):raise ServiceError('请求格式不正确。')
            if not isinstance(data,dict):raise ServiceError('请求内容应为对象。')
            return data

        def do_GET(self):
            path=urlsplit(self.path).path
            try:
                if path=='/api/health':
                    self.send_json({'status':'ok','is_demo':True,'engine':service.model.status()['mode'],'model':service.model.status(),'courier':service.courier.status(),'human':service.human_client.status(),'database':'sqlite','coze_required':False})
                elif path=='/api/demo/accounts':
                    con=connect(db_path)
                    try:rows=[dict(row) for row in con.execute("SELECT user_id,display_name FROM users WHERE user_id IN ('demo_user_a','demo_user_b') AND is_demo=1 ORDER BY user_id")]
                    finally:con.close()
                    self.send_json({'accounts':rows})
                elif path=='/api/me':
                    self.send_json({'user_id':self.get_user()})
                elif path=='/api/orders':
                    user=self.require_user()
                    con=connect(db_path)
                    try:
                        rows=[]
                        for row in con.execute('SELECT order_id,order_status,shipping_status,amount_fen,created_at FROM orders WHERE user_id=? ORDER BY created_at DESC',(user,)):
                            value=dict(row)
                            value['items']=[dict(i) for i in con.execute('SELECT item_title,quantity FROM order_items WHERE order_id=?',(value['order_id'],))]
                            rows.append(value)
                    finally:con.close()
                    self.send_json({'orders':rows})
                elif path=='/api/human/requests':
                    user=self.require_user()
                    con=connect(db_path)
                    try:rows=[dict(r) for r in con.execute('SELECT request_id,order_id,reason,transfer_state AS status,external_ticket_id AS ticket_id,queue_position,agent_name,created_at FROM human_requests WHERE user_id=? ORDER BY created_at DESC',(user,))]
                    finally:con.close()
                    self.send_json({'requests':rows})
                elif path=='/api/courier/calls':
                    user=self.require_user()
                    con=connect(db_path)
                    try:
                        rows=[]
                        for row in con.execute('SELECT * FROM courier_api_calls WHERE user_id=? ORDER BY called_at DESC LIMIT 30',(user,)):
                            value=dict(row)
                            value.pop('user_id',None)
                            value['request']=json.loads(value.pop('request_json'))
                            value['response']=json.loads(value.pop('response_json') or 'null')
                            rows.append(value)
                    finally:con.close()
                    self.send_json({'calls':rows,'is_simulated':True})
                elif path=='/api/human/calls':
                    user=self.require_user()
                    con=connect(db_path)
                    try:
                        rows=[]
                        for row in con.execute('SELECT * FROM human_api_calls WHERE user_id=? ORDER BY called_at DESC LIMIT 30',(user,)):
                            value=dict(row)
                            value.pop('user_id',None)
                            value['request']=json.loads(value.pop('request_json'))
                            value['response']=json.loads(value.pop('response_json') or 'null')
                            rows.append(value)
                    finally:con.close()
                    self.send_json({'calls':rows,'is_simulated':True})
                elif path in ['/','/index.html','/app.js','/style.css']:
                    filename='index.html' if path in ['/','/index.html'] else path[1:]
                    file=ROOT/'web'/filename
                    body=file.read_bytes()
                    mime={'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8','style.css':'text/css; charset=utf-8'}[filename]
                    self.send_response(200)
                    self.send_header('Content-Type',mime)
                    self.send_header('Content-Length',str(len(body)))
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(body)
                else:self.send_json({'error':'页面不存在。'},404)
            except ServiceError as exc:self.send_json({'error':str(exc)},exc.status)
            except (sqlite3.Error,OSError):self.send_json({'error':'服务暂时不可用，请重试。'},503)

        def do_POST(self):
            path=urlsplit(self.path).path
            try:
                data=self.read_json()
                if path=='/api/demo/login':
                    account=data.get('account')
                    if account not in ['demo_user_a','demo_user_b']:raise ServiceError('无效的演示账号。')
                    token=secrets.token_urlsafe(32)
                    digest=hashlib.sha256(token.encode()).hexdigest()
                    con=connect(db_path)
                    try:
                        with con:
                            con.execute('DELETE FROM demo_sessions WHERE expires_at<=?',(int(time.time()),))
                            con.execute('INSERT INTO demo_sessions VALUES (?,?,?)',(digest,account,int(time.time())+86400))
                    finally:con.close()
                    self.send_json({'user_id':account,'is_demo':True},cookie='snack_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400')
                elif path=='/api/chat':
                    user=self.require_user()
                    # body 中的 user_id 被忽略，身份只从服务端会话取得。
                    result=service.chat(user,data.get('message',''),data.get('conversation_id'),data.get('selected_order_id'),str(data.get('failure_mode') or ''))
                    self.send_json(result)
                elif path=='/api/human/request':
                    user=self.require_user()
                    self.send_json(service.request_human(user,data.get('conversation_id'),data.get('failure_mode','')))
                elif path=='/api/human/query':
                    user=self.require_user()
                    self.send_json(service.human_ticket(user,data.get('request_id')))
                elif path=='/api/demo/human-state':
                    user=self.require_user()
                    self.send_json(service.human_ticket(user,data.get('request_id'),data.get('status')))
                elif path=='/api/demo/logistics':
                    user=self.require_user()
                    self.send_json(service.set_logistics_scenario(user,data.get('tracking_number'),data.get('scenario')))
                else:self.send_json({'error':'接口不存在。'},404)
            except ServiceError as exc:self.send_json({'error':str(exc)},exc.status)
            except (sqlite3.Error,OSError):self.send_json({'error':'本次处理未成功，请重试。'},503)
            except (TypeError,ValueError):self.send_json({'error':'请求参数不正确。'},400)

    server=CustomerServiceServer(('127.0.0.1',port),Handler)
    try:
        courier_path=COURIER_DB_PATH if Path(db_path).resolve()==DB_PATH.resolve() else Path(str(db_path)+'.courier.sqlite')
        server.courier_server=create_courier_server(courier_port,courier_path)
        server.courier_thread=threading.Thread(target=server.courier_server.serve_forever,daemon=True)
        server.courier_thread.start()
        courier=CourierClient('http://127.0.0.1:'+str(server.courier_server.server_port),server.courier_server.token)
        human_path=HUMAN_DB_PATH if Path(db_path).resolve()==DB_PATH.resolve() else Path(str(db_path)+'.human.sqlite')
        server.human_server=create_human_server(human_port,human_path)
        server.human_thread=threading.Thread(target=server.human_server.serve_forever,daemon=True)
        server.human_thread.start()
        human=HumanClient('http://127.0.0.1:'+str(server.human_server.server_port),server.human_server.token)
        service=CustomerService(db_path,model=model,courier=courier,human_client=human)
        server.service=service
    except Exception:
        server.server_close()
        raise
    return server


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--courier-port',type=int,default=8766)
    parser.add_argument('--human-port',type=int,default=8767)
    args=parser.parse_args()
    server=create_server(args.port,courier_port=args.courier_port,human_port=args.human_port)
    print(f'零食订单客服已启动：http://127.0.0.1:{server.server_port}',flush=True)
    print('使用模拟订单；DeepSeek 配置后启用，未配置或调用失败时使用本地规则。',flush=True)
    print('模拟快递 HTTP 接口：'+server.service.courier.base_url+'/v1/traces/query',flush=True)
    print('模拟人工 HTTP 接口：'+server.service.human_client.base_url+'/v1/handoffs/create',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
