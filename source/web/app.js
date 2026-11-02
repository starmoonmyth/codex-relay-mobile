const $ = id => document.getElementById(id);
const state = {chat:null,chats:[],meta:null,busy:false,refreshing:false,snapshot:'',lastList:0,modelInfo:null,modelSaving:false,lastData:null,online:true};
const attachmentDrafts = new Map();
function chatStatus(data) {
  if(state.busy)return 'sending';
  const latest=data.receipts.at(-1),turn=data.turn?.status;
  if(latest?.result==='failed')return 'failed';
  if(latest&&!latest.seen_in_history&&!latest.grok_finished){
    if(latest.status==='正在投递')return 'sending';
    if(latest.result==='uncertain')return 'uncertain';
    if(latest.result==='sent')return ['claude','grok'].includes(data.chat.provider)?'working':'queued';
  }
  const unanswered=data.messages.some(message=>message.questions?.some(question=>question.answer==null));
  if(unanswered&&turn!=='completed'&&turn!=='failed'&&turn!=='interrupted')return 'needs_input';
  if(data.activity&&['working','queued','needs_input','failed','interrupted'].includes(data.activity.state))return data.activity.state;
  if(turn==='inProgress')return 'working';
  if(turn==='needsInput')return 'needs_input';
  if(turn==='failed')return 'failed';
  if(turn==='interrupted')return 'interrupted';
  if(turn==='completed'||latest?.grok_finished||latest?.status==='已完成')return 'completed';
  if(['claude','grok'].includes(data.chat.provider)){
    const last=data.messages.at(-1);
    if(last?.role==='user')return 'working';
    if(last?.role==='assistant')return 'completed';
  }
  return 'idle';
}
function drawTurnStatus(kind) {
  const labels={idle:'就绪',loading:'读取中',sending:'发送中',queued:'排队中',working:'处理中',needs_input:'待你回复',completed:'已完成',failed:'失败',interrupted:'已中断',uncertain:'待确认',offline:'连接中断'};
  const icon={idle:'<circle cx="12" cy="12" r="8"/><path d="m8 12 3 3 5-6"/>',
    loading:'<circle cx="12" cy="12" r="8" stroke-dasharray="32 18"/>',
    sending:'<path d="M12 19V5m-6 6 6-6 6 6"/>',
    queued:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    working:'<circle cx="12" cy="12" r="8" stroke-dasharray="32 18"/>',
    needs_input:'<path d="M5 4h14v12H9l-4 4V4Z"/><path d="M12 7v4m0 2v.1"/>',
    completed:'<circle cx="12" cy="12" r="9"/><path d="m7 12 3 3 7-7"/>',
    failed:'<path d="m8 3-5 5v8l5 5h8l5-5V8l-5-5H8Z"/><path d="m9 9 6 6m0-6-6 6"/>',
    interrupted:'<circle cx="12" cy="12" r="9"/><path d="M9 8v8m6-8v8"/>',
    uncertain:'<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m0 3v.1"/>',
    offline:'<path d="m3 3 18 18M4 10a12 12 0 0 1 3-2m3-1a12 12 0 0 1 10 3M7 14a7 7 0 0 1 3-1m5 1 2 1M12 18v.1"/>'};
  const badge=$('turn-status');if(badge.dataset.state===kind&&badge.querySelector('svg'))return;
  badge.dataset.state=kind;badge.replaceChildren();
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  for(const [name,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'2','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'}))svg.setAttribute(name,value);
  svg.innerHTML=icon[kind]||icon.idle;badge.append(svg,node('span','status-label',labels[kind]||labels.idle));
  badge.title=kind==='needs_input'?'有问题需要你回复':kind==='uncertain'?'投递结果尚未确认，请先检查原聊天':labels[kind]||labels.idle;
}
const storage = {
  get(key, fallback='') { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key,value); } catch {} },
  remove(key) { try { localStorage.removeItem(key); } catch {} }
};
const key = (kind, id=state.chat?.id) => `${kind}:${id}`;
function pending(id=state.chat?.id) { try { return JSON.parse(storage.get(key('pending',id),'null')); } catch { return null; } }
function notice(text) { $('send-notice').textContent = text; }
function connection(text, online) { state.online=online;$('connection').textContent=text;$('connection').classList.toggle('online',online);if(state.chat)drawTurnStatus(!online?'offline':state.lastData?chatStatus(state.lastData):'loading'); }
async function api(path, body) {
  let response;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),path==='/api/send'||path==='/api/answer'||path.startsWith('/api/codex/model')?90000:path.startsWith('/api/usage/')?45000:15000);
  try {response = await fetch(path, {cache:'no-store', credentials:'same-origin',
    signal:controller.signal,
    ...(body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})});}
  catch {throw Error('电脑响应超时或连接中断，请确认电脑与 Tailscale 在线。');}
  finally{clearTimeout(timeout);}
  const value = await response.json();
  if(response.status===401) { showLogin(); throw Error('请先配对这台电脑。'); }
  if(!response.ok) {const error=Error(value.error || `请求失败 (${response.status})`);error.status=response.status;throw error;}
  return value;
}
function showLogin() { document.body.classList.remove('chat-layout','keyboard-open');$('workspace').hidden=true;$('login').hidden=false; }
function drawer(open) { document.body.classList.toggle('drawer',open); $('scrim').hidden=!open; }
function node(tag, className, text) { const item=document.createElement(tag); if(className)item.className=className; if(text!==undefined)item.textContent=text; return item; }
function timeLabel(raw) { const date=new Date(raw>1e12?raw:raw*1000),days=Math.floor((Date.now()-date)/86400000); return days===0?'今天':days===1?'昨天':date.toLocaleDateString('zh-CN',{month:'numeric',day:'numeric'}); }
function unread(chat) {return chat.activity?.state==='completed'&&!!chat.activity.completion_id&&storage.get(key('seen-completion',chat.id))!==chat.activity.completion_id;}
function seedReadMarkers(chats) {
  for(const provider of new Set(chats.map(chat=>chat.provider))){
    const marker='completion-baseline:'+provider;if(storage.get(marker))continue;
    for(const chat of chats.filter(chat=>chat.provider===provider))if(chat.activity?.completion_id)storage.set(key('seen-completion',chat.id),chat.activity.completion_id);
    storage.set(marker,'1');
  }
}
function markVisibleCompletion() {
  const data=state.lastData,box=$('conversation');
  if(!state.chat||!data||data.chat.id!==state.chat.id||document.hidden||document.body.classList.contains('drawer')||document.querySelector('dialog[open]'))return;
  if(!data.activity?.visible_completion||box.scrollHeight-box.scrollTop-box.clientHeight>100)return;
  const marker=key('seen-completion',state.chat.id);
  if(storage.get(marker)!==data.activity.completion_id){storage.set(marker,data.activity.completion_id);drawList();}
}
function listBadge(chat) {
  const kind=chat.activity?.state,isUnread=unread(chat);
  const label=isUnread?'完成 · 未查看':({working:'处理中',queued:'排队中',sending:'发送中',needs_input:'待回复',failed:'失败',interrupted:'中断'})[kind];
  if(!label)return null;
  const badge=node('span','chat-activity '+(isUnread?'unread':kind));badge.setAttribute('aria-label',isUnread?'已完成，尚未查看':label);
  const icon=node('span','chat-activity-icon');icon.setAttribute('aria-hidden','true');
  if(kind==='working'||kind==='sending')icon.innerHTML='<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2"><circle cx="10" cy="10" r="7" stroke-dasharray="28 16"/></svg>';
  else icon.textContent=isUnread?'●':kind==='failed'?'!':kind==='needs_input'?'?':'◷';
  badge.append(icon,document.createTextNode(label));return badge;
}
function drawList() {
  const query=$('search').value.toLowerCase(); const list=$('chat-list'),top=list.scrollTop; list.replaceChildren();
  const chats=state.chats.filter(c=>(c.title+' '+c.cwd).toLowerCase().includes(query));
  for(const chat of chats) {
    const button=node('button','chat'+(chat.id===state.chat?.id?' active':''));
    const heading=node('span','chat-heading');heading.append(node('span','chat-name',(chat.pinned?'⌁ ':'')+chat.title));
    const badge=listBadge(chat);if(badge)heading.append(badge);button.classList.toggle('has-unread',unread(chat));button.append(heading);
    const meta=node('span','chat-meta'); const project=chat.cwd.replaceAll('\\','/').split('/').filter(Boolean).pop()||'未指定项目';
    meta.append(node('span','project',({claude:'Claude · ',grok:'Grok · ',codex:'Codex · '})[chat.provider]+project),node('span','',timeLabel(chat.updated_at)));button.append(meta);
    button.onclick=()=>selectChat(chat); list.append(button);
  }
  if(!chats.length)list.append(node('p','small',state.chats.length?'没有匹配的聊天。':'还没有聊天。请先在电脑的 Codex、Claude Code 或 Grok 中打开一个聊天。'));
  list.scrollTop=top;
}
function imagesFor(id=state.chat?.id) {
  if(!id)return [];
  if(!attachmentDrafts.has(id)) {
    let saved=[];try{saved=JSON.parse(storage.get(key('images',id),'[]'));}catch{}
    attachmentDrafts.set(id,Array.isArray(saved)?saved.filter(image=>/^[a-f0-9]{32}$/.test(image.id)):[]);
  }
  return attachmentDrafts.get(id);
}
function saveImages(id) {storage.set(key('images',id),JSON.stringify(imagesFor(id).filter(image=>image.id)));}
function imageIds(id=state.chat?.id) {return imagesFor(id).filter(image=>image.id).map(image=>image.id);}
function sameImages(a,b) {return JSON.stringify(a||[])===JSON.stringify(b||[]);}
function updateSend() {
  if(state.chat)drawTurnStatus(!state.online?'offline':state.busy?'sending':state.lastData?chatStatus(state.lastData):'loading');
  const images=imagesFor(),loading=images.some(image=>image.uploading||image.failed);
  const modelRequired=['claude','grok'].includes(state.chat?.provider)&&!state.modelInfo?.requested_model;
  $('send').disabled=!state.chat||(!$('message').value.trim()&&!images.length)||state.busy||loading||modelRequired||state.modelSaving;
  $('choose-model').disabled=state.busy||state.modelSaving;
  $('add-image').disabled=!state.chat||state.busy||state.chat.provider==='grok';
  $('add-image').title=state.chat?.provider==='grok'?'Grok 当前支持文字续聊':'选择照片或拍照';
  for(const button of $('attachment-drafts').querySelectorAll('button'))button.disabled=state.busy;
}
function imageGallery(images) {
  const gallery=node('div','image-gallery');
  for(const image of images||[]) {
    const link=node('a','image-link');link.href=image.url;link.target='_blank';link.rel='noopener';
    const preview=node('img');preview.src=image.url;preview.alt=image.name||'图片附件';preview.loading='lazy';
    link.append(preview);gallery.append(link);
  }
  return gallery;
}
function drawImages() {
  const holder=$('attachment-drafts'),images=imagesFor();holder.replaceChildren();holder.hidden=!images.length;
  for(const image of images) {
    const card=node('div','attachment'+(image.failed?' failed':'')),preview=node('img');preview.src=image.url;preview.alt=image.name||'待发送图片';
    const remove=node('button','remove-image','×');remove.type='button';remove.setAttribute('aria-label','移除图片 '+image.name);
    remove.onclick=()=>{const list=imagesFor(),index=list.indexOf(image);if(index>=0)list.splice(index,1);saveImages(state.chat.id);drawImages();updateSend();};
    card.append(preview,remove);
    if(image.uploading||image.failed)card.append(node('span','upload-state',image.failed?'上传失败':'上传中…'));
    holder.append(card);
  }
  updateSend();
}
async function prepareImage(file) {
  if(file.type==='image/svg+xml'||!file.type.startsWith('image/'))throw Error('请选择照片或截图，暂不支持 SVG。');
  if(file.size>32*1024*1024)throw Error('所选图片超过 32 MB，请先缩小。');
  const url=URL.createObjectURL(file),picture=new Image();
  try {
    picture.src=url;await picture.decode();
    const scale=Math.min(1,3072/Math.max(picture.naturalWidth,picture.naturalHeight));
    const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(picture.naturalWidth*scale));canvas.height=Math.max(1,Math.round(picture.naturalHeight*scale));
    const context=canvas.getContext('2d');const png=file.type==='image/png'||file.type==='image/gif';
    if(!png){context.fillStyle='#ffffff';context.fillRect(0,0,canvas.width,canvas.height);}
    context.drawImage(picture,0,0,canvas.width,canvas.height);
    let data=canvas.toDataURL(png?'image/png':'image/jpeg',.92);
    if(data.length>Math.ceil(8*1024*1024/3)*4+100) {
      const copy=document.createElement('canvas');copy.width=canvas.width;copy.height=canvas.height;
      const ctx=copy.getContext('2d');ctx.fillStyle='#ffffff';ctx.fillRect(0,0,copy.width,copy.height);ctx.drawImage(canvas,0,0);
      data=copy.toDataURL('image/jpeg',.85);
    }
    return data;
  }catch(error){if(error.message?.includes('MB'))throw error;throw Error('无法读取这张图片，请改用 JPEG、PNG 或 WebP。');}
  finally{URL.revokeObjectURL(url);}
}
async function addImages(files) {
  if(!state.chat||state.busy)return;
  if(state.chat.provider==='grok'){notice('Grok 当前支持文字续聊，图片投递尚未开放。');return;}
  const chatId=state.chat.id;
  for(const file of Array.from(files)) {
    const list=imagesFor(chatId);
    const item={name:file.name||'图片',url:'',uploading:true};list.push(item);
    if(state.chat?.id===chatId)drawImages();
    try {
      const data=await prepareImage(file);item.url=data;if(state.chat?.id===chatId)drawImages();
      const uploaded=await api('/api/upload',{name:item.name,data:data.split(',')[1]});
      if(list.filter(image=>image!==item).reduce((total,image)=>total+(image.size||0),0)+uploaded.size>(state.meta?.max_attachment_bytes||32*1024*1024))throw Error('图片合计超过 32 MB，请移除部分图片后再添加。');
      Object.assign(item,uploaded,{uploading:false});saveImages(chatId);
      if(state.chat?.id===chatId)notice('图片已就绪，可以单独发送，也可以补充说明。');
    }catch(error){item.uploading=false;item.failed=true;if(state.chat?.id===chatId)notice(error.message+' 请移除后重新选择。');}
    if(state.chat?.id===chatId)drawImages();
  }
}
function resizeComposer() { $('message').style.height='auto'; $('message').style.height=Math.min($('message').scrollHeight,window.innerHeight*.3)+'px'; }
async function selectChat(chat) {
  if($('usage-settings').open)$('usage-settings').close();
  if(state.chat)storage.set(key('draft'),$('message').value);
  state.chat=chat;state.lastData=null;state.online=true;drawTurnStatus('loading');state.snapshot='';state.modelInfo=null;drawModel(null);storage.set('selected-chat',chat.id);
  $('chat-title').textContent=chat.title; $('chat-path').textContent=chat.cwd;
  $('message').placeholder=chat.provider==='grok'?'输入 Grok 任务…':'输入任务，或添加图片…';
  document.querySelector('.composer-wrap .hint').textContent=chat.provider==='grok'?'Grok 当前支持文字续聊':'图片合计最多 32 MB · 发送后电脑继续工作';
  $('message').disabled=false;$('message').value=storage.get(key('draft'));resizeComposer();drawImages();updateSend();notice('');
  $('conversation').replaceChildren(node('p','small','正在读取聊天…'));drawList();drawer(false);
  const attempt=pending(); if(attempt)notice('有一条投递等待确认。原文再次发送会查询同一条记录。');
  await refreshChat(true);
}
function markdown(text) {
  const wrapper=node('div','markdown');
  if(window.marked&&window.DOMPurify) {
    wrapper.innerHTML=DOMPurify.sanitize(marked.parse(text,{breaks:true,gfm:true}),{FORBID_TAGS:['img','iframe','style','input','form','video','audio'],FORBID_ATTR:['style']});
    for(const link of wrapper.querySelectorAll('a')) {
      const href=link.getAttribute('href')||'';
      if(!/^https?:\/\//i.test(href))link.removeAttribute('href');
      else{link.target='_blank';link.rel='noopener noreferrer';}
    }
  } else {wrapper.textContent=text;wrapper.style.whiteSpace='pre-wrap';}
  return wrapper;
}
function renderSnapshot(data, force) {
  state.lastData=data;
  const listed=state.chats.find(chat=>chat.id===data.chat.id);if(listed&&data.activity)listed.activity=data.activity;
  drawList();
  drawModel(data.chat.provider==='grok'?data.grok_model:data.chat.provider==='claude'?data.claude_model:data.codex_model);
  drawTurnStatus(chatStatus(data));
  const box=$('conversation');const near=box.scrollHeight-box.scrollTop-box.clientHeight<100;
  const signature=JSON.stringify(data);if(signature===state.snapshot){markVisibleCompletion();return;}
  state.snapshot=signature;
  const fragment=document.createDocumentFragment();
  for(const m of data.messages) {
    const bubble=node('article','message '+m.role+(m.phase==='commentary'?' progress':''));
    if(m.role==='user') {
      if(m.images?.length)bubble.append(imageGallery(m.images));
      else if(m.image_count)bubble.append(node('span','small',`图片附件 ${m.image_count} 张 · 可在电脑查看`));
      if(m.text)bubble.append(node('div','user-text',m.text));
      if(m.requested_model)bubble.append(node('div','small','请求模型：'+m.requested_model));
    }
    else{const name=data.chat.provider==='codex'?'CODEX':data.chat.provider.toUpperCase()+' · '+(m.model||'模型未记录');bubble.append(node('span','speaker',m.questions?.length?name+' · 需要回复':m.phase==='commentary'?name+' · 进展':name));if(m.questions?.length)bubble.append(questionCard(m,data.chat.id));else bubble.append(markdown(m.text));}
    fragment.append(bubble);
  }
  for(const receipt of data.receipts) {
    if(receipt.seen_in_history)continue;
    const card=node('article','pending');card.append(node('div','pending-text',receipt.display_text??receipt.text),node('div','pending-status',receipt.status));
    if(receipt.requested_model)card.append(node('div','small','请求模型：'+receipt.requested_model));
    if(receipt.images?.length)card.prepend(imageGallery(receipt.images));
    if(receipt.error)card.append(node('div','pending-error',receipt.error));fragment.append(card);
  }
  if(!fragment.childNodes.length)fragment.append(node('p','small','还没有消息。在下方输入任务，继续这个聊天。'));
  const oldTop=box.scrollTop;box.replaceChildren(fragment);
  if(near||force){box.scrollTop=box.scrollHeight;$('jump').hidden=true;} else {box.scrollTop=oldTop;$('jump').hidden=false;}
  markVisibleCompletion();
  const attempt=pending(data.chat.id);
  if(data.receipts.at(-1)?.status==='已完成'&&/已启动.*回复/.test($('send-notice').textContent))notice('任务已完成。');
  if(attempt) {
    const receipt=data.receipts.find(r=>r.id===attempt.request_id);
    if(receipt&&receipt.status!=='正在投递')settle(receipt,attempt,data.chat.id);
  }
}
function settle(receipt,attempt,chatId) {
  if(receipt.result==='sent'||receipt.seen_in_history) {
    storage.remove(key('pending',chatId));
    if(storage.get(key('draft',chatId))===attempt.text)storage.remove(key('draft',chatId));
    if(sameImages(imageIds(chatId),attempt.image_ids)){attachmentDrafts.set(chatId,[]);saveImages(chatId);if(state.chat?.id===chatId)drawImages();}
    if(state.chat?.id===chatId) {
      if($('message').value===attempt.text){$('message').value='';resizeComposer();}
      notice(receipt.status==='已完成'?'已完成。':['claude','grok'].includes(receipt.provider)?(receipt.provider==='grok'?'Grok':'Claude Code')+' 已启动，回复会在这里更新。':receipt.warning?'消息已入队，CLI 有提示，请查看原聊天。':'已入队，电脑接收后会在这里显示回复。');
    }
  }else if(receipt.result==='failed') {
    storage.remove(key('pending',chatId));
    if(state.chat?.id===chatId)notice(receipt.error||'投递失败，请重试。');
  }else if(state.chat?.id===chatId) notice(receipt.error||'投递结果待确认。请先检查原聊天；再次发送原文只查询同一条记录。');
  updateSend();
}
async function refreshChat(force=false) {
  const id=state.chat?.id;if(!id)return;
  try {const data=await api('/api/chat/'+encodeURIComponent(id));if(state.chat?.id===id){renderSnapshot(data,force);connection('已连接 · '+state.meta.label,true);}}
  catch(error){connection('连接中断',false);notice(error.message+' 请检查电脑和 Tailscale 连接。');}
}
async function loadChats() {
  const data=await api('/api/chats');seedReadMarkers(data.chats);state.chats=data.chats;state.lastList=Date.now();drawList();
}
function drawProviderSelector() {
  const select=$('provider'); if(!select||!state.meta)return;
  const labels={codex:'Codex',claude:'Claude Code',grok:'Grok',all:'全部工作台'};
  select.value=state.meta.provider||'codex';
  for(const option of select.options) {
    option.disabled=option.value!=='all'&&!state.meta.available?.[option.value];
    option.textContent=labels[option.value]+(option.disabled?'（此电脑未检测到）':'');
  }
}
function modelLabel(model) {
  return ({opus:'Opus（opus）',sonnet:'Sonnet（sonnet）',haiku:'Haiku（haiku）'})[model]||model;
}
function usageNumber(value) {return Number.isFinite(value)?new Intl.NumberFormat('zh-CN',{maximumFractionDigits:4}).format(value):'未提供';}
function usageDate(value) {
  if(value==null)return '';
  const date=new Date(typeof value==='number'?value*1000:value);
  return Number.isNaN(date.getTime())?'':date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
}
function usageMetric(label,value,detail='') {
  const card=node('div','usage-metric');card.append(node('span','',label),node('strong','',value));
  if(detail)card.append(node('small','',detail));return card;
}
function usageTokens(container,title,usage) {
  if(!usage)return;
  container.append(node('h3','',title));const grid=node('div','usage-grid');
  grid.append(usageMetric('总 Token',usageNumber(usage.total_tokens)),usageMetric('输入',usageNumber(usage.input_tokens),'包含缓存读取和写入'),
    usageMetric('输出',usageNumber(usage.output_tokens)),usageMetric('缓存读取',usageNumber(usage.cached_input_tokens)));
  container.append(grid);
  if(usage.cache_write_input_tokens||usage.reasoning_output_tokens)container.append(node('p','small',`缓存写入 ${usageNumber(usage.cache_write_input_tokens)} · 推理 ${usageNumber(usage.reasoning_output_tokens)}（已包含在输入或输出中）`));
}
function drawUsage(data) {
  const content=$('usage-content');content.replaceChildren();
  const local=data.local,balance=data.balance;
  if(data.plan){
    const plan=data.plan,section=node('section','usage-section');section.append(node('h3','','套餐用量'+(plan.plan?' · '+plan.plan:'')));
    if(plan.status==='ok'){
      for(const item of plan.windows){
        const row=node('div','usage-window');row.append(node('span','',`${item.label} · 剩余 ${usageNumber(Math.max(0,Math.min(100,100-item.used_percent)))}%`));
        const progress=document.createElement('progress');progress.max=100;progress.value=Math.min(100,Math.max(0,item.used_percent));progress.setAttribute('aria-label',item.label+'套餐已用比例');row.append(progress);
        const reset=usageDate(item.resets_at);row.append(node('small','small','已用 '+usageNumber(item.used_percent)+'%'+(reset?' · 重置 '+reset:'')));section.append(row);
      }
      if(!plan.windows.length)section.append(node('p','small','接口未提供套餐用量百分比。'));
      for(const credit of plan.credits)section.append(usageMetric(credit.label,credit.unlimited?'不限额':usageNumber(credit.value),credit.unit));
    }else section.append(node('p','small',plan.message));
    if(plan.source)section.append(node('p','small',plan.source));
    if(plan.updated_at)section.append(node('p','small','查询时间 '+usageDate(plan.updated_at)));
    section.append(node('p','small','套餐属于原生登录账号，与中转站 API Key 的余额分别计算。'));content.append(section);
  }
  const account=node('section','usage-section');account.append(node('h3','','服务商余额 / 额度'));
  if(balance.status==='ok'){
    const amount=balance.remaining===-1?'不限额':balance.remaining==null?'接口未提供余额':usageNumber(balance.remaining);
    account.append(usageMetric(balance.label,amount,balance.remaining>=0?balance.unit:''));
    if(balance.today){const grid=node('div','usage-grid');grid.append(usageMetric('今日实际扣费',usageNumber(balance.today.cost),'USD'),usageMetric('今日请求',usageNumber(balance.today.requests)));account.append(grid);}
    for(const limit of balance.limits){
      const row=node('p','small',`${limit.label}：已用 ${usageNumber(limit.used)} / ${usageNumber(limit.limit)} ${limit.unit||balance.unit}`);
      if(limit.resets_at)row.append(document.createTextNode(' · 重置 '+usageDate(limit.resets_at)));account.append(row);
    }
  }else account.append(node('p','small',balance.message));
  if(balance.source)account.append(node('p','small',balance.source+' · '+balance.service));
  if(balance.updated_at)account.append(node('p','small','查询时间 '+usageDate(balance.updated_at)));
  content.append(account);
  const session=node('section','usage-section');
  if(local.available){usageTokens(session,'本聊天累计',local.total);usageTokens(session,data.provider==='grok'?'最近一轮任务':'最近一次模型调用',local.last);}
  else session.append(node('h3','','本聊天用量'),node('p','small',local.error||'此聊天尚未记录 Token 用量，完成一次请求后再查看。'));
  if(local.context_window)session.append(node('p','small','模型上下文容量 '+usageNumber(local.context_window)+' Token'));
  if(local.updated_at)session.append(node('p','small','记录时间 '+usageDate(local.updated_at)));
  if(local.windows.length&&data.plan?.status!=='ok'){
    session.append(node('h3','','账号额度快照'));
    for(const window of local.windows){
      const label=window.minutes?window.minutes>=1440?`${usageNumber(window.minutes/1440)} 天`:`${usageNumber(window.minutes/60)} 小时`:'额度周期';
      const expired=window.resets_at&&window.resets_at*1000<Date.now();
      const row=node('div','usage-window');row.append(node('span','',`${label}已用 ${usageNumber(window.used_percent)}%${expired?' · 快照已过期':''}`));
      const bar=document.createElement('progress');bar.max=100;bar.value=Math.min(100,Math.max(0,window.used_percent));bar.setAttribute('aria-label',label+'额度已用比例');row.append(bar);
      if(window.resets_at)row.append(node('small','small','重置时间 '+usageDate(window.resets_at)));session.append(row);
    }
    session.append(node('p','small','额度来自聊天最后记录，可能与中转站余额属于不同账号。'));
  }
  content.append(session);
}
async function refreshUsage() {
  const chat=state.chat;if(!chat||$('refresh-usage').disabled)return;
  $('refresh-usage').disabled=true;$('usage-error').textContent='正在读取…';
  try{
    const data=await api('/api/usage/'+encodeURIComponent(chat.id));
    if(state.chat?.id!==chat.id||!$('usage-settings').open)return;
    drawUsage(data);$('usage-error').textContent='';
  }catch(error){if(state.chat?.id===chat.id)$('usage-error').textContent=error.message;}
  finally{$('refresh-usage').disabled=false;}
}
$('usage-button').onclick=()=>{
  if(!state.chat)return;
  $('usage-chat').textContent=({codex:'Codex',claude:'Claude Code',grok:'Grok'})[state.chat.provider]+' · '+state.chat.title;
  $('usage-content').replaceChildren();$('usage-settings').showModal();refreshUsage();
};
$('refresh-usage').onclick=refreshUsage;
function drawModel(info) {
  state.modelInfo=info;
  const visible=!!state.chat;
  $('model-controls').hidden=!visible;
  $('choose-model').textContent=info?.requested_model?(state.chat?.provider==='codex'?'聊天模型：':'发送模型：')+modelLabel(info.requested_model):info?'请选择发送模型':'正在读取模型…';
  $('last-model').textContent=state.chat?.provider==='codex'?(info?.confirmed?'最近与桌面确认 · 下轮生效':'沿用原聊天 · 点击选择'):info?.last_response_model?'最近回复：'+info.last_response_model:'最近回复模型未记录';
  updateSend();
}
$('choose-model').onclick=async()=>{
  const id=state.chat?.id,provider=state.chat?.provider;if(!id)return;
  const codex=provider==='codex',grok=provider==='grok';
  $('model-title').textContent='这个聊天的 '+(codex?'Codex':grok?'Grok':'Claude')+' 模型';
  $('model-description').textContent=grok?'恢复原 Grok CLI 会话，并在每次发送时明确指定模型。进行中的任务完成后才能继续发送。':codex?'同步修改原聊天的下一轮模型，电脑端也会使用这个设置。进行中的回复继续使用原模型。':'从这里发送的任务使用下方模型，与 Claude 桌面端分别设置。修改在下次发送时生效。';
  $('model-help').textContent=grok?'模型列表来自此电脑 Grok 的本地目录，也可填写自定义模型 ID。':codex?'模型列表来自此电脑的 Codex。队列在执行时使用原聊天的模型；电脑端后续修改也会影响排队任务。中转站可填写模型 ID。':'Opus、Sonnet、Haiku 是 Claude Code 别名。中转站可填写完整模型 ID。回复标注接口报告的模型。';
  const select=$('model-choice');select.replaceChildren();$('model-error').textContent='';$('custom-model').hidden=true;
  $('save-model').disabled=true;$('model-settings').showModal();
  state.modelSaving=true;updateSend();
  try{
    const info=codex?await api('/api/codex/models',{thread_id:id}):state.modelInfo;
    if(state.chat?.id!==id){$('model-settings').close();return;}
    if(!info)throw Error('模型尚未读取，请稍后重试。');
    drawModel(info);
    if(!codex){const automatic=node('option','',info.automatic_model?(info.automatic_source==='default'?'使用此电脑助手默认：':'沿用最近回复：')+info.automatic_model:'请选择模型');automatic.value='';automatic.disabled=!info.automatic_model;select.append(automatic);}
    for(const model of info.choices){const option=node('option','',modelLabel(model));option.value=model;select.append(option);}
    const custom=node('option','','自定义模型 ID…');custom.value='__custom__';select.append(custom);
    select.value=info.selected||(codex?'__custom__':'');$('model-id').value='';$('custom-model').hidden=select.value!=='__custom__';
    $('model-error').textContent=info.warning||'';$('save-model').disabled=false;
  }catch(error){$('model-error').textContent=error.message;}
  finally{state.modelSaving=false;updateSend();}
};
$('model-choice').onchange=()=>{$('custom-model').hidden=$('model-choice').value!=='__custom__';};
$('model-form').onsubmit=async event=>{
  event.preventDefault();const id=state.chat?.id;if(!id||state.modelSaving)return;
  const choice=$('model-choice').value,model=choice==='__custom__'?$('model-id').value.trim():choice;
  if(choice==='__custom__'&&!model){$('model-error').textContent='请填写模型 ID。';return;}
  if(!model&&!state.modelInfo.automatic_model){$('model-error').textContent='请选择模型。';return;}
  state.modelSaving=true;$('save-model').disabled=true;updateSend();
  try{
    const info=await api(state.chat.provider==='codex'?'/api/codex/model':state.chat.provider==='grok'?'/api/grok/model':'/api/claude/model',{thread_id:id,model});
    if(state.chat?.id===id){drawModel(info);notice((state.chat.provider==='codex'?'已同步原聊天下一轮模型：':'下次发送使用：')+modelLabel(info.requested_model));}
    $('model-settings').close();
  }catch(error){$('model-error').textContent=error.message;}
  finally{state.modelSaving=false;$('save-model').disabled=false;updateSend();}
};
async function boot() {
  try {
    state.meta=await api('/api/meta');$('login').hidden=true;$('workspace').hidden=false;document.body.classList.add('chat-layout');updateViewport();
    $('device').replaceChildren();
    const peers=state.meta.peers.length?state.meta.peers:[{label:state.meta.label,origin:state.meta.origin}];
    for(const peer of peers) {const option=node('option','',peer.label+(peer.origin===state.meta.origin?' · 此电脑':''));option.value=peer.origin;option.selected=peer.origin===state.meta.origin;$('device').append(option);}
    drawProviderSelector();
    await loadChats();connection('已连接 · '+state.meta.label,true);
    const target=new URLSearchParams(location.search).get('chat')||storage.get('selected-chat');
    const chat=state.chats.find(c=>c.id===target);
    if(chat)await selectChat(chat);else drawer(window.innerWidth<=760);
    await loadNotificationSettings();
    prepareNotificationWorker().catch(error=>{$('notification-state').textContent=error.message;});
  }catch(error){if($('workspace').hidden){showLogin();$('pair-error').textContent=error.message;}else{connection('连接中断',false);notice(error.message);}}
}
$('pair-form').onsubmit=async event=>{event.preventDefault();try{await api('/api/pair',{token:$('token').value.trim()});$('token').value='';$('pair-error').textContent='';await boot();}catch(error){$('pair-error').textContent=error.message;}};
$('composer').onsubmit=async event=>{
  event.preventDefault();if(state.busy||!state.chat||$('send').disabled)return;
  const id=state.chat.id,text=$('message').value,image_ids=imageIds();const previous=pending(id);
  const attempt=previous?.text===text&&sameImages(previous.image_ids,image_ids)?previous:{thread_id:id,text,image_ids,request_id:crypto.randomUUID(),...(state.chat.provider==='grok'?{grok_model:state.modelInfo.requested_model}:state.chat.provider==='claude'?{claude_model:state.modelInfo.requested_model}:state.modelInfo?.requested_model?{codex_model:state.modelInfo.requested_model}:{})};
  storage.set(key('pending',id),JSON.stringify(attempt));storage.set(key('draft',id),text);
  state.busy=true;updateSend();notice('正在投递…');
  try {const receipt=await api('/api/send',attempt);settle(receipt,attempt,id);if(state.chat?.id===id)await refreshChat(true);}
  catch(error){if(error.status===409){storage.remove(key('pending',id));if(state.chat?.id===id){await refreshChat();notice(error.message);}}else if(state.chat?.id===id)notice(error.message+' 原文再次发送会确认同一条消息，不会重复入队。');}
  finally{state.busy=false;updateSend();}
};
$('message').oninput=()=>{if(state.chat)storage.set(key('draft'),$('message').value);resizeComposer();updateSend();};
$('add-image').onclick=()=>$('image-picker').click();
$('image-picker').onchange=event=>{const files=Array.from(event.target.files);event.target.value='';addImages(files);};
$('message').addEventListener('paste',event=>{const files=Array.from(event.clipboardData?.files||[]).filter(file=>file.type.startsWith('image/'));if(files.length){event.preventDefault();addImages(files);}});
$('composer').addEventListener('dragover',event=>event.preventDefault());
$('composer').addEventListener('drop',event=>{event.preventDefault();addImages(Array.from(event.dataTransfer?.files||[]).filter(file=>file.type.startsWith('image/')));});
$('message').onkeydown=event=>{if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)&&!event.isComposing){event.preventDefault();$('composer').requestSubmit();}};
$('search').oninput=drawList;$('menu').onclick=()=>drawer(true);$('scrim').onclick=()=>{drawer(false);markVisibleCompletion();};
$('refresh').onclick=()=>Promise.all([loadChats(),refreshChat(true)]).catch(error=>notice(error.message));
$('device').onchange=()=>{const url=$('device').value;if(url===state.meta.origin)return;if(state.chat)storage.set(key('draft'),$('message').value);location.assign(url);};
$('provider').onchange=async()=>{
  const select=$('provider'),previous=state.meta.provider||'codex';select.disabled=true;
  try {
    const info=await api('/api/provider',{provider:select.value});Object.assign(state.meta,info);drawProviderSelector();
    await loadChats();
    const current=state.chat&&state.chats.find(chat=>chat.id===state.chat.id);
    if(current) await selectChat(current);
    else {state.chat=null;drawModel(null);$('chat-title').textContent='选择一个聊天';$('chat-path').textContent='在这里继续桌面上的任务';$('message').disabled=true;drawList();}
    notice('已切换到 '+({codex:'Codex',claude:'Claude Code',all:'Codex + Claude Code'}[state.meta.provider]||state.meta.provider));
  } catch(error) {select.value=previous;notice(error.message);}
  finally {select.disabled=false;drawProviderSelector();}
};
$('logout').onclick=async()=>{await api('/api/logout',{});state.chat=null;showLogin();drawer(false);};
$('jump').onclick=()=>{$('conversation').scrollTop=$('conversation').scrollHeight;$('jump').hidden=true;};
$('conversation').onscroll=()=>{const box=$('conversation');if(box.scrollHeight-box.scrollTop-box.clientHeight<100)$('jump').hidden=true;markVisibleCompletion();};
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('close',markVisibleCompletion);
window.addEventListener('storage',event=>{if(event.key?.startsWith('seen-completion:'))drawList();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.meta)refreshChat();});
window.addEventListener('online',()=>{if(state.meta)refreshChat();});
const viewportState={frame:0,width:0,fullHeight:0};
function updateViewport() {
  cancelAnimationFrame(viewportState.frame);
  viewportState.frame=requestAnimationFrame(()=>{
    const viewport=window.visualViewport,box=$('conversation');
    const height=Math.round(viewport?.height||window.innerHeight),width=Math.round(viewport?.width||window.innerWidth);
    const focused=/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName||'');
    if(width!==viewportState.width){viewportState.fullHeight=height;viewportState.width=width;}
    if(!focused)viewportState.fullHeight=Math.max(viewportState.fullHeight,height);
    const near=box.scrollHeight-box.scrollTop-box.clientHeight<100,top=box.scrollTop;
    document.documentElement.style.setProperty('--visible-height',height+'px');
    document.documentElement.style.setProperty('--viewport-top',Math.max(0,Math.round(viewport?.offsetTop||0))+'px');
    document.body.classList.toggle('keyboard-open',focused&&viewportState.fullHeight-height>120);
    requestAnimationFrame(()=>{box.scrollTop=near?box.scrollHeight:top;});
  });
}
window.visualViewport?.addEventListener('resize',updateViewport);
window.visualViewport?.addEventListener('scroll',updateViewport);
window.addEventListener('resize',updateViewport);
document.addEventListener('focusin',updateViewport);
document.addEventListener('focusout',()=>setTimeout(updateViewport,100));
updateViewport();
setInterval(async()=>{
  if(document.hidden||$('workspace').hidden||state.refreshing)return;
  state.refreshing=true;try{await refreshChat();if(Date.now()-state.lastList>5000)await loadChats();}catch{}finally{state.refreshing=false;}
},3000);
(async()=>{
  const fragment=new URLSearchParams(location.hash.slice(1));const token=fragment.get('pair');
  if(location.hash)history.replaceState(null,'',location.pathname);
  if(token){try{await api('/api/pair',{token});}catch(error){showLogin();$('pair-error').textContent=error.message;return;}}
  await boot();
})();

function questionCard(message,chatId) {
  const form=node('form','question-card'),draftKey=`question:${chatId}:${message.id}`;
  let saved={};try{saved=JSON.parse(storage.get(draftKey,'{}'));}catch{}
  const values=Array.isArray(saved.answers)?saved.answers:[];
  const answered=message.questions.every(question=>question.answer!==null&&question.answer!==undefined);
  const recoverQueued=answered&&message.questions.every(q=>q.reply_status==='queued'&&q.reply_request_id===message.questions[0].reply_request_id);
  const controls=[];
  for(const [index,question] of message.questions.entries()) {
    const field=node('fieldset');field.append(node('legend','',question.title));
    const existing=question.answer??values[index]??'';
    for(const option of question.options||[]) {
      const label=node('label','question-option'),radio=node('input');radio.type='radio';radio.name=`answer-${index}`;radio.value=option;radio.checked=existing===option;radio.disabled=answered;
      label.append(radio,node('span','',option));field.append(label);
      radio.onchange=()=>{values[index]=option;free.value='';persist();};controls.push(radio);
    }
    const free=node('textarea');free.rows=2;free.maxLength=4000;free.placeholder='也可以填写自己的回答';free.setAttribute('aria-label',`自行回答：${question.title}`);
    free.value=(question.options||[]).includes(existing)?'':existing;free.disabled=answered;
    free.oninput=()=>{values[index]=free.value;for(const radio of field.querySelectorAll('input'))radio.checked=false;persist();};
    controls.push(free);field.append(free);form.append(field);values[index]=existing;
  }
  const replyLabel=message.questions.every(q=>q.reply_status==='received')?'已回复':message.questions.some(q=>q.reply_status==='uncertain')?'投递待确认':message.questions.every(q=>q.reply_status==='submitted')?'已提交':'已入队';
  const status=node('p','question-status',answered?replyLabel+'：'+message.questions.map(q=>q.answer).join('；'):saved.request_id?'上次投递等待确认，点击可查询同一条回复。':'请选择选项，或填写自己的回答。');status.setAttribute('role','status');
  const submit=node('button','primary',recoverQueued?'立即提交':answered?replyLabel:'提交回复');submit.type='submit';submit.disabled=answered&&!recoverQueued;
  function persist(){saved.answers=values;storage.set(draftKey,JSON.stringify(saved));}
  form.append(status,submit);
  form.onsubmit=async event=>{
    event.preventDefault();if((answered&&!recoverQueued)||submit.disabled)return;
    if(values.length!==message.questions.length||values.some(value=>!value?.trim())){status.textContent='请回答每个问题。';return;}
    const signature=JSON.stringify(values);
    if(recoverQueued){saved.request_id=message.questions[0].reply_request_id;saved.signature=signature;}
    else if(saved.signature!==signature){saved.request_id=crypto.randomUUID();saved.signature=signature;}
    persist();submit.disabled=true;for(const control of controls)control.disabled=true;status.textContent='正在提交…';
    try {
      const receipt=await api('/api/answer',{thread_id:chatId,message_id:message.id,answers:values,request_id:saved.request_id});
      if(receipt.result==='failed'){delete saved.request_id;persist();throw Error(receipt.error||'提交失败，请重试。');}
      status.textContent=receipt.result==='sent'?(receipt.delivery_mode==='immediate'?'桌面已接收回复，正在继续执行。':receipt.error||'回复已入队，等待电脑接收。'):'投递结果待确认，请先检查原聊天。';
      if(state.chat?.id===chatId)await refreshChat();
    }catch(error){status.textContent=error.message+' 再次提交会确认同一条回复。';submit.disabled=false;for(const control of controls)control.disabled=false;}
  };
  return form;
}

const notificationState={enabled:false,push:false,cursor:0,loaded:false,polling:false};
async function loadNotificationSettings() {
  const settings=await api('/api/notifications');
  if(notificationState.loaded)settings.cursor=notificationState.cursor;
  Object.assign(notificationState,settings,{loaded:true});drawNotificationSettings();
}
function drawNotificationSettings() {
  const supported='Notification'in window&&'PushManager'in window&&'serviceWorker'in navigator&&window.isSecureContext;
  $('enable-local').hidden=notificationState.enabled;
  $('enable-push').disabled=!supported;
  $('enable-push').textContent=notificationState.push?'重新连接系统推送':'开启系统推送';
  $('test-push').hidden=!notificationState.push;
  $('disable-notifications').hidden=!notificationState.enabled;
  $('notifications-button').classList.toggle('enabled',notificationState.enabled);
  $('notification-state').textContent=notificationState.error||(notificationState.push?'系统推送已连接。可点“测试推送”确认手机接收回执。':notificationState.enabled?'页面内提醒已开启；关闭页面后需系统推送。':'提醒尚未开启。');
  if(!supported)$('notification-state').textContent+=' 当前浏览器不支持系统推送；iPhone 请从主屏幕打开。';
}
function showTaskNotification(value) {
  if(value.created&&Date.now()/1000-value.created>120)return;
  const container=$('notification-toasts');
  for(const previous of container.children)if(previous.dataset.thread===value.thread_id)previous.remove();
  const item=node('div','notification-toast'),open=node('button','',value.title+' · '+value.body.replace('，点击查看。','')),close=node('button','toast-close','×');
  item.dataset.thread=value.thread_id;
  close.setAttribute('aria-label','关闭提醒');close.onclick=()=>item.remove();
  open.onclick=async()=>{item.remove();const chat=state.chats.find(chat=>chat.id===value.thread_id);if(chat)await selectChat(chat);else{await loadChats();const current=state.chats.find(chat=>chat.id===value.thread_id);if(current)await selectChat(current);}};
  item.append(open,close);container.append(item);
  while(container.children.length>2)container.firstElementChild.remove();
  setTimeout(()=>item.remove(),7000);
}
async function pollNotifications() {
  if(!notificationState.enabled||notificationState.polling||document.hidden||$('workspace').hidden)return;
  notificationState.polling=true;
  try {
    const data=await api('/api/notifications/events?after='+notificationState.cursor);
    for(const event of data.events)if(event.id>notificationState.cursor)showTaskNotification(event);
    notificationState.cursor=Math.max(notificationState.cursor,data.cursor);
  }catch{}finally{notificationState.polling=false;}
}
$('notifications-button').onclick=()=>{$('notification-settings').showModal();Promise.all([loadNotificationSettings(),inspectNotificationWorker()]).catch(error=>$('notification-state').textContent=error.message);};
$('enable-local').onclick=async()=>{try{Object.assign(notificationState,await api('/api/notifications',{enabled:true}));drawNotificationSettings();}catch(error){$('notification-state').textContent=error.message;}};
$('enable-push').onclick=async()=>{
  const button=$('enable-push');button.disabled=true;
  try {
    const permission=await Notification.requestPermission();
    if(permission!=='granted')throw Error('未获得通知权限，请在系统或浏览器设置中允许通知。');
    const registration=await currentNotificationWorker();
    const raw=notificationState.public_key.replace(/-/g,'+').replace(/_/g,'/');
    const applicationServerKey=Uint8Array.from(atob(raw+'='.repeat((4-raw.length%4)%4)),c=>c.charCodeAt(0));
    const previous=await registration.pushManager.getSubscription();
    if(previous)await previous.unsubscribe();
    const subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey});
    Object.assign(notificationState,await api('/api/notifications',{enabled:true,subscription:subscription.toJSON()}));drawNotificationSettings();
    await inspectNotificationWorker();
  }catch(error){$('notification-state').textContent=error.message;button.disabled=false;}
};
$('test-push').onclick=async()=>{const button=$('test-push');button.disabled=true;try{
  await inspectNotificationWorker();
  const result=await api('/api/notifications/test',{});
  $('notification-state').textContent='推送服务已接受；正在等待手机接收回执…可以退到主屏幕查看通知。';
  notificationState.testing=result.delivery_id;
  await checkDelivery(result.delivery_id);
}catch(error){$('notification-state').textContent=error.message;}finally{button.disabled=false;}};
$('disable-notifications').onclick=async()=>{try{Object.assign(notificationState,await api('/api/notifications',{enabled:false}));drawNotificationSettings();if('serviceWorker'in navigator){const registration=await navigator.serviceWorker.getRegistration();const subscription=await registration?.pushManager?.getSubscription();if(subscription)await subscription.unsubscribe();}}catch(error){$('notification-state').textContent=error.message;}};
navigator.serviceWorker?.addEventListener('message',event=>{if(event.data?.type==='open-chat'){const chat=state.chats.find(chat=>chat.id===event.data.thread_id);if(chat)selectChat(chat);}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)pollNotifications();});
setInterval(pollNotifications,4000);

async function workerInfo(worker) {
  if(!worker)throw Error('通知组件未激活。');
  return new Promise((resolve,reject)=>{
    const channel=new MessageChannel(),timeout=setTimeout(()=>{channel.port1.close();reject(Error('通知组件仍是旧版或未响应。请点“重新连接系统推送”。'));},2500);
    channel.port1.onmessage=event=>{clearTimeout(timeout);channel.port1.close();resolve(event.data);};
    worker.postMessage({type:'relay-diagnostics'},[channel.port2]);
  });
}
async function currentNotificationWorker() {
  const registration=await navigator.serviceWorker.register('/sw.js',{updateViaCache:'none'});
  await registration.update();
  const deadline=Date.now()+15000;
  while(Date.now()<deadline) {
    try{const info=await workerInfo(registration.active);if(info.version==='1.3.7'&&info.push_handler)return registration;}catch{}
    await new Promise(resolve=>setTimeout(resolve,250));
  }
  throw Error('通知组件更新未完成。请关闭主屏幕应用后重新打开，再点“重新连接系统推送”。');
}
async function prepareNotificationWorker() {
  if(!('serviceWorker'in navigator)||!window.isSecureContext)return;
  const registration=await currentNotificationWorker();
  if('Notification'in window&&Notification.permission==='granted'&&registration.pushManager){
    const subscription=await registration.pushManager.getSubscription();
    if(subscription){
      const settings=await api('/api/notifications/sync',{subscription:subscription.toJSON()});
      Object.assign(notificationState,settings,{cursor:notificationState.cursor});
      drawNotificationSettings();
    }else if(notificationState.push){
      $('notification-state').textContent='手机当前没有推送订阅，请点“重新连接系统推送”。';
    }
  }
  await inspectNotificationWorker();
}
async function inspectNotificationWorker() {
  const standalone=matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
  const permission='Notification'in window?Notification.permission:'unsupported';
  let version='未连接';
  try{const registration=await navigator.serviceWorker.getRegistration();const info=await workerInfo(registration?.active);version=info.version;}catch{}
  $('notification-diagnostics').textContent=`应用 1.3.7 · 通知组件 ${version} · ${standalone?'主屏幕模式':'浏览器模式'} · 权限 ${permission==='granted'?'已允许':permission==='denied'?'已拒绝':'未允许'}`;
  await api('/api/notifications/client',{permission,standalone,worker:version});
  return {permission,standalone,version};
}
function describeDelivery(receipt) {
  if(receipt.clicked)return '手机已收到，并已点击通知。';
  if(receipt.shown)return '手机已收到，系统已接受显示。若通知栏没有提示，请检查 iPhone 通知设置、专注模式或通知摘要。';
  if(receipt.error==='NotAllowedError')return '手机收到推送，但系统拒绝显示。请在 iPhone 设置 → 通知中允许 Codex 消息通知。';
  if(receipt.received)return receipt.error?'手机收到推送，但显示失败：'+receipt.error:'手机已收到，正在等待系统显示回执。';
  if(receipt.error)return '推送未送达：'+receipt.error;
  return '推送服务已接受，但尚无手机回执。请先点“测试本机通知”；若本机能显示，再重新连接系统推送。手机不通 Tailscale 时回执也无法返回。';
}
async function checkDelivery(identity) {
  const receipt=await api('/api/notifications/delivery/'+identity);
  $('notification-state').textContent=describeDelivery(receipt);
  if(receipt.shown||receipt.error||receipt.clicked)notificationState.testing=null;
}
$('test-local-notification').onclick=async()=>{
  const button=$('test-local-notification');button.disabled=true;
  try {
    if(!('Notification'in window)||Notification.permission!=='granted')throw Error('尚未允许系统通知，请先开启系统推送。');
    const registration=await currentNotificationWorker();
    await registration.showNotification('Codex 本机通知测试',{body:'这条通知由手机直接显示，不经过 Apple 远程推送。',tag:'codex-local-test-'+Date.now(),icon:'/icon-192.png'});
    $('notification-state').textContent='手机系统已接受本机通知。请退到主屏幕查看通知中心；若仍没有，问题在手机的通知显示设置。';
    await inspectNotificationWorker();
  }catch(error){$('notification-state').textContent=error.message;}finally{button.disabled=false;}
};
setInterval(()=>{if(notificationState.testing&&!document.hidden)checkDelivery(notificationState.testing).catch(()=>{});},3000);
