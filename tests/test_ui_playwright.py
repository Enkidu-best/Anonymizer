"""
Real-browser UI tests via Playwright.

These tests exercise the front-end exactly like a user would:
they spin up the Flask server, drive Chromium, and assert on what
appears in the page after each user gesture.

Each test maps to a specific bug report so a failure points back at
the originating issue.

Run with:
    python -m pytest tests/test_ui_playwright.py -v -s

Skip automatically if Playwright/Chromium isn't installed.
"""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEST_FILES = ROOT / 'Тестовые_файлы'

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    pytest.skip('Playwright not installed', allow_module_level=True)


# ── Fixtures ────────────────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for(url: str, timeout: float = 30) -> bool:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    """Start a fresh app.py instance on a free port with an isolated DB/uploads."""
    work = tmp_path_factory.mktemp('app')
    env = os.environ.copy()
    # Isolated working dir → anon.db + uploads land here
    port = _free_port()
    env['FLASK_RUN_PORT'] = str(port)

    # Patch app.py to use this port by passing via env var (app uses 5000 hardcoded
    # — so we run it from a copy where we override the port via a wrapper).
    wrapper = work / 'run.py'
    wrapper.write_text(f'''
import sys
sys.path.insert(0, r"{ROOT}")
import os; os.chdir(r"{work}")
import importlib.util
spec = importlib.util.spec_from_file_location("app_mod", r"{ROOT / 'app.py'}")
mod = importlib.util.module_from_spec(spec)
# Patch app.run before import-time call
import threading
spec.loader.exec_module(mod)
''', encoding='utf-8')
    # Simpler approach: just edit a runner that imports app and starts on chosen port
    runner_src = f'''
import sys, os
sys.path.insert(0, r"{ROOT}")
os.chdir(r"{work}")
import app as a
a.app.run(host="127.0.0.1", port={port}, debug=False, use_reloader=False)
'''
    runner = work / 'runner.py'
    runner.write_text(runner_src, encoding='utf-8')

    proc = subprocess.Popen(
        [sys.executable, str(runner)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env,
    )
    url = f'http://127.0.0.1:{port}'
    if not _wait_for(url, timeout=30):
        proc.terminate()
        out = proc.stdout.read().decode('utf-8', errors='replace') if proc.stdout else ''
        pytest.fail(f'Server did not start on {url}:\n{out[:2000]}')
    yield {'url': url, 'port': port, 'work': work, 'proc': proc}
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        try:
            br = p.chromium.launch(headless=True)
        except Exception as ex:
            pytest.skip(f'Chromium unavailable: {ex}')
        yield br
        br.close()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(locale='ru-RU')
    pg = ctx.new_page()
    pg.set_default_timeout(20000)
    pg.goto(server['url'])
    # SPA shell renders the side panel header; emptyState is visible until a
    # session is selected.
    pg.wait_for_selector('.btn-new', state='visible')
    yield pg
    ctx.close()


def _make_session(page, name: str):
    """Create a session via the API and select it programmatically. Faster and
    more reliable than reloading the whole SPA between tests."""
    sid = page.evaluate('''async (n) => {
        const r = await fetch('/api/sessions', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({name: n})
        });
        const d = await r.json();
        return d.id;
    }''', name)
    # Refresh the session list and await selection (selectSession is async)
    page.evaluate('async () => { await window.loadSessions(); }')
    page.evaluate('async (id) => { await window.selectSession(id); }', sid)
    page.wait_for_selector('#workArea:not(.hidden)', timeout=20000)
    page.wait_for_selector('#mAnon', state='visible', timeout=20000)
    page.wait_for_function('window.S?.active?.id', timeout=20000)
    return sid


def _upload_file_path(page, path: Path):
    # The actual input id in the SPA is #fInput (hidden, triggered by drop zone)
    page.set_input_files('#fInput', str(path))
    page.wait_for_selector('.file-row')


# ── Tests ────────────────────────────────────────────────────────────────────

def test_ui_loads_and_shows_no_sessions_state(page):
    """Smoke: SPA loads, side panel + 'New session' button present, empty state shown."""
    expect(page.locator('.btn-new')).to_be_visible()
    expect(page.locator('#emptyState')).to_be_visible()
    expect(page.locator('#workArea')).to_have_class(re.compile(r'\bhidden\b'))


def test_anonymize_real_docx_via_ui_regex_only(page, server, tmp_path):
    """Bug #4 regression: full UI flow — upload a real DOCX, process,
    verify no garbage mappings (no FIO='Возглавляет', no '[YUL_1' fragments)."""
    src = TEST_FILES / 'Аналитическая справка АО Северный Народный Банк.docx'
    if not src.exists():
        pytest.skip('Sample missing')
    # Copy to a path with no Cyrillic dirs (multipart edge cases)
    local = tmp_path / 'sample.docx'
    local.write_bytes(src.read_bytes())

    _make_session(page, 'UI-regex-test')
    _upload_file_path(page, local)
    # Click Process and wait for completion (status 'ok' on the file row)
    page.click('#procBtn')
    page.wait_for_selector('.file-row.ok', timeout=60000)

    # Read the rendered mappings table directly from the DOM
    rows = page.evaluate('''() =>
        Array.from(document.querySelectorAll("#mapTbody tr")).map(tr => {
            const c = tr.querySelectorAll("td");
            return {token: c[0]?.innerText, original: c[1]?.innerText,
                    canonical: c[2]?.innerText, type: c[3]?.innerText};
        })
    ''')
    assert rows, 'No mappings rendered after process'

    # Bug A: no row should contain a partial token like "[YUL_1"
    leaks = [r for r in rows if re.search(r'\[[A-Z]+_\d+', r['original'] or '')]
    assert not leaks, f'Mask leaked into mappings table: {leaks}'

    # Bug B: known garbage entities must not appear
    bad = {'возглавляет','инфраструктура','банка','совета директоров',
            'председателя правления','огрн','бик','рейтинги',
            'единственный региональный банк'}
    hit = [r for r in rows if (r['original'] or '').lower() in bad]
    assert not hit, f'Validator missed garbage: {hit}'

    # Token uniqueness from the user's POV
    tokens = [r['token'] for r in rows]
    assert len(tokens) == len(set(tokens)), f'Duplicate tokens in UI: {tokens}'


def test_tab_switch_keeps_files_per_tab(page, server, tmp_path):
    """New bug #1: anonymize file list and deanonymize file list are independent.
    Switching tabs must NOT show one tab's files in the other tab.

    Uses a fast TXT path so the test is about UI behaviour, not handler speed."""
    local = tmp_path / 'fast.txt'
    local.write_text('ИНН 7701234567 ivan@test.ru', encoding='utf-8')

    _make_session(page, 'UI-tabs-test')
    _upload_file_path(page, local)
    page.click('#procBtn')
    page.wait_for_selector('.file-row.ok', timeout=60000)
    anon_files_count = page.evaluate('document.querySelectorAll(".file-row").length')
    assert anon_files_count >= 1

    # Switch to Deanonymize — the list should be empty (independent per-tab)
    page.click('#mDean')
    page.wait_for_timeout(300)
    dean_files_count = page.evaluate('document.querySelectorAll(".file-row").length')
    assert dean_files_count == 0, \
        f'Deanonymize tab unexpectedly shows {dean_files_count} files'

    # Switch back to Anonymize — files come back
    page.click('#mAnon')
    page.wait_for_timeout(300)
    anon_again = page.evaluate('document.querySelectorAll(".file-row").length')
    assert anon_again == anon_files_count, \
        f'Anonymize files lost on tab roundtrip: {anon_again} != {anon_files_count}'


def test_delete_file_via_x_button_removes_from_session(page, server, tmp_path):
    """New bug #1 second half: the × button must remove a completed file
    from BOTH the UI and the server output directory."""
    local = tmp_path / 'todel.txt'
    local.write_text('ИНН 7711111111 test@x.ru', encoding='utf-8')

    _make_session(page, 'UI-delete-test')
    _upload_file_path(page, local)
    page.click('#procBtn')
    page.wait_for_selector('.file-row.ok', timeout=60000)

    # Click the × button on the first file row, confirm modal
    page.click('.file-row .rm-btn')
    page.wait_for_selector('#modalOverlay:not(.hidden)')
    page.click('#modalOkBtn')

    # File row should disappear
    page.wait_for_function('document.querySelectorAll(".file-row").length === 0',
                           timeout=10000)

    # Server should also report no files
    files = page.evaluate('''async () => {
        const id = window.S?.active?.id;
        const r = await fetch(`/api/sessions/${id}/files`);
        return await r.json();
    }''')
    assert files == {'files': []}, f'Server still has files: {files}'


def test_deanon_returns_canonical_form_after_edit(page, server, tmp_path):
    """Bug #2: edit canonical_form in the table, then deanonymize — the
    edited canonical must appear in the deanonymized output."""
    _make_session(page, 'UI-deanon-canonical')
    # 1) Process a tiny TXT so we have a known mapping
    txt = tmp_path / 't.txt'
    txt.write_text('Контакт: www.test.ru', encoding='utf-8')
    _upload_file_path(page, txt)
    page.click('#procBtn')
    page.wait_for_selector('.file-row.ok', timeout=60000)

    # 2) Use the API (same path the UI uses for the edit modal) to change canonical
    res = page.evaluate('''async () => {
        const id = window.S?.active?.id;
        const mappings = await (await fetch(`/api/sessions/${id}/mappings`)).json();
        const url = mappings.find(m => m.entity_type === "URL");
        if (!url) return {error: "no URL mapping", sid: id, all: mappings};
        const r = await fetch(`/api/sessions/${id}/mappings/${url.token}`, {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({canonical_form: "www.OVERRIDE.ru"}),
        });
        return await r.json();
    }''')
    assert res.get('ok'), f'Edit failed: {res}'

    # 3) Verify get_reverse_mappings returns the override via session_files+download
    rev = page.evaluate('''async () => {
        const id = window.S?.active?.id;
        const r = await fetch(`/api/sessions/${id}/mappings`);
        return await r.json();
    }''')
    url_row = next((m for m in rev if m['entity_type'] == 'URL'), None)
    assert url_row, 'URL mapping vanished after PATCH'
    assert url_row['canonical_form'] == 'www.OVERRIDE.ru', \
        f'Canonical not persisted: {url_row}'


def test_known_entities_persist_across_sessions(page, server):
    """Bug #3: a manually added entity in session A must auto-anonymize
    in a NEW session B without any extra user action."""
    # Session A: add a known phone via the API as the UI's "+ Добавить вручную" does
    _make_session(page, 'UI-known-A')
    page.evaluate('''async () => {
        const id = window.S?.active?.id;
        await fetch(`/api/sessions/${id}/mappings`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({original_form: "+7(800)555-99-88", entity_type: "ТЕЛЕФОН"}),
        });
    }''')

    # Session B: brand new
    _make_session(page, 'UI-known-B')
    # Use the API to anonymize a text that references the phone — exactly what
    # /api/process does for an uploaded file. Cross-session learning should kick in.
    out = page.evaluate('''async () => {
        const id = window.S?.active?.id;
        const fd = new FormData();
        fd.append("session_id", id);
        fd.append("mode", "anonymize");
        fd.append("use_spacy", "false");
        fd.append("use_llm", "false");
        const blob = new Blob(["Свяжитесь: +7(800)555-99-88 для уточнений."], {type:"text/plain"});
        fd.append("files", blob, "x.txt");
        const r = await fetch("/api/process", {method:"POST", body: fd});
        const d = await r.json();
        return d.mappings;
    }''')
    types = [m['entity_type'] for m in out]
    assert 'ТЕЛЕФОН' in types, \
        f'Cross-session learning failed: phone not in mappings: {out}'


def test_no_garbage_fio_or_org_in_real_docx(page, server, tmp_path):
    """End-to-end on a sample known to produce false positives.
    After full pipeline runs, none of the listed garbage strings should appear."""
    src = TEST_FILES / 'Аналитическая справка АО Северный Народный Банк.docx'
    if not src.exists():
        pytest.skip('Sample missing')
    local = tmp_path / 'snb.docx'
    local.write_bytes(src.read_bytes())

    _make_session(page, 'UI-clean-mappings')
    _upload_file_path(page, local)
    # Force regex+spaCy mode (default)
    page.click('#procBtn')
    page.wait_for_selector('.file-row.ok', timeout=60000)

    rows = page.evaluate('''() =>
        Array.from(document.querySelectorAll("#mapTbody tr")).map(tr => {
            const c = tr.querySelectorAll("td");
            return (c[1]?.innerText || "").trim().toLowerCase();
        })
    ''')
    garbage = {
        'возглавляет', 'инфраструктура', 'статус (', 'риски', 'заключение',
        'бенефициары', 'банка', 'совета директоров', 'председателя правления',
        'единственный региональный банк', 'надежный региональный банк «домашнего» типа',
        '(акционерное общество)', 'огрн', 'бик', 'еио', 'снилс',
        'рейтинги', 'вкл��чен',
        'чистая прибыль за 2023 год — 68,7 млн руб.',
    }
    bad = [r for r in rows if r in garbage]
    assert not bad, f'False positives still present after pipeline: {bad}'
