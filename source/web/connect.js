let setup;
const $ = id => document.getElementById(id);
fetch('/api/local/setup', {cache:'no-store'}).then(async response => {
  const data = await response.json(); if (!response.ok) throw Error(data.error);
  setup = data; $('setup-status').textContent = `${data.label} 已就绪，手机配对后即可发送任务。`;
  $('service-status').textContent=data.startup?.enabled?`已启用登录启动，异常退出后约 ${data.startup.recovery_seconds} 秒内恢复。关闭此网页不会停止服务。`:'自动恢复未启用'+(data.startup?.error?'：'+data.startup.error:'；重新双击应用可尝试启用。');
  $('provider').value = data.provider || 'codex';
  const available = data.available || {};
  $('provider-status').textContent = `Codex ${available.codex ? '可用' : '未找到'} · Claude Code ${available.claude ? '可用' : '未找到会话或 CLI'} · Grok ${available.grok ? '可用' : '未找到会话或 CLI'}`;
  $('phone-link').value = data.origin; $('manual-token').value = data.token; $('setup').hidden = false;
  window.dispatchEvent(new Event('relay-ready'));
}).catch(error => { $('setup-error').textContent = error.message; $('qr').hidden = true; });
async function copy(text, button) {
  try { await navigator.clipboard.writeText(text); button.textContent = '已复制'; }
  catch { $('setup-error').textContent = '无法自动复制，请选中地址或配对码后手动复制。'; }
}
$('copy').onclick = () => copy(setup.link, $('copy'));
$('copy-token').onclick = () => copy(setup.token, $('copy-token'));
$('provider').onchange = async () => {
  const select = $('provider'); select.disabled = true;
  try {
    const response = await fetch('/api/local/provider', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:select.value})});
    const value = await response.json(); if (!response.ok) throw Error(value.error || '切换工作台失败');
    setup.provider = value.provider; $('provider-status').textContent = `已切换为 ${({all:'全部工作台',claude:'Claude Code',grok:'Grok',codex:'Codex'})[value.provider]}`;
  } catch (error) { $('setup-error').textContent = error.message; select.value = setup.provider || 'codex'; }
  finally { select.disabled = false; }
};
$('stop').onclick = async () => {
  const response = await fetch('/api/local/stop', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(response.ok) { $('setup').hidden = true; $('setup-status').textContent = '已主动停止，自动恢复已暂停。重新双击应用可恢复，手机继续使用原主屏幕图标。'; }
};
