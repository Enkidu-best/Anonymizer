// «Сессии» (описание 3.9), окно удаления (3.10), «Словарь» (3.11), «Настройки» (3.12), «Справка» (3.13).
'use strict';

// ── Сессии ───────────────────────────────────────────────────────────────────
S.sessUI = { q: '', checked: new Set() };
async function openSession(id) {
  await loadSession(id);
  S.rejected = [];
  S.lastStop = null;
  go('anon');
}
function deleteDialog(list) {
  const files = list.reduce((a, s) => a + (s.files || 0), 0), records = list.reduce((a, s) => a + (s.records || 0), 0);
  const title = list.length === 1 ? t('del.title', { name: list[0].name }) : t('del.title_n', { sessions: tn('pl.sessions', list.length) });
  return dialog({ title: esc(title),
    body: `<p>${t('del.text', { files: esc(tn('pl.files', files)), records: esc(tn('pl.records', records)) })}</p><p>${t('del.final')}</p>`,
    buttons: [{ label: t('common.cancel'), value: null }, { label: t('del.go'), value: 'del', kind: 'danger', icon: 'trash-2' }],
    hint: t('del.hint') }).then(r => r.btn === 'del');
}
async function deleteSessions(list) {
  if (!list.length || !(await deleteDialog(list))) return;
  for (const s of list) {
    try { await api(`/api/sessions/${s.id}`, { method: 'DELETE' }); } catch (e) { toast(errText(e)); }
    S.sessUI.checked.delete(s.id);
    if (S.sid === s.id) await loadSession(null);
    if (S.restore.sid === s.id) await loadRestoreSession(null);
  }
  toast(list.length === 1 ? t('del.done') : t('del.done_n', { sessions: tn('pl.sessions', list.length) }));
  await loadSessList();
  render();
}
VIEWS.sessions = {
  render(el) {
    crumbs([t('menu.sessions')]);
    if (S.sessList === null) { el.innerHTML = ''; loadSessList().then(render); return; }
    const U = S.sessUI;
    const days = (S.settings && S.settings.retention_days) || 30;
    const q = U.q.trim().toLowerCase();
    const list = S.sessList.filter(s => !q || s.name.toLowerCase().includes(q));
    const nChecked = S.sessList.filter(s => U.checked.has(s.id)).length;
    let rows;
    if (!S.sessList.length) rows = `<div class="empty">${esc(t('sessions.empty'))}</div>`;
    else if (!list.length) rows = `<div class="empty">${esc(t('sessions.nothing'))}</div>`;
    else rows = list.map(s => `<div class="row click" data-id="${esc(s.id)}"><input type="checkbox" class="cb" data-chk="${esc(s.id)}" ${U.checked.has(s.id) ? 'checked' : ''} aria-label="${esc(s.name)}">
      <span class="ic">${ic('folder')}</span><div class="main2"><div class="nm" data-name="${esc(s.id)}" title="${esc(s.name)}">${esc(s.name)}</div>
      <div class="meta">${esc(t('sessions.meta', { files: tn('pl.files', s.files), records: tn('pl.records', s.records), date: fmtDate(s.created_at) }))}
      ${s.expires_in_days <= 3 ? `<span class="chip warn">${ic('clock')}${esc(t('sessions.expires', { days: tn('pl.days', s.expires_in_days) }))}</span>` : ''}</div></div>
      <div class="acts"><button class="ib" data-more="${esc(s.id)}" data-pop="1" title="${esc(t('file.more'))}" aria-label="${esc(t('file.more'))}">${ic('ellipsis')}</button>
      <button class="ib" data-del="${esc(s.id)}" title="${esc(t('sessions.delete'))}" aria-label="${esc(t('sessions.delete'))}">${ic('x')}</button></div></div>`).join('');
    el.innerHTML = `<div class="head"><div class="t"><h1>${esc(t('sessions.title'))}</h1><div class="sub">${esc(t('sessions.sub', { days: tn('pl.days', days) }))}</div></div>
      <div class="act"><button class="btn primary" id="s-new">${ic('plus')}${esc(t('sessions.new'))}</button></div></div>
      <div class="sessline"><span class="search" style="width:320px">${ic('search')}<input class="field" id="s-q" placeholder="${esc(t('sessions.search'))}" aria-label="${esc(t('sessions.search'))}" value="${esc(U.q)}"></span>
      ${S.sessList.length ? `<label class="check"><input type="checkbox" class="cb" id="s-all" ${nChecked && nChecked === S.sessList.length ? 'checked' : ''}>${esc(t('sessions.select_all'))}</label>` : ''}
      <span class="grow"></span>${nChecked ? `<button class="btn danger-text" id="s-delchk">${ic('trash-2')}${esc(t('sessions.delete_checked', { n: nChecked }))}</button>` : ''}</div>
      <div class="card list" id="s-list">${rows}</div>`;
    $('s-new').onclick = () => { loadSession(null).then(() => { S.rejected = []; S.lastStop = null; go('anon'); }); };
    const sq = $('s-q');
    sq.oninput = () => { U.q = sq.value; const pos = sq.selectionStart; render(); const n = $('s-q'); n.focus(); n.setSelectionRange(pos, pos); };
    const all = $('s-all');
    if (all) all.onchange = () => { U.checked = new Set(all.checked ? S.sessList.map(s => s.id) : []); render(); };
    const dc = $('s-delchk');
    if (dc) dc.onclick = () => deleteSessions(S.sessList.filter(s => U.checked.has(s.id)));
    el.querySelectorAll('[data-chk]').forEach(c => c.onchange = () => { c.checked ? U.checked.add(c.dataset.chk) : U.checked.delete(c.dataset.chk); render(); });
    el.querySelectorAll('.row[data-id]').forEach(r => r.onclick = e => { if (!e.target.closest('button, input')) openSession(r.dataset.id); });
    el.querySelectorAll('[data-del]').forEach(b => b.onclick = () => deleteSessions(S.sessList.filter(s => s.id === b.dataset.del)));
    el.querySelectorAll('[data-more]').forEach(b => b.onclick = () => {
      const s = S.sessList.find(x => x.id === b.dataset.more);
      const items = [{ label: t('sessions.open'), icon: 'folder-open', action: () => openSession(s.id) },
        { label: t('sess.rename'), icon: 'pencil', action: () => renameInline(el.querySelector(`[data-name="${CSS.escape(s.id)}"]`), s.name, async v => { await renameSession(s.id, v); await loadSessList(); toast(t('sessions.renamed')); }) },
        { label: t('sessions.restore'), icon: 'undo-2', action: async () => { await loadRestoreSession(s.id); go('restore'); } }];
      if ((s.ready_anon || 0) > 1) items.push({ label: t('sessions.zip'), icon: 'download', action: () => downloadZip(s.id, 'anon') });
      items.push('-', { label: t('sessions.delete'), icon: 'trash-2', danger: true, action: () => deleteSessions([s]) });
      menuPop(b, items);
    });
  },
};

// ── Словарь ──────────────────────────────────────────────────────────────────
S.dict = null;
S.dictUI = { list: 'always', q: '' };
async function loadDict() {
  try { S.dict = await api('/api/dictionary'); } catch { S.dict = { always: [], never: [], migrated: 0 }; }
}
function dictSource(x) {
  const date = fmtDay(x.added_at);
  if (x.source === 'legacy') return t('dict.src_legacy', { date });
  if (x.source === 'unmask') return t('dict.src_unmask', { date });
  return t('dict.src_manual', { date });
}
async function dictAdd() {
  const U = S.dictUI;
  const opts = TYPES.map(c => `<option value="${esc(c)}">${esc(typeLabel(c))}</option>`).join('');
  const r = await dialog({ title: esc(t('dict.add_title')),
    body: `<label class="fl" for="d-v">${esc(t('dict.value'))}</label><input class="field" id="d-v">
      <label class="fl" for="d-t">${esc(t('rec.type'))}</label><select class="field" id="d-t">${opts}</select>
      <label class="fl">${esc(t('dict.list'))}</label>
      <label class="radio"><input type="radio" name="d-l" value="always" ${U.list === 'always' ? 'checked' : ''}>${esc(t('dict.always', { n: S.dict.always.length }).replace(/ · \d+$/, ''))}</label>
      <label class="radio"><input type="radio" name="d-l" value="never" ${U.list === 'never' ? 'checked' : ''}>${esc(t('dict.never', { n: S.dict.never.length }).replace(/ · \d+$/, ''))}</label>`,
    buttons: [{ label: t('common.cancel'), value: null }, { label: t('dict.add'), value: 'add', kind: 'primary' }],
    read: dlg => ({ value: dlg.querySelector('#d-v').value.trim(), entity_type: dlg.querySelector('#d-t').value, list: dlg.querySelector('input[name=d-l]:checked').value }),
    validate: (btn, d) => d.value ? null : t('err.empty') });
  if (r.btn !== 'add') return;
  try {
    const d = await api('/api/dictionary', { json: r.data });
    toast(d.moved ? t(r.data.list === 'always' ? 'dict.moved_to_always' : 'dict.moved_to_never') : t('dict.added'));
  } catch (e) { toast(errText(e)); }
  U.list = r.data.list;
  await loadDict();
  render();
}
async function dictRemove(x, which) {
  let d;
  try { d = await api('/api/dictionary/remove', { json: { value: x.value, entity_type: x.entity_type, list: which } }); } catch (e) { toast(errText(e)); return; }
  await loadDict();
  render();
  const row = d.row || x;
  toast(t('dict.removed', { value: x.value }), { action: { label: t('toast.undo'), fn: async () => {
    await api('/api/dictionary', { json: { value: row.value, entity_type: row.entity_type, list: which, source: row.source, added_at: row.added_at } }).catch(() => {});
    await loadDict();
    render();
  } } });
}
VIEWS.dict = {
  render(el) {
    crumbs([t('menu.dict')]);
    if (!S.dict) { el.innerHTML = ''; loadDict().then(render); return; }
    const U = S.dictUI, D = S.dict;
    const all = D[U.list] || [];
    const q = U.q.trim().toLowerCase();
    const list = all.filter(x => !q || x.value.toLowerCase().includes(q));
    let rows;
    if (!all.length) rows = `<div class="empty">${esc(t(U.list === 'always' ? 'dict.empty_always' : 'dict.empty_never'))}</div>`;
    else if (!list.length) rows = `<div class="empty">${esc(t('dict.nothing'))}</div>`;
    else rows = list.map((x, i) => `<div class="row"><div class="main2"><div class="nm" title="${esc(x.value)}">${esc(x.value)}</div>
      <div class="meta">${esc(typeLabel(x.entity_type))} · ${esc(dictSource(x))}</div></div>
      <div class="acts"><button class="ib" data-rm="${i}" title="${esc(t('dict.remove'))}" aria-label="${esc(t('dict.remove'))}">${ic('x')}</button></div></div>`).join('');
    el.innerHTML = `<div class="head"><div class="t"><h1>${esc(t('dict.title'))}</h1><div class="sub">${esc(t('dict.sub'))}</div></div>
      <div class="act"><button class="btn primary" id="d-add">${ic('plus')}${esc(t('dict.add'))}</button></div></div>
      ${D.migrated ? `<div class="alert info" style="margin-bottom:12px">${ic('info')}<span class="grow">${esc(t('dict.migrated', { values: tn('pl.values', D.migrated) }))}</span>
        <button class="ib" id="d-hide" title="${esc(t('dict.hide'))}" aria-label="${esc(t('dict.hide'))}">${ic('x')}</button></div>` : ''}
      <div class="sessline"><div class="seg" role="group"><button data-list="always" class="${U.list === 'always' ? 'on' : ''}">${esc(t('dict.always', { n: D.always.length }))}</button>
        <button data-list="never" class="${U.list === 'never' ? 'on' : ''}">${esc(t('dict.never', { n: D.never.length }))}</button></div><span class="grow"></span>
        <span class="search" style="width:280px">${ic('search')}<input class="field" id="d-q" placeholder="${esc(t('dict.search'))}" aria-label="${esc(t('dict.search'))}" value="${esc(U.q)}"></span></div>
      <div class="card list">${rows}</div>
      ${all.length ? `<div style="margin-top:12px"><button class="link small" id="d-clear">${esc(t('dict.clear'))}</button></div>` : ''}`;
    $('d-add').onclick = dictAdd;
    const h = $('d-hide');
    if (h) h.onclick = async () => { await api('/api/dictionary/note-hide', { json: {} }).catch(() => {}); D.migrated = 0; render(); };
    el.querySelectorAll('[data-list]').forEach(b => b.onclick = () => { U.list = b.dataset.list; render(); });
    const dq = $('d-q');
    dq.oninput = () => { U.q = dq.value; const pos = dq.selectionStart; render(); const n = $('d-q'); n.focus(); n.setSelectionRange(pos, pos); };
    el.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => dictRemove(list[+b.dataset.rm], U.list));
    const c = $('d-clear');
    if (c) c.onclick = async () => {
      if (!(await confirmDlg(t('dict.clear_title', { values: tn('pl.values', all.length) }), esc(t('dict.clear_text')), t('dict.clear')))) return;
      await api('/api/dictionary/clear', { json: { list: U.list } }).catch(e => toast(errText(e)));
      await loadDict();
      render();
    };
  },
};

// ── Настройки ────────────────────────────────────────────────────────────────
S.setOpen = { more: false, service: false };
S.llmInfo = null;
S.logs = null;
async function saveSetting(key, value, anchorId) {
  try { S.settings = await api('/api/settings', { json: { [key]: value } }); } catch (e) { toast(errText(e)); return; }
  if (key === 'use_llm' && value) S.llmInfo = null;
  render();
  pollStatus();
  const el = $('saved-' + (anchorId || key));
  if (el) { el.classList.add('show'); setTimeout(() => el.classList.remove('show'), 2000); }
}
const savedChip = key => `<span class="chip ok xs saved" id="saved-${key}">${esc(t('set.saved'))}</span>`;
function sw(key, on) { return `<button class="sw${on ? ' on' : ''}" role="switch" aria-checked="${!!on}" data-sw="${key}" aria-label="${esc(t('set.' + key))}"></button>`; }
function srow(key, label, desc, ctl) {
  return `<div class="srow"><div class="lab"><div>${esc(label)}</div>${desc ? `<div class="d">${esc(desc)}</div>` : ''}</div><div class="ctl">${ctl}${savedChip(key)}</div></div>`;
}
function selectCtl(key, values) {
  return `<select class="field" data-sel="${key}" aria-label="${esc(t('set.' + key))}">${values.map(([v, label]) =>
    `<option value="${esc(v)}" ${String(S.settings[key]) === String(v) ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>`;
}
function llmControls() {
  const L = S.llmInfo;
  if (!L) return `<span class="muted small">…</span>`;
  if (!L.available) return `<span class="muted small">${esc(t('set.ollama_off'))}</span><button class="btn sm" id="ollama-start">${esc(t('set.ollama_start'))}</button>`;
  if (!L.models.length) return `<span class="muted small">${esc(t('set.ollama_no_model'))}</span>`;
  const cur = S.settings.llm_model || L.model;
  return `<select class="field" data-sel="llm_model" aria-label="${esc(t('set.model'))}">${L.models.map(m => `<option ${m === cur ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select>
    <span class="muted small">${esc(L.has_model || L.models.includes(cur) ? t('set.ollama_ready') : t('set.ollama_no_model'))}</span>`;
}
async function openJobs() {
  let rows = [];
  try { rows = await api('/api/jobs/recent'); } catch { /* пусто */ }
  const when = s => { if (!s) return ''; const d = new Date(s * 1000); return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
  const lab = (p, k) => DICT[p + k] ? t(p + k) : (k || '');
  const body = rows.length ? `<table class="tbl"><thead><tr><th>${esc(t('jobs.when'))}</th><th>${esc(t('jobs.kind'))}</th><th>${esc(t('jobs.ext'))}</th><th>${esc(t('jobs.time'))}</th><th>${esc(t('jobs.status'))}</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${when(r.started)}</td><td>${esc(lab('jobs.', r.kind))}</td><td>${esc((r.ext || '').replace('.', '').toUpperCase())}</td>
      <td>${r.seconds != null ? fmtDur(r.seconds) : ''}</td><td>${esc(lab('jobs.', r.status))}</td></tr>`).join('')}</tbody></table>` : `<p class="muted">${esc(t('jobs.empty'))}</p>`;
  await dialog({ title: esc(t('jobs.title')), body, wide: true, buttons: [{ label: t('common.close'), value: 'ok', kind: 'primary' }] });
}
VIEWS.settings = {
  render(el) {
    crumbs([t('menu.settings')]);
    if (!S.settings || !('retention_days' in S.settings)) { el.innerHTML = ''; api('/api/settings').then(d => { S.settings = d; render(); }).catch(() => {}); return; }
    const st = S.settings;
    if (st.use_llm && !S.llmInfo) api('/api/llm-status').then(d => { S.llmInfo = d; if (S.view === 'settings') render(); }).catch(() => {});
    if (S.setOpen.service && !S.logs) api('/api/logs').then(d => { S.logs = d; if (S.view === 'settings') render(); }).catch(() => {});
    const grp = (id, title, rows, collapsible, desc) => `<div class="card sgrp">${collapsible
      ? `<button class="gh${S.setOpen[id] ? '' : ' closed'}" data-grp="${id}" aria-expanded="${!!S.setOpen[id]}">${ic('chevron-down')}<h2>${esc(title)}</h2>${desc && !S.setOpen[id] ? `<span class="d">${esc(desc)}</span>` : ''}</button>`
      : `<div class="gh"><h2>${esc(title)}</h2></div>`}${!collapsible || S.setOpen[id] ? `<div class="body">${rows}</div>` : ''}</div>`;
    const ret = `<div class="seg" role="group" aria-label="${esc(t('set.retention'))}">${[7, 30, 90].map(d => `<button data-ret="${d}" class="${st.retention_days === d ? 'on' : ''}">${esc(tn('pl.days', d))}</button>`).join('')}</div>`;
    let html = `<div class="head"><div class="t"><h1>${esc(t('set.title'))}</h1><div class="sub">${esc(t('set.sub'))}</div></div></div>`;
    html += grp('anon', t('set.g_anon'), srow('pdf_to_word', t('set.pdf_to_word'), t('set.pdf_to_word_d'), sw('pdf_to_word', st.pdf_to_word))
      + srow('keep_pdf', t('set.keep_pdf'), t('set.keep_pdf_d'), sw('keep_pdf', st.keep_pdf)));
    html += grp('restore', t('set.g_restore'), srow('org_quotes', t('set.org_quotes'), t('set.org_quotes_d'), selectCtl('org_quotes', [['always', t('set.org_quotes_always')], ['keep', t('set.org_quotes_keep')]]))
      + srow('org_opf', t('set.org_opf'), t('set.org_opf_d'), selectCtl('org_opf', [['context', t('set.org_opf_context')], ['always', t('set.org_opf_always')], ['never', t('set.org_opf_never')]]))
      + srow('highlight_case', t('set.highlight_case'), t('set.highlight_case_d'), sw('highlight_case', st.highlight_case)));
    html += grp('storage', t('set.g_storage'), srow('retention_days', t('set.retention'), t('set.retention_d'), ret));
    html += grp('more', t('set.g_more'), srow('use_llm', t('set.use_llm'), t('set.use_llm_d'), sw('use_llm', st.use_llm))
      + (st.use_llm ? srow('llm_model', t('set.model'), t('set.model_d'), llmControls()) : ''), true);
    const V = S.info || {};
    html += grp('service', t('set.g_service'),
      srow('data', t('set.data_dir'), V.data_dir || '', `<button class="btn sm" id="open-data">${ic('folder-open')}${esc(t('set.open'))}</button>`)
      + srow('journal', t('set.journal'), S.logs ? S.logs.dir : '', `<button class="btn sm" id="open-logs">${ic('folder-open')}${esc(t('set.journal_open'))}</button><button class="btn sm" id="open-jobs">${ic('list')}${esc(t('set.jobs'))}</button>`)
      + srow('verbose', t('set.verbose'), t('set.verbose_d'), `<button class="sw${S.logs && S.logs.verbose ? ' on' : ''}" role="switch" aria-checked="${!!(S.logs && S.logs.verbose)}" id="sw-verbose" aria-label="${esc(t('set.verbose'))}"></button>`)
      + srow('version', t('set.version'), '', `<span class="small">${esc(t('set.version_v', { app: shortVer(V.version || UI_VERSION), ui: shortVer(UI_VERSION), server: shortVer(V.version || '?') }))}</span>`)
      + srow('stop', t('set.stop'), '', `<button class="btn sm danger-text" id="stop-app">${ic('power')}${esc(t('set.stop'))}</button>`), true, t('set.g_service_d'));
    el.innerHTML = html;
    el.querySelectorAll('[data-grp]').forEach(b => b.onclick = () => { S.setOpen[b.dataset.grp] = !S.setOpen[b.dataset.grp]; render(); });
    el.querySelectorAll('[data-sw]').forEach(b => b.onclick = () => saveSetting(b.dataset.sw, !st[b.dataset.sw]));
    el.querySelectorAll('[data-sel]').forEach(s => s.onchange = () => saveSetting(s.dataset.sel, s.value));
    el.querySelectorAll('[data-ret]').forEach(b => b.onclick = () => saveSetting('retention_days', +b.dataset.ret));
    const os = $('ollama-start');
    if (os) os.onclick = async () => { os.disabled = true; try { S.llmInfo = await api('/api/llm-start', { json: {} }); } catch { /* сеть */ } render(); };
    const od = $('open-data');
    if (od) od.onclick = () => api('/api/data/open', { json: {} }).catch(e => toast(errText(e)));
    const ol = $('open-logs');
    if (ol) ol.onclick = () => api('/api/logs/open', { json: {} }).catch(e => toast(errText(e)));
    const oj = $('open-jobs');
    if (oj) oj.onclick = openJobs;
    const vb = $('sw-verbose');
    if (vb) vb.onclick = async () => {
      const on = !(S.logs && S.logs.verbose);
      if (on && !(await confirmDlg(t('set.verbose_title'), esc(t('set.verbose_text')), t('set.verbose_go')))) return;
      try { S.logs = { ...S.logs, ...(await api('/api/logs/verbose', { json: { on } })) }; } catch (e) { toast(errText(e)); }
      render();
    };
    const sa = $('stop-app');
    if (sa) sa.onclick = async () => {
      if (!(await confirmDlg(t('set.stop_title'), esc(t('set.stop_text')), t('set.stop_go')))) return;
      try { await api('/api/shutdown', { json: {} }); } catch { /* уже остановлена */ }
      clearTimeout(STATUS_T);
      $('content').innerHTML = `<div class="card empty">${esc(t('set.stopped'))}</div>`;
      setTimeout(() => window.close(), 800);
    };
  },
};

// ── Справка: те же разделы, что в описании (3.13) ────────────────────────────
VIEWS.help = {
  render(el) {
    crumbs([t('menu.help')]);
    el.innerHTML = `<div class="head"><div class="t"><h1>${esc(t('help.title'))}</h1><div class="sub">${esc(t('help.sub'))}</div></div></div>
      <div class="help">${[1, 2, 3, 4, 5, 6].map(i => `<section class="card"><h2>${esc(t(`help.s${i}_t`))}</h2><div style="margin-top:8px">${t(`help.s${i}`)}</div></section>`).join('')}</div>`;
  },
};
