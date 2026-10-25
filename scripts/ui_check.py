"""Проверка окна Anonymizer (описание 8, стандарт 1.4, 1.9, 4.4, 6.8): на каждом экране, в светлой и тёмной теме,
1440 и 900 px - контраст (общий модуль common/a11y, axe-core WCAG 2.1 AA), обрезанный текст, кнопки без действия;
алфавит RU / EN; одинаковые ключи словарей и ключи, на которые ссылается код окна.

Свой сервер на порту 8074 и своя временная папка данных; документы вымышленные; безголовый Chromium - браузер и окна
Андрея не открываются (правило 10).

    .venv312/bin/python scripts/ui_check.py            # всё
    .venv312/bin/python scripts/ui_check.py --shots D  # и снимки экранов в папку D
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.a11y import contrast  # noqa: E402

PORT = 8074
URL = f'http://127.0.0.1:{PORT}'
# латиница, допустимая в русском окне (стандарт 1.4): названия форматов, программ и моделей, клавиши, единицы
RU_LATIN_OK = {'Anonymizer', 'Word', 'PDF', 'Excel', 'PowerPoint', 'RTF', 'TXT', 'ZIP', 'URL', 'IBAN', 'SWIFT', 'VIN',
               'EMAIL', 'Ollama', 'Cmd', 'C', 'V', 'RU', 'EN', 'Aa', 'DOCX', 'XLSX', 'DOC', 'ODT', 'Mac', 'macOS',
               'LibreOffice', 'qwen', 'b', 'ollama', 'pull', 'docx', 'xlsx', 'pdf', 'txt', 'Application', 'Support', 'Logs', 'Enter', 'Esc', 'Chrome', 'N'}
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, ok, detail))
    print(f"{'OK ' if ok else 'НЕТ'} {name}{' - ' + detail if detail and not ok else ''}", flush=True)


# ── словари ──────────────────────────────────────────────────────────────────
def dict_checks() -> None:
    ru = json.loads((ROOT / 'static/i18n/ru.json').read_text(encoding='utf-8'))
    en = json.loads((ROOT / 'static/i18n/en.json').read_text(encoding='utf-8'))
    check('словари: одинаковые ключи RU и EN', set(ru) == set(en),
          f'только RU: {sorted(set(ru) - set(en))[:5]}, только EN: {sorted(set(en) - set(ru))[:5]}')
    bad_en = [k for k, v in en.items() if re.search('[А-Яа-яЁё]', re.sub(r'<[^>]+>', '', v))]
    check('словари: в EN нет кириллицы', not bad_en, ', '.join(bad_en[:5]))
    js = ''.join((ROOT / 'static/js' / f).read_text(encoding='utf-8') for f in os.listdir(ROOT / 'static/js'))
    keys = set(re.findall(r"\bt[n]?\('([a-z_]+\.[A-Za-z0-9_.]+)'", js)) | set(re.findall(r'data-i18n(?:-title)?="([a-z_.]+)"', (ROOT / 'static/index.html').read_text(encoding='utf-8')))
    missing = sorted(k for k in keys if k not in ru and not k.endswith(('_', '.')))   # «'llmrev.cat_' + …» - собирается в коде
    check('словари: все ключи окна есть в словаре', not missing, ', '.join(missing[:8]))
    bad_dash = [k for k, v in ru.items() if '—' in v or '–' in v]
    check('тексты: короткое тире « - » (4.5)', not bad_dash, ', '.join(bad_dash[:5]))
    bad_units = [k for k, v in ru.items() if re.search(r'\b(KB|MB)\b', v)]
    check('тексты: «КБ / МБ» (4.5)', not bad_units, ', '.join(bad_units))


# ── окно ─────────────────────────────────────────────────────────────────────
def start_server(work: Path):
    env = dict(os.environ, ANONYMIZER_DATA_DIR=str(work / 'data'), ANONYMIZER_LOG_DIR=str(work / 'logs'),
               ANONYMIZER_PORT=str(PORT), ANONYMIZER_NO_OPEN='1', HEAVY_LOCK_WHO='background')
    proc = subprocess.Popen([sys.executable, str(ROOT / 'app.py')], cwd=str(ROOT), env=env,
                            stdout=open(work / 'server.log', 'w'), stderr=subprocess.STDOUT)
    for _ in range(240):
        try:
            st = json.loads(urllib.request.urlopen(URL + '/api/status', timeout=1).read())
            if st['ner']['ready'] or st['ner']['error']:
                return proc
        except Exception:
            pass
        time.sleep(0.25)
    proc.terminate()
    raise SystemExit('сервер не запустился: ' + (work / 'server.log').read_text()[-1500:])


def api(path, data=None):
    req = urllib.request.Request(URL + path, data=json.dumps(data).encode() if data is not None else None,
                                 method='POST' if data is not None else 'GET', headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or b'{}')


def sample_docs(work: Path) -> list[Path]:
    from docx import Document
    sys.path.insert(0, str(ROOT / 'tests' / 'bench'))
    import ids
    ids.R.seed(4003)
    paras = ['ДОГОВОР ПОСТАВКИ № 17', f'ООО «Северный ветер», ИНН {ids.inn10()}, в лице генерального директора '
             'Петрова Ивана Сергеевича, и Сидорова Анна Викторовна заключили договор.',
             'Агент - ООО «Вектор Трейд». Перевозчик - ООО «Вектор Тренд».', 'Подписи: Петров И. С. / Сидорова А. В.']
    d = Document()
    for p in paras:
        d.add_paragraph(p)
    a = work / 'docs' / 'Договор поставки - Петров.docx'
    a.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(a))
    b = work / 'docs' / 'Письмо.txt'
    b.write_text('Ответственный - Петров Иван Сергеевич, телефон +7 900 123-45-67.', encoding='utf-8')
    return [a, b]


def data_values(sid: str) -> list[str]:
    """Значения из данных (имена файлов, сессий, формы записей, метки) - их алфавит не проверяется."""
    info, logs = api('/api/info'), api('/api/logs')
    vals = [s['name'] for s in api('/api/sessions')] + [info['data_dir'], logs['dir']]
    for f in api(f'/api/sessions/{sid}/files')['files']:
        vals += [f['input'], f.get('output') or '', f.get('extra') or '']
    for m in api(f'/api/sessions/{sid}/mappings?all=1'):
        vals += [m['original_form'], m['canonical_form'], m.get('base_form') or '', m['token']]
    for lst in api('/api/dictionary').values():
        if isinstance(lst, list):
            vals += [x['value'] for x in lst]
    return sorted({v for v in vals if v}, key=len, reverse=True)


SCREENS = [
    ('Обезличить - пусто', None),
    ('Обезличить - готово', 'go'),
    ('Окно «Не маскировать»', 'unmask'),
    ('Окно записи', 'record'),
    ('Просмотр', 'preview'),
    ('Вернуть данные - текст', 'restore_text'),
    ('Вернуть данные - файл', 'restore_file'),
    ('Просмотр возвращённого', 'restore_preview'),
    ('Сессии', 'sessions'),
    ('Окно удаления', 'delete'),
    ('Словарь', 'dict'),
    ('Настройки', 'settings'),
    ('Справка', 'help'),
]


def go(pg, step, state):
    if step is None:
        return
    js = {'go': "go('anon')", 'sessions': "go('sessions')", 'dict': "go('dict')", 'help': "go('help')"}
    if step in js:
        pg.evaluate(f"(async () => {{ if ('{step}' === 'go' && S.sid !== '{state['sid']}') await loadSession('{state['sid']}'); {js[step]}; }})()")
    elif step == 'unmask':
        pg.evaluate(f"(async () => {{ await loadSession('{state['sid']}'); go('anon'); unmask('{state['tok']}'); }})()")
    elif step == 'record':
        pg.evaluate(f"(async () => {{ await loadSession('{state['sid']}'); go('anon'); editRecord('{state['tok']}'); }})()")
    elif step == 'preview':
        pg.evaluate(f"(async () => {{ await loadSession('{state['sid']}'); go('anon'); openPreview(S.files.find(f => f.input.endsWith('.docx'))); }})()")
        pg.wait_for_selector('#pv-body .tok', timeout=20000)
    elif step == 'restore_text':
        pg.evaluate(f"(async () => {{ S.restore.mode = 'text'; S.restore.text = 'Направляем [FIO_1] ответ.\\nКопия - [FIO_9].'; await loadRestoreSession('{state['sid']}'); go('restore'); }})()")
        pg.wait_for_selector('#go-restore')
        pg.click('#go-restore')
        pg.wait_for_selector('#rs-result')
    elif step == 'restore_file':
        pg.evaluate(f"(async () => {{ S.restore.mode = 'file'; await loadRestoreSession('{state['sid']}'); go('restore'); }})()")
        pg.wait_for_selector('#files-restore .row')
    elif step == 'restore_preview':
        pg.evaluate(f"(async () => {{ S.restore.mode = 'file'; await loadRestoreSession('{state['sid']}'); go('restore'); openPreview(S.restore.files.find(f => f.section === 'restore')); }})()")
        pg.wait_for_selector('#pv-body .rv', timeout=20000)
    elif step == 'delete':
        pg.evaluate("go('sessions')")
        pg.wait_for_selector('#s-list [data-del]')
        pg.click('#s-list [data-del]')
    elif step == 'settings':
        pg.evaluate("S.setOpen = {more: true, service: true}; go('settings')")
    pg.wait_for_timeout(700)


def screen_checks(pg, title, theme, width, values):
    where = f'{title} · {theme} · {width}'
    bad = contrast.check(pg)
    check(f'контраст: {where}', not bad, contrast.summary(bad) if bad else '')
    cut = pg.evaluate("""[...document.querySelectorAll('body *')].filter(e => e.offsetParent && e.children.length === 0
        && e.textContent.trim() && !e.closest('.sheet') && getComputedStyle(e).overflow !== 'visible'
        && e.scrollWidth > e.clientWidth + 1 && e.tagName !== 'TEXTAREA').map(e => e.textContent.trim().slice(0, 40))""")
    sel = pg.evaluate("[...document.querySelectorAll('select')].filter(s => s.offsetParent && s.scrollWidth > s.clientWidth + 1).map(s => s.id || s.dataset.sel)")
    check(f'обрезанный текст: {where}', not cut and not sel, f'{cut[:4]} {sel}')
    dead = pg.evaluate("""[...document.querySelectorAll('button')].filter(b => b.offsetParent && !b.disabled && !b.onclick
        && !b.closest('.hl-dialog') && b.type !== 'submit').map(b => b.textContent.trim() || b.title || b.id)""")
    check(f'кнопки с действием: {where}', not dead, ', '.join(dead[:5]))


def alphabet(pg, lang, values, where):
    txt = pg.evaluate("""() => { let t = document.body.innerText;          // как видит человек: блоки - с новой строки
        for (const e of document.querySelectorAll('.sheet, .result')) if (e.innerText) t = t.split(e.innerText).join(' ');
        return t; }""")
    attrs = pg.evaluate("[...document.querySelectorAll('[title], [placeholder], [aria-label]')].filter(e => e.offsetParent).map(e => [e.title, e.placeholder, e.getAttribute('aria-label')].filter(Boolean).join(' ')).join(' ')")
    txt = txt + ' ' + attrs
    for v in values:
        txt = txt.replace(v, ' ')
    txt = re.sub(r'\[[A-Z]+_\d+\]', ' ', txt)
    if lang == 'en':
        bad = re.findall('[А-Яа-яЁё]+', txt)
        check(f'EN: нет кириллицы - {where}', not bad, ' '.join(sorted(set(bad))[:8]))
    else:
        words = set(re.findall(r'[A-Za-z]+', txt))
        bad = sorted(w for w in words if w not in RU_LATIN_OK)
        check(f'RU: латиница только из белого списка - {where}', not bad, ' '.join(bad[:8]))


def main() -> int:
    shots = Path(sys.argv[sys.argv.index('--shots') + 1]) if '--shots' in sys.argv else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    dict_checks()
    work = Path(tempfile.mkdtemp(prefix='anon_ui_check_'))
    proc = start_server(work)
    try:
        from playwright.sync_api import sync_playwright
        state = prepare(work, sample_docs(work))
        with sync_playwright() as p:
            br = p.chromium.launch()
            errors = []
            for theme in ('light', 'dark'):
                for width in (1440, 900):
                    ctx = br.new_context(viewport={'width': width, 'height': 900}, locale='ru-RU', color_scheme=theme)
                    pg = ctx.new_page()
                    pg.on('pageerror', lambda e: errors.append(str(e)))
                    pg.goto(URL)
                    pg.wait_for_function("(e => e && e.textContent !== '…')(document.querySelector('#model .ms-t'))")
                    for title, step in SCREENS:
                        pg.keyboard.press('Escape')
                        go(pg, step, state)
                        screen_checks(pg, title, theme, width, state['values'])
                        if theme == 'light' and width == 1440:
                            alphabet(pg, 'ru', state['values'], title)
                        if shots:
                            pg.screenshot(path=str(shots / f'{title} - {width} - {"светлая" if theme == "light" else "тёмная"}.png'))
                    ctx.close()
            ctx = br.new_context(viewport={'width': 1440, 'height': 900}, locale='en-US')
            pg = ctx.new_page()
            pg.on('pageerror', lambda e: errors.append(str(e)))
            pg.goto(URL)
            pg.wait_for_function("(e => e && e.textContent !== '…')(document.querySelector('#model .ms-t'))")
            pg.evaluate("setLang('en')")
            pg.wait_for_timeout(500)
            for title, step in SCREENS:
                pg.keyboard.press('Escape')
                go(pg, step, state)
                alphabet(pg, 'en', state['values'], title)
            ctx.close()
            br.close()
            check('нет ошибок JavaScript', not errors, '; '.join(errors[:3]))
    finally:
        proc.terminate()
        proc.wait(10)
    ok = sum(r[1] for r in results)
    print(f'\nИтого: {ok} из {len(results)}')
    return 0 if ok == len(results) else 1


def prepare(work: Path, docs: list[Path]) -> dict:
    """Сессия с двумя обезличенными файлами, ответом нейросети в «Вернуть данные» и записью в Словаре."""
    import io
    sid = api('/api/sessions', {})['id']

    def upload(path: Path, section: str, name=None):
        boundary = '----anon' + str(time.time()).replace('.', '')
        body = io.BytesIO()
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="section"\r\n\r\n{section}\r\n'.encode())
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name or path.name}"\r\n'
                   'Content-Type: application/octet-stream\r\n\r\n'.encode())
        body.write(path.read_bytes())
        body.write(f'\r\n--{boundary}--\r\n'.encode())
        req = urllib.request.Request(f'{URL}/api/sessions/{sid}/files', data=body.getvalue(), method='POST',
                                     headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        return json.loads(urllib.request.urlopen(req).read())

    for d in docs:
        e = upload(d, 'anon')
        api(f'/api/sessions/{sid}/process', {'input': e['input'], 'section': 'anon'})
    out = next(f['output'] for f in api(f'/api/sessions/{sid}/files')['files'] if f['output'].endswith('.docx'))
    reply = work / 'reply.docx'
    reply.write_bytes(urllib.request.urlopen(f'{URL}/api/download/{sid}/{urllib.parse.quote(out)}').read())
    e = upload(reply, 'restore', 'Ответ нейросети.docx')
    api(f'/api/sessions/{sid}/process', {'input': e['input'], 'section': 'restore'})
    api('/api/dictionary', {'value': 'Зелёная долина', 'entity_type': 'ЮЛ', 'list': 'always'})
    tok = next(m['token'] for m in api(f'/api/sessions/{sid}/mappings') if m['entity_type'] == 'ФИО')
    return {'sid': sid, 'tok': tok, 'values': data_values(sid)}


if __name__ == '__main__':
    t0 = time.time()
    code = main()
    print(f'за {time.time() - t0:.0f} с')
    sys.exit(code)
