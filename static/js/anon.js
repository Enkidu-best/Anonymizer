// «Обезличить» (описание 3.2), таблица замен (3.3), окно записи (3.4), «Не маскировать» (3.5), предложения нейросети
// (3.15). Загрузка, ход работы и строки файлов - общие с «Вернуть данные» (restore.js).
'use strict';

const sectionSid = section => section === 'anon' ? S.sid : S.restore.sid;
const filesOf = section => (section === 'anon' ? S.files : (S.restore.files || [])).filter(f => f.section === section);
S.rejected = [];        // файлы, не принятые до загрузки: больше 100 МБ, чужой формат (только в окне)
S.llm = {};             // состояние проверки нейросетью по id задачи

// ── Загрузка файлов: каждый своим запросом (было Р-41) ───────────────────────
function pickFiles() {
  const inp = $('file-input');
  inp.value = '';
  inp.click();
}
document.addEventListener('DOMContentLoaded', () => {
  const inp = $('file-input');
  inp.onchange = () => { addFiles([...inp.files], S.view === 'restore' ? 'restore' : 'anon'); inp.value = ''; };
});
function bindDrop(el, section) {
  if (!el) return;
  el.onclick = () => pickFiles();
  el.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickFiles(); } };
  el.ondragover = e => { e.preventDefault(); el.classList.add('over'); };
  el.ondragleave = () => el.classList.remove('over');
  el.ondrop = e => { e.preventDefault(); el.classList.remove('over'); addFiles([...e.dataTransfer.files], section); };
}
const OK_EXT = ['txt', 'docx', 'docm', 'doc', 'odt', 'pptx', 'pdf', 'xlsx', 'xlsm', 'rtf', 'jpg', 'jpeg', 'png', 'heic', 'tif', 'tiff', 'bmp', 'webp'];
async function addFiles(list, section) {
  if (!list.length) return;
  let sid;
  try {
    sid = section === 'anon' ? await ensureSession() : S.restore.sid;
  } catch (e) { toast(errText(e)); return; }
  if (!sid) return;
  for (const f of list) {
    if (!OK_EXT.includes(extOf(f.name))) { S.rejected.push({ input: f.name, size: f.size, code: 'format', section }); continue; }
    if (f.size > 100 * 1024 * 1024) { S.rejected.push({ input: f.name, size: f.size, code: 'too_big', section }); continue; }
    const fd = new FormData();
    fd.append('file', f, f.name);
    fd.append('section', section);
    try {
      const entry = await api(`/api/sessions/${sid}/files`, { method: 'POST', body: fd });
      if (S.run && S.run.section === section) S.run.list.push(entry.input);    // идёт обработка - в очередь
    } catch (e) {
      if (e.code === 'duplicate') toast(t('file.duplicate'));
      else S.rejected.push({ input: f.name, size: f.size, code: e.code, section });
    }
  }
  await reloadSection(section);
  render();
}
async function reloadSection(section) {
  if (section === 'anon') await loadSession(S.sid);
  else await loadRestoreSession(S.restore.sid);
}

// ── Ход работы (стандарт 2.2): процент, этап, прошло, осталось, «Остановить» ─
const STAGE_FRAC = { read: 0.1, find: 0.45, replace: 0.75, leaks: 0.9, restore: 0.5, llm: 0.95 };
async function gpuChoice() {
  const HL = window.HeavyLockUI;
  if (!HL) return 'wait';
  let st;
  try { st = await api('/api/heavy-lock?kind=accurate'); } catch { return 'wait'; }
  if (st.ours || (st.free && !(st.queue || []).length)) return 'wait';
  await HL.ready;
  return HL.ask(st.holder, { lang: LANG, cpu: st.cpu, queue: (st.queue || []).map(w => w.program),
                             page: st.page ? '/api/heavy-lock/page' : null });
}
async function startRun(section) {
  if (S.run) return;
  const sid = sectionSid(section);
  const list = filesOf(section).filter(f => f.status === 'new').map(f => f.input);
  if (!sid || !list.length) return;
  let lock = 'wait', llm = true;
  if (S.status && S.status.use_llm) {          // окно «Видеокарта занята» до начала (3.14, было Р-11)
    const ch = await gpuChoice();
    if (ch === null && section === 'anon') return;
    if (ch === null) llm = false; else lock = ch;
  }
  S.run = { section, sid, list, i: 0, done: 0, stage: 'read', started: Date.now(), fileStarted: Date.now(), stop: false,
            pid: Math.random().toString(36).slice(2), useSpacy: true, lock, llm, phase: 'run', times: [] };
  S.lastStop = null;
  render();
  await runLoop();
}
async function waitNer() {
  // модель распознавания имён ещё загружается - ждём; не загрузилась - выбор человека (описание 3.2, было Р-13)
  for (;;) {
    const ner = (S.status && S.status.ner) || {};
    if (ner.ready) return 'ready';
    if (!ner.loading && ner.error) return 'failed';
    S.run.phase = 'ner_wait';
    paintRun();
    await new Promise(res => { S.nerWait = () => { S.nerWait = null; res(); }; setTimeout(res, 1500); });
    if (!S.run) return 'stopped';
  }
}
async function runLoop() {
  const run = S.run;
  if (run.section === 'anon' && run.useSpacy) {
    const w = await waitNer();
    if (w === 'failed') { run.phase = 'ner_failed'; paintRun(); return; }
    if (w === 'stopped') return;
  }
  run.phase = 'run';
  const timer = setInterval(async () => {
    paintRun();
    try {
      const p = await api(`/api/progress/${run.pid}`);
      if (p.stage) run.stage = p.stage;
    } catch { /* сеть */ }
  }, 700);
  try {
    for (; run.i < run.list.length; run.i++) {
      if (run.stop) break;
      const name = run.list[run.i];
      run.stage = run.section === 'restore' ? 'restore' : 'read';
      run.fileStarted = Date.now();
      paintRun();
      let r;
      try {
        r = await api(`/api/sessions/${run.sid}/process`, { json: { input: name, section: run.section, use_spacy: run.useSpacy,
          lock: run.lock, llm: run.llm, progress: run.pid } });
      } catch (e) {
        if (e.code === 'ner_failed') { run.phase = 'ner_failed'; paintRun(); return; }
        if (e.code === 'offline') { run.stop = true; break; }
        await reloadSection(run.section);
        continue;
      }
      run.times.push((Date.now() - run.fileStarted) / 1000);
      run.done++;
      const list = run.section === 'anon' ? S.files : S.restore.files;
      const k = list.findIndex(f => f.input === name && f.section === run.section);
      if (k >= 0) list[k] = r.file;
      if (run.section === 'anon' && S.session) S.session.records = r.records;
      if (r.file && r.file.result && r.file.result.llm_job) pollLlm(r.file.result.llm_job);
      paintRun();
    }
  } finally {
    clearInterval(timer);
  }
  const stopped = run.stop && run.i < run.list.length;
  S.run = null;
  if (stopped) S.lastStop = { section: run.section, done: run.done, total: run.list.length };
  await reloadSection(run.section);
  if (run.section === 'anon') await loadMaps();
  render();
}
function stopRun() {
  if (!S.run) return;
  if (S.run.phase !== 'run') { const r = S.run; S.run = null; reloadSection(r.section).then(render); return; }
  S.run.stop = true;
  api('/api/llm-cancel', { json: {} }).catch(() => {});
  paintRun();
}
function progressCard(section) {
  const run = S.run && S.run.section === section ? S.run : null;
  if (!run) return '';
  return `<div class="card prog" id="prog">${progressInner(run)}</div>`;
}
function progressInner(run) {
  if (run.phase === 'ner_wait') {
    return `<div class="bar"><i style="width:2%"></i></div><div class="prow">${ic('clock')}<span>${esc(t('prog.ner_wait'))}</span>
      <span class="grow"></span><button class="btn sm" id="p-stop">${ic('circle-stop')}${esc(t('prog.stop'))}</button></div>`;
  }
  if (run.phase === 'ner_failed') {
    return `<div class="prow"><b>${ic('triangle-alert')} ${esc(t('prog.ner_failed'))}</b><span class="grow"></span>
      <button class="btn sm" id="p-retry">${ic('refresh-cw')}${esc(t('prog.ner_retry'))}</button>
      <button class="btn sm danger-text" id="p-noner">${esc(t('prog.without_ner'))}</button></div>
      <div class="warnline">${esc(t('prog.without_ner_hint'))}</div>`;
  }
  const n = run.list.length;
  const frac = Math.min(0.99, (run.done + (STAGE_FRAC[run.stage] || 0.1)) / n);
  const pct = Math.round(frac * 100);
  const el = (Date.now() - run.started) / 1000;
  const avg = run.times.length ? run.times.reduce((a, b) => a + b, 0) / run.times.length : null;
  const left = avg !== null ? Math.max(0, avg * (n - run.done) - (Date.now() - run.fileStarted) / 1000) : (frac > 0.05 ? el / frac - el : null);
  const stageKey = 'stage.' + (run.stage || 'read');
  return `<div class="bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></div>
    <div class="prow"><b>${pct} %</b><span>${esc(t('prog.files', { i: Math.min(run.i + 1, n), n }))} · ${esc(t(stageKey))}</span>
    <span class="muted">${fmtDur(el)}${left !== null ? ' · ' + esc(t('prog.left', { t: fmtDur(left) })) : ''}</span><span class="grow"></span>
    ${run.stop ? `<span class="muted">${esc(t('prog.stopping'))}</span>` : `<button class="btn sm" id="p-stop">${ic('circle-stop')}${esc(t('prog.stop'))}</button>`}</div>`;
}
function paintRun() {
  renderStatus();
  const run = S.run;
  const p = $('prog');
  if (!run || !p) return;
  p.innerHTML = progressInner(run);
  bindRun();
  // строки файлов: «В очереди» → этап
  run.list.forEach((name, k) => {
    const row = document.querySelector(`.row[data-input="${CSS.escape(name)}"] .meta`);
    if (!row) return;
    if (k === run.i && run.phase === 'run') row.innerHTML = `${ic('clock')}${esc(t('stage.' + (run.stage || 'read')))}…`;
    else if (k > run.i) row.innerHTML = esc(t('file.queued'));
  });
}
function bindRun() {
  const s = $('p-stop');
  if (s) s.onclick = stopRun;
  const r = $('p-retry');
  if (r) r.onclick = async () => { await nerRetry(); S.run.phase = 'ner_wait'; paintRun(); await runLoop(); };
  const n = $('p-noner');
  if (n) n.onclick = async () => { S.run.useSpacy = false; await runLoop(); };
}

// ── Строки файлов ────────────────────────────────────────────────────────────
function fileMeta(f) {
  const res = f.result || {};
  if (f.status === 'new') return { cls: '', html: esc(t('file.new', { size: fmtSize(f.size || 0) })) };
  if (f.status === 'error') return { cls: 'bad', html: `${ic('circle-x')}${esc(errText({ code: f.error }))}` };
  if (S.rebuild && f.section === 'anon') return { cls: 'info', html: `${ic('refresh-cw')}${esc(t('file.rebuilding'))}` };
  if (f.section === 'restore') {
    const r = res.restore || {};
    if (res.image) return { cls: 'bad', html: esc(t('file.image_restore')) };
    const head = r.tokens ? t('file.restored', { n: fmtNum(r.restored || 0), exact: fmtNum(r.exact || 0), cases: fmtNum(r.check_case || 0) })
      : t('file.restored_none');
    const unk = (r.unknown || []).length
      ? `<span class="chip bad">${esc(t('file.unknown', { tokens: r.unknown.map(x => '[' + x + ']').join(', ') }))}</span>` : '';
    return { cls: '', html: `<span>${esc(head)}</span>${unk}<span>→ ${esc(f.output || '')}</span>` };
  }
  const leaks = (res.leaks || []).length;
  const out = f.output ? ` · ${withTokens(f.output)}` : '';
  if (res.apply_errors) return { cls: 'bad', html: `${ic('triangle-alert')}${esc(t('file.not_applied', { n: tn('pl.values', res.apply_errors) }))}` };
  if (res.image) return { cls: leaks ? 'warn' : 'ok', html: `${ic(leaks ? 'triangle-alert' : 'check')}${esc(t('file.found_ok', { n: fmtNum(res.found || 0) }))} · ${esc(t('file.image'))}` };
  if (leaks) return { cls: 'warn', html: `${ic('triangle-alert')}${esc(t('file.found_check', { n: fmtNum(res.found || 0), places: tn('pl.places', leaks) }))}` };
  return { cls: 'ok', html: `${ic('check')}${esc(t('file.found_ok', { n: fmtNum(res.found || 0) }))}${out}` +
    (res.without_ner ? ` · <span class="warn">${esc(t('file.without_ner'))}</span>` : '') };
}
function fileRow(f) {
  const m = fileMeta(f);
  const done = f.status === 'done';
  const pptx = extOf(f.output || f.input) === 'pptx';
  const img = (f.result || {}).image;
  let acts = '';
  if (done && !(S.rebuild && f.section === 'anon')) {
    acts += pptx ? `<span class="muted small">${esc(t('file.no_preview_pptx'))}</span>`
      : (f.section === 'restore' && img) ? '' : `<button class="btn sm" data-act="preview">${ic('eye')}${esc(t('file.preview'))}</button>`;
    acts += `<button class="btn sm" data-act="dl" data-pop="1">${ic('download')}${esc(t('file.download'))}${ic('chevron-down')}</button>`;
  }
  if (f.status === 'new' && !(S.run && S.run.list.includes(f.input))) acts += `<button class="ib" data-act="rm-new" title="${esc(t('file.remove_new'))}" aria-label="${esc(t('file.remove_new'))}">${ic('x')}</button>`;
  else if (!S.run || !S.run.list.includes(f.input) || f.status !== 'new') acts += `<button class="ib" data-act="more" data-pop="1" title="${esc(t('file.more'))}" aria-label="${esc(t('file.more'))}">${ic('ellipsis')}</button>`;
  return `<div class="row" data-input="${esc(f.input)}" data-section="${f.section}"><span class="ic">${ic(fileIcon(f.input))}</span>
    <div class="main2"><div class="nm" title="${esc(f.input)}">${esc(f.input)}</div><div class="meta ${m.cls}">${m.html}</div>${llmLine(f)}</div>
    <div class="acts">${acts}</div></div>`;
}
function rejectedRow(x, i) {
  return `<div class="row"><span class="ic">${ic(fileIcon(x.input))}</span><div class="main2"><div class="nm" title="${esc(x.input)}">${esc(x.input)}</div>
    <div class="meta bad">${ic('circle-x')}${esc(errText({ code: x.code }))}</div></div>
    <div class="acts"><button class="ib" data-rej="${i}" title="${esc(t('file.remove_new'))}" aria-label="${esc(t('file.remove_new'))}">${ic('x')}</button></div></div>`;
}
function fileList(section) {
  const files = filesOf(section);
  const rej = S.rejected.map((x, i) => [x, i]).filter(([x]) => x.section === section);
  if (!files.length && !rej.length) return '';
  return `<div class="card list" id="files-${section}">${files.map(fileRow).join('')}${rej.map(([x, i]) => rejectedRow(x, i)).join('')}</div>`;
}
function bindFileList(section) {
  const box = $('files-' + section);
  if (!box) return;
  box.querySelectorAll('[data-rej]').forEach(b => b.onclick = () => { S.rejected.splice(+b.dataset.rej, 1); render(); });
  box.querySelectorAll('.row[data-input]').forEach(row => {
    const f = filesOf(section).find(x => x.input === row.dataset.input);
    if (!f) return;
    row.querySelectorAll('[data-act]').forEach(b => b.onclick = e => fileAction(f, b.dataset.act, e.currentTarget));
  });
  box.querySelectorAll('[data-llm-look]').forEach(b => b.onclick = () => openReview(b.dataset.llmLook));
  box.querySelectorAll('[data-llm-cancel]').forEach(b => b.onclick = () => api('/api/llm-cancel', { json: { job: b.dataset.llmCancel } }).catch(() => {}));
}
function download(sid, name) {
  const a = document.createElement('a');
  a.href = `/api/download/${encodeURIComponent(sid)}/${encodeURIComponent(name)}`;
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
}
function downloadZip(sid, section) {
  const a = document.createElement('a');
  a.href = `/api/download-all/${encodeURIComponent(sid)}?section=${section}`;
  document.body.appendChild(a); a.click(); a.remove();
}
async function fileAction(f, act, anchor) {
  const sid = sectionSid(f.section);
  if (act === 'preview') return openPreview(f);
  if (act === 'dl') {
    const items = [{ label: t(f.section === 'anon' ? 'file.dl_main' : 'file.dl_main_restore', { ext: extOf(f.output).toUpperCase() }), icon: 'download', action: () => download(sid, f.output) }];
    if (f.extra) items.push({ label: t('file.dl_pdf'), icon: 'download', action: () => download(sid, f.extra) });
    return menuPop(anchor, items);
  }
  if (act === 'rm-new') return removeFile(f, false);
  if (act === 'more') {
    const items = [];
    const res = f.result || {};
    if (f.status === 'done' && f.section === 'anon' && S.status && S.status.use_llm && !res.image)
      items.push({ label: t('file.recheck_llm'), icon: 'sparkles', action: () => recheckLlm(f) });
    if (res.apply_errors || f.status === 'error') items.push({ label: t('file.open_log'), icon: 'list', action: () => api('/api/logs/open', { json: {} }).catch(() => {}) });
    items.push({ label: t('file.remove'), icon: 'trash-2', danger: true, action: () => removeFile(f, true) });
    return menuPop(anchor, items);
  }
}
async function removeFile(f, ask) {
  if (ask && !(await confirmDlg(t('file.remove_title', { name: f.input }), esc(t('file.remove_text')), t('file.remove'), 'primary'))) return;
  try {
    await api(`/api/sessions/${sectionSid(f.section)}/files`, { method: 'DELETE', json: { input: f.input, section: f.section } });
    if (ask) toast(t('file.removed'));
  } catch (e) { toast(errText(e)); }
  await reloadSection(f.section);
  render();
}

// ── Проверка нейросетью в фоне (только если включена) ────────────────────────
function llmLine(f) {
  const jid = (f.result || {}).llm_job;
  if (!jid || f.section !== 'anon') return '';
  const st = S.llm[jid] || { status: 'running' };
  if (!S.llm[jid]) pollLlm(jid);
  const line = (cls, html) => `<div class="sub2 ${cls}">${ic('sparkles')}${html}</div>`;
  if (st.status === 'wait_lock') return line('warn', `${esc(t('llmrow.wait', { program: st.holder || '?' }))} <button class="link" data-llm-cancel="${jid}">${esc(t('llmrow.cancel_wait'))}</button>`);
  if (st.status === 'running') return line('info', esc(t('llmrow.running')));
  if (st.status === 'stale') return line('warn', esc(t('llmrow.stale')));
  if (st.status === 'cancelled') return line('', esc(t('llmrow.cancelled')));
  if (st.status === 'lost') return (f.result || {}).llm_applied ? line('ok', esc(t('llmrow.applied'))) : line('', esc(t('llmrow.lost')));
  if (st.status === 'error') return line('warn', esc(t(st.error === 'busy' ? 'llmrow.busy' : 'llmrow.error')));
  if (st.status === 'applied') return line('ok', esc(t('llmrow.applied')));
  const p = st.proposal || { add: [], remove: [], merge: [] };
  if (!p.add.length && !p.remove.length && !p.merge.length) return line('ok', esc(t('llmrow.ok')));
  return line('info', esc(t('llmrow.proposes', { add: p.add.length, remove: p.remove.length }) + (p.merge.length ? t('llmrow.proposes_merge', { merge: p.merge.length }) : '')) +
    ` <button class="link" data-llm-look="${jid}">${esc(t('llmrow.look'))}</button>`);
}
const LLM_POLL = {};
function pollLlm(jid) {
  if (LLM_POLL[jid]) return;
  LLM_POLL[jid] = true;
  const tick = async () => {
    try { S.llm[jid] = await api(`/api/llm-jobs/${jid}`); }
    catch (e) { S.llm[jid] = { status: e.status === 404 ? 'lost' : 'error', error: 'unavailable' }; }
    const st = S.llm[jid].status;
    if (S.view === 'anon' && !S.sub) {
      const f = S.files.find(x => (x.result || {}).llm_job === jid);
      const row = f && document.querySelector(`.row[data-input="${CSS.escape(f.input)}"] .main2`);
      if (row) {
        const old = row.querySelector('.sub2');
        if (old) old.remove();
        row.insertAdjacentHTML('beforeend', llmLine(f));
        bindFileList('anon');
      }
    }
    if (st === 'running' || st === 'wait_lock') setTimeout(tick, 2500); else LLM_POLL[jid] = false;
  };
  tick();
}
async function recheckLlm(f) {
  // ещё раз: файл обрабатывается заново с проверкой нейросетью
  const ch = await gpuChoice();
  if (ch === null) return;
  S.files = S.files.map(x => x.input === f.input && x.section === 'anon' ? { ...x, status: 'new' } : x);
  S.run = { section: 'anon', sid: S.sid, list: [f.input], i: 0, done: 0, stage: 'read', started: Date.now(), fileStarted: Date.now(),
            stop: false, pid: Math.random().toString(36).slice(2), useSpacy: !(f.result || {}).without_ner, lock: ch, llm: true, phase: 'run', times: [] };
  render();
  await runLoop();
}
async function openReview(jid) {
  const st = S.llm[jid];
  if (!st || !st.proposal) return;
  const p = st.proposal;
  const row = (kind, key, checked, html) => `<label class="ck"><input type="checkbox" data-kind="${kind}" data-key="${esc(key)}" ${checked ? 'checked' : ''}><span>${html}</span></label>`;
  let body = `<p class="muted">${esc(t('llmrev.sub'))}</p><div class="rv-list">`;
  if (p.add.length) body += `<h3>${esc(t('llmrev.add'))}</h3>` + p.add.map((a, i) => row('add', i, true, `<b>${esc(typeLabel(a.type))}</b> ${esc(a.value)}`)).join('');
  // «Снять маску» по умолчанию не отмечено: метрика - полнота (вопрос 5, было Р-25)
  if (p.remove.length) body += `<h3>${esc(t('llmrev.remove'))}</h3>` + p.remove.map(r => row('remove', r.token, false,
    `${tokHtml(r.token)} ${esc(r.value)} <span class="muted">${DICT['llmrev.cat_' + r.category] ? '- ' + esc(t('llmrev.cat_' + r.category)) : ''}</span>`)).join('');
  if (p.merge.length) body += `<h3>${esc(t('llmrev.merge'))}</h3>` + p.merge.map(m => row('merge', m.token, true,
    `${tokHtml(m.token)} ${esc(m.value)} = ${tokHtml(m.into)} ${esc(m.into_value)}`)).join('');
  body += '</div>';
  const r = await dialog({ title: esc(t('llmrev.title')), body, wide: true,
    buttons: [{ label: t('common.cancel'), value: null }, { label: t('llmrev.apply'), value: 'apply', kind: 'primary' }],
    read: dlg => [...dlg.querySelectorAll('input:checked')].map(c => [c.dataset.kind, c.dataset.key]) });
  if (r.btn !== 'apply') return;
  const pick = k => r.data.filter(x => x[0] === k).map(x => k === 'add' ? +x[1] : x[1]);
  S.rebuild = true; render();
  try {
    await api(`/api/llm-jobs/${jid}/apply`, { json: { add: pick('add'), remove: pick('remove'), merge: pick('merge') } });
    S.llm[jid] = { ...st, status: 'applied' };
  } catch (e) { toast(errText(e)); }
  S.rebuild = false;
  await loadSession(S.sid);
  render();
}

// ── Итог ─────────────────────────────────────────────────────────────────────
function summaryCard(section) {
  if (S.run && S.run.section === section) return '';
  const files = filesOf(section);
  const done = files.filter(f => f.status === 'done');
  let stop = '';
  if (S.lastStop && S.lastStop.section === section)
    stop = `<div class="card sum"><span class="big warn">${ic('circle-stop')}${esc(t(section === 'anon' ? 'prog.stopped' : 'prog.stopped_restore', { done: S.lastStop.done, total: S.lastStop.total }))}</span></div>`;
  if (!done.length) return stop;
  const sid = sectionSid(section);
  const zip = done.length + done.filter(f => f.extra).length >= 2 ? `<button class="btn" id="zip-${section}">${ic('download')}${esc(t('sum.zip'))}</button>` : '';
  if (section === 'restore') {
    const n = done.reduce((a, f) => a + (((f.result || {}).restore || {}).restored || 0), 0);
    const unk = done.reduce((a, f) => a + ((((f.result || {}).restore || {}).unknown) || []).length, 0);
    return stop + `<div class="card sum"><span class="big">${ic('shield-check')}${esc(t('restore.sum_files', { files: tn('pl.files', done.length), n: fmtNum(n) }))}</span>
      ${unk ? `<span class="chip bad">${ic('triangle-alert')}${esc(t('restore.unknown_chip', { n: unk }))}</span>` : ''}<span class="grow"></span>${zip}</div>`
      + `<div class="sec"><div class="card legend" style="padding:12px 16px"><span class="muted small">${esc(t('pv.legend'))}</span>
      <span class="chip ok">${esc(t('rv.exact'))}</span><span class="chip warn">${esc(t('rv.case'))}</span><span class="chip bad">${esc(t('rv.unknown'))}</span></div></div>`;
  }
  const check = done.filter(f => ((f.result || {}).leaks || []).length || (f.result || {}).apply_errors).length;
  const errs = files.filter(f => f.status === 'error').length;
  const records = S.session ? S.session.records : 0;
  const head = errs ? t('sum.partial', { done: done.length, total: done.length + errs })
    : t('sum.done', { files: tn('pl.files', done.length), records: tn('pl.records', records) }) + (check ? '' : ' · ' + t('sum.no_leaks'));
  return stop + `<div class="card sum" id="sum-anon"><span class="big${errs ? ' warn' : ''}">${ic(errs ? 'triangle-alert' : 'shield-check')}${esc(head)}</span>
    ${check ? `<span class="chip warn">${ic('triangle-alert')}${esc(t('sum.check_files', { files: tn('pl.check_files', check) }))}</span>` : ''}
    <span class="grow"></span>${zip}<button class="btn" id="instr">${ic('copy')}${esc(t('sum.instr'))}</button></div>`;
}
function bindSummary(section) {
  const z = $('zip-' + section);
  if (z) z.onclick = () => downloadZip(sectionSid(section), section);
  const i = $('instr');
  if (i) i.onclick = copyInstruction;
}
async function copyInstruction() {
  const text = t('llm.instruction');
  if (await copyText(text)) { toast(t('sum.instr_copied')); return; }
  await dialog({ title: esc(t('sum.instr_title')), body: `<p class="muted">${esc(t('sum.instr_manual'))}</p><textarea class="field" rows="5" readonly id="instr-ta">${esc(text)}</textarea>`,
    buttons: [{ label: t('common.close'), value: 'ok', kind: 'primary' }], init: dlg => { const ta = dlg.querySelector('textarea'); ta.focus(); ta.select(); } });
}

// ── Экран «Обезличить» ───────────────────────────────────────────────────────
function sessLine() {
  if (!S.session) return `<div class="sessline muted">${ic('info')}<span>${esc(t('sess.appear'))}</span></div>`;
  const s = S.session;
  return `<div class="sessline" id="sessline"><span class="muted">${esc(t('sess.label'))}</span><span class="nm" id="sess-name">${esc(s.name)}</span>
    <span class="muted">· ${esc(tn('pl.files', s.files_anon + s.files_restore))} · ${esc(tn('pl.records', s.records))}</span>
    <button class="ib" id="sess-rename" title="${esc(t('sess.rename'))}" aria-label="${esc(t('sess.rename'))}">${ic('pencil')}</button>
    <span class="grow"></span><button class="btn sm" id="sess-new">${ic('plus')}${esc(t('sess.new'))}</button></div>`;
}
function renameInline(span, current, save) {
  const inp = document.createElement('input');
  inp.className = 'field';
  inp.value = current;
  inp.setAttribute('aria-label', t('sess.rename'));
  span.replaceWith(inp);
  inp.focus(); inp.select();
  let done = false;
  const finish = async ok => {
    if (done) return;
    done = true;
    const v = inp.value.trim();
    if (ok && v && v !== current) await save(v);
    render();
  };
  inp.onkeydown = e => { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') { e.stopPropagation(); finish(false); } };
  inp.onblur = () => finish(true);
}
async function renameSession(sid, name) {
  try { await api(`/api/sessions/${sid}`, { method: 'PATCH', json: { name } }); } catch (e) { toast(errText(e)); }
  if (S.sid === sid) await loadSession(sid);
  if (S.restore.sid === sid) await loadRestoreSession(sid);
}
VIEWS.anon = {
  render(el) {
    if (S.sub === 'preview' && S.pv) return renderPreview(el);
    crumbs(S.session ? [t('menu.anon'), sessTitle({ name: S.session.name, files: S.session.files })] : [t('menu.anon')]);
    const files = filesOf('anon');
    const nNew = files.filter(f => f.status === 'new').length;
    const running = S.run && S.run.section === 'anon';
    const btn = `<button class="btn primary" id="go-anon" ${!nNew || running ? 'disabled' : ''}>${ic('eye-off')}${esc(nNew && !running ? t('anon.go_n', { n: nNew }) : t('anon.go'))}</button>` +
      (!nNew || running ? `<span class="xs muted">${esc(t(running ? 'anon.running' : files.length ? 'anon.no_new' : 'anon.need_files'))}</span>` : '');
    const big = !files.length && !S.rejected.some(x => x.section === 'anon');
    el.innerHTML = `<div class="head"><div class="t"><h1>${esc(t('anon.title'))}</h1><div class="sub">${esc(t('anon.sub'))}</div></div><div class="act">${btn}</div></div>`
      + sessLine()
      + (big ? `<div class="drop big" id="drop-anon" tabindex="0" role="button">${ic('upload')}<div>${t('drop.big')}</div><div class="small">${esc(t('drop.formats'))}</div></div>`
        : `<div class="drop strip" id="drop-anon" tabindex="0" role="button">${ic('upload')}<span>${t('drop.strip')}</span></div>`)
      + progressCard('anon') + summaryCard('anon') + fileList('anon')
      + `<div id="table-sec">${tableSection()}</div>`;
    bindDrop($('drop-anon'), 'anon');
    const go_ = $('go-anon');
    if (go_) go_.onclick = () => startRun('anon');
    bindRun();
    bindSummary('anon');
    bindFileList('anon');
    bindTable();
    if (S.session) {
      $('sess-rename').onclick = () => renameInline($('sess-name'), S.session.name, v => renameSession(S.sid, v));
      $('sess-new').onclick = () => { loadSession(null).then(() => { S.rejected = S.rejected.filter(x => x.section !== 'anon'); S.lastStop = null; render(); }); };
    }
  },
};

// ── Таблица замен (описание 3.3) ─────────────────────────────────────────────
function groupMaps(list) {
  const by = new Map();
  for (const m of list) {
    if (!by.has(m.token)) by.set(m.token, { token: m.token, forms: [], type: m.entity_type, canonical: m.canonical_form, base: m.base_form, edited: m.canonical_edited, status: m.status, in_dict: m.in_dict });
    const g = by.get(m.token);
    if (!g.forms.includes(m.original_form)) g.forms.push(m.original_form);
    if (m.status === 'active') g.status = 'active';
  }
  const order = p => Object.keys(PREFIX_TYPE).indexOf(p);
  return [...by.values()].sort((a, b) => {
    const pa = prefixOf(a.token), pb = prefixOf(b.token);
    return (order(pa) - order(pb)) || (+a.token.split('_')[1] - +b.token.split('_')[1]);
  });
}
function tableRows() {
  const tb = S.table;
  const excl = tb.type === '__excluded';
  let groups = excl ? groupMaps(S.mapsAll.filter(m => m.status === 'excluded')).filter(g => !S.maps.some(m => m.token === g.token)) : groupMaps(S.maps);
  if (!excl && tb.type) groups = groups.filter(g => typeCode(g.type) === tb.type);
  const q = tb.q.trim().toLowerCase();
  if (q) groups = groups.filter(g => g.token.toLowerCase().includes(q) || g.forms.some(f => f.toLowerCase().includes(q)) || (g.canonical || '').toLowerCase().includes(q));
  if (!groups.length) {
    const msg = excl ? (tb.q ? t('table.nothing') : t('table.no_excluded')) : (S.maps.length ? t('table.nothing') : t('table.empty'));
    return `<tr><td colspan="5" class="muted">${esc(msg)}</td></tr>`;
  }
  return groups.map(g => {
    return `<tr data-token="${esc(g.token)}"><td>${tokHtml(g.token)}</td><td>${esc(g.forms[0])}${g.forms.length > 1 ? ` <span class="forms">· ${g.forms.slice(1).map(esc).join(' · ')}</span>` : ''}</td>
      <td class="hide-narrow">${esc(g.base || g.canonical || g.forms[0])}</td><td class="muted">${esc(typeLabel(g.type))}</td>
      <td><button class="ib" data-row-more="${esc(g.token)}" data-pop="1" title="${esc(t('file.more'))}" aria-label="${esc(t('file.more'))}">${ic('ellipsis')}</button></td></tr>`;
  }).join('');
}
function tableSection() {
  const n = groupMaps(S.maps).length;
  const tb = S.table;
  const opts = `<option value="">${esc(t('table.type_all'))}</option>` + TYPES.map(c => `<option value="${esc(c)}" ${tb.type === c ? 'selected' : ''}>${esc(typeLabel(c))}</option>`).join('')
    + `<option value="__excluded" ${tb.type === '__excluded' ? 'selected' : ''}>${esc(t('table.excluded'))}</option>`;
  return `<div class="sec"><div class="sec-h"><h2><button id="tbl-toggle" class="${tb.open ? '' : 'closed'}" aria-expanded="${tb.open}">${ic('chevron-down')}${esc(t('table.title', { n }))}</button></h2><span class="grow"></span>
    ${tb.open ? `<span class="search" style="width:250px">${ic('search')}<input class="field" id="tbl-q" placeholder="${esc(t('table.search'))}" aria-label="${esc(t('table.search'))}" value="${esc(tb.q)}"></span>
    <select class="field" id="tbl-type" aria-label="${esc(t('table.col_type'))}">${opts}</select>` : ''}
    <button class="btn" id="tbl-add">${ic('plus')}${esc(t('table.add'))}</button></div>
    ${tb.open ? `<div class="card" style="overflow:hidden"><table class="tbl"><thead><tr><th style="width:150px">${esc(t('table.col_token'))}</th><th>${esc(t('table.col_value'))}</th>
      <th class="hide-narrow" title="${esc(t('table.col_base_hint'))}">${esc(t('table.col_base'))} <span class="hint-i" title="${esc(t('table.col_base_hint'))}">?</span></th>
      <th style="width:170px">${esc(t('table.col_type'))}</th><th style="width:40px"></th></tr></thead><tbody id="tbl-body">${tableRows()}</tbody></table></div>` : ''}</div>`;
}
function bindTable() {
  const tg = $('tbl-toggle');
  if (!tg) return;
  tg.onclick = () => { S.table.open = !S.table.open; refreshTable(); };
  $('tbl-add').onclick = () => addRecord();
  const q = $('tbl-q');
  if (q) q.oninput = () => { S.table.q = q.value; $('tbl-body').innerHTML = tableRows(); bindRows(); };
  const ty = $('tbl-type');
  if (ty) ty.onchange = () => { S.table.type = ty.value; $('tbl-body').innerHTML = tableRows(); bindRows(); };
  bindRows();
}
function bindRows() {
  document.querySelectorAll('[data-row-more]').forEach(b => b.onclick = () => {
    const tok = b.dataset.rowMore;
    const excl = S.table.type === '__excluded';
    menuPop(b, excl ? [{ label: t('table.mask_again'), icon: 'eye-off', action: () => maskAgain(tok) }]
      : [{ label: t('table.edit'), icon: 'pencil', action: () => editRecord(tok) },
         { label: t('table.unmask'), icon: 'eye', action: () => unmask(tok) }]);
  });
}
function refreshTable() {
  const sec = $('table-sec');
  if (!sec) return;
  sec.innerHTML = tableSection();
  bindTable();
}

// ── Окно записи (описание 3.4) ───────────────────────────────────────────────
function guessType(v) {
  if (/@/.test(v)) return 'EMAIL';
  const d = v.replace(/\D/g, '');
  if (d.length >= 6 && d.length >= v.replace(/\s/g, '').length * 0.7) return d.length === 20 ? 'РС' : d.length === 9 ? 'БИК' : 'ИНН';
  if (/(^|\s)(г\.|ул\.|д\.|обл|район|р-н|пр-т|кв\.)/i.test(v)) return 'АДРЕС';
  if (/[«"]|^(ООО|АО|ПАО|ЗАО|ОАО|ИП)\s/.test(v)) return 'ЮЛ';
  return 'ФИО';
}
function recordDialog({ title, token = '', original = '', canonical = '', type = 'ФИО', showScope = true, beforeFiles = false }) {
  const opts = TYPES.map(c => `<option value="${esc(c)}" ${typeCode(type) === c ? 'selected' : ''}>${esc(typeLabel(c))}</option>`).join('');
  const body = (token ? `<p>${esc(t('rec.token'))} ${tokHtml(token)}</p>` : '')
    + `<label class="fl" for="rec-v">${esc(t('rec.value'))}</label><input class="field" id="rec-v" value="${esc(original)}"><div class="fh">${esc(t('rec.value_hint'))}</div>`
    + `<label class="fl" for="rec-c">${esc(t('rec.base'))}</label><input class="field" id="rec-c" value="${esc(canonical && canonical !== original ? canonical : '')}"><div class="fh">${esc(t('rec.base_hint'))}</div>`
    + `<label class="fl" for="rec-t">${esc(t('rec.type'))}</label><select class="field" id="rec-t">${opts}</select>`
    + (showScope ? `<label class="fl">${esc(t('rec.scope'))}</label><label class="radio"><input type="radio" name="rec-s" value="session" checked>${esc(t('rec.scope_session'))}</label>
       <label class="radio"><input type="radio" name="rec-s" value="always">${esc(t('rec.scope_always'))}</label>` : '')
    + (beforeFiles ? `<p class="muted small" style="margin-top:10px">${ic('info')} ${esc(t('rec.before_files'))}</p>` : '');
  return dialog({ title: esc(title), body, buttons: [{ label: t('rec.cancel'), value: null }, { label: t('rec.save'), value: 'save', kind: 'primary' }],
    read: dlg => ({ original_form: dlg.querySelector('#rec-v').value.trim(), canonical_form: dlg.querySelector('#rec-c').value.trim(),
      entity_type: dlg.querySelector('#rec-t').value, scope: (dlg.querySelector('input[name=rec-s]:checked') || {}).value || 'session' }),
    validate: (btn, d) => !d.original_form && !d.canonical_form ? t('err.empty') : null,
    init: dlg => { const v = dlg.querySelector('#rec-v'); v.focus(); v.select(); } }).then(r => r.btn === 'save' ? r.data : null);
}
async function saveRecord(url, method, data) {
  try { return await api(url, { method, json: data }); } catch (e) {
    if (e.status === 409 && e.data.duplicate) {
      const d = e.data.duplicate;
      const r = await dialog({ title: esc(t('dup.title')), body: `<p>${t('dup.text', { token: tokHtml(d.token), value: esc(d.value) })}</p>`,
        buttons: [{ label: t('common.cancel'), value: null }, { label: t('dup.separate'), value: 'separate' }, { label: t('dup.merge'), value: 'merge', kind: 'primary' }] });
      if (!r.btn) return null;
      return api(url, { method, json: { ...data, on_duplicate: r.btn } });
    }
    throw e;
  }
}
async function addRecord(prefill = null) {
  const doneFiles = S.files.some(f => f.section === 'anon' && f.status === 'done');
  const data = await recordDialog({ title: prefill ? t('rec.mask_title') : t('rec.add_title'), original: prefill || '', canonical: prefill || '',
    type: prefill ? guessType(prefill) : 'ФИО', beforeFiles: !doneFiles });
  if (!data) return null;
  try {
    const sid = await ensureSession();
    const r = await saveRecord(`/api/sessions/${sid}/mappings`, 'POST', data);
    if (!r) return null;
    toast(t(prefill ? 'pv.masked' : 'rec.added', { token: r.token }));
    await afterEdit();
    return r;
  } catch (e) { toast(errText(e)); return null; }
}
async function editRecord(token) {
  const g = groupMaps(S.maps).find(x => x.token === token);
  if (!g) return;
  const data = await recordDialog({ title: t('rec.edit_title'), token, original: g.forms[0], canonical: g.edited ? g.canonical : g.base, type: g.type, showScope: !!g.in_dict });
  if (!data) return;
  try {
    const r = await saveRecord(`/api/sessions/${S.sid}/mappings/${encodeURIComponent(token)}`, 'PATCH', data);
    if (!r) return;
    toast(t('rec.saved', { token: r.new_token || token }));
    await afterEdit();
  } catch (e) { toast(errText(e)); }
}
// «Не маскировать» - одно действие одним словом и одно окно (описание 3.5, было Р-21)
async function unmask(token) {
  const g = groupMaps(S.maps).find(x => x.token === token);
  if (!g) return;
  const r = await dialog({ title: esc(t('unmask.title', { value: g.forms[0] })),
    body: `<p>${t('unmask.text', { token: tokHtml(token) })}</p>
      <label class="radio"><input type="radio" name="um" value="session" checked>${esc(t('unmask.scope_session'))}</label>
      <label class="radio"><input type="radio" name="um" value="always">${esc(t('unmask.scope_always'))}</label>`,
    buttons: [{ label: t('common.cancel'), value: null }, { label: t('unmask.go'), value: 'go', kind: 'primary' }], hint: t('unmask.hint'),
    read: dlg => dlg.querySelector('input[name=um]:checked').value,
    init: dlg => dlg.querySelector('.btns .primary').focus() });
  if (r.btn !== 'go') return;
  try {
    await api(`/api/sessions/${S.sid}/mappings/${encodeURIComponent(token)}`, { method: 'DELETE', json: { scope: r.data } });
  } catch (e) { toast(errText(e)); return; }
  await afterEdit();
  toast(t('unmask.done', { value: g.forms[0] }), { action: { label: t('toast.undo'), fn: () => maskAgain(token, true) } });
}
async function maskAgain(token, quiet = false) {
  try { await api(`/api/sessions/${S.sid}/mappings/${encodeURIComponent(token)}/mask`, { json: {} }); } catch (e) { toast(errText(e)); return; }
  await afterEdit();
  if (!quiet) toast(t('table.masked_again', { token }));
}
// пересборка после любой правки - сама (было Р-19)
async function afterEdit() {
  await loadMaps();
  const need = S.files.some(f => f.section === 'anon' && f.status === 'done');
  if (need) {
    S.rebuild = true;
    if (S.view === 'anon' && !S.sub) render(); else renderStatus();
    try { await api(`/api/sessions/${S.sid}/reprocess`, { json: {} }); } catch (e) { toast(errText(e)); }
    S.rebuild = false;
  }
  await loadSession(S.sid);
  if (S.view === 'anon' && S.sub === 'preview' && S.pv) await reopenPreview();
  else render();
}
