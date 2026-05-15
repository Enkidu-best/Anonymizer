"""
Anonymizer — Flask backend
Run:  python app.py
Then open http://localhost:5000 in your browser.
"""

import io
import os
import sys
import shutil
import threading
import webbrowser
import zipfile
from pathlib import Path

APP_VERSION = "2.0.0"

from flask import Flask, request, jsonify, send_file, send_from_directory

# ── Path resolution (works both in development and PyInstaller bundle) ────────
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    DATA_DIR   = Path(sys.executable).parent
else:
    BUNDLE_DIR = Path(__file__).parent
    DATA_DIR   = Path(__file__).parent

STATIC_DIR  = BUNDLE_DIR / 'static'
UPLOADS_DIR = DATA_DIR   / 'uploads'
DB_PATH     = DATA_DIR   / 'anon.db'

UPLOADS_DIR.mkdir(exist_ok=True)

# ── DB init ───────────────────────────────────────────────────────────────────
from core.db import init_db
init_db(DB_PATH)

# ── Start NER loading in background ──────────────────────────────────────────
from core.anonymizer import start_ner_loading
start_ner_loading()

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC_DIR))
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024


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
    return jsonify(get_all_sessions(DB_PATH))


@app.route('/api/sessions', methods=['POST'])
def create_session_route():
    from core.db import create_session
    import uuid
    data = request.get_json(force=True, silent=True) or {}
    name = data.get('name', '').strip() or f'Сессия {uuid.uuid4().hex[:6].upper()}'
    sid  = create_session(DB_PATH, name)
    return jsonify({'id': sid, 'name': name}), 201


@app.route('/api/sessions/<sid>', methods=['DELETE'])
def delete_session(sid):
    from core.db import delete_session as db_delete_session
    db_delete_session(DB_PATH, sid)
    session_dir = UPLOADS_DIR / sid
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
    return jsonify({'ok': True})


@app.route('/api/sessions/<sid>/mappings', methods=['GET'])
def session_mappings(sid):
    from core.db import get_session_mappings
    return jsonify(get_session_mappings(DB_PATH, sid))


@app.route('/api/sessions/<sid>/mappings/<token>', methods=['DELETE'])
def delete_mapping_route(sid, token):
    from core.db import delete_mapping, get_session_mappings, add_exclusion
    mappings = get_session_mappings(DB_PATH, sid)
    m = next((x for x in mappings if x['token'] == token), None)
    if m:
        add_exclusion(DB_PATH, sid, m['original_form'], m['entity_type'])
    delete_mapping(DB_PATH, sid, token)
    return jsonify({'ok': True})


@app.route('/api/sessions/<sid>/mappings/<token>', methods=['PATCH'])
def update_mapping_route(sid, token):
    from core.db import get_session_mappings, delete_mapping, add_exclusion, get_or_create_token
    data = request.get_json(force=True, silent=True) or {}
    mappings = get_session_mappings(DB_PATH, sid)
    old = next((x for x in mappings if x['token'] == token), None)
    if not old:
        return jsonify({'error': 'Token not found'}), 404
    new_original  = (data.get('original_form',  old['original_form'])).strip()
    new_canonical = (data.get('canonical_form', old['canonical_form'])).strip()
    new_type      = (data.get('entity_type',    old['entity_type'])).strip()
    if new_original != old['original_form']:
        add_exclusion(DB_PATH, sid, old['original_form'], old['entity_type'])
    delete_mapping(DB_PATH, sid, token)
    new_token = get_or_create_token(DB_PATH, sid, new_original, new_canonical, new_type)
    return jsonify({'ok': True, 'new_token': new_token})


@app.route('/api/sessions/<sid>/mappings', methods=['POST'])
def add_mapping_route(sid):
    from core.db import get_or_create_token
    data = request.get_json(force=True, silent=True) or {}
    original    = (data.get('original_form')  or '').strip()
    canonical   = (data.get('canonical_form') or '').strip()
    entity_type = (data.get('entity_type')    or '').strip()
    if not entity_type:
        return jsonify({'error': 'entity_type обязателен'}), 400
    if not canonical:
        canonical = original
    if not original:
        original = canonical
    if not original:
        return jsonify({'error': 'original_form или canonical_form обязательны'}), 400
    token = get_or_create_token(DB_PATH, sid, original, canonical, entity_type)
    return jsonify({'token': token, 'original_form': original,
                    'canonical_form': canonical, 'entity_type': entity_type}), 201




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
    for input_file in input_dir.iterdir():
        try:
            r = process_uploaded_file(
                input_path=input_file,
                output_dir=output_dir,
                session_id=sid,
                db_path=DB_PATH,
                mode='anonymize',
            )
            results.append({'filename': input_file.name, 'status': 'ok'})
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
    for f in files:
        if not f.filename:
            continue
        src = in_dir / f.filename
        f.save(str(src))
        try:
            r = process_uploaded_file(
                input_path=src,
                output_dir=out_dir,
                session_id=sid,
                db_path=DB_PATH,
                mode=mode,
                use_spacy=use_spacy,
                use_llm=use_llm,
            )
            out_name = r['output_filename']
            results.append({'filename': f.filename,
                             'output':   out_name,
                             'status':   'ok',
                             'entities_found': r.get('entities_found', 0)})
        except Exception as ex:
            results.append({'filename': f.filename,
                             'status':  'error',
                             'error':   str(ex)})

    return jsonify({
        'results':    results,
        'session_id': sid,
        'mappings':   get_session_mappings(DB_PATH, sid),
    })


# ── Download single file ──────────────────────────────────────────────────────
@app.route('/api/download/<sid>/<filename>')
def download_file(sid, filename):
    file_path = UPLOADS_DIR / sid / 'output' / filename
    if not file_path.exists():
        return jsonify({'error': 'Файл не найден'}), 404
    return send_file(str(file_path), as_attachment=True,
                     download_name=filename)


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
def _open_browser():
    import time
    time.sleep(1.2)
    webbrowser.open('http://127.0.0.1:5000')


if __name__ == '__main__':
    t = threading.Thread(target=_open_browser, daemon=True)
    t.start()
    print('Anonymizer v' + APP_VERSION)
    print('http://127.0.0.1:5000')
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
