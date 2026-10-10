// 使用本机独立测试 Chrome 的 DevTools 协议，不读取日常浏览器资料。
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const phase=process.argv[2] || 'welcome';
const targets=await fetch('http://127.0.0.1:9222/json/list').then(r=>r.json());
const target=targets.find(t=>t.type==='page');
if(!target)throw new Error('测试浏览器尚未启动');
const ws=new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
let sequence=0;const pending=new Map();const consoleErrors=[];const checks=[];
ws.addEventListener('message',event=>{
  const data=JSON.parse(event.data);
  if(data.id){const task=pending.get(data.id);if(task){clearTimeout(task.timer);pending.delete(data.id);data.error?task.reject(new Error(data.error.message)):task.resolve(data.result);}}
  else if(data.method==='Runtime.exceptionThrown')consoleErrors.push(data.params.exceptionDetails.text);
  else if(data.method==='Log.entryAdded'&&data.params.entry.level==='error')consoleErrors.push(data.params.entry.text);
});
function cdp(method,params={}){return new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error(`CDP 超时：${method}`));},30000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(result.exceptionDetails.text);return result.result.value;}
async function wait(expression,timeout=45000){const deadline=Date.now()+timeout;while(Date.now()<deadline){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,200));}throw new Error(`等待页面状态超时：${expression}`);}
async function screenshot(name){const result=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});const folder=path.join(root,'examples','screenshots');await fs.mkdir(folder,{recursive:true});const target=path.join(folder,name+'.png');await fs.writeFile(target,Buffer.from(result.data,'base64'));return target;}
async function fill(text){await evaluate(`(()=>{const n=document.getElementById('message-input');n.value=${JSON.stringify(text)};n.dispatchEvent(new Event('input',{bubbles:true}));})()`);}
async function click(selector){await evaluate(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n)throw new Error('button missing');n.click();})()`);}
async function send(text){await fill(text);await click('#send-button');await wait(`!document.getElementById('send-button').hidden`,210000);let value=await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1'))`);if(value.pending?.failure?.includes('暂时无法连接模型服务')){record('transient_provider_connection_retried_with_original_id',{request_id:value.pending.request.request_id});await click('.failure-box button');await wait(`!document.getElementById('send-button').hidden`,210000);value=await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1'))`);}if(value.pending)throw new Error(value.pending.failure || '客服响应失败');return value;}
function record(name,detail){checks.push({name,passed:true,detail});console.log(JSON.stringify({check:name,detail}));}

try{
  await cdp('Runtime.enable');await cdp('Log.enable');await cdp('Page.enable');
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  if(phase==='welcome'){
    await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    await cdp('Page.navigate',{url:'http://127.0.0.1:8787/'});
    await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    const layout=await evaluate(`({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,welcome:!document.getElementById('welcome').hidden,sendDisabled:document.getElementById('send-button').disabled})`);
    assert.equal(layout.scrollWidth,layout.width);assert.equal(layout.welcome,true);assert.equal(layout.sendDisabled,true);
    record('desktop_welcome_and_empty_input',layout);
    record('desktop_screenshot',await screenshot('desktop-welcome'));
    await click('.starter');
    assert.match(await evaluate(`document.getElementById('message-input').value`),/办公室/);
    assert.equal(await evaluate(`document.querySelectorAll('.turn').length`),0);
    record('starter_fills_editable_draft_without_sending',true);
    await fill('');
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await wait(`innerWidth===390`);
    assert.equal(await evaluate(`document.documentElement.scrollWidth`),390);
    await click('#menu-toggle');assert.equal(await evaluate(`document.getElementById('sidebar').classList.contains('open')`),true);
    await click('#sidebar-scrim');assert.equal(await evaluate(`document.getElementById('sidebar').classList.contains('open')`),false);
    record('mobile_layout_and_sidebar',true);record('mobile_screenshot',await screenshot('mobile-welcome'));
    await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  }else if(phase==='phone'){
    await cdp('Page.navigate',{url:'http://127.0.0.1:8787/'});
    await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    const usageBefore=await evaluate(`fetch('/api/bootstrap').then(r=>r.json()).then(v=>v.model_usage)`);
    async function latest(){return evaluate(`fetch('/api/conversation?id='+JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId).then(r=>r.json()).then(d=>d.history.at(-1))`);}
    async function profile(value){await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value=${JSON.stringify(value)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`);}
    await profile('U_NEW');
    assert.match(await evaluate(`document.getElementById('message-input').placeholder`),/手机号/);
    await click('.starter[data-prompt*="下单手机号"]');
    assert.match(await evaluate(`document.getElementById('message-input').value`),/13800000001/);
    assert.equal(await evaluate(`document.querySelectorAll('.turn').length`),0);
    record('phone_starter_and_input_hint_are_visible',true);
    await send('这是13800000001，帮我看一下');const candidates=await latest();
    assert.equal(candidates.module,'order_query');assert.equal(candidates.status,'needs_clarification');
    assert.match(candidates.content,/共匹配到9笔/);assert.match(candidates.content,/最近5笔/);
    const buttons=await evaluate(`Array.from(document.querySelectorAll('.turn.assistant:last-child .quick-replies button')).map(b=>b.textContent)`);
    assert.equal(buttons.length,5);assert.ok(buttons.includes('查询订单 SN-DEMO-0009'));
    record('phone_queries_external_demo_database_and_lists_five_of_nine_orders',{content:candidates.content,buttons});
    record('phone_candidate_screenshot',await screenshot('phone-order-candidates'));
    await click('.turn.assistant:last-child .quick-replies button');await click('#send-button');
    await wait(`!document.getElementById('send-button').hidden`,210000);
    const chosen=await latest();assert.equal(chosen.status,'completed');assert.match(chosen.content,/SN-DEMO-0009/);assert.match(chosen.content,/待付款/);
    record('phone_candidate_button_selects_pending_payment_order',chosen.content);
    await send('查询订单 SN-DEMO-0002 物流到哪了');const tracked=await latest();
    assert.equal(tracked.status,'completed');assert.match(tracked.content,/SN-DEMO-0002/);assert.match(tracked.content,/本机模拟快递/);
    record('select_order_outside_recent_five_and_query_mock_logistics',tracked.content);
    await send('发货了吗');const follow=await latest();assert.match(follow.content,/SN-DEMO-0002/);assert.match(follow.content,/已发货/);
    record('phone_query_selected_order_supports_followup',follow.content);
    await send('电话：1380000000');const invalid=await latest();assert.equal(invalid.status,'needs_clarification');assert.match(invalid.content,/11位/);assert.doesNotMatch(invalid.content,/SN-DEMO-/);
    record('invalid_phone_is_explained_without_old_order_fallback',invalid.content);
    await send('13800000001');const corrected=await latest();assert.equal(corrected.status,'needs_clarification');assert.match(corrected.content,/共匹配到9笔/);
    record('bare_corrected_phone_returns_candidate_orders',corrected.content);
    await send('13800000001 或 13800000002');const multiple=await latest();assert.equal(multiple.status,'needs_clarification');assert.match(multiple.content,/多个手机号/);assert.doesNotMatch(multiple.content,/SN-DEMO-/);
    record('two_phone_numbers_clarify_before_order_lookup',multiple.content);
    await send('13800000003');const missing=await latest();assert.equal(missing.status,'no_match');assert.doesNotMatch(missing.content,/SN-DEMO-/);
    record('valid_unknown_phone_does_not_reuse_previous_candidates',missing.content);
    await profile('U_RETURN');await send('13800000002');const sole=await latest();assert.equal(sole.status,'completed');assert.match(sole.content,/SN-DEMO-0010/);
    record('account_b_phone_single_match_returns_order_detail',sole.content);
    await send('13800000001');const denied=await latest();assert.equal(denied.status,'no_match');assert.doesNotMatch(denied.content,/SN-DEMO-/);
    record('phone_cannot_expand_account_ownership',denied.content);
    await cdp('Page.reload');await wait(`document.querySelectorAll('.turn.assistant').length===2`);
    assert.match(await evaluate(`document.querySelector('.turn.assistant:last-child .message-meta').textContent`),/订单客服/);
    record('phone_order_history_restores_after_reload',true);
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    assert.equal(await evaluate(`document.documentElement.scrollWidth`),390);
    record('phone_order_chat_fits_mobile',await screenshot('phone-order-mobile'));
    const usageAfter=await evaluate(`fetch('/api/bootstrap').then(r=>r.json()).then(v=>v.model_usage)`);
    record('actual_provider_usage',{before:usageBefore,after:usageAfter,reported_tokens_delta:usageAfter.reported_total_tokens-usageBefore.reported_total_tokens});
  }else if(phase==='live'){
    await cdp('Page.reload');await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    await click('#new-chat');
    const first=await send('办公室吃，必须独立小包，不辣，不要坚果，不要临期，最好低糖，买三件，含运费最多60元');
    const cards=await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId && Array.from(document.querySelectorAll('.plan-card')).map(c=>({title:c.querySelector('.plan-heading strong').textContent,total:c.querySelector('.total-price').textContent,skus:Array.from(c.querySelectorAll('.product-row')).map(i=>i.dataset.skuId)}))`);
    assert.equal(first.revision,1);assert.equal(cards.length,3);assert.match(cards[0].total,/44\.70/);assert.deepEqual(cards[0].skus,['P001']);
    record('real_deepseek_to_inline_recommendation_cards',{revision:first.revision,cards});
    await new Promise(r=>setTimeout(r,350));record('recommendation_screenshot',await screenshot('desktop-recommendation'));
    const second=await send('预算改成含运费40元，其他条件保留');assert.equal(second.revision,2);
    const latest=await evaluate(`fetch('/api/conversation?id='+JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId).then(r=>r.json()).then(d=>d.history.at(-1))`);
    assert.ok(latest.cards.length>0);assert.ok(latest.cards.every(c=>c.pricing.total_payable_fen<=4000&&c.items.reduce((s,i)=>s+i.quantity,0)===3));
    record('real_multi_turn_budget_and_quantity_preserved',latest.cards.map(c=>c.pricing.total_payable_fen));
    await cdp('Page.reload');await wait(`document.querySelectorAll('.plan-card').length>=3`);
    assert.equal(await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).revision`),2);
    record('refresh_restores_committed_conversation',true);
    await click('.turn.assistant:last-child .product-select');
    assert.equal(await evaluate(`document.getElementById('reference-chip').hidden`),false);
    const selected=await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).reference`);assert.ok(selected.sku_id);
    const third=await send('这款商品改成只买两件，含运费预算仍然40元，其他条件不变');assert.equal(third.revision,3);
    const chosen=await evaluate(`fetch('/api/conversation?id='+JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId).then(r=>r.json()).then(d=>d.history.at(-1))`);
    assert.ok(chosen.cards.length>0);assert.ok(chosen.cards.every(c=>c.items.length===1&&c.items[0].sku_id===selected.sku_id&&c.items[0].quantity===2));
    record('real_card_reference_and_repricing',{sku:selected.sku_id,totals:chosen.cards.map(c=>c.pricing.total_payable_fen)});
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await evaluate(`document.querySelector('.turn.assistant:last-child').scrollIntoView({block:'start'})`);
    await new Promise(r=>setTimeout(r,350));assert.equal(await evaluate(`document.documentElement.scrollWidth`),390);
    record('mobile_recommendation_screenshot',await screenshot('mobile-recommendation'));
    await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    await click('#new-chat');await send('推荐点零食');
    assert.equal(await evaluate(`document.querySelector('.message-status')?.textContent`),'还差一点信息');assert.equal(await evaluate(`document.querySelectorAll('.plan-card').length`),0);
    record('real_necessary_clarification_without_cards',true);
    await click('#new-chat');await send('只要无糖的零食礼盒，必须礼盒包装，含运费最多10元');
    assert.equal(await evaluate(`document.querySelector('.message-status')?.textContent`),'暂未匹配');assert.equal(await evaluate(`document.querySelectorAll('.plan-card').length`),0);
    record('real_no_match_without_invented_cards',true);
  }else if(phase==='orders'){
    await cdp('Page.navigate',{url:'http://127.0.0.1:8787/'});
    await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    await evaluate(`(()=>{window.__orderRequests=[];const original=window.fetch;window.fetch=(input,options)=>{if(input==='/api/chat')window.__orderRequests.push(JSON.parse(options.body));return original(input,options);};})()`);
    const before=await evaluate(`fetch('/api/bootstrap').then(r=>r.json()).then(v=>v.model_usage)`);
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_GUEST';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await fill('查询订单 SN-DEMO-0001');await click('#send-button');await wait(`document.querySelector('.failure-box')`);
    assert.match(await evaluate(`document.querySelector('.failure-box').textContent`),/有订单绑定的演示身份/);
    record('guest_order_access_is_rejected_without_fabricated_result',true);
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_NEW';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    async function latest(){return evaluate(`fetch('/api/conversation?id='+JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId).then(r=>r.json()).then(d=>d.history.at(-1))`);}
    const first=await send('查询订单 SN-DEMO-0001 是否发货');const shipping=await latest();
    assert.equal(shipping.module,'order_query');assert.match(shipping.content,/尚未发货/);assert.match(shipping.content,/SN-DEMO-0001/);
    assert.ok(!/点击.{0,18}人工|联系人工(?:按钮|入口)/.test(shipping.content));assert.equal(shipping.cards.length,0);
    record('order_shipping_reply_respects_current_ui_capabilities',{request_id:await evaluate(`window.__orderRequests.at(-1).request_id`),content:shipping.content});
    const second=await send('发货了吗');assert.equal(second.conversationId,first.conversationId);assert.equal(second.revision,2);
    const follow=await latest();assert.match(follow.content,/SN-DEMO-0001/);record('order_followup_keeps_selected_order',follow.content);
    await send('查询订单 SN-DEMO-0002 物流到哪了');const logistics=await latest();
    assert.match(logistics.content,/本机模拟快递/);assert.match(logistics.content,/SN-DEMO-0002/);
    record('logistics_reply_uses_actual_mock_http_call',{request_id:await evaluate(`window.__orderRequests.at(-1).request_id`),content:logistics.content});
    record('order_chat_desktop_screenshot',await screenshot('order-integration-desktop'));
    await click('#new-chat');await send('查我之前买的薯片订单');const candidates=await latest();
    assert.equal(candidates.status,'needs_clarification');
    const choices=await evaluate(`Array.from(document.querySelectorAll('.turn.assistant:last-child .quick-replies button')).map(b=>b.textContent)`);
    assert.ok(choices.length>=2);record('candidate_orders_have_selection_buttons',choices);
    await send('第二笔');const selected=await latest();assert.equal(selected.status,'completed');assert.match(selected.content,/SN-DEMO-0001/);
    record('ordinal_selects_from_current_order_candidates',selected.content);
    await cdp('Page.reload');await wait(`document.querySelectorAll('.turn.assistant').length===2`);
    assert.match(await evaluate(`document.querySelector('.turn.assistant:last-child .message-meta').textContent`),/订单客服/);
    record('order_history_restored_after_refresh',true);
    await send('查订单，同时推荐低糖零食');const mixed=await latest();
    assert.equal(mixed.module,'conversation_router');assert.equal(mixed.status,'needs_clarification');
    assert.equal(await evaluate(`document.querySelectorAll('.turn.assistant:last-child .quick-replies button').length`),2);
    record('mixed_request_does_not_silently_drop_one_module',mixed.content);
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_RETURN';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await send('查询订单 SN-DEMO-0001');const denied=await latest();assert.equal(denied.status,'no_match');assert.ok(!denied.content.includes('尚未发货'));
    record('account_b_cannot_query_account_a_order',denied.content);
    await send('查询订单 SN-DEMO-0010 是否发货');const own=await latest();assert.equal(own.status,'completed');assert.match(own.content,/SN-DEMO-0010/);
    record('account_b_can_query_its_own_order',own.content);
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    assert.equal(await evaluate(`document.documentElement.scrollWidth`),390);record('order_chat_mobile_screenshot',await screenshot('order-integration-mobile'));
    const after=await evaluate(`fetch('/api/bootstrap').then(r=>r.json()).then(v=>v.model_usage)`);
    record('model_usage_for_order_browser_checks',{before,after,requests:await evaluate(`window.__orderRequests || []`)});
  }else if(phase==='integrated'){
    await cdp('Page.navigate',{url:'http://127.0.0.1:8787/'});
    await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    const usageBefore=await evaluate(`fetch('/api/bootstrap').then(r=>r.json()).then(v=>v.model_usage)`);
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_NEW';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    const rec=await send('买两件小零食，尽量便宜，不辣，不要坚果，含运费最多20元');
    async function latest(){return evaluate(`fetch('/api/conversation?id='+JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId).then(r=>r.json()).then(d=>d.history.at(-1))`);}
    const first=await latest();assert.equal(first.module,'product_recommendation');assert.ok(first.cards.length);
    assert.ok(first.cards.every(c=>c.items.every(i=>!['P024','P025'].includes(i.sku_id))));
    assert.ok(first.cards.every(c=>c.items.reduce((n,i)=>n+i.quantity,0)===2&&c.pricing.total_payable_fen<=2000));
    assert.ok(first.content.startsWith('亲～'));assert.ok(first.cards.every(c=>c.reasons.length>=2));
    record('recommendation_without_expiry_requirement_excludes_near_expiry',{revision:rec.revision,plans:first.cards.map(c=>({items:c.items.map(i=>i.sku_id),total:c.pricing.total_payable_fen,reasons:c.reasons}))});
    const order=await send('查询订单 SN-DEMO-0001 是否发货');const orderMessage=await latest();
    assert.equal(order.conversationId,rec.conversationId);assert.equal(orderMessage.module,'order_query');assert.equal(orderMessage.cards.length,0);
    assert.match(orderMessage.content,/SN-DEMO-0001/);assert.match(orderMessage.content,/尚未发货/);
    assert.match(await evaluate(`document.querySelector('.turn.assistant:last-child .message-meta').textContent`),/订单客服/);
    record('same_chat_routes_order_and_renders_order_reply',{revision:order.revision,content:orderMessage.content});
    await send('查询订单 SN-DEMO-0002 物流到哪了');const logistics=await latest();
    assert.equal(logistics.module,'order_query');assert.match(logistics.content,/本机模拟快递/);assert.match(logistics.content,/SN-DEMO-0002/);
    record('order_logistics_uses_current_mock_http_response',{content:logistics.content});
    const resumed=await send('继续刚才的推荐，条件不变');const resumedMessage=await latest();
    assert.equal(resumedMessage.module,'product_recommendation');assert.ok(resumedMessage.cards.length);
    assert.ok(resumedMessage.cards.every(c=>c.items.reduce((n,i)=>n+i.quantity,0)===2&&c.pricing.total_payable_fen<=2000));
    assert.ok(resumedMessage.cards.every(c=>c.items.every(i=>i.sku_id!=='P024')));
    record('return_to_recommendation_preserves_budget_and_quantity',{revision:resumed.revision,totals:resumedMessage.cards.map(c=>c.pricing.total_payable_fen)});
    record('integrated_chat_screenshot',await screenshot('integrated-chat-desktop'));
    await send('查询订单 SN-DEMO-0001，同时推荐低糖零食');const mixed=await latest();
    assert.equal(mixed.module,'conversation_router');assert.equal(mixed.status,'needs_clarification');assert.equal(mixed.cards.length,0);
    assert.equal(await evaluate(`document.querySelectorAll('.turn.assistant:last-child .quick-replies button').length`),2);
    record('mixed_intent_clarification_has_two_choices',mixed.content);
    await click('.turn.assistant:last-child .quick-replies button');assert.match(await evaluate(`document.getElementById('message-input').value`),/先查询订单/);
    await click('#new-chat');await send('查我之前买的薯片订单');const candidates=await latest();
    assert.equal(candidates.module,'order_query');assert.equal(candidates.status,'needs_clarification');
    const choices=await evaluate(`Array.from(document.querySelectorAll('.turn.assistant:last-child .quick-replies button')).map(b=>b.textContent)`);
    assert.ok(choices.length>=2);record('matching_order_candidates_are_selectable',choices);
    await send('第二笔');const selected=await latest();assert.equal(selected.status,'completed');assert.match(selected.content,/SN-DEMO-/);
    record('ordinal_selects_order_from_current_candidates',selected.content);
    const cid=await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId`);
    await cdp('Page.reload');await wait(`document.querySelectorAll('.turn.assistant').length===2`);
    assert.equal(await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId`),cid);
    assert.match(await evaluate(`document.querySelector('.turn.assistant:last-child .message-meta').textContent`),/订单客服/);
    record('refresh_restores_order_history_and_module_label',cid);
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    assert.equal(await evaluate(`document.documentElement.scrollWidth`),390);
    record('integrated_chat_mobile_screenshot',await screenshot('integrated-chat-mobile'));
    const usageAfter=await evaluate(`fetch('/api/bootstrap').then(r=>r.json()).then(v=>v.model_usage)`);
    record('actual_provider_usage',{before:usageBefore,after:usageAfter,reported_tokens_delta:usageAfter.reported_total_tokens-usageBefore.reported_total_tokens});
  }else if(phase==='reply'){
    await cdp('Page.navigate',{url:'http://127.0.0.1:8787/'});
    await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    await evaluate(`(()=>{window.__replyRequests=[];const original=window.fetch;window.fetch=(input,options)=>{if(input==='/api/chat')window.__replyRequests.push(JSON.parse(options.body));return original(input,options);};})()`);
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_GUEST';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    async function latest(){return evaluate(`fetch('/api/conversation?id='+JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId).then(r=>r.json()).then(d=>d.history.at(-1))`);}
    function checkReply(message){
      assert.ok(message.content.startsWith('亲～'));assert.equal(message.content.split('😊').length-1,1);
      assert.ok(!message.content.includes('以下是可选方案'));assert.ok(!message.content.includes('商品应付和运费分别列出'));
      for(const card of message.cards){assert.ok(card.reasons.length>=2&&card.reasons.length<=3);for(const item of card.items)assert.ok(message.content.includes(item.product_name));if(card.pricing.total_payable_fen!==null)assert.ok(message.content.includes((card.pricing.total_payable_fen/100).toFixed(2)));}
    }
    await click('#new-chat');await send('办公室吃，必须独立小包，不辣，不要坚果，不要临期，最好低糖，买三件，含运费最多60元');
    const first=await latest();checkReply(first);assert.equal(first.cards[0].pricing.total_payable_fen,4470);
    record('real_generated_reasons_customer_reply_and_exact_prices',{content:first.content,reasons:first.cards.map(c=>c.reasons)});
    await new Promise(r=>setTimeout(r,500));record('natural_reply_desktop_screenshot',await screenshot('desktop-natural-reply'));
    await click('.turn.assistant:last-child .product-select');
    const selected=await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).reference.sku_id`);
    await send('这款商品改成两件，含运费最多40元，其他条件不变');
    const follow=await latest();checkReply(follow);assert.ok(follow.cards.every(c=>c.items.length===1&&c.items[0].sku_id===selected&&c.items[0].quantity===2));
    record('natural_followup_keeps_reference_and_reprices',{content:follow.content,total:follow.cards[0].pricing.total_payable_fen});
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await evaluate(`document.querySelector('.turn.assistant:last-child').scrollIntoView({block:'start'})`);await new Promise(r=>setTimeout(r,500));
    assert.equal(await evaluate(`document.documentElement.scrollWidth`),390);record('natural_reply_mobile_screenshot',await screenshot('mobile-natural-reply'));
    await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    await click('#new-chat');await send('办公室吃，不辣，买两件，含运费最多20元');
    const lowBudget=await latest();checkReply(lowBudget);assert.ok(lowBudget.cards.every(c=>c.pricing.total_payable_fen<=2000));
    assert.ok(lowBudget.cards.every(c=>c.items.every(i=>i.sku_id!=='P024')&&(c.gifts || []).every(i=>i.sku_id!=='P024')));assert.ok(!lowBudget.content.includes('临期燕麦饼干'));
    record('low_budget_alternatives_with_readable_expiry_notice',{content:lowBudget.content,cards:lowBudget.cards.map(c=>({items:c.items,reasons:c.reasons,total:c.pricing.total_payable_fen}))});
    await new Promise(r=>setTimeout(r,500));record('screenshot_scenario_revised',await screenshot('desktop-low-budget-reply'));
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_UNKNOWN';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await send('只要原味燕麦小饼干，买一件，商品金额最多20元');
    const conditional=await latest();checkReply(conditional);assert.equal(conditional.cards[0].pricing.total_payable_fen,null);assert.ok(conditional.content.includes('待确认'));
    record('real_conditional_shipping_is_not_claimed_as_total',{content:conditional.content});
    const requests=await evaluate(`window.__replyRequests`);record('real_requests_for_server_pipeline_audit',requests.map(r=>({request_id:r.request_id,scope:r.conversation_id || 'new'})));
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_GUEST';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  }else if(phase==='acceptance'){
    await cdp('Page.navigate',{url:'http://127.0.0.1:8787/'});
    await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    await click('#new-chat');
    await send('办公室吃，必须独立小包，不辣，不要坚果，不要临期，三件，含运费最多60元');
    const message=await evaluate(`fetch('/api/conversation?id='+JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId).then(r=>r.json()).then(d=>d.history.at(-1))`);
    assert.ok(message.cards.length);assert.ok(message.content.startsWith('亲～'));
    assert.ok(message.cards.every(c=>c.items.reduce((n,i)=>n+i.quantity,0)===3&&c.pricing.total_payable_fen<=6000));
    record('stage5_real_browser_api_model_data_reply',{content:message.content,cards:message.cards});
    await cdp('Emulation.setDeviceMetricsOverride',{width:320,height:844,deviceScaleFactor:1,mobile:true});
    await wait(`innerWidth===320`);
    await evaluate(`(()=>{const sizes=Array.from(document.querySelectorAll('*')).filter(n=>n instanceof HTMLElement).map(n=>[n,parseFloat(getComputedStyle(n).fontSize)]);for(const [n,size] of sizes)n.style.fontSize=(size*2)+'px';})()`);
    await evaluate(`document.querySelector('.turn.assistant:last-child').scrollIntoView({block:'start'})`);
    await new Promise(r=>setTimeout(r,300));
    const layout=await evaluate(`({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,composer:document.querySelector('.composer').getBoundingClientRect().toJSON(),prices:Array.from(document.querySelectorAll('.total-price')).map(n=>({text:n.textContent,box:n.getBoundingClientRect().toJSON(),font:getComputedStyle(n).fontSize})),input:document.getElementById('message-input').getBoundingClientRect().toJSON()})`);
    record('stage5_320px_200percent_text_screenshot',await screenshot('stage5-320px-text200'));
    assert.equal(layout.scrollWidth,320);assert.ok(layout.prices.every(p=>p.box.right<=320&&p.box.left>=0));
    assert.ok(layout.input.width>100&&layout.input.right<=320&&layout.composer.bottom<=844);
    const header=await evaluate(`({header:document.querySelector('.chat-header').getBoundingClientRect().toJSON(),title:document.querySelector('.header-title').getBoundingClientRect().toJSON()})`);
    assert.ok(header.title.top>=header.header.top&&header.title.bottom<=header.header.bottom);
    record('S11_enlarged_header_does_not_cover_messages',header);
    for(let index=0;index<layout.prices.length;index++){
      await evaluate(`document.querySelectorAll('.total-price')[${index}].scrollIntoView({block:'center'})`);
      const visible=await evaluate(`({price:document.querySelectorAll('.total-price')[${index}].getBoundingClientRect().toJSON(),composer:document.querySelector('.composer-area').getBoundingClientRect().toJSON(),header:document.querySelector('.chat-header').getBoundingClientRect().toJSON()})`);
      assert.ok(visible.price.top>=visible.header.bottom&&visible.price.bottom<=visible.composer.top);
      record('S11_price_can_scroll_into_visible_chat_'+index,visible);
    }
    record('stage5_320px_enlarged_price_screenshot',await screenshot('stage5-320px-price200'));
    record('S11_320px_200percent_text_no_overflow',layout);
    await cdp('Page.reload');await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    await click('#new-chat');
    await evaluate(`(()=>{window.__originalFetch=window.fetch;window.fetch=async(input,options)=>{if(input==='/api/chat'){const response=await window.__originalFetch(input,options);return await new Promise(resolve=>{window.__releaseOldChat=()=>resolve(response);});}return window.__originalFetch(input,options);};})()`);
    await fill('不辣的饼干，必须独立包装，不要临期，含运费20元以内');await click('#send-button');
    await wait(`typeof window.__releaseOldChat==='function'`,210000);
    await click('#new-chat');await fill('新的咨询还没发送');await evaluate(`window.__releaseOldChat()`);
    await new Promise(r=>setTimeout(r,400));
    assert.equal(await evaluate(`document.querySelectorAll('.turn').length`),0);
    assert.equal(await evaluate(`document.getElementById('message-input').value`),'新的咨询还没发送');
    assert.equal(await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId`),null);
    record('S04_late_real_response_does_not_enter_new_chat',true);
    await evaluate(`window.fetch=window.__originalFetch`);
    await click('#new-chat');
  }else if(phase==='faults'){
    await cdp('Page.reload');await wait(`document.querySelector('#connection-label')?.textContent.includes('已配置')`);
    await click('#new-chat');
    await evaluate(`(()=>{window.__originalFetch=window.fetch;window.__faultRequestIds=[];let failed=false;window.fetch=async(input,options)=>{if(input==='/api/chat'){const request=JSON.parse(options.body);window.__faultRequestIds.push(request.request_id);if(!failed){failed=true;return new Response(JSON.stringify({schema_version:'1.0',conversation_id:null,request_id:request.request_id,turn_id:null,base_revision:0,revision:0,module:'product_recommendation',status:'error',message:{id:'msg_injected_error',role:'assistant',content:'测试注入：模型超时，本轮未保存。',cards:[]},clarification:null,error:{code:'model_timeout',retryable:true}}),{status:200,headers:{'Content-Type':'application/json'}});}}return window.__originalFetch(input,options);};})()`);
    const text='不辣的饼干，必须独立包装，不要临期，含运费20元以内';
    await fill(text);await click('#send-button');await wait(`document.querySelector('.failure-box')`);
    assert.equal(await evaluate(`document.getElementById('message-input').value`),text);assert.equal(await evaluate(`document.querySelectorAll('.plan-card').length`),0);
    record('injected_failure_preserves_draft_and_hides_cards',true);record('failure_screenshot',await screenshot('desktop-error'));
    await click('.failure-box button');await wait(`document.querySelectorAll('.plan-card').length>0`,110000);
    const ids=await evaluate(`window.__faultRequestIds`);assert.equal(ids.length,2);assert.equal(ids[0],ids[1]);
    assert.equal(await evaluate(`document.querySelectorAll('.turn.user').length`),1);
    record('retry_reuses_request_id_and_has_one_user_turn',true);
    await evaluate(`window.fetch=window.__originalFetch`);
    const before=await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1'))`);
    await evaluate(`(()=>{let s=JSON.parse(localStorage.getItem('shiwei-chat-v1'));s.pending={request:{schema_version:'1.0',conversation_id:null,request_id:${JSON.stringify(ids[0])},expected_revision:0,message:${JSON.stringify(text)},selected_reference:null},profile:'U_GUEST',local_id:'local_lost_response',failure:'待确认'};s.conversationId=null;s.revision=0;s.draft=${JSON.stringify(text)};localStorage.setItem('shiwei-chat-v1',JSON.stringify(s));})()`);
    await cdp('Page.reload');await wait(`document.querySelectorAll('.plan-card').length>0`);
    const restored=await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1'))`);assert.equal(restored.conversationId,before.conversationId);assert.equal(restored.pending,null);assert.equal(await evaluate(`document.querySelectorAll('.turn.user').length`),1);
    record('lost_response_recovered_from_cache_after_refresh',true);
    await evaluate(`(()=>{let s=JSON.parse(localStorage.getItem('shiwei-chat-v1'));s.pending={request:{schema_version:'1.0',conversation_id:null,request_id:${JSON.stringify(ids[0])},expected_revision:0,message:${JSON.stringify(text)},selected_reference:null},profile:'U_GUEST',local_id:'local_lost_response',failure:'待确认'};s.conversationId=null;s.revision=0;s.draft='下一条还没发送的需求';localStorage.setItem('shiwei-chat-v1',JSON.stringify(s));})()`);
    await cdp('Page.reload');await wait(`document.querySelectorAll('.plan-card').length>0`);
    assert.equal(await evaluate(`document.getElementById('message-input').value`),'下一条还没发送的需求');
    record('cache_recovery_preserves_different_next_draft',true);
    await evaluate(`(()=>{window.__originalFetch=window.fetch;window.fetch=(input,options)=>{if(String(input).startsWith('/api/conversation?'))return new Promise(resolve=>{window.__releaseHistory=()=>window.__originalFetch(input,options).then(resolve);});return window.__originalFetch(input,options);};})()`);
    await click('.conversation-row:not(.active)');await click('#new-chat');await fill('新的需求');
    assert.equal(await evaluate(`document.getElementById('send-button').disabled`),false);
    await evaluate(`window.__releaseHistory()`);await new Promise(r=>setTimeout(r,250));
    assert.equal(await evaluate(`document.querySelectorAll('.turn').length`),0);
    assert.equal(await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).conversationId`),null);
    record('navigation_during_history_load_keeps_new_chat_ready',true);await evaluate(`window.fetch=window.__originalFetch`);
    await evaluate(`(()=>{window.__xss=0;window.__originalFetch=window.fetch;window.fetch=async(input,options)=>{if(input==='/api/chat'){const r=JSON.parse(options.body);return new Response(JSON.stringify({schema_version:'1.0',conversation_id:r.conversation_id,request_id:r.request_id,turn_id:'turn_xss',base_revision:r.expected_revision,revision:r.expected_revision+1,module:'product_recommendation',status:'no_match',message:{id:'msg_xss',role:'assistant',content:'<img src=x onerror="window.__xss=1">',cards:[]},clarification:null,error:null}),{status:200,headers:{'Content-Type':'application/json'}});}return window.__originalFetch(input,options);};})()`);
    await send('测试文本呈现');assert.equal(await evaluate(`window.__xss`),0);assert.equal(await evaluate(`document.querySelectorAll('.assistant-text img').length`),0);
    record('injected_html_is_rendered_as_text',true);await evaluate(`window.fetch=window.__originalFetch`);
    await click('#new-chat');
    await evaluate(`(()=>{window.__originalFetch=window.fetch;window.fetch=(input,options)=>{if(input==='/api/chat')return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}));return window.__originalFetch(input,options);};})()`);
    await fill('这次请求测试停止等待');await click('#send-button');await wait(`!document.getElementById('stop-wait').hidden`);await click('#stop-wait');await wait(`document.querySelector('.failure-box')`);
    assert.match(await evaluate(`document.querySelector('.failure-box').textContent`),/结果待确认/);assert.equal(await evaluate(`document.getElementById('message-input').value`),'这次请求测试停止等待');
    record('injected_stop_wait_preserves_uncertain_request',true);await evaluate(`window.fetch=window.__originalFetch`);await click('#new-chat');
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_VIP';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    assert.equal(await evaluate(`JSON.parse(localStorage.getItem('shiwei-chat-v1')).profile`),'U_VIP');assert.equal(await evaluate(`document.querySelectorAll('.turn').length`),0);
    record('demo_profile_change_starts_new_conversation',true);
    // 测试最后恢复空白的匿名会话，避免将注入结果当作真实对话证据。
    await evaluate(`(()=>{const s=document.getElementById('demo-profile');s.value='U_GUEST';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  }else throw new Error('unknown phase');
  assert.equal(consoleErrors.length,0,JSON.stringify(consoleErrors));record('browser_has_no_script_or_csp_errors',true);
  const folder=path.join(root,'acceptance');const file=path.join(folder,phase==='acceptance'?'stage5-browser.json':`stage4-browser-${phase}.json`);
  await fs.writeFile(file,JSON.stringify({verified_at:new Date().toISOString(),phase,real_model_used:['live','faults','reply','acceptance','integrated','orders','phone'].includes(phase),fault_injection_used:phase==='faults'||phase==='acceptance',all_passed:true,checks,console_errors:consoleErrors},null,2)+'\n');
}catch(error){
  if(phase==='phone')await fs.writeFile(path.join(root,'acceptance/stage4-browser-phone.json'),JSON.stringify({verified_at:new Date().toISOString(),phase,all_passed:false,checks,error:String(error),console_errors:consoleErrors},null,2)+'\n');
  if(phase==='integrated')await fs.writeFile(path.join(root,'acceptance/stage4-browser-integrated.json'),JSON.stringify({verified_at:new Date().toISOString(),phase,all_passed:false,checks,error:String(error),console_errors:consoleErrors},null,2)+'\n');
  if(phase==='orders')await fs.writeFile(path.join(root,'acceptance/stage4-browser-orders.json'),JSON.stringify({verified_at:new Date().toISOString(),phase,all_passed:false,checks,error:String(error),console_errors:consoleErrors},null,2)+'\n');
  if(phase==='acceptance')await fs.writeFile(path.join(root,'acceptance/stage5-browser.json'),JSON.stringify({verified_at:new Date().toISOString(),phase,all_passed:false,checks,error:String(error),console_errors:consoleErrors},null,2)+'\n');
  throw error;
}finally{ws.close();}
