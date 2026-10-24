"""Files of a session: `uploads/<сессия>/manifest.json` (description 3.2).

One entry per uploaded file, in the order they were added:
    {"input": "Договор поставки - Петров.docx", "section": "anon" | "restore", "size": 1234, "added": 1729760000.0,
     "status": "new" | "done" | "error", "output": "Договор поставки [FIO_1].docx", "extra": "… .pdf" | null,
     "result": {"found": 14, "leaks": [...], "apply_errors": 0, "restore": {...}, "llm_job": "…", "note": "…"},
     "error": "too_big" | "format" | … | null}
The section decides where the file is shown («Обезличить» / «Вернуть данные»), the result - the summary after
reopening (was Р-17, Р-18). Manifests of 3.x ({input: {mode: output}}) are read as finished files.
"""
import json
import time
from pathlib import Path

SECTIONS = {'anon': 'anonymize', 'restore': 'deanonymize'}
_LEGACY = {'anonymize': 'anon', 'deanonymize': 'restore'}


def _path(session_dir: Path) -> Path:
    return Path(session_dir) / 'manifest.json'


def load(session_dir: Path) -> list:
    try:
        data = json.loads(_path(session_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict) and data.get('v') == 2:
        return data.get('files', [])
    out = []          # 3.x: {input: {"anonymize": out, "anonymize_pdf": extra, "deanonymize": out}}
    for inp, modes in (data or {}).items():
        for mode, section in _LEGACY.items():
            if isinstance(modes, dict) and modes.get(mode):
                out.append({'input': inp, 'section': section, 'size': 0, 'added': 0, 'status': 'done',
                            'output': modes[mode], 'extra': modes.get(mode + '_pdf'), 'result': None,
                            'error': None})
    return out


def save(session_dir: Path, files: list):
    p = _path(session_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps({'v': 2, 'files': files}, ensure_ascii=False), encoding='utf-8')
    tmp.replace(p)


def find(files: list, input_name: str, section: str = None):
    return next((f for f in files if f['input'] == input_name and (section is None or f['section'] == section)),
                None)


def add(session_dir: Path, input_name: str, section: str, size: int) -> dict:
    files = load(session_dir)
    entry = {'input': input_name, 'section': section, 'size': size, 'added': time.time(), 'status': 'new',
             'output': None, 'extra': None, 'result': None, 'error': None}
    files.append(entry)
    save(session_dir, files)
    return entry


def update(session_dir: Path, input_name: str, section: str, **fields) -> dict:
    files = load(session_dir)
    entry = find(files, input_name, section)
    if entry is None:
        entry = {'input': input_name, 'section': section, 'size': 0, 'added': time.time(), 'status': 'new',
                 'output': None, 'extra': None, 'result': None, 'error': None}
        files.append(entry)
    entry.update(fields)
    save(session_dir, files)
    return entry


def remove(session_dir: Path, input_name: str, section: str):
    files = load(session_dir)
    entry = find(files, input_name, section)
    if entry:
        files.remove(entry)
        save(session_dir, files)
    return entry


def by_output(files: list, output_name: str):
    return next((f for f in files if output_name in (f.get('output'), f.get('extra'))), None)
