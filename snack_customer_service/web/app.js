'use strict';
const $ = selector => document.querySelector(selector);
let conversationId = '';
let humanRequestId = '';
let humanState = '';
let busy = false;
let accountVersion = 0;
let toastTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5000);
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求未成功，请重试。');
  return data;
}
function message(role, text) {
  const row = element('div', 'message ' + role);
  row.append(element('div', 'avatar', role === 'user' ? '我' : '食'));
  const content = element('div', 'message-content');
  content.append(element('div', 'speaker', role === 'user' ? '你' : '小食光 · 订单客服'));
  content.append(element('div', 'bubble', text));
  row.append(content);
  $('#messages').append(row);
  $('#messages').scrollTop = $('#messages').scrollHeight;
  return content;
}
function welcome() {
  $('#messages').replaceChildren();
  const content = message('assistant', '你好，我是小食光订单客服。\n告诉我订单号、快递运单号或商品名称，我帮你核实订单状态。');
  const buttons = element('div', 'welcome-grid');
  for (const [label, text] of [['查我的牛肉干', '我买的牛肉干物流到哪了'], ['查看待发货订单', '查订单 SN-DEMO-0001']]) {
    const button = element('button', '', label);
    button.type = 'button';
    button.addEventListener('click', () => send(text));
    buttons.append(button);
  }
  content.append(buttons);
  renderReplyTrace(null);
}

function renderReplyTrace(trace) {
  const target = $('#reply-trace');
  if (!trace) {
    target.replaceChildren(element('p', '', '咨询后可查看命中的场景话术与生成结果。'));
    return;
  }
  const generated = trace.mode === 'deepseek_grounded_reply';
  const title = element('strong', 'calls-title', generated ? 'DeepSeek 已生成回复 · 独立质检通过' : '使用场景话术兜底');
  const info = element('p', 'call-number', `订单场景：${trace.scene} · 生成尝试 ${trace.attempts} 次 · ${trace.elapsed_ms} ms`);
  target.replaceChildren(title, info);
  if (trace.fallback_reason) target.append(element('p', 'call-number', `兜底原因：${trace.fallback_reason}`));
  for (const hit of trace.retrieval || []) {
    const details = element('details', 'call-detail');
    details.append(element('summary', '', `命中话术：${hit.id} · 评分 ${hit.score}`));
    details.append(element('p', '', hit.guide));
    details.append(element('p', 'call-number', `版本 ${hit.version} · 来源：补建演示库 · 关键词：${(hit.matched_keywords || []).join('、') || '场景匹配'}`));
    target.append(details);
  }
  if (!(trace.retrieval || []).length) target.append(element('p', '', '本轮未取得场景话术，保留已确认的查询结果。'));
}
const shippingNames = { unshipped: '待发货', shipped: '已发货', partially_shipped: '部分发货', unknown: '待核实' };
function statusName(order) {
  if (order.order_status === 'cancelled') return '已取消';
  if (order.order_status === 'pending_payment') return '待付款';
  if (order.order_status === 'completed') return '已完成';
  return shippingNames[order.shipping_status] || '待核实';
}
function money(fen) { return fen === null || fen === undefined ? '暂无金额' : '¥ ' + (Number(fen) / 100).toFixed(2); }
function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(date).replaceAll('/', '-');
}
function renderOrder(order) {
  const box = $('#order-detail');
  box.replaceChildren();
  const header = element('div', 'detail-header');
  header.append(element('h3', '', order.order_id), element('span', 'badge' + (order.shipping_status === 'unknown' ? ' warning' : ''), statusName(order)));
  box.append(header);
  box.append(element('div', 'detail-products', (order.items || []).map(item => item.item_title + ' × ' + item.quantity).join('\n')));
  const meta = element('div', 'meta');
  meta.append(element('span', '', '订单金额'), element('span', '', money(order.amount_fen)));
  box.append(meta);
  for (const shipment of order.shipments || []) {
    const block = element('div', 'tracking-block');
    const title = element('div', 'tracking-title');
    title.append(element('span', '', shipment.carrier_name || '快递公司待确认'), element('span', '', shipment.logistics_status === 'exception' ? '物流异常' : '包裹信息'));
    block.append(title, element('div', 'tracking-number', shipment.tracking_number || '暂未取得运单号'));
    if (shipment.simulation_time) block.append(element('div', 'tracking-number', '模拟业务时间：' + formatTime(shipment.simulation_time)));
    if (shipment.estimated_delivery_at) block.append(element('div', 'tracking-event', '预计 ' + shipment.estimated_delivery_at + ' 送达'));
    const events = shipment.tracking_events || [];
    if (!events.length) block.append(element('div', 'tracking-event', shipment.data_source === 'not_queried' ? '本次未查询物流轨迹，可点击“查物流”。' : shipment.logistics_status === 'query_failed' ? '本次物流查询未成功' : '暂无物流轨迹'));
    for (const event of events) {
      const row = element('div', 'tracking-event', event.status_desc);
      row.append(element('small', '', formatTime(event.status_time)));
      if (event.location) row.append(element('small', '', event.location));
      block.append(row);
    }
    box.append(block);
  }
  if (!(order.shipments || []).length) box.append(element('div', 'tracking-event', order.shipping_status === 'unshipped' ? '发货后可查询包裹物流。' : '暂未取得包裹信息。'));
  const shipments = (order.shipments || []).filter(shipment => shipment.tracking_number);
  $('#courier-tracking').replaceChildren();
  for (const shipment of shipments) {
    const option = element('option', '', shipment.tracking_number);
    option.value = shipment.tracking_number;
    $('#courier-tracking').append(option);
  }
  if (!shipments.length) $('#courier-tracking').append(element('option', '', '当前订单暂无运单'));
  $('#courier-tracking').disabled = !shipments.length;
  $('#apply-scenario').disabled = !shipments.length;
}
function renderCourierCalls(calls, target = '#courier-calls', emptyMessage) {
  const box = $(target);
  box.replaceChildren();
  if (!calls.length) {
    box.append(element('p', '', emptyMessage || '本次未调用快递接口：仅查询发货状态、订单尚未发货，或尚未选定订单。'));
    return;
  }
  const labels = { success: '调用成功', no_records: '暂无轨迹', timeout: '接口超时', http_error: '接口错误', not_found: '运单无结果', invalid_response: '响应格式异常', connection_failed: '连接失败' };
  box.append(element('strong', 'calls-title', '已发起 ' + calls.length + ' 次 HTTP 调用'));
  for (const call of calls) {
    const row = element('details', 'call-detail');
    row.append(element('summary', '', (labels[call.status] || call.status) + ' · ' + call.elapsed_ms + ' ms' + (call.http_status ? ' · HTTP ' + call.http_status : '')));
    row.append(element('p', 'call-number', call.request_id));
    row.append(element('p', 'call-endpoint', call.method + ' ' + call.endpoint));
    row.append(element('span', 'call-section', '请求参数'));
    row.append(element('pre', '', JSON.stringify(call.request, null, 2)));
    row.append(element('span', 'call-section', '接口响应'));
    row.append(element('pre', '', call.response ? JSON.stringify(call.response, null, 2) : '未取得可用响应：' + (labels[call.status] || call.status)));
    box.append(row);
  }
}
function resetDetail() {
  const empty = element('div', 'empty-detail');
  empty.append(element('span', '', '▤'), element('h3', '', '查到的订单会显示在这里'), element('p', '', '订单状态和物流轨迹，都以查询结果为准。'));
  $('#order-detail').replaceChildren(empty);
  $('#courier-tracking').replaceChildren(element('option', '', '先查询一笔有运单的订单'));
  $('#courier-tracking').disabled = true;
  $('#apply-scenario').disabled = true;
}
async function loadOrders() {
  const data = await api('/api/orders');
  $('#order-count').textContent = data.orders.length + ' 笔';
  $('#order-list').replaceChildren();
  for (const order of data.orders) {
    const button = element('button', 'order-item');
    button.type = 'button';
    const info = element('div', 'item-info');
    info.append(element('strong', '', order.items.map(item => item.item_title).join('、')), element('small', '', order.order_id));
    button.append(element('span', 'item-icon', '▧'), info, element('span', 'item-status', statusName(order)));
    button.addEventListener('click', () => send('查询订单 ' + order.order_id));
    $('#order-list').append(button);
  }
}
async function send(text, selectedOrderId) {
  if (busy || !text.trim()) return;
  busy = true;
  $('#send-button').disabled = true;
  $('#account-select').disabled = true;
  $('#message-input').value = '';
  message('user', text);
  const pending = message('assistant', '正在核实订单信息…');
  pending.parentElement.classList.add('typing');
  const version = accountVersion;
  try {
    const result = await api('/api/chat', { message: text, conversation_id: conversationId || undefined, selected_order_id: selectedOrderId, failure_mode: $('#courier-fault').value });
    if (version !== accountVersion) return;
    conversationId = result.conversation_id;
    $('#engine-label').textContent = result.reply_status?.mode === 'deepseek_grounded_reply' ? '智能客服在线' : '客服在线';
    pending.parentElement.remove();
    const content = message('assistant', result.reply);
    renderReplyTrace(result.reply_status || null);
    if (result.orders && result.orders.length) renderOrder(result.orders[0]);
    else resetDetail();
    renderCourierCalls(result.courier_calls || []);
    for (const order of result.candidates || []) {
      const button = element('button', 'candidate-button', order.order_id);
      button.type = 'button';
      button.append(element('small', '', (order.items || []).map(item => item.item_title).join('、')));
      button.addEventListener('click', () => send('选择订单 ' + order.order_id, order.order_id));
      content.append(button);
    }
    if (result.next_action === 'request_human') {
      const button = element('button', 'candidate-button', '转接模拟人工客服');
      button.type = 'button';
      button.addEventListener('click', () => requestHuman(result.conversation_id));
      content.append(button);
    }
    $('#messages').scrollTop = $('#messages').scrollHeight;
  } catch (error) {
    pending.parentElement.remove();
    message('assistant', error.message);
  } finally {
    busy = false;
    $('#send-button').disabled = false;
    $('#account-select').disabled = false;
    $('#apply-scenario').disabled = $('#courier-tracking').disabled || !$('#courier-tracking').value;
    $('#message-input').focus();
  }
}
async function requestHuman(targetConversationId) {
  if (busy) return;
  try {
    if (!targetConversationId && !conversationId) {
      await send('我需要人工客服');
      if (!conversationId) return;
    }
    setHumanBusy(true);
    const result = await api('/api/human/request', { conversation_id: targetConversationId || conversationId, failure_mode: $('#human-fault').value });
    renderHuman(result);
    message('assistant', result.message + '\n请求编号：' + result.request_id + (result.ticket_id ? '\n模拟工单：' + result.ticket_id : ''));
  } catch (error) { toast(error.message); }
  finally { setHumanBusy(false); }
}
const humanStateNames = { not_connected: '尚未连接服务', queued: '排队中', connected: '模拟坐席已接入', closed: '模拟处理完成', failed: '转接失败', unknown: '创建结果待确认' };
function setHumanBusy(value) {
  busy = value;
  $('#send-button').disabled = value;
  $('#account-select').disabled = value;
  $('#human-button').disabled = value;
  $('#refresh-human').disabled = value || !humanRequestId;
  $('#connect-human').disabled = value || humanState !== 'queued';
  $('#close-human').disabled = value || humanState !== 'connected';
}
function resetHuman() {
  humanRequestId = '';
  humanState = '';
  $('#human-ticket').textContent = '点击“联系人工”或回复中的转接入口即可创建工单。';
  $('#human-demo').open = false;
  $('#human-fault').value = '';
  renderCourierCalls([], '#human-calls', '转接后显示实际请求与响应。');
  $('#refresh-human').disabled = true;
  $('#connect-human').disabled = true;
  $('#close-human').disabled = true;
}
function renderHuman(result) {
  humanRequestId = result.request_id;
  humanState = result.status;
  const box = $('#human-ticket');
  box.replaceChildren(element('strong', '', humanStateNames[result.status] || result.status));
  box.append(element('p', '', result.message));
  box.append(element('small', '', '请求：' + result.request_id));
  if (result.ticket_id) box.append(element('small', '', '工单：' + result.ticket_id));
  if (result.order_id) box.append(element('small', '', '关联订单：' + result.order_id));
  const call = (result.human_calls || [])[0];
  const receipt = call && call.response && call.response.data;
  if (result.status_verified && receipt) box.append(element('small', '', '已携带 ' + receipt.forwarded_orders + ' 笔订单、' + receipt.forwarded_messages + ' 条对话。'));
  if (result.updated_at) box.append(element('small', '', '状态更新时间：' + formatTime(result.updated_at)));
  renderCourierCalls(result.human_calls || [], '#human-calls', '本次未取得新的接口调用结果。');
  $('#human-demo').open = true;
  $('#human-demo').scrollIntoView({ block: 'nearest' });
}
async function updateHuman(targetState, requestId = humanRequestId) {
  if (busy || !requestId) return;
  setHumanBusy(true);
  try {
    const result = await api(targetState ? '/api/demo/human-state' : '/api/human/query', { request_id: requestId, status: targetState });
    renderHuman(result);
    if (targetState) message('assistant', result.message);
  } catch (error) { toast(error.message); }
  finally { setHumanBusy(false); }
}
async function login() {
  accountVersion += 1;
  conversationId = '';
  $('#send-button').disabled = true;
  $('#account-select').disabled = true;
  try {
    await api('/api/demo/login', { account: $('#account-select').value });
    welcome();
    resetDetail();
    renderCourierCalls([]);
    resetHuman();
    await loadOrders();
  } catch (error) { toast(error.message); }
  finally { $('#send-button').disabled = false; $('#account-select').disabled = false; }
}
$('#chat-form').addEventListener('submit', event => { event.preventDefault(); send($('#message-input').value); });
$('#message-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(event.currentTarget.value); }
});
$('#account-select').addEventListener('change', login);
document.querySelectorAll('[data-message]').forEach(button => button.addEventListener('click', () => send(button.dataset.message)));
$('#human-button').addEventListener('click', () => requestHuman());
$('#new-chat').addEventListener('click', () => { if (!busy) { conversationId = ''; welcome(); resetDetail(); renderCourierCalls([]); resetHuman(); } });
$('#refresh-human').addEventListener('click', () => updateHuman());
$('#connect-human').addEventListener('click', () => updateHuman('connected'));
$('#close-human').addEventListener('click', () => updateHuman('closed'));
$('#apply-scenario').addEventListener('click', async () => {
  if (busy || $('#courier-tracking').disabled || !$('#courier-tracking').value) return;
  const tracking = $('#courier-tracking').value;
  busy = true;
  $('#apply-scenario').disabled = true;
  $('#account-select').disabled = true;
  try {
    const result = await api('/api/demo/logistics', { tracking_number: tracking, scenario: $('#courier-scenario').value });
    toast(result.message);
    $('#courier-fault').value = '';
    busy = false;
    await send('运单号 ' + tracking + ' 物流到哪了');
  } catch (error) { toast(error.message); }
  finally { busy = false; $('#account-select').disabled = false; $('#apply-scenario').disabled = $('#courier-tracking').disabled || !$('#courier-tracking').value; }
});
$('#view-requests').addEventListener('click', async () => {
  if (busy) return;
  const version = accountVersion;
  try {
    const result = await api('/api/human/requests');
    if (version !== accountVersion) return;
    const content = message('assistant', result.requests.length ? '你的模拟人工请求：' : '当前账号暂无人工服务请求。');
    for (const item of result.requests.slice(0, 5)) {
      const button = element('button', 'candidate-button', item.request_id + ' · ' + (humanStateNames[item.status] || item.status));
      button.type = 'button';
      if (item.order_id) button.append(element('small', '', item.order_id));
      button.addEventListener('click', () => updateHuman(undefined, item.request_id));
      content.append(button);
    }
  } catch (error) { toast(error.message); }
});
login();
