"""
Anonymizer — Flask backend + native window.

    python app.py             native window (pywebview), falls back to the browser
    python app.py --browser   open in the default browser
    python app.py --server    server only (prints the URL; used by tests and checks)
"""

import io
import json
import re
import os
import sys
import shutil
import threading
import time
import webbrowser
import zipfile
from pathlib import Path

from core.version import __version__ as APP_VERSION

from flask import Flask, request, jsonify, send_file, send_from_directory

# ── Path resolution (works both in development and PyInstaller bundle) ────────
def _user_data_dir() -> Path:
    """Per-user writable folder; never next to the program (a .app bundle is read-only)."""
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Anonymizer'
    if sys.platform == 'win32':
        return Path(os.environ.get('APPDATA', Path.home())) / 'Anonymizer'
    return Path.home() / '.local' / 'share' / 'Anonymizer'


if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    DATA_DIR   = _user_data_dir()
else:
    BUNDLE_DIR = Path(__file__).parent
    DATA_DIR   = Path(__file__).parent

# Override for tests / custom installs
if os.environ.get('ANONYMIZER_DATA_DIR'):
    DATA_DIR = Path(os.environ['ANONYMIZER_DATA_DIR'])
    DATA_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR  = BUNDLE_DIR / 'static'
UPLOADS_DIR = DATA_DIR   / 'uploads'
DB_PATH     = DATA_DIR   / 'anon.db'

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
UPLOAD_RETENTION_DAYS = int(os.environ.get('ANONYMIZER_RETENTION_DAYS', '30'))


def _purge_old_uploads():
    """Originals are personal data: delete session upload folders older than N days."""
    import time
    limit = time.time() - UPLOAD_RETENTION_DAYS * 86400
    for d in UPLOADS_DIR.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < limit:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


_purge_old_uploads()

# ── DB init ───────────────────────────────────────────────────────────────────
from core.db import init_db
init_db(DB_PATH)

# ── Journal (no personal data) ───────────────────────────────────────────────
from core import log as journal
journal.setup(journal.default_dir(getattr(sys, 'frozen', False), Path(__file__).parent))
from core import settings as user_settings
user_settings.configure(DATA_DIR)


def _startup_journal():
    info = {}
    try:
        import subprocess as _sp
        from core.llm import check_ollama
        st = check_ollama()
        info = {'ollama': st.get('available'), 'models': st.get('models')}
        v = _sp.run(['ollama', '--version'], capture_output=True, text=True, timeout=5)
        info['ollama_version'] = (v.stdout or v.stderr).strip().split()[-1] if v.returncode == 0 else None
        ps = _sp.run(['ollama', 'ps'], capture_output=True, text=True, timeout=5)
        info['ollama_ps'] = [' '.join(l.split()) for l in ps.stdout.splitlines()[1:]]
    except Exception:
        pass
    journal.startup_info(APP_VERSION, info)


threading.Thread(target=_startup_journal, daemon=True).start()

# ── Start NER loading in background ──────────────────────────────────────────
from core.anonymizer import start_ner_loading
start_ner_loading()

# ── Warm Ollama (if available) so first LLM request isn't cold ───────────────
try:
    from core.llm import preload_model_async
    preload_model_async()
except Exception as _ex:
    print(f'[LLM] preload skipped: {_ex}')

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC_DIR))
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
PORT = None   # set at launch


@app.before_request
def _local_only():
    """Only the app's own page may talk to the server: a web page opened in any
    browser could otherwise POST to 127.0.0.1 (delete sessions, read mappings).
    Host check also defeats DNS rebinding."""
    host = (request.host or '').split(':')[0]
    if host not in ('127.0.0.1', 'localhost'):
        return jsonify({'error': 'forbidden host'}), 403
    origin = request.headers.get('Origin')
    if origin and origin != 'null':
        from urllib.parse import urlparse
        o = urlparse(origin)
        if o.hostname not in ('127.0.0.1', 'localhost') or (PORT and o.port != PORT):
            return jsonify({'error': 'forbidden origin'}), 403
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        site = request.headers.get('Sec-Fetch-Site')
        if site and site not in ('same-origin', 'none'):
            return jsonify({'error': 'cross-site request'}), 403


# ── Static files & SPA ───────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'index.html')


# ── Status ────────────────────────────────────────────────────────────────────
@app.route('/api/status')
def status():
    from core.anonymizer import get_ner_status
    s = get_ner_status()
    s['version'] = APP_VERSION
    return jsonify(s)


@app.route('/api/debug')
def debug():
    import platform
    from core.anonymizer import get_ner_status

    ner = get_ner_status()

    spacy_ok  = False
    spacy_ver = 'unknown'
    model_installed = False
    try:
        import spacy
        spacy_ver = spacy.__version__
        spacy_ok = True
        model_installed = spacy.util.is_package('ru_core_news_lg')
    except Exception as e:
        spacy_ver = f'IMPORT ERROR: {e}'

    info = {
        'python_version':  sys.version,
        'platform':        platform.platform(),
        'executable':      sys.executable,
        'spacy_version':   spacy_ver,
        'spacy_import':    spacy_ok,
        'model_installed': model_installed,
        'ner_status':      ner,
    }
    lines = [f'{k}: {v}' for k, v in info.items()]
    return '<pre>' + '\n\n'.join(lines) + '</pre>'


@app.route('/api/ner-retry', methods=['POST'])
def ner_retry():
    from core.anonymizer import retry_ner_loading
    retry_ner_loading()
    return jsonify({'ok': True})


# ── LLM (Ollama) ──────────────────────────────────────────────────────────────
@app.route('/api/llm-status')
def llm_status():
    from core.llm import check_ollama
    return jsonify(check_ollama())


@app.route('/api/llm-progress')
def llm_progress():
    from core.llm import get_llm_progress
    return jsonify(get_llm_progress())


@app.route('/api/llm-cancel', methods=['POST'])
def llm_cancel():
    from core.llm import cancel_llm
    cancel_llm()
    return jsonify({'ok': True})


@app.route('/api/llm-start', methods=['POST'])
def llm_start():
    from core.llm import try_start_ollama, check_ollama
    ok = try_start_ollama()
    if ok:
        return jsonify(check_ollama())
    return jsonify({'available': False, 'models': [], 'model': None,
                    'error': 'Ollama не найден или не удалось запустить'}), 200


@app.route('/api/llm-model', methods=['POST'])
def llm_set_model():
    from core.llm import set_model
    data = request.get_json(force=True, silent=True) or {}
    set_model(data.get('model', ''))
    return jsonify({'ok': True})


# ── Sessions ──────────────────────────────────────────────────────────────────
@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    from core.db import get_all_sessions
    out = get_all_sessions(DB_PATH)
    for x in out:             # files in the session folder — for «Удалить выбранные»
        d = UPLOADS_DIR / x['id'] / 'output'
        x['file_count'] = sum(1 for f in d.iterdir() if f.is_file()) if d.exists() else 0
    return jsonify(out)


@app.route('/api/sessions', methods=['POST'])
def create_session_route():
    from core.db import create_session
    import uuid
    data = request.get_json(force=True, silent=True) or {}
    name = data.get('name', '').strip() or f'Сессия {uuid.uuid4().hex[:6].upper()}'
    sid  = create_session(DB_PATH, name)
    journal.event('session_create', session=sid[:8])
    return jsonify({'id': sid, 'name': name}), 201


@app.route('/api/sessions/<sid>', methods=['DELETE'])
def delete_session(sid):
    from core.db import delete_session as db_delete_session
    db_delete_session(DB_PATH, sid)
    journal.event('session_delete', session=sid[:8])
    session_dir = UPLOADS_DIR / sid
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
    return jsonify({'ok': True})


@app.route('/api/sessions/<sid>/mappings', methods=['GET'])
def session_mappings(sid):
    from core.db import get_session_mappings
    # ?all=1 adds the archive: entries marked «не маскировать» or replaced (kept for old files)
    return jsonify(get_session_mappings(DB_PATH, sid, include_inactive=request.args.get('all') == '1'))


@app.route('/api/sessions/<sid>/mappings/<token>', methods=['DELETE'])
def delete_mapping_route(sid, token):
    from core.db import delete_mapping, get_session_mappings, add_exclusion
    mappings = get_session_mappings(DB_PATH, sid)
    m = next((x for x in mappings if x['token'] == token), None)
    # every form of this entity («Иванов», «Иванову П.С.») is excluded, not only the first row
    for x in mappings:
        if x['token'] == token:
            add_exclusion(DB_PATH, sid, x['original_form'], x['entity_type'])
    delete_mapping(DB_PATH, sid, token)    # marks «не маскировать», values kept for old files
    from core import log
    log.event('mapping_edit', token=token, action='exclude')
    return jsonify({'ok': True})


@app.route('/api/sessions/<sid>/mappings/<token>', methods=['PATCH'])
def update_mapping_route(sid, token):
    from core.db import (get_session_mappings, delete_mapping, add_exclusion,
                          get_or_create_token, remember_entity)
    data = request.get_json(force=True, silent=True) or {}
    mappings = get_session_mappings(DB_PATH, sid)
    old = next((x for x in mappings if x['token'] == token), None)
    if not old:
        return jsonify({'error': 'Token not found'}), 404
    new_original  = (data.get('original_form',  old['original_form'])).strip()
    new_canonical = (data.get('canonical_form', old['canonical_form'])).strip()
    new_type      = (data.get('entity_type',    old['entity_type'])).strip()
    from core.db import update_mapping, retire_mapping
    from core import log
    choice = data.get('on_duplicate')       # None → ask; 'merge' / 'separate'
    if (new_original != old['original_form'] or new_type != old['entity_type']) and not choice:
        dup = _duplicate_of(sid, new_original, new_type, exclude=token)
        if dup:
            return jsonify({'duplicate': dup}), 409
    if new_original == old['original_form'] and new_type == old['entity_type']:
        # only the base form changed: same token, nothing to re-detect
        if new_canonical != old['canonical_form']:
            update_mapping(DB_PATH, sid, token, {'canonical_form': new_canonical})
        log.event('mapping_edit', token=token, action='canonical')
        remember_entity(DB_PATH, new_original, new_type)
        return jsonify({'ok': True, 'new_token': token})
    # value or type changed: the old token is retired (kept to restore earlier files)
    if new_original != old['original_form']:
        add_exclusion(DB_PATH, sid, old['original_form'], old['entity_type'])
    retire_mapping(DB_PATH, sid, token)
    edited = bool(old.get('canonical_edited')) or new_canonical != old['canonical_form']
    new_token = get_or_create_token(DB_PATH, sid, new_original, new_canonical, new_type,
                                    canonical_edited=edited, separate=choice == 'separate')
    log.event('mapping_edit', token=token, action='replace', new_token=new_token)
    # User explicitly confirmed this entity — remember globally for future sessions
    remember_entity(DB_PATH, new_original, new_type)
    return jsonify({'ok': True, 'new_token': new_token})


@app.route('/api/sessions/<sid>/mappings', methods=['POST'])
def add_mapping_route(sid):
    from core.db import get_or_create_token, remember_entity
    data = request.get_json(force=True, silent=True) or {}
    original    = (data.get('original_form')  or '').strip()
    canonical   = (data.get('canonical_form') or '').strip()
    entity_type = (data.get('entity_type')    or '').strip()
    if not entity_type:
        return jsonify({'error': 'entity_type обязателен'}), 400
    edited = bool(canonical) and bool(original) and canonical != original
    if not canonical:
        canonical = original
    if not original:
        original = canonical
    if not original:
        return jsonify({'error': 'original_form или canonical_form обязательны'}), 400
    choice = data.get('on_duplicate')       # None → ask; 'merge' / 'separate'
    if not choice:
        dup = _duplicate_of(sid, original, entity_type)
        if dup:
            return jsonify({'duplicate': dup}), 409
    token = get_or_create_token(DB_PATH, sid, original, canonical, entity_type,
                                canonical_edited=edited, manual=True, separate=choice == 'separate')
    # Manually added → strong signal this is real PII. Cross-session learn.
    remember_entity(DB_PATH, original, entity_type)
    return jsonify({'token': token, 'original_form': original,
                    'canonical_form': canonical, 'entity_type': entity_type}), 201




def _duplicate_of(sid, original, entity_type, exclude=None):
    """«Такое значение уже есть: [ADR_2] …» (task 3, §1.2): the active entry this value
    already belongs to by the automatic rules, or None."""
    from core.db import find_same_entity, get_session_mappings
    tok = find_same_entity(DB_PATH, sid, original, entity_type)
    if not tok or tok == exclude:
        return None
    forms = [m for m in get_session_mappings(DB_PATH, sid) if m['token'] == tok]
    if not forms:
        return None
    return {'token': tok, 'value': forms[0]['canonical_form'] or forms[0]['original_form'],
            'entity_type': forms[0]['entity_type']}


@app.route('/api/settings', methods=['GET', 'POST'])
def settings_route():
    """«Настройки»: restore of company names (quotes, legal form), yellow highlight."""
    if request.method == 'POST':
        return jsonify(user_settings.update(request.get_json(force=True, silent=True) or {}))
    return jsonify(user_settings.get())


@app.route('/api/sessions/<sid>/duplicates', methods=['GET'])
def session_duplicates(sid):
    """«Проверка дублей»: pairs proposed to a human (never numbers), minus «Это разные»."""
    from core.db import get_session_mappings, get_distinct_pairs
    from core.entities import review_pairs
    return jsonify(review_pairs(get_session_mappings(DB_PATH, sid), get_distinct_pairs(DB_PATH, sid)))


@app.route('/api/sessions/<sid>/distinct', methods=['POST'])
def mark_distinct_route(sid):
    """«Это разные»: the pair is remembered and not proposed or merged again."""
    from core.db import mark_distinct
    data = request.get_json(force=True, silent=True) or {}
    pairs = data.get('pairs') or [[data.get('a'), data.get('b')]]
    for a, b in pairs:
        if a and b and a != b:
            mark_distinct(DB_PATH, sid, a, b)
            journal.event('mapping_edit', token=a, action='distinct', other=b)
    return jsonify({'ok': True})


def _manifest(session_dir: Path) -> dict:
    try:
        return json.loads((session_dir / 'manifest.json').read_text(encoding='utf-8'))
    except Exception:
        return {}


def _remember_output(session_dir: Path, input_name: str, mode: str, output_name: str):
    """input file → output file per mode (output names are anonymized, not derived from input)."""
    m = _manifest(session_dir)
    m.setdefault(input_name, {})[mode] = output_name
    (session_dir / 'manifest.json').write_text(json.dumps(m, ensure_ascii=False), encoding='utf-8')


# ── Reprocess after manual edits ──────────────────────────────────────────────
@app.route('/api/sessions/<sid>/reprocess', methods=['POST'])
def reprocess_session(sid):
    from core.handlers import process_uploaded_file

    session_dir = UPLOADS_DIR / sid
    input_dir   = session_dir / 'input'
    output_dir  = session_dir / 'output'

    if not input_dir.exists():
        return jsonify({'error': 'Оригинальные файлы не найдены'}), 404

    results = []
    man = _manifest(session_dir)
    for input_file in input_dir.iterdir():
        if man.get(input_file.name) and 'anonymize' not in man[input_file.name]:
            continue   # uploaded for deanonymization — nothing to re-anonymize
        try:
            # drop the previous output of this input: its name may change after edits
            prev = _manifest(session_dir).get(input_file.name, {}).get('anonymize')
            if prev and (output_dir / prev).exists():
                (output_dir / prev).unlink()
            with journal.Job('reprocess', session=sid[:8], ext=input_file.suffix.lower()):
                r = process_uploaded_file(
                    input_path=input_file,
                    output_dir=output_dir,
                    session_id=sid,
                    db_path=DB_PATH,
                    mode='anonymize',
                )
            _remember_output(session_dir, input_file.name, 'anonymize', r['output_filename'])
            results.append({'filename': input_file.name, 'status': 'ok',
                            'output': r['output_filename'], 'leaks': r.get('leaks')})
        except Exception as ex:
            results.append({'filename': input_file.name, 'status': 'error', 'error': str(ex)})

    return jsonify({'results': results})


# ── User patterns ─────────────────────────────────────────────────────────────
@app.route('/api/patterns', methods=['GET', 'POST'])
def patterns():
    from core.db import get_top_patterns, save_user_pattern
    if request.method == 'GET':
        return jsonify(get_top_patterns(DB_PATH))
    data = request.get_json(force=True, silent=True) or {}
    save_user_pattern(DB_PATH, data.get('pattern', ''), data.get('entity_type', 'FIO'))
    return jsonify({'ok': True}), 201


@app.route('/api/patterns/<int:pid>', methods=['DELETE'])
def delete_pattern(pid):
    from core.db import delete_user_pattern
    delete_user_pattern(DB_PATH, pid)
    return jsonify({'ok': True})


# ── Process ───────────────────────────────────────────────────────────────────
@app.route('/api/process', methods=['POST'])
def process():
    from core.handlers import process_uploaded_file
    from core.db       import get_session_mappings

    mode       = request.form.get('mode', 'anonymize')
    sid        = request.form.get('session_id', '').strip()
    use_spacy  = request.form.get('use_spacy',  'true').lower() != 'false'
    use_llm    = request.form.get('use_llm',    'false').lower() == 'true'
    # engine: fast = rules + spaCy; accurate = fast + LLM review in the background;
    # llm_only = only the LLM (to judge its quality). Old clients send use_spacy/use_llm.
    engine     = request.form.get('engine') or ('accurate' if use_llm else 'fast')
    llm_only   = engine == 'llm_only'
    background_review = engine == 'accurate' and mode == 'anonymize'
    if background_review:
        use_llm = False            # the file is ready after the rules; the LLM reviews in the background
    if not sid:
        return jsonify({'error': 'session_id не передан'}), 400

    files = request.files.getlist('files')
    if not files or all(not f.filename for f in files):
        return jsonify({'error': 'Файлы не переданы'}), 400

    session_dir = UPLOADS_DIR / sid
    in_dir  = session_dir / 'input'
    out_dir = session_dir / 'output'
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for n, f in enumerate(files, 1):
        if not f.filename:
            continue
        from core.handlers import safe_upload_name
        src = in_dir / safe_upload_name(f.filename)
        f.save(str(src))
        try:
            # file number in the request, type and size only — the name may contain a company name
            with journal.Job(mode, session=sid[:8], file_no=n, ext=src.suffix.lower(),
                             size_kb=round(src.stat().st_size / 1024), spacy=use_spacy, llm=use_llm,
                             engine=engine if mode == 'anonymize' else None) as job:
                r = process_uploaded_file(
                    input_path=src,
                    output_dir=out_dir,
                    session_id=sid,
                    db_path=DB_PATH,
                    mode=mode,
                    use_spacy=use_spacy and not llm_only,
                    use_llm=use_llm or llm_only,
                    llm_only=llm_only,
                )
                r['job'] = job.id
            if background_review:
                from core.handlers import IMAGE_EXT
                if src.suffix.lower() in IMAGE_EXT:
                    r['note'] = 'Для изображений проверка нейросетью не выполняется: закрашено по распознанному тексту'
                else:
                    r['llm_job'] = _start_review(sid, src, out_dir / r['output_filename'])
            out_name = r['output_filename']
            _remember_output(session_dir, src.name, mode, out_name)
            if r.get('extra_output'):
                _remember_output(session_dir, src.name, mode + '_pdf', r['extra_output'])
            results.append({'filename': f.filename,
                             'output':   out_name,
                             'extra_output': r.get('extra_output'),
                             'converted_from': r.get('converted_from'),
                             'status':   'ok',
                             'entities_found': r.get('entities_found', 0),
                             'leaks': r.get('leaks'),
                             'restore': r.get('restore'),
                             'job': r.get('job'),
                             'llm_job': r.get('llm_job'),
                             'apply_errors': r.get('apply_errors'),
                             'note': r.get('note')})
        except Exception as ex:
            journal.error('process_failed', ex, file_no=n)
            results.append({'filename': f.filename,
                             'status':  'error',
                             'error':   str(ex)})

    _auto_name_session(sid, [r['filename'] for r in results if r.get('status') == 'ok'])
    return jsonify({
        'results':    results,
        'session_id': sid,
        'mappings':   get_session_mappings(DB_PATH, sid),
    })


# ── Background LLM review («Точно») ───────────────────────────────────────────
REVIEWS = {}     # job id → state; in memory, a review lives as long as the app runs


def _start_review(sid, src: Path, out: Path) -> str:
    """The file is already downloadable; the local LLM reviews the found values and the
    suspicious lines in a thread and proposes additions / removals."""
    import uuid as _uuid
    jid = _uuid.uuid4().hex[:12]
    REVIEWS[jid] = {'id': jid, 'status': 'running', 'session': sid, 'input': src.name,
                    'output': out.name, 'started': time.time(), 'updated': time.time()}

    def work():
        from core.extract import extract_text
        from core.db import get_session_mappings
        from core.handlers import CONVERT_EXT, _convert_to_docx
        from core.llm_review import review
        st = REVIEWS[jid]
        try:
          with journal.Job('llm_review', session=sid[:8], ext=src.suffix.lower()):
            orig_path = src
            if src.suffix.lower() in CONVERT_EXT:
                import tempfile
                orig_path = _convert_to_docx(src, Path(tempfile.mkdtemp()))
            original = extract_text(orig_path, meta=False)
            masked = extract_text(out, meta=False)
            st['proposal'] = review(original, masked, get_session_mappings(DB_PATH, sid))
            st['status'] = 'done'
        except Exception as ex:
            journal.error('llm_review_failed', ex)
            st['status'] = 'error'
            st['error'] = 'Нейросеть недоступна или не ответила' if 'urlopen' in repr(ex) or 'Connection' in repr(ex) \
                else str(ex)[:200]
        st['updated'] = time.time()

    threading.Thread(target=work, daemon=True).start()
    return jid


@app.route('/api/llm-jobs/<jid>')
def llm_job_status(jid):
    from core.db import get_session_mappings
    st = REVIEWS.get(jid)
    if not st:
        return jsonify({'error': 'Задача не найдена (приложение перезапускалось?)', 'status': 'lost'}), 404
    out = {k: v for k, v in st.items() if k != 'proposal'}
    if st['status'] == 'running' and time.time() - st['started'] > 240:
        out['status'] = 'stale'
    out['elapsed'] = round(time.time() - st['started'], 1)
    p = st.get('proposal')
    if p:
        vals = {m['token']: m for m in get_session_mappings(DB_PATH, st['session'])}
        out['proposal'] = {
            'add': p['add'],
            'remove': [dict(x, value=vals.get(x['token'], {}).get('original_form', '')) for x in p['remove']
                       if x['token'] in vals],
            'merge': [dict(x, value=vals.get(x['token'], {}).get('original_form', ''),
                           into_value=vals.get(x['into'], {}).get('original_form', ''))
                      for x in p['merge'] if x['token'] in vals and x['into'] in vals],
            'seconds': p.get('seconds'), 'checked': p.get('checked')}
    return jsonify(out)


@app.route('/api/llm-jobs/<jid>/apply', methods=['POST'])
def llm_job_apply(jid):
    """Apply the proposals the user ticked, then rebuild the file."""
    from core.db import (get_session_mappings, add_exclusion, delete_mapping, get_or_create_token,
                         add_alias, retire_mapping)
    from core.handlers import process_uploaded_file
    st = REVIEWS.get(jid)
    if not st or st.get('status') != 'done':
        return jsonify({'error': 'Нет готового предложения'}), 400
    data = request.get_json(force=True, silent=True) or {}
    sid = st['session']
    p = st['proposal']
    maps = get_session_mappings(DB_PATH, sid)
    remove = set(data.get('remove', []))
    for m in maps:
        if m['token'] in remove:
            add_exclusion(DB_PATH, sid, m['original_form'], m['entity_type'])
    for t in remove:
        delete_mapping(DB_PATH, sid, t)
    for i in data.get('add', []):
        if 0 <= i < len(p['add']):
            a = p['add'][i]
            get_or_create_token(DB_PATH, sid, a['value'], a['value'], a['type'], manual=True)
    from core.db import merge_tokens
    for x in p['merge']:
        if x['token'] in set(data.get('merge', [])):
            merge_tokens(DB_PATH, sid, x['token'], x['into'])
    journal.event('llm_review_applied', removed=len(remove), added=len(data.get('add', [])),
                  merged=len(data.get('merge', [])))
    session_dir = UPLOADS_DIR / sid
    src = session_dir / 'input' / st['input']
    old = session_dir / 'output' / st['output']
    with journal.Job('reprocess', session=sid[:8], ext=src.suffix.lower()):
        r = process_uploaded_file(src, session_dir / 'output', sid, DB_PATH, 'anonymize')
    if old.exists() and old.name != r['output_filename']:
        old.unlink()
    _remember_output(session_dir, src.name, 'anonymize', r['output_filename'])
    st['status'] = 'applied'
    return jsonify({'ok': True, 'output': r['output_filename'], 'leaks': r.get('leaks'),
                    'mappings': get_session_mappings(DB_PATH, sid)})


_DEFAULT_SESSION_NAME = re.compile(r'^(?:Сессия\s+(?:\d{2}\.\d{2}\.\d{2,4}|[A-F0-9]{6})|)$')


def _auto_name_session(sid, filenames):
    """A default name («Сессия 01.10.26») becomes «<file> · 01.10.26 14:05» after the first
    processed file, so sessions can be told apart. Names typed by the user stay."""
    from core.db import get_all_sessions, rename_session
    import datetime
    if not filenames:
        return
    sess = next((x for x in get_all_sessions(DB_PATH) if x['id'] == sid), None)
    if not sess or not _DEFAULT_SESSION_NAME.match((sess.get('name') or '').strip()):
        return
    stem = Path(filenames[0]).stem[:60]
    more = f' +{len(filenames) - 1}' if len(filenames) > 1 else ''
    rename_session(DB_PATH, sid, f'{stem}{more} · {datetime.datetime.now():%d.%m.%y %H:%M}')


# ── Preview of a processed file (no need to open it) ─────────────────────────
@app.route('/api/sessions/<sid>/preview/<path:filename>')
def preview_file(sid, filename):
    from core.extract import extract_text
    from core.handlers import IMAGE_EXT, leak_check
    from core.db import get_session_mappings
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'invalid filename'}), 400
    path = UPLOADS_DIR / sid / 'output' / filename
    if not path.exists():
        return jsonify({'error': 'Файл не найден'}), 404
    forms = {}
    for m in get_session_mappings(DB_PATH, sid):
        forms.setdefault(m['token'], {'type': m['entity_type'], 'forms': []})['forms'].append(m['original_form'])
    ext = path.suffix.lower()
    from core.db import get_occurrences
    inp = next((i for i, o in _manifest(UPLOADS_DIR / sid).items() if filename in o.values()), None)
    out = {'filename': filename, 'forms': forms, 'kind': 'text', 'pages': 0, 'text': '', 'input': inp,
           'occurrences': get_occurrences(DB_PATH, sid, filename)}
    from core.handlers import load_restore_marks
    marks = load_restore_marks(path.parent, filename)
    if marks is not None:
        # a restored file: inserted values coloured, no leak check (the values are meant to be there)
        out['restore'] = {'marks': marks,
                          'exact': sum(1 for m, _ in marks if m == 'exact'),
                          'case': sum(1 for m, _ in marks if m == 'case'),
                          'unknown': sum(1 for m, _ in marks if m == 'unknown')}
    if path.suffix.lower() in ('.docx', '.docm'):
        out['kind'] = 'docx'      # rendered like in Word by docx-preview in the browser
    if ext in IMAGE_EXT:
        out['kind'] = 'image'
    elif ext in ('.xlsx', '.xlsm'):
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True)
        out['kind'] = 'xlsx'
        out['sheets'] = [{'name': ws.title,
                          'rows': [['' if v is None else str(v) for v in row[:40]]
                                   for row in ws.iter_rows(max_row=800, values_only=True)]}
                         for ws in wb.worksheets]
        out['text'] = extract_text(path, meta=False)[:300000]
        out['leaks'] = None if marks is not None else leak_check(path, sid, DB_PATH)
    else:
        try:
            out['text'] = extract_text(path, meta=False)[:300000]
        except Exception as ex:
            out['text'] = f'Не удалось прочитать файл: {ex}'
        if ext == '.pdf':
            import pymupdf
            out['kind'] = 'pdf'
            out['pages'] = len(pymupdf.open(str(path)))
        out['leaks'] = None if marks is not None else leak_check(path, sid, DB_PATH)
    from core.entities import review_pairs
    from core.db import get_distinct_pairs
    out['similar'] = review_pairs(get_session_mappings(DB_PATH, sid), get_distinct_pairs(DB_PATH, sid))
    return jsonify(out)


@app.route('/api/sessions/<sid>/mappings/<token>/alias', methods=['POST'])
def add_alias_route(sid, token):
    """«Добавить вариант написания»: another written form of this entity gets the same token."""
    from core.db import get_session_mappings, add_alias, remember_entity
    value = ((request.get_json(force=True, silent=True) or {}).get('original_form') or '').strip()
    m = next((x for x in get_session_mappings(DB_PATH, sid) if x['token'] == token), None)
    if not m or not value:
        return jsonify({'error': 'Запись не найдена или пустое значение'}), 400
    add_alias(DB_PATH, sid, token, value, m['entity_type'])
    remember_entity(DB_PATH, value, m['entity_type'])
    journal.event('mapping_edit', token=token, action='alias')
    return jsonify({'ok': True, 'token': token})


@app.route('/api/sessions/<sid>/mappings/<token>/merge', methods=['POST'])
def merge_mapping(sid, token):
    """«Это одно и то же»: every form of `token` moves under `into`; the old token is kept
    (retired) so files downloaded earlier still restore."""
    from core.db import get_session_mappings, merge_tokens
    into = (request.get_json(force=True, silent=True) or {}).get('into')
    maps = get_session_mappings(DB_PATH, sid)
    if not any(m['token'] == token for m in maps) or not any(m['token'] == into for m in maps) \
            or into == token:
        return jsonify({'error': 'Записи не найдены'}), 404
    merge_tokens(DB_PATH, sid, token, into)
    journal.event('mapping_edit', token=token, action='merge', into=into)
    return jsonify({'ok': True, 'mappings': get_session_mappings(DB_PATH, sid)})


@app.route('/api/sessions/<sid>/page/<path:filename>/<int:page>.png')
def preview_pdf_page(sid, filename, page):
    import pymupdf
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'invalid filename'}), 400
    path = UPLOADS_DIR / sid / 'output' / filename
    doc = pymupdf.open(str(path))
    if not 0 <= page < len(doc):
        return jsonify({'error': 'page'}), 404
    png = doc[page].get_pixmap(dpi=110).tobytes('png')
    return send_file(io.BytesIO(png), mimetype='image/png')


# ── List session output files ─────────────────────────────────────────────────
@app.route('/api/sessions/<sid>/files')
def session_files(sid):
    out_dir = UPLOADS_DIR / sid / 'output'
    if not out_dir.exists():
        return jsonify({'files': []})
    items = []
    for fp in sorted(out_dir.iterdir()):
        if fp.is_file():
            try:
                items.append({
                    'output': fp.name,
                    'size':   fp.stat().st_size,
                })
            except OSError:
                pass
    return jsonify({'files': items})


@app.route('/api/sessions/<sid>/files/<path:filename>', methods=['DELETE'])
def delete_session_file(sid, filename):
    """Remove a single output file from the session. Also removes the matching
    input source if present (input files share the original stem)."""
    # Prevent path traversal
    if '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({'error': 'invalid filename'}), 400
    out_dir = UPLOADS_DIR / sid / 'output'
    in_dir  = UPLOADS_DIR / sid / 'input'
    target = out_dir / filename
    if not target.exists():
        return jsonify({'error': 'file not found'}), 404
    try:
        target.unlink()
    except OSError as ex:
        return jsonify({'error': str(ex)}), 500
    # Also remove the input file this output was made from
    session_dir = UPLOADS_DIR / sid
    man = _manifest(session_dir)
    for inp, outs in list(man.items()):
        if filename in outs.values():
            try:
                (in_dir / inp).unlink()
            except OSError:
                pass
            man.pop(inp, None)
    (session_dir / 'manifest.json').write_text(json.dumps(man, ensure_ascii=False), encoding='utf-8')
    return jsonify({'ok': True})


# ── Download single file ──────────────────────────────────────────────────────
@app.route('/api/download/<sid>/<filename>')
def download_file(sid, filename):
    file_path = UPLOADS_DIR / sid / 'output' / filename
    if not file_path.exists():
        return jsonify({'error': 'Файл не найден'}), 404
    journal.event('download', session=sid[:8], ext=file_path.suffix.lower(),
                  size_kb=round(file_path.stat().st_size / 1024))
    return send_file(str(file_path), as_attachment=True,
                     download_name=filename)


# ── Journal ───────────────────────────────────────────────────────────────────
@app.route('/api/logs')
def logs_info():
    return jsonify({'dir': str(journal.LOG_DIR), 'verbose': journal.VERBOSE,
                    'stale_jobs': journal.stale_jobs()})


@app.route('/api/jobs/recent')
def recent_jobs():
    """«Последние задачи»: the last 20 jobs — time, file type, mode, duration, status.
    No file names and no values (the journal has none)."""
    return jsonify(journal.recent_jobs(20))


@app.route('/api/logs/open', methods=['POST'])
def logs_open():
    import subprocess
    opener = 'open' if sys.platform == 'darwin' else ('explorer' if sys.platform == 'win32' else 'xdg-open')
    try:
        subprocess.Popen([opener, str(journal.LOG_DIR)])
    except OSError as ex:
        return jsonify({'error': str(ex), 'dir': str(journal.LOG_DIR)}), 500
    return jsonify({'ok': True, 'dir': str(journal.LOG_DIR)})


@app.route('/api/logs/verbose', methods=['POST'])
def logs_verbose():
    on = bool((request.get_json(force=True, silent=True) or {}).get('on'))
    journal.set_verbose(on)
    return jsonify({'verbose': journal.VERBOSE})


# ── Download all files as ZIP ─────────────────────────────────────────────────
@app.route('/api/download-all/<sid>')
def download_all(sid):
    out_dir = UPLOADS_DIR / sid / 'output'
    if not out_dir.exists():
        return jsonify({'error': 'Нет результатов'}), 404

    files = list(out_dir.iterdir())
    if not files:
        return jsonify({'error': 'Нет файлов для скачивания'}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fp in files:
            zf.write(str(fp), fp.name)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True,
                     download_name=f'anonymized_{sid[:8]}.zip')


# ── Graceful shutdown ─────────────────────────────────────────────────────────
@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    def _stop():
        import time; time.sleep(0.5); os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({'ok': True})


# ── Launch ────────────────────────────────────────────────────────────────────
def _free_port(preferred=5000) -> int:
    import socket
    for port in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


def _serve(port):
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)


def main():
    global PORT
    PORT = int(os.environ.get('ANONYMIZER_PORT') or _free_port(5000 if not getattr(sys, 'frozen', False) else 0))
    url = f'http://127.0.0.1:{PORT}'
    print('Anonymizer v' + APP_VERSION)
    print(url, flush=True)
    print('Data:', DATA_DIR, flush=True)
    if '--server' in sys.argv:
        _serve(PORT)
        return
    if '--browser' not in sys.argv:
        try:
            import webview
            webview.settings['ALLOW_DOWNLOADS'] = True
            threading.Thread(target=_serve, args=(PORT,), daemon=True).start()
            _wait_ready(url)
            webview.create_window(f'Anonymizer {APP_VERSION}', url, width=1360, height=900,
                                  min_size=(1000, 680))
            webview.start()
            os._exit(0)
        except ImportError:
            pass
    threading.Thread(target=lambda: (_wait_ready(url), webbrowser.open(url)), daemon=True).start()
    _serve(PORT)


def _wait_ready(url, timeout=20):
    import time
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url + '/api/status', timeout=1)
            return
        except Exception:
            time.sleep(0.2)


if __name__ == '__main__':
    main()
