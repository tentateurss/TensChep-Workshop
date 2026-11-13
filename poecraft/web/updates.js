"use strict";
(async function () {
  const el = (id) => document.getElementById('updates-' + id);
  let strings = {};
  let dismissed = null;
  async function draw() {
    const lang = localStorage.getItem('poecraft.lang') || 'en';
    const locale = await (await fetch('/api/locale?lang=' + encodeURIComponent(lang))).json();
    strings = {...locale.en, ...locale.strings};
    const t = (key) => strings['updates.' + key] || key;
    const s = await (await fetch('/api/application-updates')).json();
    el('title').textContent = t('title'); el('check').textContent = t('check');
    el('channel').value = s.channel;
    el('status').textContent = t('installed') + s.current + ' · ' + t(s.state === 'idle' ? 'checking' : s.state) + (s.release ? s.release.version : '');
    el('notes').textContent = s.release ? s.release.notes : '';
    el('notes').dataset.version = s.release?.version || '';
    el('link').hidden = !s.release;
    const busy = ['downloading','verifying','preparing','restarting'].includes(s.installation?.state);
    el('install').hidden = !s.release || !s.install_supported || dismissed === s.release?.version;
    el('install').disabled = busy;
    el('install').textContent = t('install');
    el('later').hidden = !s.release; el('later').textContent = t('later');
    if (s.installation?.state !== 'idle') el('status').textContent += ' ? ' + (s.installation.error || t(s.installation.state));
    if (s.release) { el('link').href = s.release.url; el('link').textContent = t('download'); }
  }
  el('later').onclick = () => { dismissed = el('notes').dataset.version; el('notes').textContent = ''; el('install').hidden = true; };
  el('install').onclick = async () => {
    if (!confirm(strings['updates.confirm'])) return;
    await fetch('/api/application-updates/install', {method:'POST',headers:{'X-Workshop-Update':'1'}});
    await draw();
  };
  el('check').onclick = el('channel').onchange = async () => {
    try {
      await fetch('/api/application-updates/check?channel=' + encodeURIComponent(el('channel').value), {method:'POST',headers:{'X-Workshop-Update':'1'}});
      await draw();
    } catch (_) { el('status').textContent = strings['updates.error'] || 'Update check failed'; }
  };
  setInterval(() => draw().catch(() => {}), 5000);
  await draw();
})().catch(() => {});
