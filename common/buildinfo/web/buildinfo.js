// Общий блок buildinfo (стандарт 6.5): окно не говорит со старым сервером. При старте и раз в минуту - /api/info:
//   build ≠ disk_build - сервер запущен до правки кода: «Программа обновилась - перезапустите» → POST /api/restart, ждём
//                        сервер на новом коде, перезагружаем окно;
//   build ≠ сборки, с которой открыто окно - сервер уже на новом коде, а окно старое: та же полоса → перезагрузка окна.
// Подключение: <script src="/common/buildinfo/buildinfo.js"></script>, затем BuildInfo.watch({lang}) или
// BuildInfo.watch({onStale: (show) => …}) - своя полоса программы (show(false) - убрать).
(function () {
  'use strict';
  const T = {
    ru: { updated: 'Программа обновилась - перезапустите', restart: 'Перезапустить', restarting: 'Перезапускаю…',
          failed: 'Не перезапустилась - закройте окно и запустите программу ещё раз' },
    en: { updated: 'The program was updated - restart it', restart: 'Restart', restarting: 'Restarting…',
          failed: 'It did not restart - close the window and start the program again' },
  };
  const S = { first: null, last: null, stale: false, opts: null };
  const lang = () => ((S.opts && S.opts.lang) || document.documentElement.lang || 'ru').slice(0, 2) === 'en' ? 'en' : 'ru';
  const t = k => T[lang()][k];

  async function info() {
    const r = await fetch(S.opts.infoUrl, { cache: 'no-store' });
    return r.json();
  }
  function bar(show, text) {
    let el = document.getElementById('bi-bar');
    if (!show) { if (el) el.remove(); return; }
    if (!el) {
      el = document.createElement('div');
      el.id = 'bi-bar';
      el.setAttribute('role', 'status');
      el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;display:flex;gap:12px;align-items:center;' +
        'justify-content:center;padding:8px 16px;font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;' +
        'background:var(--warn-soft,#fbf1dc);color:var(--warn,#7a4d00);border-bottom:1px solid var(--border,#dfe3e8)';
      el.innerHTML = '<span id="bi-text"></span><button id="bi-restart" type="button" style="font:inherit;font-weight:600;' +
        'padding:4px 12px;border-radius:8px;border:1px solid currentColor;background:transparent;color:inherit;cursor:pointer"></button>';
      el.querySelector('#bi-restart').onclick = restart;
      document.body.appendChild(el);
    }
    el.querySelector('#bi-text').textContent = text || t('updated');
    el.querySelector('#bi-restart').textContent = t('restart');
  }
  async function restart() {
    const b = document.getElementById('bi-restart');
    if (b) { b.disabled = true; b.textContent = t('restarting'); }
    try {
      const d = S.last || await info();
      if (d.build && d.disk_build && d.build !== d.disk_build) {
        await fetch(S.opts.restartUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const end = Date.now() + 60000;
        for (;;) {
          await new Promise(r => setTimeout(r, 700));
          try { const n = await info(); if (n.build && n.build === n.disk_build) break; } catch (e) { /* перезапускается */ }
          if (Date.now() > end) { bar(true, t('failed')); return; }
        }
      }
      location.reload();
    } catch (e) { bar(true, t('failed')); }
  }
  async function tick() {
    let d;
    try { d = await info(); } catch (e) { return; }          // не отвечает - об этом скажет сама программа
    S.last = d;
    if (!S.first) S.first = d;
    const serverOld = !!(d.build && d.disk_build && d.build !== d.disk_build);
    const windowOld = !!(S.first.build && d.build && d.build !== S.first.build);
    S.stale = serverOld || windowOld;
    if (S.opts.onStale) S.opts.onStale(S.stale, restart);
    else bar(S.stale);
  }
  window.BuildInfo = {
    watch(opts) {
      S.opts = Object.assign({ infoUrl: '/api/info', restartUrl: '/api/restart', every: 60000 }, opts || {});
      tick();
      setInterval(tick, S.opts.every);
      return S;
    },
    check: tick,
    restart,
    state: S,
  };
})();
