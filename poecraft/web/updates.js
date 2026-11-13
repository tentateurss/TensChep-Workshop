"use strict";
(async function () {
  const el = (id) => document.getElementById('updates-' + id);
  let strings = {};
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
    el('link').hidden = !s.release;
    if (s.release) { el('link').href = s.release.url; el('link').textContent = t('download'); }
  }
  el('check').onclick = el('channel').onchange = async () => {
    try {
      await fetch('/api/application-updates/check?channel=' + encodeURIComponent(el('channel').value), {method:'POST',headers:{'X-Workshop-Update':'1'}});
      await draw();
    } catch (_) { el('status').textContent = strings['updates.error'] || 'Update check failed'; }
  };
  setInterval(() => draw().catch(() => {}), 5000);
  await draw();
})().catch(() => {});
