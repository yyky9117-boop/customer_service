"""本机浏览器验收脚本，交付运行不依赖此文件或 websocket 包。"""
import base64
import json
import os
import time
import urllib.request
from pathlib import Path
import websocket

ROOT=Path(__file__).resolve().parent
http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
debug_port=int(os.environ.get('SNACK_BROWSER_DEBUG_PORT','9235'))
deadline=time.monotonic()+10
page=None
while time.monotonic()<deadline:
    try:pages=json.loads(http.open(f'http://127.0.0.1:{debug_port}/json/list',timeout=2).read())
    except urllib.error.URLError:pages=[]
    page=next((p for p in pages if p.get('type')=='page' and '8765' in p.get('url','')),None)
    if page:break
    time.sleep(0.1)
if page is None:raise RuntimeError('浏览器尚未打开客服页面')
ws=websocket.create_connection(page['webSocketDebuggerUrl'],suppress_origin=True,timeout=100)
sequence=0
errors=[]

def cdp(method,params=None):
    global sequence
    sequence+=1
    ws.send(json.dumps({'id':sequence,'method':method,'params':params or {}}))
    while True:
        result=json.loads(ws.recv())
        if result.get('method')=='Runtime.exceptionThrown':errors.append(result['params'])
        if result.get('id')==sequence:
            if 'error' in result:raise RuntimeError(result['error'])
            return result.get('result',{})

def evaluate(expression):
    result=cdp('Runtime.evaluate',{'expression':expression,'awaitPromise':True,'returnByValue':True})
    if result.get('exceptionDetails'):raise AssertionError(result['exceptionDetails'])
    return result.get('result',{}).get('value')

def wait_for(expression):
    return evaluate('(async()=>{for(let i=0;i<750;i++){if('+expression+')return true;await new Promise(r=>setTimeout(r,100));}throw new Error("UI timeout")})()')

def send(text):
    evaluate("document.querySelector('#message-input').value="+json.dumps(text)+";document.querySelector('#chat-form').requestSubmit();true")
    wait_for('!busy && !document.querySelector(".typing")')
    return evaluate('document.querySelector("#messages").innerText')

def screenshot(name):
    evaluate('document.querySelector("#toast").hidden=true;true')
    time.sleep(0.25)
    png=cdp('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})['data']
    (ROOT/name).write_bytes(base64.b64decode(png))

checks=[]
cdp('Runtime.enable')
cdp('Page.enable')
wait_for('document.querySelectorAll(".order-item").length===9')
checks.append('用户 A 只展示自己的 9 笔订单')
text=send('我买的牛肉干物流到哪了')
assert '匹配到多笔订单' in text
assert evaluate('document.querySelectorAll(".candidate-button").length')==2
checks.append('商品查询触发多笔订单选择')
evaluate('Array.from(document.querySelectorAll(".candidate-button")).find(b=>b.innerText.includes("SN-DEMO-0002")).click();true')
wait_for('!busy')
assert '运输中' in evaluate('document.querySelector("#messages").innerText')
assert 'DEMO-TRACK-1002' in evaluate('document.querySelector("#order-detail").innerText')
checks.append('点击候选后，物流回复与订单卡片一致')
assert '1 次 HTTP 调用' in evaluate('document.querySelector("#courier-calls").innerText')
assert 'HTTP 200' in evaluate('document.querySelector("#courier-calls").innerText')
assert evaluate('document.querySelectorAll("#order-detail .tracking-event").length')>=3
checks.append('物流接口调用、HTTP 状态和多节点轨迹实际显示')
assert '2026-10-08 18:00' in send('那它什么时候到')
checks.append('连续追问保留订单并读取有来源的预计送达时间')
screenshot('desktop-preview.png')
evaluate('document.querySelector("#courier-scenario").value="delivering";document.querySelector("#apply-scenario").click();true')
wait_for('!busy && document.querySelector("#messages .message:last-child").innerText.includes("正在派送")')
checks.append('操作面板推进物流后重新调用接口，回复更新为派送中')
evaluate('document.querySelector("#courier-scenario").value="delivered";document.querySelector("#apply-scenario").click();true')
wait_for('!busy && document.querySelector("#messages .message:last-child").innerText.includes("已签收")')
checks.append('签收场景取消预计送达，展示签收轨迹')
evaluate('document.querySelector("#courier-calls .call-detail").open=true;document.querySelector(".order-panel").scrollTop=440;true')
screenshot('courier-interface-preview.png')
evaluate('document.querySelector("#courier-fault").value="logistics_timeout";true')
assert '本次物流查询未成功' in send('物流到哪了')
assert '接口超时' in evaluate('document.querySelector("#courier-calls").innerText')
assert '已发货' in evaluate('document.querySelector("#messages .message:last-child").innerText')
checks.append('真实 HTTP 超时显示失败，保留已发货事实')
evaluate('document.querySelector("#courier-fault").value="logistics_error";true')
assert '本次物流查询未成功' in send('物流到哪了')
assert 'HTTP 503' in evaluate('document.querySelector("#courier-calls").innerText')
checks.append('接口错误显示实际 HTTP 503')
evaluate('document.querySelector("#courier-fault").value="logistics_malformed";true')
assert '本次物流查询未成功' in send('物流到哪了')
assert '响应格式异常' in evaluate('document.querySelector("#courier-calls").innerText')
checks.append('非法 JSON 不进入物流回复或订单卡片')
evaluate('document.querySelector("#courier-scenario").value="default";document.querySelector("#apply-scenario").click();true')
wait_for('!busy && document.querySelector("#messages .message:last-child").innerText.includes("运输中")')
send('SN-DEMO-0008物流到哪了')
assert '2 次 HTTP 调用' in evaluate('document.querySelector("#courier-calls").innerText')
checks.append('一单两包裹产生两次独立接口调用')
send('SN-DEMO-0002发货了吗')
assert '本次未调用快递接口' in evaluate('document.querySelector("#courier-calls").innerText')
checks.append('只询问发货状态时不调用快递接口')
text=send('SN-DEMO-0007 发货了吗')
assert '暂未确认' in text
evaluate('document.querySelector("#messages .message:last-child .candidate-button").click();true')
wait_for('document.querySelector("#messages").innerText.includes("请求编号：HR-")')
assert '当前排队' in evaluate('document.querySelector("#messages").innerText')
requests=evaluate('(async()=>await (await fetch("/api/human/requests")).json())()')['requests']
assert requests and requests[0]['order_id']=='SN-DEMO-0007' and requests[0]['status']=='queued'
checks.append('未知状态通过实际人工接口创建排队工单并保存记录')
evaluate('document.querySelector("#account-select").value="demo_user_b";document.querySelector("#account-select").dispatchEvent(new Event("change"));true')
wait_for('document.querySelectorAll(".order-item").length===1 && !document.querySelector("#account-select").disabled')
assert 'SN-DEMO-0010' in evaluate('document.querySelector("#order-list").innerText')
text=send('SN-DEMO-0002')
assert '没有找到' in text
checks.append('切换用户 B 后仅显示 1 笔订单，查询 A 的订单无结果')
cdp('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
evaluate('document.querySelector(".order-panel").scrollTop=0;true')
assert evaluate('document.documentElement.scrollWidth<=window.innerWidth')
screenshot('mobile-preview.png')
checks.append('390px 手机视口无横向溢出')
assert not errors,errors
health=evaluate('(async()=>await (await fetch("/api/health")).json())()')
report={'passed':len(checks),'checks':checks,'browser_errors':errors,'model_live_verified':health['model']['connection_verified'],
        'courier_transport':'http','courier_is_simulated':True}
(ROOT/'browser-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
cdp('Browser.close')
ws.close()
