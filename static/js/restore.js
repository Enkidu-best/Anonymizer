// «Вернуть данные» (описание 3.7): ответ нейросети текстом (вопрос 1 - основной путь) или файлом; сессия, из которой
// подставляются значения. Текст не сохраняется на диск и пропадает при уходе с экрана.
'use strict';
S.restore.files = [];
S.restore.session = null;
S.sessList = null;

async function loadSessList() {
  try { S.sessList = await api('/api/sessions'); } catch { S.sessList = S.sessList || []; }
  return S.sessList;
}
async function loadRestoreSession(sid) {
  S.restore.sid = sid || null;
  S.restore.session = null;
  S.restore.files = [];
  if (!sid) return;
  try {
    const d = await api(`/api/sessions/${sid}`);
    S.restore.session = d;
    S.restore.files = d.file_list || [];
  } catch (e) { if (e.status === 404) S.restore.sid = null; }
}
function spansHtml(text, spans) {
  let out = '', last = 0;
  for (const [a, b, mark] of spans) {
    out += esc(text.slice(last, a)) + `<span class="rv rv-${mark}" title="${esc(t('rv.' + mark))}">${esc(text.slice(a, b))}</span>`;
    last = b;
  }
  return out + esc(text.slice(last));
}
async function restoreText() {
  const R = S.restore;
  const text = ($('rs-text') || {}).value || R.text;
  R.text = text;
  if (!text.trim()) { toast(t('restore.paste_empty')); return; }
  let lock = 'wait', llm = true;
  if (S.status && S.status.use_llm) {
    const ch = await gpuChoice();
    if (ch === null) llm = false; else lock = ch;          // «Отмена» - падеж только правилами (3.7)
  }
  const btn = $('go-restore');
  if (btn) btn.disabled = true;
  try {
    R.result = await api(`/api/sessions/${R.sid}/restore-text`, { json: { text, lock, llm } });
  } catch (e) { toast(errText(e)); }
  render();
}
async function copyRestored() {
  if (await copyText(S.restore.result.text)) toast(t('restore.copied'));
  else toast(t('err.generic'));
}
VIEWS.restore = {
  render(el) {
    if (S.sub === 'preview' && S.pv) return renderPreview(el);
    const R = S.restore;
    if (S.sessList === null) { loadSessList().then(render); el.innerHTML = ''; crumbs([t('menu.restore')]); return; }
    if (!R.sid || !S.sessList.some(s => s.id === R.sid)) {
      const pick = (S.sid && S.sessList.some(s => s.id === S.sid)) ? S.sid : (S.sessList[0] || {}).id;
      if (pick && pick !== R.sid) { loadRestoreSession(pick).then(render); el.innerHTML = ''; return; }
      if (!pick) R.sid = null;
    }
    const sess = R.session;
    crumbs(sess ? [t('menu.restore'), sessTitle({ name: sess.name, files: sess.files })] : [t('menu.restore')]);
    const textMode = R.mode === 'text';
    if (!R.sid) {
      el.innerHTML = `<div class="head"><div class="t"><h1>${esc(t('restore.title'))}</h1><div class="sub">${esc(t('restore.sub'))}</div></div>
        <div class="act"><button class="btn primary" disabled>${ic('undo-2')}${esc(t('restore.go'))}</button></div></div>
        <div class="card empty">${ic('info')} ${esc(t('restore.no_sessions'))}</div>`;
      return;
    }
    const files = filesOf('restore');
    const nNew = files.filter(f => f.status === 'new').length;
    const running = S.run && S.run.section === 'restore';
    const action = textMode
      ? `<button class="btn primary" id="go-restore">${ic('undo-2')}${esc(t('restore.go'))}</button>`
      : `<button class="btn primary" id="go-restore" ${!nNew || running ? 'disabled' : ''}>${ic('undo-2')}${esc(nNew && !running ? t('restore.go_n', { n: nNew }) : t('restore.go'))}</button>` +
        (!nNew || running ? `<span class="xs muted">${esc(t(running ? 'anon.running' : files.length ? 'anon.no_new' : 'anon.need_files'))}</span>` : '');
    const opts = S.sessList.map(s => `<option value="${esc(s.id)}" ${s.id === R.sid ? 'selected' : ''}>${esc(s.name)} · ${esc(tn('pl.files', s.files))} · ${esc(tn('pl.records', s.records))}</option>`).join('');
    let body = `<div class="head"><div class="t"><h1>${esc(t('restore.title'))}</h1><div class="sub">${esc(t(textMode ? 'restore.sub_text' : 'restore.sub'))}</div></div><div class="act">${action}</div></div>
      <div class="sessline"><label class="muted" for="rs-sess">${esc(t('sess.label'))}</label><select class="field" id="rs-sess">${opts}</select><span class="grow"></span>
      <div class="seg" role="group"><button data-mode="text" class="${textMode ? 'on' : ''}">${esc(t('restore.mode_text'))}</button><button data-mode="file" class="${textMode ? '' : 'on'}">${esc(t('restore.mode_file'))}</button></div></div>`;
    if (textMode) {
      const res = R.result;
      body += `<textarea class="field paste" id="rs-text" placeholder="${esc(t('restore.paste'))}" aria-label="${esc(t('restore.paste'))}">${esc(R.text)}</textarea>`;
      if (res) {
        const st = res.stats || {};
        body += `<div class="card sum" style="margin-top:12px"><span class="big">${ic('shield-check')}${esc(st.tokens ? t('file.restored', { n: fmtNum(st.restored || 0), exact: fmtNum(st.exact || 0), cases: fmtNum(st.check_case || 0) }) : t('file.restored_none'))}</span>
          ${(st.unknown || []).length ? `<span class="chip bad">${esc(t('file.unknown', { tokens: st.unknown.map(x => '[' + x + ']').join(', ') }))}</span>` : ''}
          <span class="grow"></span><button class="btn" id="rs-copy">${ic('copy')}${esc(t('restore.copy'))}</button></div>
          <div class="result" id="rs-result">${spansHtml(res.text, res.spans || [])}</div>`;
      }
      body += `<div class="sec"><div class="card legend" style="padding:12px 16px"><span class="muted small">${esc(t('pv.legend'))}</span>
        <span class="chip ok">${esc(t('rv.exact'))}</span><span class="chip warn">${esc(t('rv.case'))}</span><span class="chip bad">${esc(t('rv.unknown'))}</span>
        <span class="muted small">${esc(t('restore.not_saved'))}</span></div></div>`;
    } else {
      const big = !files.length && !S.rejected.some(x => x.section === 'restore');
      body += (big ? `<div class="drop big" id="drop-restore" tabindex="0" role="button">${ic('upload')}<div>${t('drop.big_restore')}</div><div class="small">${esc(t('drop.formats_restore'))}</div></div>`
        : `<div class="drop strip" id="drop-restore" tabindex="0" role="button">${ic('upload')}<span>${t('drop.strip_restore')}</span></div>`)
        + progressCard('restore') + summaryCard('restore') + fileList('restore');
    }
    el.innerHTML = body;
    $('rs-sess').onchange = async e => { await loadRestoreSession(e.target.value); R.result = null; render(); };
    el.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => {
      if (textMode && $('rs-text')) R.text = $('rs-text').value;
      R.mode = b.dataset.mode; store.set('restoreMode', R.mode); render();
    });
    const g = $('go-restore');
    if (g) g.onclick = () => textMode ? restoreText() : startRun('restore');
    if (textMode) {
      const ta = $('rs-text');
      ta.oninput = () => { R.text = ta.value; };
      ta.onkeydown = e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); restoreText(); } };
      const c = $('rs-copy');
      if (c) c.onclick = copyRestored;
    } else {
      bindDrop($('drop-restore'), 'restore');
      bindRun();
      bindSummary('restore');
      bindFileList('restore');
    }
  },
};
