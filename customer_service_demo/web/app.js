const $ = (id) => document.getElementById(id);
const STORAGE_KEY = 'shiwei-chat-v1';
const profileNames = {U_GUEST:'匿名顾客', U_NEW:'普通新客 · 订单账号 A', U_RETURN:'普通老客 · 订单账号 B', U_VIP:'VIP 会员', U_UNKNOWN:'资料待确认', U_LOCKED:'偏远地区顾客'};
const state = {conversationId:null, revision:0, profile:'U_GUEST', reference:null, pending:null, messages:[], busy:false, ready:false, generation:0};
let controller = null, timeoutTimer = null, toastTimer = null, config = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function svgIcon(kind) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox','0 0 32 32'); svg.setAttribute('aria-hidden','true');
  const shapes = kind === 'chat' ? [['path',{d:'M5 7h22v16H14l-6 4v-4H5Z'}]] :
    [['path',{d:'M9 5h14l3 5-3 17H9L6 10Z'}],['path',{d:'M9 5V3h14v2M9 9h14M12 22h8'}],['circle',{cx:13,cy:15,r:1}],['circle',{cx:19,cy:15,r:1}],['path',{d:'M13 18q3 3 6 0'}]];
  for (const [tag, attributes] of shapes) {
    const part = document.createElementNS(ns,tag);
    for (const [key,value] of Object.entries(attributes)) part.setAttribute(key,String(value));
    svg.append(part);
  }
  return svg;
}
function money(fen) { return fen === null ? '待确认' : `${Math.floor(fen / 100)}.${String(fen % 100).padStart(2,'0')}`; }
function toast(text) {
  $('toast').textContent = text; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => {$('toast').hidden = true;}, 3200);
}
function save() {
  try { localStorage.setItem(STORAGE_KEY,JSON.stringify({conversationId:state.conversationId, revision:state.revision,
    profile:state.profile, reference:state.reference, pending:state.pending, draft:$('message-input').value})); }
  catch { /* 隐私模式不阻止正常对话。 */ }
}
function draft(text) {
  $('message-input').value = text; resizeInput(); updateControls(); save(); $('message-input').focus();
}
function resizeInput() {
  const input = $('message-input'); input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight,150)}px`;
  const count = Array.from(input.value).length; $('character-count').textContent = `${count} / 2000`; $('character-count').hidden = count < 1600;
}
function updateControls() {
  $('send-button').disabled = !state.ready || state.busy || !$('message-input').value.trim();
  $('send-button').hidden = state.busy;
  $('stop-wait').hidden = !state.busy;
  $('demo-profile').disabled = state.busy;
  $('demo-profile').value = state.profile;
}
function setStatus(text, warning=false) {
  $('connection-label').textContent = text; $('connection-dot').classList.toggle('warning',warning);
}
async function api(path, options={}) {
  const response = await fetch(path,{credentials:'same-origin',cache:'no-store',...options});
  let value;
  try { value = await response.json(); } catch { throw new Error('客服返回了无法读取的结果，请重试原消息。'); }
  if (!response.ok) throw new Error(value.error?.message || '客服暂时无法连接，请重试原消息。');
  return value;
}
function renderReference() {
  $('reference-chip').hidden = !state.reference;
  if (state.reference) $('reference-label').textContent = `正在聊：${state.reference.label}`;
}
function selectReference(message, card, item=null) {
  state.reference = {message_id:message.id, plan_id:card.plan_id, sku_id:item?.sku_id ?? null,
    label:item ? item.product_name : `${card.title} · ${card.items.map(i=>i.product_name).join('、')}`};
  renderReference(); save(); $('message-input').focus();
  if (!$('message-input').value.trim()) draft(item ? '这款商品，' : '这个方案，');
  toast('已选中，可以继续问或提出修改');
}
function planCard(message, card) {
  const root = el('article',`plan-card${card.rank===1?' featured':''}`);
  root.dataset.planId = card.plan_id;
  const heading = el('div','plan-heading'); const title = el('strong','',card.title);
  if (card.rank===1) title.append(el('span','rank-badge','优先推荐'));
  const types = {single_product:'单品', multi_quantity:'同款多件', bundle:'组合', add_on:'可选凑单', gift_plan:'礼赠'};
  heading.append(title,el('span','plan-type',types[card.plan_type] || '推荐方案')); root.append(heading);
  const rows = el('div','plan-items');
  for (const [index,item] of card.items.entries()) {
    const row = el('div','product-row'); row.dataset.skuId = item.sku_id;
    const image = el('div',`product-picture palette-${(Number(item.sku_id.replace(/\D/g,'')) || index)%3}`);
    if (item.image_url && /^https?:\/\//.test(item.image_url)) {
      const img = el('img'); img.src = item.image_url; img.alt = item.product_name; img.loading = 'lazy';
      img.addEventListener('error',()=>{image.replaceChildren(svgIcon('snack'));},{once:true}); image.append(img);
    } else image.append(svgIcon('snack'));
    const info = el('div','product-info'); info.append(el('div','product-name',item.product_name),el('div','product-unit',`单价 ¥${money(item.unit_price_fen)} / 件`));
    const choose = el('button','product-select','↗'); choose.type='button'; choose.setAttribute('aria-label',`选择${item.product_name}继续咨询`);
    choose.addEventListener('click',()=>selectReference(message,card,item));
    row.append(image,info,el('span','product-quantity',`× ${item.quantity}`),choose); rows.append(row);
  }
  root.append(rows);
  const p = card.pricing; const pricing = el('div','plan-pricing'); const amount = el('div');
  amount.append(el('div','price-caption',p.total_payable_fen===null?'商品应付 · 运费待确认':'含运费 · 整单应付'));
  const total = el('div','total-price'); total.append(el('span','currency','¥'),document.createTextNode(money(p.total_payable_fen ?? p.merchandise_payable_fen))); amount.append(total);
  if (p.status!=='confirmed') amount.append(el('span','conditional-tag','条件待确认'));
  const breakdown = el('div','price-breakdown'); breakdown.append(el('div','',`原价合计 ¥${money(p.subtotal_fen)}`),el('div','',`商品应付 ¥${money(p.merchandise_payable_fen)}`),el('div','',p.shipping_fen===null?'运费待确认':`运费 ¥${money(p.shipping_fen)}`));
  pricing.append(amount,breakdown); root.append(pricing);
  const reasons = el('div','plan-reasons'); for (const reason of card.reasons) reasons.append(el('span','reason',reason)); root.append(reasons);
  const notes = el('div','plan-notes');
  for (const gift of card.gifts || []) notes.append(el('p','plan-gift',`赠品：${gift.product_name} × ${gift.quantity}`));
  for (const note of [...p.conditions,...card.notes]) {
    if (note === '商品、身份与优惠为演示资料。') continue;
    notes.append(el('p',`plan-note${/临期|待确认|预算目标/.test(note)?' important':''}`,note));
  }
  root.append(notes);
  if (p.discounts.length) {
    const details = el('details','discount-details'); details.append(el('summary','',`查看 ${p.discounts.length} 项优惠 · 合计减 ¥${money(p.discounts.reduce((sum,d)=>sum+d.amount_fen,0))}`));
    for (const d of p.discounts) {const row=el('div','discount-row');row.append(el('span','',d.label),el('strong','',`− ¥${money(d.amount_fen)}`));details.append(row);} root.append(details);
  }
  const action = el('button','plan-action','围绕这个方案继续聊 ↗'); action.type='button'; action.addEventListener('click',()=>selectReference(message,card)); root.append(action);
  return root;
}
function turn(message) {
  const row = el('div',`turn ${message.role}`); row.dataset.messageId = message.id;
  if (message.role==='user') {row.append(el('div','user-bubble',message.content));return row;}
  row.append(el('div','assistant-avatar','✦'));
  const names = {product_recommendation:'选购顾问',order_query:'订单客服',conversation_router:'客服助手'};
  const body = el('div','assistant-message'); const meta = el('div','message-meta',`拾味 · ${names[message.module] || '客服顾问'}`);
  if (message.status==='needs_clarification') meta.append(el('span','message-status','还差一点信息'));
  if (message.status==='no_match') meta.append(el('span','message-status','暂未匹配'));
  if (message.status==='unsupported') meta.append(el('span','message-status','尚未接入'));
  body.append(meta,el('p','assistant-text',message.content));
  if (message.cards?.length) {const cards=el('div','recommendations'); for(const card of message.cards)cards.append(planCard(message,card));body.append(cards);}
  if (message.clarification) {
    const quick = el('div','quick-replies');
    const options = message.module==='conversation_router' ? ['先查询订单','先继续商品推荐'] :
      message.module==='order_query' ? [...new Set(message.content.match(/SN-DEMO-\d{4}/g) || [])].map(id=>`查询订单 ${id}`) :
      message.clarification.asked_fields.includes('budget_scope') ? ['预算指含运费的总金额','预算只计算商品金额','预算是每份商品金额'] :
      message.clarification.asked_fields.includes('purpose') ? ['自己吃','办公室分享','准备送人'] : [];
    for(const text of options){const button=el('button','',text);button.type='button';button.addEventListener('click',()=>draft(text));quick.append(button);}if(options.length)body.append(quick);
  }
  const tools = el('div','message-tools'); const copy = el('button','','复制回答');copy.type='button';
  copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(message.content);toast('回答已复制');}catch{toast('无法自动复制，可以选中文字复制');}});tools.append(copy);body.append(tools);
  row.append(body); return row;
}
function failureTurn() {
  const row = el('div','turn assistant');row.append(el('div','assistant-avatar','✦'));
  const box=el('div','failure-box');box.setAttribute('role','alert');box.append(el('p','',state.pending.failure || '处理结果待确认，请重试原消息。'));
  const retry=el('button','','重试原消息');retry.type='button';retry.addEventListener('click',()=>transmit(state.pending));
  const edit=el('button','','编辑需求');edit.type='button';edit.addEventListener('click',editPending);
  box.append(retry,edit,el('div','failure-help','原输入已保留。重试会核对同一请求，成功回复不会重复写入。'));row.append(box);return row;
}
function render(scroll=null) {
  $('welcome').hidden = state.messages.length>0;
  $('messages').replaceChildren(...state.messages.map(turn));
  if (state.busy && state.pending) {
    const row=el('div','turn assistant');row.append(el('div','assistant-avatar','✦'));const text=el('div','pending-text');
    const dots=el('span','typing-dots');for(let i=0;i<3;i++)dots.append(el('i'));text.append(dots,el('span','','理解咨询，查询已确认的信息，整理客服回复…'));row.append(text);$('messages').append(row);
  } else if(state.pending?.failure) $('messages').append(failureTurn());
  renderReference();updateControls();
  if(scroll) requestAnimationFrame(()=>{
    if(scroll==='reply') {const last=$('messages').querySelector('.turn.assistant:last-child');const scrollBox=$('chat-scroll');if(last)scrollBox.scrollTo({top:scrollBox.scrollTop+last.getBoundingClientRect().top-scrollBox.getBoundingClientRect().top-18,behavior:'smooth'});}
    else $('chat-scroll').scrollTo({top:$('chat-scroll').scrollHeight,behavior:'smooth'});
  });
}
async function refreshConversations() {
  try {
    const {conversations}=await api('/api/conversations');const list=$('conversations');list.replaceChildren();
    if(!conversations.length)list.append(el('p','empty-history','从第一句开始，慢慢挑。'));
    for(const item of conversations){const button=el('button',`conversation-row${item.id===state.conversationId?' active':''}`);button.type='button';button.title=item.title;button.append(svgIcon('chat'),el('span','',item.title));button.addEventListener('click',()=>loadConversation(item.id));list.append(button);}
  } catch { /* 不影响当前对话；用户下一次操作会重试获取。 */ }
}
function clearTransient() {if(state.pending)state.messages=state.messages.filter(m=>m.id!==state.pending.local_id);state.pending=null;}
function editPending() {
  const old=state.pending?.request.message;clearTransient();render();save();if(!$('message-input').value&&old)draft(old);else $('message-input').focus();
}
function navigateAway() {
  state.generation++;controller?.abort();controller=null;clearTimeout(timeoutTimer);state.busy=false;state.pending=null;
}
function newConversation(profile=state.profile) {
  navigateAway();state.conversationId=null;state.revision=0;state.reference=null;state.messages=[];state.profile=profile;state.ready=Boolean(config);
  $('message-input').value='';render();resizeInput();save();closeSidebar();refreshConversations();$('message-input').focus();
}
async function loadConversation(cid) {
  if(cid===state.conversationId&&!state.pending)return;
  navigateAway();const generation=state.generation;state.ready=false;updateControls();closeSidebar();
  try {
    const data=await api(`/api/conversation?id=${encodeURIComponent(cid)}`);
    if(generation!==state.generation)return;
    state.conversationId=data.conversation_id;state.revision=data.revision;state.profile=data.demo_user_id;state.messages=data.history;state.reference=null;
    $('message-input').value='';state.ready=true;render('bottom');resizeInput();save();refreshConversations();
  } catch(error) {if(generation===state.generation){state.ready=true;updateControls();toast(error.message);}}
}
async function syncConflict(pending, generation) {
  const cid=state.conversationId;
  const data=await api(`/api/conversation?id=${encodeURIComponent(cid)}`);
  if(generation!==state.generation || cid!==state.conversationId)return;
  state.revision=data.revision;state.messages=data.history;
  pending.local_id=`local_${crypto.randomUUID().replaceAll('-','')}`;
  state.messages.push({id:pending.local_id,role:'user',content:pending.request.message});
  pending.request={...pending.request,request_id:`req_${crypto.randomUUID().replaceAll('-','')}`,expected_revision:data.revision};
  pending.failure='对话已经有新回复，已同步最新记录。请重试，让这条需求基于最新对话继续。';
}
async function transmit(pending) {
  if(state.busy||!pending)return;
  const generation=state.generation;const localController=new AbortController();controller=localController;
  state.pending=pending;pending.failure=null;state.busy=true;render('bottom');save();
  const localTimer=setTimeout(()=>localController.abort(),(config?.response_timeout_seconds || ((config?.request_timeout_seconds || 45)*2+25))*1000);
  timeoutTimer=localTimer;
  try {
    const result=await api('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Demo-User':pending.profile},body:JSON.stringify(pending.request),signal:localController.signal});
    if(generation!==state.generation)return;
    if(result.status==='error') {
      if(result.error.code==='revision_conflict'&&state.conversationId)await syncConflict(pending,generation);
      else pending.failure=result.message.content;
      if(generation!==state.generation)return;
      setStatus(result.error.retryable?'连接暂不可用 · 可重试':'请调整需求或检查配置',true);
    } else {
      state.conversationId=result.conversation_id;state.revision=result.revision;
      if(!state.messages.some(m=>m.id===result.message.id))state.messages.push({...result.message,module:result.module,status:result.status,clarification:result.clarification});
      else state.messages=state.messages.filter(m=>m.id!==pending.local_id);
      if($('message-input').value.trim()===pending.request.message)$('message-input').value='';
      if(state.reference?.message_id===pending.request.selected_reference?.message_id)state.reference=null;
      state.pending=null;resizeInput();setStatus('商品推荐 · 订单查询已接入');
    }
  } catch(error) {
    if(generation!==state.generation)return;
    pending.failure=error.name==='AbortError'?'已停止等待，处理结果待确认。可以重试原消息查看结果。':error.message;
    setStatus(error.name==='AbortError'?'本次结果待确认':'连接暂不可用 · 可重试',true);
  } finally {
    clearTimeout(localTimer);
    if(generation===state.generation){state.busy=false;controller=null;render(state.pending?'bottom':'reply');save();refreshConversations();}
  }
}
function send() {
  if(!state.ready||state.busy)return;
  const text=$('message-input').value.trim();if(!text)return;
  if(Array.from(text).length>2000){toast('请将需求控制在 2000 字以内');return;}
  if(state.pending?.request.message===text){transmit(state.pending);return;}
  clearTransient();
  const reference=state.reference ? {message_id:state.reference.message_id,plan_id:state.reference.plan_id,sku_id:state.reference.sku_id} : null;
  const pending={request:{schema_version:'1.0',conversation_id:state.conversationId,request_id:`req_${crypto.randomUUID().replaceAll('-','')}`,
    expected_revision:state.revision,message:text,selected_reference:reference},profile:state.profile,local_id:`local_${crypto.randomUUID().replaceAll('-','')}`,failure:null};
  state.messages.push({id:pending.local_id,role:'user',content:text});transmit(pending);
}
function closeSidebar() {$('sidebar').classList.remove('open');$('sidebar-scrim').hidden=true;$('menu-toggle').setAttribute('aria-expanded','false');}

$('composer').addEventListener('submit',event=>{event.preventDefault();send();});
$('message-input').addEventListener('input',()=>{resizeInput();updateControls();save();});
$('message-input').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();send();}});
$('stop-wait').addEventListener('click',()=>controller?.abort());
$('new-chat').addEventListener('click',()=>newConversation());
$('demo-profile').addEventListener('change',event=>{const profile=event.target.value;if(profileNames[profile]){newConversation(profile);toast(`已切换为${profileNames[profile]}，开始新的咨询`);}});
$('clear-reference').addEventListener('click',()=>{state.reference=null;renderReference();save();});
document.querySelectorAll('.starter').forEach(button=>button.addEventListener('click',()=>draft(button.dataset.prompt)));
$('about-toggle').addEventListener('click',()=>{$('about-panel').hidden=!$('about-panel').hidden;$('about-toggle').setAttribute('aria-expanded',String(!$('about-panel').hidden));});
$('menu-toggle').addEventListener('click',()=>{const open=$('sidebar').classList.toggle('open');$('sidebar-scrim').hidden=!open;$('menu-toggle').setAttribute('aria-expanded',String(open));});
$('sidebar-scrim').addEventListener('click',closeSidebar);
document.addEventListener('keydown',event=>{if(event.key==='Escape'){closeSidebar();$('about-panel').hidden=true;$('about-toggle').setAttribute('aria-expanded','false');}});
window.addEventListener('focus',()=>{if(state.ready)refreshConversations();});

async function init() {
  let saved={};try{saved=JSON.parse(localStorage.getItem(STORAGE_KEY)||'{}') || {};}catch{}
  try {
    config=await api('/api/bootstrap');
    $('demo-profile').replaceChildren(...config.profiles.map(p=>{const option=el('option','',p.label);option.value=p.id;return option;}));
    state.profile=profileNames[saved.profile]?saved.profile:'U_GUEST';
    $('demo-time').textContent=`优惠演示时间固定为 ${config.reference_time.replace('T',' ').replace('+08:00','')}（上海时间）。`;
    setStatus(config.model_configured?'DeepSeek 已配置 · 可以开始咨询':'模型尚未配置',!config.model_configured);
    if(!config.model_configured){$('connection-banner').hidden=false;$('connection-banner').textContent='模型还没有配置完成。请先在本地配置密钥并重新启动客服服务。';}
    if(saved.pending?.request?.request_id) {
      try {
        const query=new URLSearchParams({scope:saved.pending.request.conversation_id || 'new',id:saved.pending.request.request_id});
        const cached=await api(`/api/request?${query}`);
        if(cached.response) {saved.conversationId=cached.response.conversation_id;if(saved.draft?.trim()===saved.pending.request.message)saved.draft='';saved.pending=null;saved.reference=null;}
      } catch { /* 没有确认结果时仍保留原请求用于安全重试。 */ }
    }
    if(typeof saved.conversationId==='string'&&/^[A-Za-z0-9_-]{1,80}$/.test(saved.conversationId)) {
      try {const data=await api(`/api/conversation?id=${encodeURIComponent(saved.conversationId)}`);state.conversationId=data.conversation_id;state.revision=data.revision;state.messages=data.history;state.profile=data.demo_user_id;}catch{toast('上次对话无法恢复，可以开始新的咨询');}
    }
    if(saved.pending?.request && saved.pending.profile===state.profile && saved.pending.request.conversation_id===state.conversationId) {
      const pending=saved.pending;
      // 未确认结果始终沿用原请求 ID；即使服务端已提交，也能安全重放。
      pending.failure='上次消息的处理结果待确认，可以重试原消息查看。';state.pending=pending;
      state.messages.push({id:pending.local_id,role:'user',content:pending.request.message});
    }
    if(saved.reference && state.messages.some(m=>m.id===saved.reference.message_id && m.cards?.some(c=>c.plan_id===saved.reference.plan_id)))state.reference=saved.reference;
    $('message-input').value=typeof saved.draft==='string'?saved.draft.slice(0,2000):'';
    state.ready=true;render(state.messages.length?'bottom':null);resizeInput();save();refreshConversations();
  } catch(error) {
    setStatus('客服暂时无法连接',true);$('connection-banner').hidden=false;$('connection-banner').textContent=`${error.message} 请刷新页面重新连接。`;
  }
}
init();
