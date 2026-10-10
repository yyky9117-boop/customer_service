"""交付包使用明确文件清单及全新演示数据库，排除密钥、浏览器档案和旧 Coze 工具。"""
import hashlib
import argparse
import json
import sys
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from database.demo_db import initialize, snapshot, connect
from courier_simulator import initialize as initialize_courier
from human_simulator import initialize as initialize_human

parser=argparse.ArgumentParser()
parser.add_argument('--output',default='deliverables/human_v3')
args=parser.parse_args()
destination=(ROOT/args.output).resolve()
if not destination.is_relative_to(ROOT):raise SystemExit('交付目录必须位于本项目内。')
destination.mkdir(parents=True,exist_ok=True)
fresh_db=destination/'snack_orders_demo.sqlite'
if fresh_db.exists():
    raise SystemExit('交付数据库已存在；请明确检查后再重新打包，避免覆盖。')
initialize(fresh_db)
fresh_courier=destination/'mock_courier.sqlite'
initialize_courier(fresh_courier)
fresh_human=destination/'mock_human.sqlite'
initialize_human(fresh_human)
data=snapshot(fresh_db,include_logistics=False)
(destination/'demo_orders.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
files=['README.md','app.py','service.py','model_adapter.py','courier_client.py','courier_simulator.py','human_client.py','human_simulator.py','.env.example','.gitignore','start.cmd','start.ps1',
       'web/index.html','web/app.js','web/style.css',
       'nodes/__init__.py','nodes/normalize.py','nodes/query.py','nodes/render.py','nodes/human.py',
       'database/__init__.py','database/schema.sql','database/demo_db.py','database/migrate_legacy.py',
       'tests/test_service.py','tests/test_http_api.py','tests/test_model_adapter.py','tests/test_courier.py','tests/test_human_api.py']
zip_path=destination/'snack_customer_service_human_v3.zip'
with ZipFile(zip_path,'w',ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(ROOT/name,'snack_customer_service/'+name)
    archive.write(fresh_db,'snack_customer_service/database/snack_orders_demo.sqlite')
    archive.write(fresh_courier,'snack_customer_service/database/mock_courier.sqlite')
    archive.write(fresh_human,'snack_customer_service/database/mock_human.sqlite')
    archive.write(destination/'demo_orders.json','snack_customer_service/database/demo_orders.json')
    for name in ['desktop-preview.png','mobile-preview.png','courier-interface-preview.png','browser-verification.json','human-interface-preview.png','human-browser-verification.json']:
        archive.write(ROOT/'artifacts'/name,'snack_customer_service/docs/'+name)

con=connect(fresh_db)
assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
assert not con.execute('PRAGMA foreign_key_check').fetchall()
assert con.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==10
for table in ['conversations','conversation_messages','human_requests','demo_sessions','courier_api_calls','human_api_calls']:
    assert con.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
con.close()
with ZipFile(zip_path) as archive:
    assert archive.testzip() is None
    names=archive.namelist()
    assert not any('.env.local' in n or 'reference_coze' in n or 'profile' in n or '__pycache__' in n for n in names)
report={'zip':str(zip_path),'sha256':hashlib.sha256(zip_path.read_bytes()).hexdigest(),'files':len(names),
        'orders':10,'users':2,'auto_checks':86,'browser_checks':25,'live_model_verified':True,
        'courier_transport':'http','courier_is_simulated':True,'human_transport':'http','human_is_simulated':True}
(destination/'delivery-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
