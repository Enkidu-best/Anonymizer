// Просмотр обезличенного файла (описание 3.6) и файла с возвращёнными данными (3.8) - экран внутри раздела, «← Назад».
// Лист документа - белый в обеих темах; Word - docx-preview, Excel - таблица, PDF-скан - страницы, фото - картинка.
'use strict';
S.pv = null;

async function openPreview(f) {
  closePop();
  const sid = sectionSid(f.section);
  S.pv = { section: f.section, sid, input: f.input, filename: f.output, data: null, sel: null, occ: {}, filter: '', ftype: '',
           q: '', orig: false, loading: true };
  S.sub = 'preview';
  render();
  await loadPreview();
}
async function loadPreview(keepScroll = false) {
  const pv = S.pv;
  const scroll = keepScroll && $('pv-doc') ? $('pv-doc').scrollTop : 0;
  try {
    pv.data = await api(`/api/sessions/${pv.sid}/preview/${encodeURIComponent(pv.filename)}`);
    pv.error = null;
  } catch (e) { pv.data = null; pv.error = e.code; }
  pv.loading = false;
  if (S.pv !== pv || S.sub !== 'preview') return;
  render();
  await pv.painting;
  if (keepScroll && $('pv-doc')) $('pv-doc').scrollTop = scroll;
}
async function reopenPreview() {
  const pv = S.pv;
  const f = filesOf(pv.section).find(x => x.input === pv.input);
  if (!f || f.status !== 'done') { S.sub = null; S.pv = null; render(); return; }
  pv.filename = f.output;
  await loadPreview(true);
}
function closePreview() { S.sub = null; S.pv = null; render(); }

function renderPreview(el) {
  const pv = S.pv;
  const restore = pv.section === 'restore';
  const sess = restore ? S.restore.session : S.session;
  crumbs([t(restore ? 'menu.restore' : 'menu.anon'), sess ? sess.name : '', pv.input], closePreview);
  const d = pv.data;
  let bar;
  if (restore) {
    const R = (d && d.restore) || { exact: 0, case: 0, unknown: 0 };
    bar = `<span class="fn">${esc(t('pv.restored_sum', { n: fmtNum(R.exact + R.case), exact: fmtNum(R.exact), cases: fmtNum(R.case), unknown: fmtNum(R.unknown) }))}</span>`;
  } else {
    bar = `<span class="muted small">${esc(t('file.download_as'))}</span><span class="fn">${withTokens(pv.filename)}</span>`;
  }
  const canOrig = !restore && d && !['pdf', 'image'].includes(d.kind);
  el.innerHTML = `<div class="pvbar">${bar}<span class="grow"></span>
      <span class="search hide-narrow" style="width:280px">${ic('search')}<input class="field" id="pv-q" placeholder="${esc(t('pv.search'))}" aria-label="${esc(t('pv.search'))}" value="${esc(pv.q)}"></span>
      ${canOrig ? `<label class="check"><input type="checkbox" class="cb" id="pv-orig" ${pv.orig ? 'checked' : ''}>${esc(t('pv.show_orig'))}</label>` : ''}
      <button class="btn sm" id="pv-dl" data-pop="1">${ic('download')}${esc(t('file.download'))}${ic('chevron-down')}</button></div>
    <div class="pv"><div class="doc" id="pv-doc"><div class="sheet" id="pv-body">${pv.loading ? `<p>${esc(t('pv.loading'))}</p>` : ''}</div></div>
      <div class="side2" id="pv-side">${d ? (restore ? sideRestored() : sideAnon()) : ''}</div></div>`;
  $('pv-dl').onclick = e => {
    const f = filesOf(pv.section).find(x => x.input === pv.input) || { output: pv.filename, section: pv.section };
    fileAction(f, 'dl', e.currentTarget);
  };
  $('pv-q').oninput = e => { pv.q = e.target.value; pvSearch(true); };
  const o = $('pv-orig');
  if (o) o.onchange = () => { pv.orig = o.checked; markTokens(); };
  bindSide();
  if (pv.error) $('pv-body').innerHTML = `<p>${esc(t('pv.failed', { what: errText({ code: pv.error }) }))}</p>`;
  pv.painting = d ? paintDoc() : Promise.resolve();
}

// ── Лист документа ───────────────────────────────────────────────────────────
async function paintDoc() {
  const pv = S.pv, d = pv.data, body = $('pv-body');
  if (!d || !body) return;
  const url = `/api/download/${encodeURIComponent(pv.sid)}/${encodeURIComponent(pv.filename)}`;
  body.className = 'sheet';
  if (d.kind === 'image') { body.innerHTML = `<div class="pages"><img src="${url}" alt=""></div>`; body.className = 'sheet docx'; return; }
  if (d.kind === 'pdf') {
    body.className = 'sheet docx';
    body.innerHTML = `<div class="pages">${Array.from({ length: d.pages }, (_, i) =>
      `<img loading="lazy" src="/api/sessions/${encodeURIComponent(pv.sid)}/page/${encodeURIComponent(pv.filename)}/${i}.png" alt="${i + 1}">`).join('')}</div>`;
    return;
  }
  if (d.kind === 'xlsx') {
    body.innerHTML = (d.sheets || []).map(sh => `<div class="xl-name">${esc(t('pv.sheet', { name: sh.name }))}</div>
      <table class="xl">${sh.rows.map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</table>
      ${sh.truncated ? `<p class="xl-cut">${esc(t('pv.xl_cut'))}</p>` : ''}`).join('');
    afterPaint();
    return;
  }
  if (d.kind === 'docx' && window.docx) {
    try {
      const blob = await (await fetch(url)).blob();
      if (!body.isConnected) return;            // экран перерисован - лист рисует новая отрисовка
      body.innerHTML = '';
      body.className = 'sheet docx';
      await docx.renderAsync(blob, body, null, { inWrapper: true, ignoreLastRenderedPageBreak: true, renderHeaders: true,
        renderFooters: true, renderFootnotes: true });
      if (!body.isConnected) return;
      fixSymbols(body);
      try { await fixAnchors(blob, body); } catch { /* картинки остаются у абзаца */ }
      if (body.isConnected) afterPaint();
      return;
    } catch {
      body.className = 'sheet';
      body.innerHTML = '';
      pv.asText = true;
    }
  }
  body.textContent = d.text || '';
  if (!d.text) body.innerHTML = `<p>${esc(t('pv.no_text'))}</p>`;
  if (pv.asText) body.insertAdjacentHTML('afterbegin', `<p class="xl-cut">${esc(t('pv.as_text'))}</p>`);
  afterPaint();
}
function afterPaint() {
  if (S.pv.section === 'restore') markRestored(); else markTokens();
}
// маркеры списков Word в кодах шрифтов Symbol / Wingdings - обычными символами (иначе квадраты без шрифта)
const SYM = { symbol: { 0xF0B7: '•', 0xF02D: '–', 0xF0A7: '♣', 0xF0A8: '♦', 0xF0DE: '⇒', 0xF0AE: '→', 0xF0B0: '°', 0xF0D7: '⋅', 0xF0E0: '◊', 0xF06F: '○' },
  wingdings: { 0xF0A7: '▪', 0xF0D8: '➢', 0xF0FC: '✓', 0xF0FE: '☑', 0xF076: '❖', 0xF06C: '●', 0xF06E: '■', 0xF071: '❑', 0xF0A8: '◻',
    0xF0E0: '→', 0xF0E8: '➔', 0xF0F0: '⇨', 0xF0D7: '➢', 0xF075: '◆', 0xF0A1: '○', 0xF0B7: '•', 0xF09F: '•', 0xF077: '◆', 0xF0AA: '✦' } };
const PUA = /[-]/g;
function symChar(code, font) {
  const f = /wingding/i.test(font || '') ? 'wingdings' : 'symbol';
  return SYM[f][code] || SYM.symbol[code] || SYM.wingdings[code] || '•';
}
function fixSymbols(root) {
  root.querySelectorAll('style').forEach(st => {
    st.textContent = st.textContent.replace(/\{[^{}]*\}/g, rule => {
      if (!PUA.test(rule)) return rule;
      const font = (rule.match(/font-family:\s*([^;}]+)/) || [])[1] || '';
      return rule.replace(PUA, c => symChar(c.charCodeAt(0), font)).replace(/font-family:\s*['"]?(?:Symbol|Wingdings[^;'"]*)['"]?\s*;?/gi, '');
    });
  });
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), nodes = [];
  while (w.nextNode()) if (/[-]/.test(w.currentNode.nodeValue)) nodes.push(w.currentNode);
  nodes.forEach(n => {
    const font = getComputedStyle(n.parentElement).fontFamily;
    n.nodeValue = n.nodeValue.replace(PUA, c => symChar(c.charCodeAt(0), font));
    if (/symbol|wingding/i.test(font)) n.parentElement.style.fontFamily = 'inherit';
  });
}
// картинки, привязанные к странице (сетка таблицы из PDF), - от края страницы за текстом, как в Word
async function fixAnchors(blob, root) {
  if (!window.JSZip) return;
  const zip = await JSZip.loadAsync(blob), f = zip.file('word/document.xml');
  if (!f) return;
  const xml = new DOMParser().parseFromString(await f.async('string'), 'application/xml');
  const WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing';
  const anchors = [...xml.getElementsByTagNameNS(WP, 'anchor')].map(a => {
    const h = a.getElementsByTagNameNS(WP, 'positionH')[0], v = a.getElementsByTagNameNS(WP, 'positionV')[0];
    return { page: h && v && h.getAttribute('relativeFrom') === 'page' && v.getAttribute('relativeFrom') === 'page', behind: a.getAttribute('behindDoc') === '1' };
  });
  const divs = [...root.querySelectorAll('section.docx article div')].filter(d => d.style.position === 'relative' && d.style.width === '0px' && d.querySelector('img,svg'));
  if (divs.length !== anchors.length) return;
  divs.forEach((d, i) => {
    if (!anchors[i].page) return;
    const sec = d.closest('section.docx');
    if (!sec) return;
    sec.style.position = 'relative'; sec.style.isolation = 'isolate';
    d.style.position = 'absolute'; sec.appendChild(d);
    if (anchors[i].behind) d.style.zIndex = '-1';
  });
}

// ── Метки в тексте: цветные «чипы», щелчок - окно метки ─────────────────────
function textNodes(root, test) {
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), out = [];
  while (w.nextNode()) if (!test || test(w.currentNode)) out.push(w.currentNode);
  return out;
}
function markTokens() {
  const pv = S.pv, body = $('pv-body');
  if (!body || !pv.data) return;
  body.querySelectorAll('span.tok').forEach(sp => sp.replaceWith(document.createTextNode(sp.dataset.raw)));
  body.normalize();
  const seen = {}, occ = pv.data.occurrences || {}, forms = pv.data.forms || {};
  textNodes(body, n => /\[[A-Z]+_\d+\]/.test(n.nodeValue)).forEach(n => {
    const frag = document.createDocumentFragment(), s = n.nodeValue;
    let last = 0;
    s.replace(/\[([A-Z]+)_(\d+)\]/g, (m, p, num, off) => {
      frag.appendChild(document.createTextNode(s.slice(last, off)));
      const key = `${p}_${num}`, k = seen[key] = (seen[key] || 0) + 1;
      const fs = (forms[key] || {}).forms || [];
      const here = (occ[key] || [])[k - 1] || fs[0] || '';
      const sp = document.createElement('span');
      sp.className = `tok k-${kgrp(p)}`;
      sp.dataset.raw = m; sp.dataset.token = key;
      sp.textContent = pv.orig && here ? here : m;
      sp.title = fs.length ? `${key}: ${fs.join(' · ')}` : key;
      sp.onclick = e => { e.stopPropagation(); tokPopup(key, sp); };
      frag.appendChild(sp);
      last = off + m.length;
      return m;
    });
    frag.appendChild(document.createTextNode(s.slice(last)));
    n.replaceWith(frag);
  });
  pvSearch(false);
}
function pvSearch(scroll) {
  const pv = S.pv, body = $('pv-body');
  if (!body) return;
  body.querySelectorAll('mark').forEach(m => m.replaceWith(document.createTextNode(m.textContent)));
  body.normalize();
  body.querySelectorAll('.tok.hit').forEach(e => e.classList.remove('hit'));
  const q = (pv.q || '').trim().toLowerCase();
  if (!q) return;
  textNodes(body, n => !n.parentElement.closest('.tok') && n.nodeValue.toLowerCase().includes(q)).forEach(n => {
    const s = n.nodeValue, low = s.toLowerCase(), frag = document.createDocumentFragment();
    let last = 0, i = low.indexOf(q);
    while (i >= 0) {
      frag.appendChild(document.createTextNode(s.slice(last, i)));
      const mk = document.createElement('mark');
      mk.textContent = s.slice(i, i + q.length);
      frag.appendChild(mk);
      last = i + q.length;
      i = low.indexOf(q, last);
    }
    frag.appendChild(document.createTextNode(s.slice(last)));
    n.replaceWith(frag);
  });
  const forms = (pv.data && pv.data.forms) || {};
  body.querySelectorAll('.tok').forEach(e => {
    const k = e.dataset.token, f = (forms[k] || {}).forms || [];
    if (k.toLowerCase().includes(q) || f.some(v => v.toLowerCase().includes(q))) e.classList.add('hit');
  });
  const first = body.querySelector('mark, .tok.hit');
  if (scroll && first) first.scrollIntoView({ block: 'center' });
}
function jump(key, dir = 1) {
  const pv = S.pv;
  const els = [...document.querySelectorAll(`#pv-body .tok[data-token="${key}"]`)];
  if (pv.sel !== key) { pv.sel = key; pv.occ[key] = -1; renderSideList(); }
  if (!els.length) { toast(t('pv.only_image')); return; }
  const k = pv.occ[key] = ((pv.occ[key] ?? -1) + dir + els.length) % els.length;
  els.forEach(e => e.classList.remove('cur'));
  const el = els[k];
  el.scrollIntoView({ block: 'center' });
  el.classList.add('flash', 'cur');
  setTimeout(() => el.classList.remove('flash'), 1300);
}
document.addEventListener('keydown', e => {
  const pv = S.pv;
  if (!pv || S.sub !== 'preview' || DLG_OPEN || ['INPUT', 'TEXTAREA', 'SELECT'].includes((document.activeElement || {}).tagName)) return;
  if (pv.section === 'anon' && pv.sel && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { e.preventDefault(); jump(pv.sel, e.key === 'ArrowDown' ? 1 : -1); }
});
function tokPopup(key, anchor) {
  const pv = S.pv;
  if (pv.section !== 'anon') return;
  const info = (pv.data.forms || {})[key] || { type: '', forms: [] };
  const same = Object.entries(pv.data.forms || {}).filter(([k]) => k !== key && prefixOf(k) === prefixOf(key));
  popup(anchor, `<div class="ptxt">${tokHtml(key)} <span class="muted">${esc(typeLabel(info.type))}</span></div>
    <div class="ptxt" style="white-space:normal">${info.forms.map(esc).join('<br>')}</div><hr>
    <button class="mitem" id="tp-un">${ic('eye')}${esc(t('table.unmask'))}</button>
    <button class="mitem" id="tp-ed">${ic('pencil')}${esc(t('table.edit'))}</button>
    ${same.length ? `<div class="ptxt"><select class="field" id="tp-merge" aria-label="${esc(t('pv.merge_with'))}" style="width:100%"><option value="">${esc(t('pv.merge_with'))}</option>
      ${same.map(([k, v]) => `<option value="${esc(k)}">[${esc(k)}] ${esc((v.forms || [])[0] || '')}</option>`).join('')}</select></div>` : ''}`, p => {
    p.querySelector('#tp-un').onclick = () => { closePop(); unmask(key); };
    p.querySelector('#tp-ed').onclick = () => { closePop(); editRecord(key); };
    const m = p.querySelector('#tp-merge');
    if (m) m.onchange = () => { if (m.value) { closePop(); mergeTokens(key, m.value); } };
  });
}
async function mergeTokens(key, into) {
  try { await api(`/api/sessions/${S.sid}/mappings/${encodeURIComponent(key)}/merge`, { json: { into } }); }
  catch (e) { toast(errText(e)); return; }
  toast(t('pv.merged', { a: key, b: into }));
  await afterEdit();
}

// ── Выделили текст → «Замаскировать» (2-300 знаков, без меток) ─────────────────
document.addEventListener('mouseup', e => {
  const pv = S.pv;
  const old = document.querySelector('.floatbtn');
  if (old && e.target.closest('.floatbtn')) return;
  if (old) old.remove();
  if (!pv || S.sub !== 'preview' || pv.section !== 'anon') return;
  setTimeout(() => {
    const sel = window.getSelection(), text = sel ? sel.toString().trim() : '';
    const doc = $('pv-doc');
    if (!doc || !text || text.length < 2 || text.length > 300 || /\[[A-Z]+_\d+\]/.test(text) || !sel.rangeCount || !doc.contains(sel.anchorNode)) return;
    const r = sel.getRangeAt(0).getBoundingClientRect(), box = doc.getBoundingClientRect();
    const b = document.createElement('button');
    b.className = 'floatbtn';
    b.innerHTML = `${ic('eye-off')}${esc(t('pv.mask'))}`;
    b.style.left = Math.max(4, Math.min(r.left - box.left + doc.scrollLeft, doc.scrollWidth - 160)) + 'px';
    b.style.top = (r.bottom - box.top + doc.scrollTop + 6) + 'px';
    b.onclick = () => { b.remove(); window.getSelection().removeAllRanges(); addRecord(text); };
    doc.appendChild(b);
  }, 0);
});

// ── Правая панель: обезличенный файл ─────────────────────────────────────────
function tokenCounts() {
  const c = {};
  (S.pv.data.text || '').replace(/\[([A-Z]+_\d+)\]/g, (m, k) => { c[k] = (c[k] || 0) + 1; return m; });
  return c;
}
function sideAnon() {
  const d = S.pv.data;
  const leaks = d.leaks || [];
  const sim = d.similar || [];
  let h = '<div class="ph">';
  if (leaks.length) {
    h += `<div class="alert warn">${ic('triangle-alert')}<span><b>${esc(t('pv.check', { places: tn('pl.places', leaks.length) }))}</b> · ${esc(t('pv.check_hint'))}<br>
      ${leaks.slice(0, 12).map((l, i) => `<button class="link" data-leak="${i}">${esc(l.value)}</button>`).join(' · ')}</span></div>`;
  }
  if (sim.length) {
    h += `<details class="dups"><summary>${esc(t('pv.dups', { n: sim.length }))}</summary><p class="muted xs">${esc(t('pv.dups_hint'))}</p>
      ${sim.map((x, i) => `<div class="pair"><div>${tokHtml(x.a)} ${esc(x.a_value)}</div><div>${tokHtml(x.b)} ${esc(x.b_value)}</div>
        <div style="display:flex;gap:6px"><button class="btn sm" data-merge="${i}">${esc(t('pv.merge'))}</button><button class="btn sm" data-distinct="${i}">${esc(t('pv.distinct'))}</button></div></div>`).join('')}</details>`;
  }
  const groups = ['p', 'o', 'a', 'n'];
  h += `<span class="search">${ic('search')}<input class="field" id="pv-filter" placeholder="${esc(t('pv.filter'))}" aria-label="${esc(t('pv.filter'))}" value="${esc(S.pv.filter)}"></span>
    <select class="field" id="pv-ftype" aria-label="${esc(t('table.col_type'))}"><option value="">${esc(t('pv.type_all'))}</option>${groups.map(g => `<option value="${g}" ${S.pv.ftype === g ? 'selected' : ''}>${esc(t(GROUP_KEY[g]))}</option>`).join('')}</select></div>
    <div class="ps" id="pv-list"></div>`;
  return h;
}
function renderSideList() {
  const pv = S.pv, box = $('pv-list');
  if (!box || pv.section !== 'anon' || !pv.data) return;
  const counts = tokenCounts(), forms = pv.data.forms || {}, q = pv.filter.trim().toLowerCase();
  const groups = { p: [], o: [], a: [], n: [] };
  Object.keys(counts).forEach(k => {
    const g = kgrp(prefixOf(k)), f = (forms[k] || {}).forms || [];
    if (pv.ftype && pv.ftype !== g) return;
    if (q && !(k.toLowerCase().includes(q) || f.some(v => v.toLowerCase().includes(q)))) return;
    groups[g].push(k);
  });
  let h = '';
  for (const g of ['p', 'o', 'a', 'n']) {
    if (!groups[g].length) continue;
    h += `<div class="grp">${esc(t(GROUP_KEY[g]))}</div>`;
    h += groups[g].sort((a, b) => counts[b] - counts[a]).map(k => {
      const f = (forms[k] || {}).forms || [];
      const on = pv.sel === k;
      return `<div class="val${on ? ' on' : ''}" data-k="${esc(k)}" tabindex="0"><div class="l"><span class="v">${on ? '<b>' : ''}${esc(f[0] || k)}${on ? '</b>' : ''}</span><span class="n">×${counts[k]}</span>${tokHtml(k)}</div>
        ${f.length > 1 ? `<div class="f">${f.slice(1, 4).map(esc).join(' · ')}${f.length > 4 ? ' …' : ''}</div>` : ''}
        ${on ? `<div class="b"><button class="btn sm" data-ed="${esc(k)}">${ic('pencil')}${esc(t('table.edit'))}</button>
          <button class="btn sm" data-alias="${esc(k)}">${ic('plus')}${esc(t('pv.alias'))}</button>
          <button class="btn sm" data-un="${esc(k)}">${ic('eye')}${esc(t('table.unmask'))}</button></div>` : ''}</div>`;
    }).join('');
  }
  box.innerHTML = h || `<div class="empty">${esc(t('pv.nothing'))}</div>`;
  box.querySelectorAll('.val').forEach(v => {
    v.onclick = e => { if (!e.target.closest('button')) jump(v.dataset.k); };
    v.onkeydown = e => { if (e.key === 'Enter') jump(v.dataset.k); };
  });
  box.querySelectorAll('[data-ed]').forEach(b => b.onclick = () => editRecord(b.dataset.ed));
  box.querySelectorAll('[data-un]').forEach(b => b.onclick = () => unmask(b.dataset.un));
  box.querySelectorAll('[data-alias]').forEach(b => b.onclick = () => addAlias(b.dataset.alias));
}
function bindSide() {
  const pv = S.pv;
  if (!pv.data) return;
  if (pv.section === 'restore') { bindRestoredSide(); return; }
  const f = $('pv-filter');
  if (f) f.oninput = () => { pv.filter = f.value; renderSideList(); };
  const ty = $('pv-ftype');
  if (ty) ty.onchange = () => { pv.ftype = ty.value; renderSideList(); };
  document.querySelectorAll('[data-leak]').forEach(b => b.onclick = () => {
    pv.q = pv.data.leaks[+b.dataset.leak].value;
    $('pv-q').value = pv.q;
    pvSearch(true);
  });
  document.querySelectorAll('[data-merge]').forEach(b => b.onclick = () => { const x = pv.data.similar[+b.dataset.merge]; mergeTokens(x.b, x.a); });
  document.querySelectorAll('[data-distinct]').forEach(b => b.onclick = async () => {
    const x = pv.data.similar[+b.dataset.distinct];
    try { await api(`/api/sessions/${S.sid}/distinct`, { json: { a: x.a, b: x.b } }); } catch (e) { toast(errText(e)); return; }
    pv.data.similar.splice(+b.dataset.distinct, 1);
    toast(t('pv.distinct_done'));
    render();
  });
  renderSideList();
}
async function addAlias(key) {
  const forms = ((S.pv.data.forms || {})[key] || {}).forms || [];
  const r = await dialog({ title: esc(t('pv.alias_title', { token: '[' + key + ']' })),
    body: `<p class="muted">${forms.map(esc).join(' · ')}</p><label class="fl" for="al-v">${esc(t('pv.alias_field'))}</label><input class="field" id="al-v">
      <label class="radio" style="margin-top:8px"><input type="radio" name="al-s" value="session" checked>${esc(t('rec.scope_session'))}</label>
      <label class="radio"><input type="radio" name="al-s" value="always">${esc(t('rec.scope_always'))}</label>`,
    buttons: [{ label: t('common.cancel'), value: null }, { label: t('pv.alias_add'), value: 'add', kind: 'primary' }],
    read: dlg => ({ original_form: dlg.querySelector('#al-v').value.trim(), scope: dlg.querySelector('input[name=al-s]:checked').value }),
    validate: (btn, d) => d.original_form ? null : t('err.empty') });
  if (r.btn !== 'add') return;
  try { await api(`/api/sessions/${S.sid}/mappings/${encodeURIComponent(key)}/alias`, { json: r.data }); }
  catch (e) { toast(errText(e)); return; }
  toast(t('rec.saved', { token: key }));
  await afterEdit();
}

// ── Файл с возвращёнными данными (3.8): зелёный - точно, жёлтый - падеж, красный - метка не найдена ──
function sideRestored() {
  const R = S.pv.data.restore || { marks: [] };
  const by = {};
  R.marks.forEach(([m, v], i) => { const k = m + '\u0001' + v; (by[k] = by[k] || { m, v, n: 0, first: i }).n++; });
  const rank = { unknown: 0, case: 1, exact: 2 };
  const items = Object.values(by).sort((a, b) => (rank[a.m] - rank[b.m]) || a.first - b.first);
  S.pv.rvItems = items;
  return `<div class="ph"><div class="legend"><span class="muted">${esc(t('pv.legend'))}</span><span class="rv rv-exact">${esc(t('rv.exact'))}</span>
    <span class="rv rv-case">${esc(t('rv.case'))}</span><span class="rv rv-unknown">${esc(t('rv.unknown'))}</span></div></div>
    <div class="ps"><div class="grp">${esc(t('pv.inserted', { n: items.length }))}</div>
    ${items.map((x, i) => `<div class="val" data-rv="${i}" tabindex="0"><div class="l"><span class="v"><span class="rv rv-${x.m}">${esc(x.v)}</span></span><span class="n">×${x.n}</span></div></div>`).join('')}</div>`;
}
function bindRestoredSide() {
  document.querySelectorAll('[data-rv]').forEach(v => {
    const go_ = () => {
      const x = S.pv.rvItems[+v.dataset.rv];
      const els = [...document.querySelectorAll('#pv-body span.rv')].filter(e => e.dataset.value === x.v);
      if (!els.length) return;
      S.pv.rvNext = S.pv.rvNext || {};
      const k = (S.pv.rvNext[x.v] || 0) % els.length;
      S.pv.rvNext[x.v] = k + 1;
      els[k].scrollIntoView({ block: 'center' });
      els[k].classList.add('flash');
      setTimeout(() => els[k].classList.remove('flash'), 1300);
    };
    v.onclick = go_;
    v.onkeydown = e => { if (e.key === 'Enter') go_(); };
  });
}
function markRestored() {
  const body = $('pv-body'), R = S.pv.data.restore;
  if (!body || !R) return;
  const nodes = textNodes(body);
  let ni = 0, off = 0;
  const hits = [];
  for (const [mark, value] of R.marks) {
    if (!value) continue;
    for (let j = ni; j < nodes.length; j++) {
      const i = nodes[j].nodeValue.indexOf(value, j === ni ? off : 0);
      if (i >= 0) { hits.push([j, i, i + value.length, mark, value]); ni = j; off = i + value.length; break; }
    }
  }
  const byNode = {};
  hits.forEach(h => (byNode[h[0]] = byNode[h[0]] || []).push(h));
  Object.entries(byNode).forEach(([j, hs]) => {
    const n = nodes[j], s = n.nodeValue, frag = document.createDocumentFragment();
    let last = 0;
    hs.forEach(([, a, b, mark, value]) => {
      frag.appendChild(document.createTextNode(s.slice(last, a)));
      const sp = document.createElement('span');
      sp.className = 'rv rv-' + mark; sp.textContent = s.slice(a, b); sp.dataset.value = value; sp.title = t('rv.' + mark);
      frag.appendChild(sp);
      last = b;
    });
    frag.appendChild(document.createTextNode(s.slice(last)));
    n.replaceWith(frag);
  });
  pvSearch(false);
}
