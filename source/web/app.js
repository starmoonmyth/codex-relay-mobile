const $ = id => document.getElementById(id);
const state = {chat:null,chats:[],meta:null,busy:false,refreshing:false,snapshot:'',lastList:0};
const attachmentDrafts = new Map();
const storage = {
  get(key, fallback='') { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key,value); } catch {} },
  remove(key) { try { localStorage.removeItem(key); } catch {} }
};
const key = (kind, id=state.chat?.id) => `${kind}:${id}`;
function pending(id=state.chat?.id) { try { return JSON.parse(storage.get(key('pending',id),'null')); } catch { return null; } }
function notice(text) { $('send-notice').textContent = text; }
function connection(text, online) { $('connection').textContent=text; $('connection').classList.toggle('online',online); }
async function api(path, body) {
  let response;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),path==='/api/send'||path==='/api/answer'?50000:15000);
  try {response = await fetch(path, {cache:'no-store', credentials:'same-origin',
    signal:controller.signal,
    ...(body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})});}
  catch {throw Error('电脑响应超时或连接中断，请确认电脑与 Tailscale 在线。');}
  finally{clearTimeout(timeout);}
  const value = await response.json();
  if(response.status===401) { showLogin(); throw Error('请先配对这台电脑。'); }
  if(!response.ok) throw Error(value.error || `请求失败 (${response.status})`);
  return value;
}
function showLogin() { document.body.classList.remove('chat-layout','keyboard-open');$('workspace').hidden=true;$('login').hidden=false; }
function drawer(open) { document.body.classList.toggle('drawer',open); $('scrim').hidden=!open; }
function node(tag, className, text) { const item=document.createElement(tag); if(className)item.className=className; if(text!==undefined)item.textContent=text; return item; }
function timeLabel(raw) { const date=new Date(raw>1e12?raw:raw*1000),days=Math.floor((Date.now()-date)/86400000); return days===0?'今天':days===1?'昨天':date.toLocaleDateString('zh-CN',{month:'numeric',day:'numeric'}); }
function drawList() {
  const query=$('search').value.toLowerCase(); const list=$('chat-list'); list.replaceChildren();
  const chats=state.chats.filter(c=>(c.title+' '+c.cwd).toLowerCase().includes(query));
  for(const chat of chats) {
    const button=node('button','chat'+(chat.id===state.chat?.id?' active':''));
    button.append(node('span','chat-name',(chat.pinned?'⌁ ':'')+chat.title));
    const meta=node('span','chat-meta'); const project=chat.cwd.replaceAll('\\','/').split('/').filter(Boolean).pop()||'未指定项目';
    meta.append(node('span','project',project),node('span','',timeLabel(chat.updated_at)));button.append(meta);
    button.onclick=()=>selectChat(chat); list.append(button);
  }
  if(!chats.length)list.append(node('p','small',state.chats.length?'没有匹配的聊天。':'还没有聊天。请先在电脑的 Codex 中打开一个聊天。'));
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
  const images=imagesFor(),loading=images.some(image=>image.uploading||image.failed);
  $('send').disabled=!state.chat||(!$('message').value.trim()&&!images.length)||state.busy||loading;
  $('add-image').disabled=!state.chat||state.busy;
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
  if(state.chat)storage.set(key('draft'),$('message').value);
  state.chat=chat;state.snapshot='';storage.set('selected-chat',chat.id);
  $('chat-title').textContent=chat.title; $('chat-path').textContent=chat.cwd;
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
  const box=$('conversation');const near=box.scrollHeight-box.scrollTop-box.clientHeight<100;
  const signature=JSON.stringify(data);if(signature===state.snapshot)return;
  state.snapshot=signature;
  const fragment=document.createDocumentFragment();
  for(const m of data.messages) {
    const bubble=node('article','message '+m.role+(m.phase==='commentary'?' progress':''));
    if(m.role==='user') {
      if(m.images?.length)bubble.append(imageGallery(m.images));
      else if(m.image_count)bubble.append(node('span','small',`图片附件 ${m.image_count} 张 · 可在电脑查看`));
      if(m.text)bubble.append(node('div','user-text',m.text));
    }
    else{bubble.append(node('span','speaker',m.questions?.length?'CODEX · 需要回复':m.phase==='commentary'?'CODEX · 进展':'CODEX'));if(m.questions?.length)bubble.append(questionCard(m,data.chat.id));else bubble.append(markdown(m.text));}
    fragment.append(bubble);
  }
  for(const receipt of data.receipts) {
    if(receipt.seen_in_history)continue;
    const card=node('article','pending');card.append(node('div','pending-text',receipt.display_text??receipt.text),node('div','pending-status',receipt.status));
    if(receipt.images?.length)card.prepend(imageGallery(receipt.images));
    if(receipt.error)card.append(node('div','pending-error',receipt.error));fragment.append(card);
  }
  if(!fragment.childNodes.length)fragment.append(node('p','small','还没有消息。在下方输入任务，继续这个聊天。'));
  const oldTop=box.scrollTop;box.replaceChildren(fragment);
  if(near||force){box.scrollTop=box.scrollHeight;$('jump').hidden=true;} else {box.scrollTop=oldTop;$('jump').hidden=false;}
  const turn=data.turn?.status;$('turn-status').textContent=({inProgress:'处理中',completed:'已完成',failed:'执行失败',interrupted:'已中断'})[turn]||'就绪';
  const attempt=pending(data.chat.id);
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
      notice(receipt.status==='已完成'?'已完成。':receipt.warning?'消息已入队，CLI 有提示，请查看原聊天。':'已入队，电脑接收后会在这里显示回复。');
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
  const data=await api('/api/chats');state.chats=data.chats;state.lastList=Date.now();drawList();
}
async function boot() {
  try {
    state.meta=await api('/api/meta');$('login').hidden=true;$('workspace').hidden=false;document.body.classList.add('chat-layout');updateViewport();
    $('device').replaceChildren();
    const peers=state.meta.peers.length?state.meta.peers:[{label:state.meta.label,origin:state.meta.origin}];
    for(const peer of peers) {const option=node('option','',peer.label+(peer.origin===state.meta.origin?' · 此电脑':''));option.value=peer.origin;option.selected=peer.origin===state.meta.origin;$('device').append(option);}
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
  const attempt=previous?.text===text&&sameImages(previous.image_ids,image_ids)?previous:{thread_id:id,text,image_ids,request_id:crypto.randomUUID()};
  storage.set(key('pending',id),JSON.stringify(attempt));storage.set(key('draft',id),text);
  state.busy=true;updateSend();notice('正在投递…');
  try {const receipt=await api('/api/send',attempt);settle(receipt,attempt,id);if(state.chat?.id===id)await refreshChat(true);}
  catch(error){if(state.chat?.id===id)notice(error.message+' 原文再次发送会确认同一条消息，不会重复入队。');}
  finally{state.busy=false;updateSend();}
};
$('message').oninput=()=>{if(state.chat)storage.set(key('draft'),$('message').value);resizeComposer();updateSend();};
$('add-image').onclick=()=>$('image-picker').click();
$('image-picker').onchange=event=>{const files=Array.from(event.target.files);event.target.value='';addImages(files);};
$('message').addEventListener('paste',event=>{const files=Array.from(event.clipboardData?.files||[]).filter(file=>file.type.startsWith('image/'));if(files.length){event.preventDefault();addImages(files);}});
$('composer').addEventListener('dragover',event=>event.preventDefault());
$('composer').addEventListener('drop',event=>{event.preventDefault();addImages(Array.from(event.dataTransfer?.files||[]).filter(file=>file.type.startsWith('image/')));});
$('message').onkeydown=event=>{if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)&&!event.isComposing){event.preventDefault();$('composer').requestSubmit();}};
$('search').oninput=drawList;$('menu').onclick=()=>drawer(true);$('scrim').onclick=()=>drawer(false);
$('refresh').onclick=()=>Promise.all([loadChats(),refreshChat(true)]).catch(error=>notice(error.message));
$('device').onchange=()=>{const url=$('device').value;if(url===state.meta.origin)return;if(state.chat)storage.set(key('draft'),$('message').value);location.assign(url);};
$('logout').onclick=async()=>{await api('/api/logout',{});state.chat=null;showLogin();drawer(false);};
$('jump').onclick=()=>{$('conversation').scrollTop=$('conversation').scrollHeight;$('jump').hidden=true;};
$('conversation').onscroll=()=>{const box=$('conversation');if(box.scrollHeight-box.scrollTop-box.clientHeight<100)$('jump').hidden=true;};
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
  state.refreshing=true;try{await refreshChat();if(Date.now()-state.lastList>30000)await loadChats();}catch{}finally{state.refreshing=false;}
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
    try{const info=await workerInfo(registration.active);if(info.version==='1.2.3'&&info.push_handler)return registration;}catch{}
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
  $('notification-diagnostics').textContent=`应用 1.2.3 · 通知组件 ${version} · ${standalone?'主屏幕模式':'浏览器模式'} · 权限 ${permission==='granted'?'已允许':permission==='denied'?'已拒绝':'未允许'}`;
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
