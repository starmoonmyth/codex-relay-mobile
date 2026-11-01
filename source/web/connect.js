let setup;
const $ = id => document.getElementById(id);
fetch('/api/local/setup', {cache:'no-store'}).then(async response => {
  const data = await response.json(); if (!response.ok) throw Error(data.error);
  setup = data; $('setup-status').textContent = `${data.label} 已就绪，手机配对后即可发送任务。`;
  $('phone-link').value = data.origin; $('manual-token').value = data.token; $('setup').hidden = false;
}).catch(error => { $('setup-error').textContent = error.message; $('qr').hidden = true; });
async function copy(text, button) {
  try { await navigator.clipboard.writeText(text); button.textContent = '已复制'; }
  catch { $('setup-error').textContent = '无法自动复制，请选中地址或配对码后手动复制。'; }
}
$('copy').onclick = () => copy(setup.link, $('copy'));
$('copy-token').onclick = () => copy(setup.token, $('copy-token'));
$('stop').onclick = async () => {
  const response = await fetch('/api/local/stop', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(response.ok) { $('setup').hidden = true; $('setup-status').textContent = '已停止。需要时重新双击手机消息助手即可。'; }
};
