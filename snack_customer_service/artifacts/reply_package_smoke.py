"""独立解压交付包，在无密钥配置下实际验证网页、物流和人工接口。"""
import hashlib
import http.cookiejar
import json
import subprocess
import sys
import uuid
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
destination=ROOT/'deliverables/reply_v4'
archive_path=destination/'snack_customer_service_reply_v4.zip'
with ZipFile(archive_path) as archive:
    contents={name:archive.read(name) for name in archive.namelist()}
for name in ['reply-interface-preview.png','reply-mobile-preview.png']:
    contents['snack_customer_service/docs/'+name]=(ROOT/'artifacts'/name).read_bytes()
with ZipFile(archive_path,'w',ZIP_DEFLATED) as archive:
    for name,data in contents.items():archive.writestr(name,data)
extract=destination/('portable_smoke_'+uuid.uuid4().hex[:8])
if extract.exists():raise SystemExit('独立验收目录已存在，避免覆盖')
with ZipFile(archive_path) as archive:
    assert archive.testzip() is None
    assert not any('.env.local' in n or '__pycache__' in n or 'profile' in n for n in archive.namelist())
    for name in archive.namelist():
        assert (extract/name).resolve().is_relative_to(extract.resolve())
    archive.extractall(extract)
code=r'''
import http.cookiejar,json,sqlite3,threading,urllib.request
from pathlib import Path
from app import create_server
from model_adapter import DeepSeekExtractor
database=Path('database/snack_orders_demo.sqlite')
with sqlite3.connect(database) as db:
 assert db.execute('pragma user_version').fetchone()[0]==6
 assert db.execute('select count(*) from order_reply_knowledge').fetchone()[0]==20
 assert db.execute('select count(*) from order_reply_runs').fetchone()[0]==0
server=create_server(0,model=DeepSeekExtractor({'MODEL_ENABLED':'false'}),courier_port=0,human_port=0)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
client=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def call(path,body=None):
 request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
 with client.open(request,timeout=10) as response:return json.loads(response.read())
try:
 call('/api/demo/login',{'account':'demo_user_a'})
 result=call('/api/chat',{'message':'SN-DEMO-0002物流到哪了'})
 assert result['reply_status']['mode']=='knowledge_fallback' and result['reply_status']['retrieval']
 assert '运输中' in result['reply'] and '2026-10-08 18:00' in result['reply']
 assert result['courier_calls'][0]['http_status']==200
 human=call('/api/human/request',{'conversation_id':result['conversation_id']})
 assert human['status']=='queued'
 assert call('/api/reply/runs')['runs'][0]['scene']=='in_transit'
 print(json.dumps({'passed':True,'source':'independently_extracted_package','model_enabled':False,'database_version':6,'knowledge_entries':20,'courier_http_verified':True,'human_http_verified':True,'reply_run_verified':True}))
finally:
 server.shutdown();server.server_close();thread.join(timeout=5)
'''
result=subprocess.run([sys.executable,'-X','utf8','-c',code],cwd=extract/'snack_customer_service',capture_output=True,text=True,encoding='utf-8',timeout=40)
if result.returncode:
    print(result.stdout)
    print(result.stderr)
    raise SystemExit(result.returncode)
report=json.loads(result.stdout.strip())
(destination/'portable-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
delivery=json.loads((destination/'delivery-verification.json').read_text(encoding='utf-8'))
delivery.update(sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),portable_verified=True,secret_configuration_included=False)
(destination/'delivery-verification.json').write_text(json.dumps(delivery,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
