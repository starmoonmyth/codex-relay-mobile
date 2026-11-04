(() => {
  const panels = [...document.querySelectorAll('[data-updates]')];
  if (!panels.length) return;
  let ready = false, checking = false, lastAuto = 0, previous;

  function draw(value, manual) {
    previous = value;
    for (const panel of panels) {
      panel.querySelector('[data-update-version]').textContent = `版本 ${value.current_version}`;
      panel.querySelector('[data-update-status]').textContent = value.error ?
        (value.available ? `上次查询发现新版 ${value.latest_version}${manual ? '；本次检查暂不可用' : ''}` : manual ? value.error : '') :
        value.available ? `发现新版 ${value.latest_version}` : value.latest_version ?
          (value.current_version === value.public_baseline ? '已是当前渠道最新版本' : `本地构建已包含公开版 ${value.public_baseline}`) : '';
      const link = panel.querySelector('[data-update-link]');
      link.hidden = !value.available && !(manual && value.error);
      link.href = value.release_url;
      link.textContent = value.available ? '前往 GitHub 下载' : '打开 GitHub 发布页';
      panel.querySelector('[data-update-channel]').textContent = value.current_version !== value.public_baseline ?
        `本地构建 · 公开基线 ${value.public_baseline}` : value.channel === 'preview' ? '预览版渠道' : '稳定版渠道';
    }
    const banner = document.getElementById('update-banner');
    if (banner) {
      banner.hidden = !value.available;
      banner.querySelector('span').textContent = `有新版本 ${value.latest_version}，请在这台电脑上更新连接服务。`;
      banner.querySelector('a').href = value.release_url;
    }
  }

  async function check(manual = false) {
    if (checking || !ready) return;
    if (!manual && Date.now() - lastAuto < 86400000) return;
    if (!manual) lastAuto = Date.now();
    checking = true;
    for (const panel of panels) {
      panel.querySelector('button').disabled = true;
      if (manual) panel.querySelector('[data-update-status]').textContent = '正在检查…';
    }
    try {
      const options = {cache: 'no-store'};
      if (manual) Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      const response = await fetch('/api/updates', options);
      if (!response.ok) throw Error('暂时无法检查更新，请稍后重试。');
      draw(await response.json(), manual);
    } catch (_) {
      if (manual) {
        if (previous) draw(previous, false);
        for (const panel of panels) panel.querySelector('[data-update-status]').textContent = '暂时无法检查更新，请稍后重试。';
      }
    } finally {
      checking = false;
      for (const panel of panels) panel.querySelector('button').disabled = false;
    }
  }

  for (const panel of panels) panel.querySelector('button').addEventListener('click', () => check(true));
  window.addEventListener('relay-ready', () => { ready = true; check(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
})();
