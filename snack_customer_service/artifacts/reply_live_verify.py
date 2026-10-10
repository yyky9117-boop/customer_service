"""实际订单网页后端→DeepSeek→独立质检，输出安全的演示验收结果。"""
import http.cookiejar
import json
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent
http=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(path,body=None):
    data=None if body is None else json.dumps(body,ensure_ascii=False).encode('utf-8')
    request=urllib.request.Request('http://127.0.0.1:8765'+path,data=data,headers={'Content-Type':'application/json'})
    with http.open(request,timeout=100) as response:return json.loads(response.read())


call('/api/demo/login',{'account':'demo_user_a'})
results=[]
for question, expected in [
    ('SN-DEMO-0001怎么还没有发货，我有点着急','尚未发货'),
    ('SN-DEMO-0002物流到哪，预计什么时候能到','2026-10-08 18:00'),
    ('我买的牛肉干物流到哪了','匹配到多笔订单'),
]:
    started=time.monotonic()
    result=call('/api/chat',{'message':question})
    trace=result['reply_status']
    assert expected in result['reply'],result
    assert trace['mode']=='deepseek_grounded_reply' and trace['review_passed'],trace
    assert trace['retrieval'],trace
    assert result['reply'].startswith('亲～') and result['reply'].count('😊')==1
    assert not any(word in result['reply'] for word in ('已通知仓库','已催单','退款成功','一定送到'))
    item={'question':question,'reply':result['reply'],'result_type':result['result_type'],
        'reply_mode':trace['mode'],'scene':trace['scene'],'knowledge_ids':[h['id'] for h in trace['retrieval']],
        'review_passed':trace['review_passed'],'attempts':trace['attempts'],'seconds':round(time.monotonic()-started,2)}
    results.append(item)
    print(json.dumps(item,ensure_ascii=False),flush=True)
runs=call('/api/reply/runs')['runs']
assert len(runs)>=3
report={'passed':True,'live_model_used':True,'cases':results,'reply_records_verified':True,
    'module':'order_query','knowledge_source':'reconstructed_demo'}
(ROOT/'reply-live-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
