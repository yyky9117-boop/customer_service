"""真实页面验证订单模块的话术检索、生成及独立调试，不伪造模型结果。"""
from pathlib import Path

helpers=Path(__file__).resolve().parent/'browser_qa.py'
exec(compile(helpers.read_text(encoding='utf-8').split('checks=[]')[0],str(helpers),'exec'))
checks=[]
cdp('Runtime.enable')
cdp('Page.enable')
wait_for('document.querySelectorAll(".order-item").length===9')
reply=send('SN-DEMO-0001怎么还没有发货，我有点着急')
assert '亲～' in reply and '😊' in reply and '尚未发货' in reply
assert not any(word in reply for word in ('已通知仓库','已催单','一定送到','退款成功'))
checks.append('待发货追问生成自然回复，保留事实且不虚构催单操作')
evaluate('document.querySelector("#reply-trace").parentElement.open=true;true')
trace=evaluate('document.querySelector("#reply-trace").innerText')
assert 'DeepSeek 已生成回复 · 独立质检通过' in trace
assert 'waiting_shipment_urgent' in trace
evaluate('document.querySelectorAll("#reply-trace details").forEach(d=>d.open=true);true')
trace=evaluate('document.querySelector("#reply-trace").innerText')
assert '补建演示库' in trace and 'order-reply-demo-v1' in trace
checks.append('独立调试面板显示场景、命中话术、版本来源及模型质检')
assert 'DeepSeek' not in evaluate('document.querySelector("#messages .message:last-child .bubble").innerText')
checks.append('普通聊天消息保持客服表达，检索与生成详情位于独立面板')
runs=evaluate('(async()=>await (await fetch("/api/reply/runs")).json())()')['runs']
latest=runs[0]
assert latest['scene']=='unshipped' and latest['review_passed'] and latest['mode']=='deepseek_grounded_reply'
checks.append('页面回复对应数据库中实际生成和质检记录')
evaluate('document.querySelector(".order-panel").scrollTop=document.querySelector("#reply-trace").parentElement.offsetTop;true')
screenshot('reply-interface-preview.png')
reply=send('SN-DEMO-0002物流到哪，预计什么时候能到')
assert '运输中' in reply and '2026-10-08 18:00' in reply
assert 'HTTP 200' in evaluate('document.querySelector("#courier-calls").innerText')
checks.append('物流与预计送达同时询问时保留本轮快递接口事实和预计时间')
assert evaluate('humanRequestId')==''
checks.append('订单回复生成不自动创建人工工单')
evaluate('document.querySelector("#new-chat").click();true')
assert '咨询后可查看' in evaluate('document.querySelector("#reply-trace").innerText')
reply=send('我买的牛肉干物流到哪了')
assert '匹配到多笔订单' in reply and evaluate('document.querySelectorAll(".candidate-button").length')==2
checks.append('新会话清除旧调试记录，多笔订单仍可通过按钮选择')
cdp('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
evaluate('document.querySelector(".order-panel").scrollTop=0;true')
assert evaluate('document.documentElement.scrollWidth<=window.innerWidth')
screenshot('reply-mobile-preview.png')
checks.append('回复与调试面板在390px手机视口无横向溢出')
evaluate('document.querySelector("#account-select").value="demo_user_b";document.querySelector("#account-select").dispatchEvent(new Event("change"));true')
wait_for('document.querySelectorAll(".order-item").length===1 && !document.querySelector("#account-select").disabled')
assert 'waiting_shipment_urgent' not in evaluate('document.querySelector("#reply-trace").innerText')
b_runs=evaluate('(async()=>await (await fetch("/api/reply/runs")).json())()')['runs']
assert all(r['run_id']!=latest['run_id'] for r in b_runs)
checks.append('切换账号清空回复调试面板，生成记录仅可查看本账号')
assert not errors,errors
report={'passed':len(checks),'checks':checks,'browser_errors':errors,'live_model_used':True,'module':'order_query','knowledge_source':'reconstructed_demo'}
(ROOT/'reply-browser-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
cdp('Browser.close')
ws.close()
