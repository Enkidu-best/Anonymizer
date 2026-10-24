// Anonymizer 4.0 - основа окна: язык, тема, масштаб, каркас, окна программы, меню, строка состояния (описание 3.1, 4.x).
// Все надписи - из словарей static/i18n/<язык>.json (стандарт 1.4); цвета - токенами app.css.
'use strict';
const UI_VERSION = '4.0.0';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const ic = (n, cls = 'i') => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[n] || ''}</svg>`;
const store = {
  get(k, d) { try { const v = localStorage.getItem('anon.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('anon.' + k, JSON.stringify(v)); } catch { /* приватное окно */ } },
};

// ── Язык окна ────────────────────────────────────────────────────────────────
let LANG = store.get('lang', (navigator.language || 'ru').toLowerCase().startsWith('ru') ? 'ru' : 'en');
let DICT = {};
async function loadLang(l) {
  DICT = await (await fetch(`/static/i18n/${l}.json?v=${UI_VERSION}`)).json();
  LANG = l;
  document.documentElement.lang = l;
}
function t(key, vars = {}) {
  const s = DICT[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] ?? m));
}
function pidx(n) {
  if (LANG !== 'ru') return n === 1 ? 0 : 1;
  const a = n % 10, b = n % 100;
  return a === 1 && b !== 11 ? 0 : (a >= 2 && a <= 4 && (b < 10 || b >= 20) ? 1 : 2);
}
function tn(key, n, vars = {}) {
  const forms = t(key, { ...vars, n: fmtNum(n) }).split('|');
  return forms[Math.min(pidx(n), forms.length - 1)];
}
const fmtNum = n => Number(n || 0).toLocaleString(LANG === 'ru' ? 'ru-RU' : 'en-US');
function fmtSize(b) {
  if (b < 1024) return `${b} ${t('unit.b')}`;
  if (b < 1048576) return `${Math.round(b / 1024)} ${t('unit.kb')}`;
  return `${(b / 1048576).toLocaleString(LANG === 'ru' ? 'ru-RU' : 'en-US', { maximumFractionDigits: 1 })} ${t('unit.mb')}`;
}
function fmtDate(s, time = true) {          // «2026-10-24 14:05:00» → «24.10.2026, 14:05»
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(s || '');
  if (!m) return s || '';
  return `${m[3]}.${m[2]}.${m[1]}` + (time ? `, ${m[4]}:${m[5]}` : '');
}
const fmtDay = s => { const m = /^\d{4}-(\d{2})-(\d{2})/.exec(s || ''); return m ? `${m[2]}.${m[1]}` : ''; };
const fmtDur = sec => { sec = Math.max(0, Math.round(sec)); return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`; };

// ── Виды записей (описание 5.3) и метки ─────────────────────────────────────
const TYPES = ['ФИО', 'ЮЛ', 'АДРЕС', 'ИНН', 'ОГРН', 'КПП', 'ОКПО', 'РС', 'КС', 'БИК', 'КАРТА', 'IBAN', 'SWIFT', 'ЛИЦЕНЗИЯ',
  'ПАСПОРТ', 'СНИЛС', 'ВУ', 'ПОЛИС', 'ТЕЛЕФОН', 'EMAIL', 'НИК', 'URL', 'ДАТАРОЖД', 'КАДАСТР', 'ЕГРН', 'НЕДВИЖ', 'ГОСНОМЕР',
  'VIN', 'РЕГНОМЕР', 'НОТАРИУС'];
const TYPE_ALIAS = { FIO: 'ФИО', YUL: 'ЮЛ', ADDR_PHYS: 'АДРЕС', ADDR_CORP: 'АДРЕС', 'АДРЕС_ФИЗ': 'АДРЕС', 'АДРЕС_ЮР': 'АДРЕС',
  PASSPORT: 'ПАСПОРТ', DOB: 'ДАТАРОЖД' };
const typeCode = x => TYPE_ALIAS[x] || x;
const typeLabel = x => { const c = typeCode(x); return DICT['types.' + c] ? t('types.' + c) : c; };
const PREFIX_TYPE = { FIO: 'ФИО', YUL: 'ЮЛ', ADR: 'АДРЕС', INN: 'ИНН', OGRN: 'ОГРН', KPP: 'КПП', OKPO: 'ОКПО', RS: 'РС', KS: 'КС',
  BIK: 'БИК', CARD: 'КАРТА', IBAN: 'IBAN', SWIFT: 'SWIFT', LIC: 'ЛИЦЕНЗИЯ', PASSPORT: 'ПАСПОРТ', SNILS: 'СНИЛС', DL: 'ВУ',
  OMS: 'ПОЛИС', TEL: 'ТЕЛЕФОН', EMAIL: 'EMAIL', NICK: 'НИК', URL: 'URL', DOB: 'ДАТАРОЖД', CAD: 'КАДАСТР', EGRN: 'ЕГРН',
  REALTY: 'НЕДВИЖ', CAR: 'ГОСНОМЕР', VIN: 'VIN', REG: 'РЕГНОМЕР', NOT: 'НОТАРИУС' };
function kgrp(prefix) {
  if (['FIO', 'NICK'].includes(prefix)) return 'p';
  if (['YUL', 'LIC', 'REG'].includes(prefix)) return 'o';
  if (['ADR', 'CAD', 'EGRN', 'REALTY'].includes(prefix)) return 'a';
  return 'n';
}
const GROUP_KEY = { p: 'grp.persons', o: 'grp.orgs', a: 'grp.addrs', n: 'grp.nums' };
const prefixOf = tok => String(tok).replace(/[[\]]/g, '').split('_')[0];
const tokHtml = tok => `<span class="tok k-${kgrp(prefixOf(tok))}">[${esc(String(tok).replace(/[[\]]/g, ''))}]</span>`;
const withTokens = s => esc(s).replace(/\[([A-Z]+_\d+)\]/g, (m, k) => tokHtml(k));
function fileIcon(name) {
  const e = (name || '').split('.').pop().toLowerCase();
  if (['xlsx', 'xlsm'].includes(e)) return 'file-spreadsheet';
  if (['jpg', 'jpeg', 'png', 'heic', 'tif', 'tiff', 'bmp', 'webp'].includes(e)) return 'image';
  if (e === 'pptx') return 'presentation';
  return 'file-text';
}
const extOf = name => (name || '').split('.').pop().toLowerCase();

// ── Состояние ────────────────────────────────────────────────────────────────
const S = {
  view: 'anon', sub: null,          // sub - экран внутри раздела («preview»)
  sid: null, session: null, files: [], maps: [], mapsAll: [],
  status: null, info: null, settings: null,
  run: null,                        // идущая обработка: {section, list, i, stage, started, stop, pid}
  rebuild: false, nerWait: null,
  restore: { mode: store.get('restoreMode', 'text'), sid: null, text: '', result: null },
  table: { open: true, q: '', type: '' },
  offline: false, updated: false,
};

// ── Сервер ───────────────────────────────────────────────────────────────────
class ApiError extends Error {
  constructor(code, status, data) { super(code); this.code = code; this.status = status; this.data = data || {}; }
}
async function api(url, opts = {}) {
  const o = { ...opts };
  if ('json' in o) {
    o.method = o.method || 'POST';
    o.headers = { 'Content-Type': 'application/json' };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  let r;
  try { r = await fetch(url, o); } catch { setOffline(true); throw new ApiError('offline', 0); }
  setOffline(false);
  let d = {};
  try { d = await r.json(); } catch { /* не JSON */ }
  if (!r.ok) throw new ApiError(d.code || d.error || 'other', r.status, d);
  return d;
}
function errText(e) {
  const code = e && e.code;
  return DICT['err.' + code] ? t('err.' + code) : t('err.generic');
}

// ── Каркас: меню, путь, тема, язык, масштаб ──────────────────────────────────
const MENU = [['anon', 'eye-off', 'menu.anon'], ['restore', 'undo-2', 'menu.restore'], ['sessions', 'folder', 'menu.sessions'],
  ['dict', 'book-open', 'menu.dict']];
function renderMenu() {
  $('menu').innerHTML = MENU.map(([k, i, key]) =>
    `<button class="mi${S.view === k ? ' on' : ''}" data-go="${k}" title="${esc(t(key))}" aria-current="${S.view === k ? 'page' : 'false'}">${ic(i)}<span>${esc(t(key))}</span></button>`).join('');
  for (const [id, view] of [['mi-help', 'help'], ['mi-settings', 'settings']]) {
    $(id).classList.toggle('on', S.view === view);
  }
  $('menu').querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(b.dataset.go));
}
function crumbs(parts, back) {
  const el = $('crumbs');
  el.innerHTML = (back ? `<button class="back" id="back">${ic('arrow-left')}${esc(t('top.back'))}</button><span>·</span>` : '') +
    parts.map((p, i) => i === parts.length - 1 ? `<b title="${esc(p)}">${esc(p)}</b>` : `<span>${esc(p)}</span><span>›</span>`).join('');
  if (back) $('back').onclick = back;
}
function applyTheme() {
  const th = store.get('theme', 'system');
  if (th === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = th;
  $('mi-theme').title = t('theme.' + th);
}
function cycleTheme() {
  const order = ['system', 'light', 'dark'];
  store.set('theme', order[(order.indexOf(store.get('theme', 'system')) + 1) % 3]);
  applyTheme();
}
// «Aa»: 80-200 %, 100 % = 17 px основного текста (стандарт 1.5); прежний масштаб 3.x не переносится
const ZOOMS = [0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];
function applyZoom() {
  const z = store.get('zoom4', 1);
  document.documentElement.style.setProperty('--z', z);
}
function setZoom(z) {
  store.set('zoom4', Math.min(2, Math.max(0.8, z)));
  applyZoom();
  if ($('zoom-val')) $('zoom-val').textContent = Math.round(store.get('zoom4', 1) * 100) + ' %';
}
function stepZoom(dir) {
  const z = store.get('zoom4', 1);
  const next = dir > 0 ? ZOOMS.find(x => x > z + 0.001) : [...ZOOMS].reverse().find(x => x < z - 0.001);
  setZoom(next ?? z);
}
function zoomPop(anchor) {
  popup(anchor, `<div class="ptxt" style="display:flex;align-items:center;gap:8px">
    <button class="btn sm" id="z-dec" title="${esc(t('zoom.smaller'))}">−</button>
    <b id="zoom-val" style="min-width:56px;text-align:center">${Math.round(store.get('zoom4', 1) * 100)} %</b>
    <button class="btn sm" id="z-inc" title="${esc(t('zoom.bigger'))}">+</button>
    <button class="btn sm" id="z-reset">${esc(t('zoom.reset'))}</button></div>`, root => {
    root.querySelector('#z-dec').onclick = () => stepZoom(-1);
    root.querySelector('#z-inc').onclick = () => stepZoom(1);
    root.querySelector('#z-reset').onclick = () => setZoom(1);
  });
}
async function setLang(l) {
  store.set('lang', l);
  await loadLang(l);
  staticTexts();
  render();
}
function staticTexts() {
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); el.setAttribute('aria-label', t(el.dataset.i18nTitle)); });
  $('lang').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.lang === LANG));
  $('ver').textContent = `${t('app.name')} ${shortVer(S.info ? S.info.version : UI_VERSION)}`;
  applyTheme();
  renderStatus();
}
const shortVer = v => String(v).replace(/\.0$/, '');

// ── Окна программы (описание 4.4): Esc и щелчок мимо - закрыть, Enter - главное действие ──
let DLG_OPEN = 0;
function dialog({ title, body = '', buttons = [], wide = false, init, read, validate, hint }) {
  return new Promise(resolve => {
    const ov = document.createElement('div');
    ov.className = 'ov';
    const id = 'dlg-t-' + Math.random().toString(36).slice(2, 8);
    ov.innerHTML = `<div class="dlg${wide ? ' wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="${id}">
      <h2 id="${id}">${title}</h2><div class="dlg-body">${body}</div><div class="err hidden" role="alert"></div>
      <div class="btns">${buttons.map((b, i) => `<button class="btn ${b.kind || ''}" data-i="${i}">${b.icon ? ic(b.icon) : ''}${esc(b.label)}</button>`).join('')}</div>
      ${hint ? `<div class="hint">${esc(hint)}</div>` : ''}</div>`;
    document.body.appendChild(ov);
    DLG_OPEN++;
    const dlg = ov.querySelector('.dlg');
    const prevFocus = document.activeElement;
    const close = (btn) => {
      const data = read ? read(dlg) : null;
      if (btn && validate) {
        const err = validate(btn, data, dlg);
        if (err) { const e = dlg.querySelector('.err'); e.textContent = err; e.classList.remove('hidden'); return; }
      }
      ov.remove();
      DLG_OPEN--;
      document.removeEventListener('keydown', onKey, true);
      if (prevFocus && prevFocus.focus) try { prevFocus.focus(); } catch { /* нет элемента */ }
      resolve({ btn, data });
    };
    const danger = buttons.some(b => b.kind === 'danger');
    const primary = buttons.findIndex(b => b.kind === 'primary');
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); return; }
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        if (e.target.tagName === 'TEXTAREA') return;
        if (danger) return;                         // необратимое: Enter нажимает кнопку в фокусе (Отмена)
        if (e.target.tagName === 'BUTTON' && e.target.closest('.btns')) return;
        if (primary >= 0) { e.preventDefault(); close(buttons[primary].value); }
      }
      if (e.key === 'Tab') {                         // фокус не уходит из окна
        const f = [...dlg.querySelectorAll('button, input, select, textarea, [tabindex]')].filter(x => !x.disabled && x.offsetParent);
        if (!f.length) return;
        if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
      }
    }
    document.addEventListener('keydown', onKey, true);
    ov.addEventListener('mousedown', e => { if (e.target === ov) close(null); });
    dlg.querySelectorAll('.btns button').forEach(b => b.onclick = () => close(buttons[+b.dataset.i].value));
    if (init) init(dlg, close);
    const auto = dlg.querySelector('[autofocus]') ||
      (danger ? dlg.querySelector(`.btns button[data-i="${buttons.findIndex(b => !b.kind)}"]`) : null) ||
      dlg.querySelector('input, select, textarea') || dlg.querySelector(`.btns button[data-i="${Math.max(primary, 0)}"]`);
    if (auto) auto.focus();
  });
}
const confirmDlg = (title, text, okLabel, kind = 'primary') => dialog({
  title: esc(title), body: text ? `<p>${text}</p>` : '',
  buttons: [{ label: t('common.cancel'), value: null }, { label: okLabel, value: 'ok', kind }],
}).then(r => r.btn === 'ok');

// ── Всплывающие меню («⋯», «Скачать ▾»), строка внизу ────────────────────────
let POP = null;
function closePop() { if (POP) { POP.remove(); POP = null; } }
function popup(anchor, html, init) {
  closePop();
  const p = document.createElement('div');
  p.className = 'pop';
  p.innerHTML = html;
  document.body.appendChild(p);
  const r = anchor.getBoundingClientRect();
  const w = p.offsetWidth, h = p.offsetHeight;
  p.style.left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)) + 'px';
  p.style.top = (r.bottom + 4 + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 4) : r.bottom + 4) + 'px';
  POP = p;
  if (init) init(p);
  return p;
}
function menuPop(anchor, items) {
  const html = items.map((x, i) => x === '-' ? '<hr>' :
    `<button class="mitem${x.danger ? ' danger' : ''}" data-i="${i}">${x.icon ? ic(x.icon) : ''}${esc(x.label)}</button>`).join('');
  popup(anchor, html, p => {
    p.querySelectorAll('[data-i]').forEach(b => b.onclick = () => { closePop(); items[+b.dataset.i].action(); });
    const first = p.querySelector('button');
    if (first) first.focus();
  });
}
document.addEventListener('mousedown', e => { if (POP && !POP.contains(e.target) && !e.target.closest('[data-pop]')) closePop(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && POP) closePop(); });
window.addEventListener('resize', closePop);

let TOAST_T = null;
function toast(msg, opts = {}) {
  const old = document.querySelector('.toast');
  if (old) old.remove();
  clearTimeout(TOAST_T);
  const el = document.createElement('div');
  el.className = 'toast';
  el.setAttribute('role', 'status');
  el.innerHTML = `<span>${esc(msg)}</span>` + (opts.action ? `<button>${esc(opts.action.label)}</button>` : '');
  if (opts.action) el.querySelector('button').onclick = () => { el.remove(); opts.action.fn(); };
  document.body.appendChild(el);
  TOAST_T = setTimeout(() => el.remove(), opts.ms || (opts.action ? 5000 : 3500));
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* нет доступа к буферу */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch { return false; }
}

// ── Строка состояния модели (стандарт 2.1) ───────────────────────────────────
function statusInfo() {
  const st = S.status || {};
  const ner = st.ner || {};
  if (S.run) return { cls: 'work', text: S.run.section === 'anon' || S.run.section === 'restore' ? t('status.working', { i: Math.min(S.run.i + 1, S.run.list.length), n: S.run.list.length }) : t('status.rebuilding') };
  if (S.rebuild) return { cls: 'work', text: t('status.rebuilding') };
  if (ner.loading) return { cls: 'work', text: t('status.ner_loading') };
  if (ner.error) return { cls: 'err', text: t('status.ner_error') };
  if (st.use_llm && st.gpu && st.gpu.busy) return { cls: 'wait', text: t('status.llm_busy', { program: st.gpu.program || '?' }) };
  if (st.use_llm && st.llm && (!st.llm.available || !st.llm.has_model)) return { cls: 'off', text: t('status.llm_none') };
  if (!st.ner) return { cls: 'off', text: '…' };
  return { cls: '', text: t('status.ready') };
}
function renderStatus() {
  const s = statusInfo();
  const el = $('model');
  el.className = 'model ' + s.cls;
  $('model-t').textContent = s.text;
}
function statusPop() {
  const st = S.status || {};
  const ner = st.ner || {};
  const lines = [ner.ready ? t('status.pop_ner_ready') : ner.loading ? t('status.pop_ner_loading') : t('status.pop_ner_error')];
  if (!st.use_llm) lines.push(t('status.pop_llm_off'));
  else if (st.gpu && st.gpu.busy) lines.push(t('status.pop_llm_busy', { program: st.gpu.program || '?' }));
  else if (st.llm && st.llm.available && st.llm.has_model) lines.push(t('status.pop_llm_ready', { model: st.llm.model }));
  else lines.push(t('status.pop_llm_none', { model: (st.llm && st.llm.model) || '' }));
  popup($('model'), lines.map(x => `<div class="ptxt">${esc(x)}</div>`).join('') +
    (ner.error && !ner.loading ? `<hr><button class="mitem" id="ner-retry">${ic('refresh-cw')}${esc(t('status.retry'))}</button>` : ''), p => {
    const b = p.querySelector('#ner-retry');
    if (b) b.onclick = () => { closePop(); nerRetry(); };
  });
}
async function nerRetry() {
  try { await api('/api/ner-retry', { json: {} }); } catch { /* сеть */ }
  pollStatus();
}
let STATUS_T = null;
async function pollStatus() {
  clearTimeout(STATUS_T);
  try {
    const was = S.status;
    S.status = await api('/api/status');
    if (was && !was.version) checkVersion();
    renderStatus();
    if (S.nerWait) S.nerWait();
  } catch { renderStatus(); }
  const ner = (S.status && S.status.ner) || {};
  STATUS_T = setTimeout(pollStatus, ner.loading || S.offline ? 1500 : 8000);
}
// ── Версия окна и сервера (стандарт 6.5), сервер не отвечает ─────────────────
async function checkVersion() {
  try {
    S.info = await api('/api/info');
    S.updated = S.info.version !== UI_VERSION;
    $('ver').textContent = `${t('app.name')} ${shortVer(S.info.version)}`;
  } catch { /* offline - баннер */ }
  renderBanner();
}
function setOffline(v) {
  if (S.offline === v) return;
  S.offline = v;
  renderBanner();
  if (!v) checkVersion();
}
function renderBanner() {
  const b = $('banner');
  let html = '';
  if (S.offline) html = `${ic('triangle-alert')}<span>${esc(t('banner.offline'))}</span>`;
  else if (S.updated) html = `${ic('info')}<span>${esc(t('banner.updated'))}</span><button class="btn sm" id="b-restart">${esc(t('banner.restart'))}</button>`;
  else if (S.info && S.info.migration && S.info.migration.error) html = `${ic('triangle-alert')}<span>${esc(t('banner.migration_failed'))}</span>`;
  b.innerHTML = html;
  b.classList.toggle('hidden', !html);
  const r = $('b-restart');
  if (r) r.onclick = () => location.reload();
}

// ── Переходы ─────────────────────────────────────────────────────────────────
const VIEWS = {};
function go(view, opts = {}) {
  closePop();
  if (S.view === 'restore' && view !== 'restore') { S.restore.text = ''; S.restore.result = null; }   // текст не хранится
  if (view === 'restore' || view === 'sessions') S.sessList = null;
  if (view === 'dict') S.dict = null;
  S.view = view;
  S.sub = opts.sub || null;
  render();
  $('content').scrollTop = 0;
}
function render() {
  renderMenu();
  renderStatus();
  const v = VIEWS[S.view] || VIEWS.anon;
  v.render($('content'));
}
// ── Сессия, выбранная на всё окно (описание 2) ───────────────────────────────
async function loadSession(sid) {
  if (!sid) { S.sid = null; S.session = null; S.files = []; S.maps = []; S.mapsAll = []; return; }
  try {
    const d = await api(`/api/sessions/${sid}`);
    S.sid = sid;
    S.session = d;
    S.files = d.file_list || [];
  } catch (e) {
    if (e.status === 404) { S.sid = null; S.session = null; S.files = []; }
    return;
  }
  await loadMaps();
}
async function loadMaps() {
  if (!S.sid) { S.maps = []; S.mapsAll = []; return; }
  try { S.mapsAll = await api(`/api/sessions/${S.sid}/mappings?all=1`); } catch { S.mapsAll = []; }
  S.maps = S.mapsAll.filter(m => m.status === 'active');
}
async function ensureSession() {
  if (S.sid) return S.sid;
  const d = await api('/api/sessions', { json: {} });
  await loadSession(d.id);
  return d.id;
}
const sessTitle = s => s ? `${s.name} · ${tn('pl.files', s.files || 0)}` : '';

// ── Запуск ───────────────────────────────────────────────────────────────────
function bindFrame() {
  $('mi-help').onclick = () => go('help');
  $('mi-settings').onclick = () => go('settings');
  $('mi-theme').onclick = cycleTheme;
  $('collapse').onclick = () => $('side').classList.toggle('collapsed');
  $('lang').querySelectorAll('button').forEach(b => b.onclick = () => setLang(b.dataset.lang));
  $('zoom').onclick = e => zoomPop(e.currentTarget);
  $('model').onclick = statusPop;
  document.addEventListener('keydown', e => {
    if (!(e.metaKey || e.ctrlKey)) return;
    if (e.key === '=' || e.key === '+') { e.preventDefault(); stepZoom(1); }
    else if (e.key === '-' || e.key === '−') { e.preventDefault(); stepZoom(-1); }
    else if (e.key === '0') { e.preventDefault(); setZoom(1); }
  });
  // файл, брошенный мимо зоны, не открывается браузером вместо окна
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());
}
async function boot() {
  applyZoom();
  await loadLang(LANG);
  bindFrame();
  staticTexts();
  await checkVersion();
  try { S.settings = await api('/api/settings'); } catch { S.settings = {}; }
  render();
  pollStatus();
}
document.addEventListener('DOMContentLoaded', boot);
