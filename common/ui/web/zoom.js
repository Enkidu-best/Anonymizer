// Общий блок ui (стандарт 6.5, раздел 7): увеличение картинки. Эталон - «Понятный документ» zoomImage (styles.js).
//   ZoomImage.open([{src, name}, …], index)  - крупно поверх окна
//   ZoomImage.bind(container, "img.zoomable") - щелчок по любой такой картинке внутри контейнера открывает их все
// Esc и щелчок мимо картинки - закрыть; ← / → - соседние; колесо - масштаб (1-5×), двойной щелчок - 100 %;
// Tab не уходит из окна; после закрытия фокус - на прежнем месте. Подписи RU / EN - по языку страницы.
(function () {
  'use strict';
  const T = { ru: { of: 'из', close: 'Закрыть (Esc)', prev: 'Предыдущая (←)', next: 'Следующая (→)', hint: 'Колесо - масштаб · Esc - закрыть' },
              en: { of: 'of', close: 'Close (Esc)', prev: 'Previous (←)', next: 'Next (→)', hint: 'Wheel - zoom · Esc - close' } };
  const t = k => T[(document.documentElement.lang || 'ru').slice(0, 2) === 'en' ? 'en' : 'ru'][k];
  const CSS = `
.zi-ov{position:fixed;inset:0;z-index:10000;background:rgba(8,12,20,.82);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#fff}
.zi-stage{max-width:96vw;max-height:calc(100vh - 96px);overflow:hidden;display:flex;align-items:center;justify-content:center}
.zi-img{max-width:96vw;max-height:calc(100vh - 96px);background:#fff;box-shadow:0 8px 30px rgba(0,0,0,.45);transform-origin:center center;transition:transform .08s;cursor:zoom-in;user-select:none}
.zi-bar{display:flex;gap:14px;align-items:center}
.zi-name{font-weight:600}
.zi-hint{color:#c9d1dc;font-size:13px}
.zi-btn{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:44px;border-radius:50%;border:0;background:rgba(255,255,255,.16);color:#fff;font-size:24px;cursor:pointer}
.zi-btn:hover,.zi-btn:focus-visible{background:rgba(255,255,255,.3);outline:2px solid #fff}
.zi-prev{left:16px}.zi-next{right:16px}
.zi-x{position:absolute;top:14px;right:16px;width:36px;height:36px;border-radius:50%;border:0;background:rgba(255,255,255,.16);color:#fff;font-size:20px;cursor:pointer}`;
  let state = null;

  function css() {
    if (document.getElementById('zi-css')) return;
    const st = document.createElement('style');
    st.id = 'zi-css';
    st.textContent = CSS;
    document.head.appendChild(st);
  }
  function show() {
    const { list, i, ov, scale } = state;
    const it = list[i];
    const img = ov.querySelector('.zi-img');
    img.src = it.src;
    img.alt = it.name || '';
    img.style.transform = `scale(${scale})`;
    ov.querySelector('.zi-name').textContent = it.name || '';
    ov.querySelector('.zi-count').textContent = list.length > 1 ? `${i + 1} ${t('of')} ${list.length}` : '';
    ov.querySelectorAll('.zi-prev, .zi-next').forEach(b => { b.hidden = list.length < 2; });
    ov.setAttribute('aria-label', it.name || '');
  }
  function go(d) {
    if (!state || state.list.length < 2) return;
    state.i = (state.i + d + state.list.length) % state.list.length;
    state.scale = 1;
    show();
  }
  function close() {
    if (!state) return;
    state.ov.remove();
    document.removeEventListener('keydown', onKey, true);
    const back = state.back;
    state = null;
    if (back && back.focus) try { back.focus(); } catch (e) { /* элемента уже нет */ }
  }
  function onKey(e) {
    if (!state) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); go(1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
    else if (e.key === 'Tab') {
      const f = [...state.ov.querySelectorAll('button')].filter(b => !b.hidden);
      if (!f.length) return;
      const k = f.indexOf(document.activeElement);
      e.preventDefault();
      f[(k + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
    }
  }
  function open(list, index = 0) {
    if (!list || !list.length) return;
    close();
    css();
    const ov = document.createElement('div');
    ov.className = 'zi-ov';
    ov.id = 'zi-ov';
    ov.setAttribute('role', 'dialog');
    ov.setAttribute('aria-modal', 'true');
    ov.innerHTML = `<button class="zi-x" type="button" title="${t('close')}" aria-label="${t('close')}">×</button>
      <button class="zi-btn zi-prev" type="button" title="${t('prev')}" aria-label="${t('prev')}">‹</button>
      <div class="zi-stage"><img class="zi-img" alt=""></div>
      <button class="zi-btn zi-next" type="button" title="${t('next')}" aria-label="${t('next')}">›</button>
      <div class="zi-bar"><span class="zi-name"></span><span class="zi-count"></span><span class="zi-hint">${t('hint')}</span></div>`;
    state = { list, i: Math.max(0, Math.min(index, list.length - 1)), ov, scale: 1, back: document.activeElement };
    ov.addEventListener('mousedown', e => { if (e.target === ov || e.target.classList.contains('zi-stage') || e.target.classList.contains('zi-bar')) close(); });
    ov.querySelector('.zi-x').onclick = close;
    ov.querySelector('.zi-prev').onclick = () => go(-1);
    ov.querySelector('.zi-next').onclick = () => go(1);
    const img = ov.querySelector('.zi-img');
    ov.addEventListener('wheel', e => {
      e.preventDefault();
      state.scale = Math.min(5, Math.max(1, state.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      img.style.transform = `scale(${state.scale})`;
    }, { passive: false });
    img.addEventListener('dblclick', () => { state.scale = 1; img.style.transform = 'scale(1)'; });
    document.body.appendChild(ov);
    document.addEventListener('keydown', onKey, true);
    show();
    ov.querySelector('.zi-x').focus();
  }
  function bind(container, selector = 'img.zoomable') {
    const root = typeof container === 'string' ? document.querySelector(container) : container;
    if (!root) return;
    root.addEventListener('click', e => {
      const el = e.target.closest(selector);
      if (!el || !root.contains(el)) return;
      const all = [...root.querySelectorAll(selector)];
      open(all.map(x => ({ src: x.dataset.full || x.currentSrc || x.src, name: x.dataset.name || x.alt || x.title || '' })), all.indexOf(el));
    });
  }
  window.ZoomImage = { open, close, bind, get state() { return state && { index: state.i, count: state.list.length, scale: state.scale }; } };
})();
