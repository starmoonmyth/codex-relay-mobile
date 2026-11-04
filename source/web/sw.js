const VERSION='1.3.8';
const CACHE='codex-relay-mobile-v9';
const SHELL=['/','/style.css','/app.js','/updates.js','/marked.js','/purify.js','/manifest.webmanifest','/icon-192.png','/icon-512.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('codex-relay-mobile-')&&key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);
  if(event.request.method!=='GET'||url.origin!==location.origin||!SHELL.includes(url.pathname))return;
  event.respondWith(fetch(event.request).then(response=>{if(response.ok){const copy=response.clone();event.waitUntil(caches.open(CACHE).then(cache=>cache.put(url.pathname,copy)));}return response;}).catch(()=>caches.match(url.pathname)));
});
self.addEventListener('push',event=>{
  let value;try{value=event.data.json();}catch{value={title:'Codex',body:'有任务状态更新。'};}
  const thread=typeof value.thread_id==='string'?value.thread_id:'';
  const source=typeof value.source_origin==='string'?value.source_origin:'';
  const received=reportDelivery(value.receipt_token,'received');
  const shown=self.registration.showNotification(value.title||'Codex',{
    body:value.body||'有任务状态更新。',icon:'/icon-192.png',badge:'/icon-192.png',
    tag:'codex-'+source+'|'+(thread||value.kind||'update'),data:{thread_id:thread,source_origin:source,receipt_token:value.receipt_token}
  }).then(()=>reportDelivery(value.receipt_token,'shown'),error=>reportDelivery(value.receipt_token,'failed',error.name));
  event.waitUntil(Promise.all([received,shown]));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();const data=event.notification.data||{};
  const target=new URL('/',self.location.origin);
  target.searchParams.set('chat',data.thread_id||'');
  if(data.source_origin)target.searchParams.set('computer',data.source_origin);
  const url=target.href;
  event.waitUntil(Promise.all([reportDelivery(data.receipt_token,'clicked'),self.clients.matchAll({type:'window',includeUncontrolled:true}).then(async clients=>{
    const existing=clients.find(client=>new URL(client.url).origin===self.location.origin);
    if(existing){await existing.navigate(url);return existing.focus();}
    return self.clients.openWindow(url);
  })]));
});
async function reportDelivery(token,stage,error='') {
  if(!token)return;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
  try {await fetch('/api/notifications/ack',{method:'POST',credentials:'include',cache:'no-store',signal:controller.signal,
    headers:{'Content-Type':'application/json'},body:JSON.stringify({token,stage,error})});}catch{}finally{clearTimeout(timeout);}
}
self.addEventListener('message',event=>{
  if(event.data?.type==='relay-diagnostics'&&event.ports[0])event.ports[0].postMessage({version:VERSION,push_handler:true});
});
