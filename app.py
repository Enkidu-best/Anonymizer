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

from flask import Flask, request, jsonify, send_file, send_from_directory

# ── Path resolution (works both in development and PyInstaller bundle) ────────
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(sys._MEIPASS)        # extracted resources
    DATA_DIR   = Path(sys.executable).parent  # writable next to .exe/.app
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
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024   # 100 MB


# ── Static files & SPA ───────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'index.html')


# ── Status ────────────────────────────────────────────────────────────────────
@app.route('/api/status')
def status():
    from core.anonymizer import get_ner_status
    return jsonify(get_ner_status())


@app.route('/api/debug')
def debug():
    """Full diagnostics — open in browser to see what's happening."""
    import sys, platform
    from core.anonymizer import get_ner_status

    ner = get_ner_status()

    # Try importing natasha directly to see the real error
    natasha_ok  = False
    natasha_err = ''
    numpy_ver   = 'unknown'
    try:
        import numpy
        numpy_ver = numpy.__version__
    except Exception as e:
        numpy_ver = f'IMPORT ERROR: {e}'

    try:
        from natasha import Segmenter
        natasha_ok = True
    except Exception as e:
        import traceback
        natasha_err = traceback.format_exc()

    info = {
        'python_version':  sys.version,
        'platform':        platform.platform(),
        'executable':      sys.executable,
        'numpy_version':   numpy_ver,
        'natasha_import':  natasha_ok,
        'natasha_error':   natasha_err,
        'ner_status':      ner,
    }
    # Return as plain text for readability
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
def create_session():
    from core.db import create_session
    import uuid
    data = request.get_json(force=True, silent=True) or {}
    name = data.get('name', '').strip() or f'Сессия {uuid.uuid4().hex[:6].upper()}'
    sid  = create_session(DB_PATH, name)
    return jsonify({'id': sid, 'name': name}), 201


@app.route('/api/sessions/<sid>', methods=['DELETE'])
def delete_session(sid):
    from core.db import delete_session
    delete_session(DB_PATH, sid)
    session_dir = UPLOADS_DIR / sid
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
    return jsonify({'ok': True})


@app.route('/api/sessions/<sid>/mappings', methods=['GET'])
def session_mappings(sid):
    from core.db import get_session_mappings
    return jsonify(get_session_mappings(DB_PATH, sid))


# ── Process ───────────────────────────────────────────────────────────────────
@app.route('/api/process', methods=['POST'])
def process():
    from core.handlers import process_uploaded_file
    from core.db       import get_session_mappings

    mode = request.form.get('mode', 'anonymize')
    sid  = request.form.get('session_id', '').strip()
    use_llm = request.form.get('use_llm', 'false').lower() == 'true'
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
                use_llm=use_llm,
            )
            # PDF deanon produces .txt — adjust reported filename
            out_name = r['output_filename']
            if r.get('_pdf_deanon_txt'):
                out_name = out_name.rsplit('.', 1)[0] + '.txt'

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
    print('🔐  Anonymizer запущен → http://127.0.0.1:5000')
    print('    Для остановки нажмите Ctrl+C или используйте кнопку «Завершить» в интерфейсе.')
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
