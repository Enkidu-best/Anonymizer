// Общий блок ui (стандарт 2.1, «_Единый вид 02», п. 1): одна строка состояния модели в шапке - одни слова и цвета
// во всех программах. Эталон - Anonymizer 4.0 (core.js statusInfo).
//   ModelStatus.mount("#model", {onClick, onRetry})  - элемент шапки (кнопка или любой элемент)
//   ModelStatus.set({state, text, program, progress, detail, onRetry})
// state: ready - «готова» (зелёный) · working - «работает» (синий, с ходом) · loading - «загружается» (серый, с ходом)
//        queued - «занята «<программа>» - в очереди» (жёлтый) · off - «нет модели» (серый) · error - «ошибка» (красный;
//        щелчок - что случилось и «Повторить»).
// text - свои слова программы вместо общих (цвет и смысл - от state); progress - 0-100, дописывается « · 40 %».
// Цвета - токены программы (--ok, --info / --accent, --warn, --faint, --bad), подписи RU / EN - по языку страницы.
(function () {
  'use strict';
  const T = {
    ru: { ready: 'Модель готова', working: 'Работает', loading: 'Модель загружается', queued: 'В очереди',
          queuedBy: 'Занята «{p}» - в очереди', off: 'Нет модели', error: 'Ошибка модели', what: 'Что случилось',
          noDetail: 'Модель не ответила.', retry: 'Повторить', close: 'Закрыть' },
    en: { ready: 'Model ready', working: 'Working', loading: 'Model loading', queued: 'Queued',
          queuedBy: 'Busy: «{p}» - queued', off: 'No model', error: 'Model error', what: 'What happened',
          noDetail: 'The model did not respond.', retry: 'Retry', close: 'Close' },
  };
  const STATES = ['ready', 'working', 'loading', 'queued', 'off', 'error'];
  const lang = () => ((document.documentElement.lang || 'ru').slice(0, 2) === 'en' ? 'en' : 'ru');
  const t = (k, p) => T[lang()][k].replace('{p}', p || '');
  const CSS = `
.ms{display:inline-flex;align-items:center;gap:7px;white-space:nowrap;padding:3px 10px;border-radius:999px;border:1px solid var(--border,#dfe3e8);background:var(--surface,#fff);color:var(--text,#1b2230);font:inherit;font-size:.9375rem;line-height:1.4;cursor:pointer;flex:none;max-width:100%}
.ms:focus-visible{outline:2px solid var(--info,var(--accent,#2450c8));outline-offset:2px}
.ms-t{overflow:hidden;text-overflow:ellipsis}
.ms-dot{width:9px;height:9px;border-radius:50%;flex:none;background:var(--ok,#276b43)}
.ms-working .ms-dot{background:var(--info,var(--accent,#2450c8))}
.ms-loading .ms-dot,.ms-off .ms-dot{background:var(--faint,#636c79)}
.ms-queued .ms-dot{background:var(--warn,#9a6200)}
.ms-error .ms-dot{background:var(--bad,#b3261e)}
.ms-error{color:var(--bad,#b3261e);border-color:currentColor}
.ms-working .ms-dot,.ms-loading .ms-dot{animation:ms-pulse 1.2s ease-in-out infinite}
@keyframes ms-pulse{50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.ms-working .ms-dot,.ms-loading .ms-dot{animation:none}}
.ms-pop{position:fixed;z-index:9000;max-width:360px;padding:12px 14px;border-radius:10px;border:1px solid var(--border,#dfe3e8);background:var(--surface,#fff);color:var(--text,#1b2230);box-shadow:0 8px 24px rgba(0,0,0,.18);font-size:.9375rem;line-height:1.45}
.ms-pop b{display:block;margin-bottom:4px}
.ms-pop p{margin:0 0 10px;overflow-wrap:anywhere}
.ms-pop .ms-row{display:flex;gap:8px;justify-content:flex-end}
.ms-pop button{font:inherit;padding:5px 12px;border-radius:8px;border:1px solid var(--border,#dfe3e8);background:var(--surface,#fff);color:var(--text,#1b2230);cursor:pointer}
.ms-pop button.ms-retry{background:var(--accent,#1f3864);border-color:var(--accent,#1f3864);color:var(--accent-text,#fff)}`;
  let el = null, opts = {}, cur = null, pop = null;

  function css() {
    if (document.getElementById('ms-css')) return;
    const st = document.createElement('style');
    st.id = 'ms-css';
    st.textContent = CSS;
    document.head.appendChild(st);
  }
  function closePop() {
    if (!pop) return;
    pop.remove();
    pop = null;
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('pointerdown', onOutside, true);
  }
  function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); closePop(); el.focus(); } }
  function onOutside(e) { if (pop && !pop.contains(e.target) && !el.contains(e.target)) closePop(); }
  function openPop() {
    closePop();
    const retry = cur.onRetry || opts.onRetry;
    pop = document.createElement('div');
    pop.className = 'ms-pop';
    pop.id = 'ms-pop';
    pop.setAttribute('role', 'dialog');
    pop.innerHTML = `<b></b><p></p><div class="ms-row"><button type="button" class="ms-close"></button>${retry ? '<button type="button" class="ms-retry"></button>' : ''}</div>`;
    pop.querySelector('b').textContent = t('what');
    pop.querySelector('p').textContent = cur.detail || t('noDetail');
    pop.querySelector('.ms-close').textContent = t('close');
    pop.querySelector('.ms-close').onclick = () => { closePop(); el.focus(); };
    if (retry) {
      const b = pop.querySelector('.ms-retry');
      b.textContent = t('retry');
      b.onclick = () => { closePop(); retry(); };
    }
    document.body.appendChild(pop);
    const r = el.getBoundingClientRect();
    const w = pop.offsetWidth;
    pop.style.top = (r.bottom + 6) + 'px';
    pop.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + 'px';
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('pointerdown', onOutside, true);
    (pop.querySelector('.ms-retry') || pop.querySelector('.ms-close')).focus();
  }
  function render() {
    if (!el || !cur) return;
    const s = cur.state;
    let text = cur.text || (s === 'queued' && cur.program ? t('queuedBy', cur.program) : t(s));
    if (cur.progress != null && (s === 'working' || s === 'loading')) text += ` · ${Math.round(cur.progress)} %`;
    el.classList.remove(...STATES.map(x => 'ms-' + x));
    el.classList.add('ms', 'ms-' + s);
    el.dataset.state = s;
    el.querySelector('.ms-t').textContent = text;
    el.setAttribute('aria-label', text);
    if (s !== 'error') closePop();
  }

  window.ModelStatus = {
    mount(target, o) {
      css();
      el = typeof target === 'string' ? document.querySelector(target) : target;
      opts = o || {};
      el.innerHTML = '<span class="ms-dot" aria-hidden="true"></span><span class="ms-t"></span>';
      if (el.tagName !== 'BUTTON') { el.setAttribute('role', 'button'); el.tabIndex = 0; }
      el.setAttribute('aria-live', 'polite');
      el.onclick = e => {
        if (cur && cur.state === 'error') openPop();
        else if (opts.onClick) opts.onClick(e);
      };
      el.onkeydown = e => { if (el.tagName !== 'BUTTON' && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); el.click(); } };
      cur = cur || { state: 'off' };
      render();
      return el;
    },
    set(s) {
      if (!STATES.includes(s.state)) throw new Error('ModelStatus: неизвестное состояние ' + s.state);
      cur = Object.assign({}, s);
      render();
    },
    refresh: render,           // после смены языка окна
    close: closePop,
    get state() { return cur ? Object.assign({}, cur) : null; },
    STATES,
  };
})();
